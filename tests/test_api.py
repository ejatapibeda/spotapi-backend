"""Offline tests (no real Spotify cookies). Run: PYTHONPATH=vendor/spotapi pytest -q tests"""
import base64
import os

import pytest
import yaml
from fastapi.testclient import TestClient
from openapi_schema_validator import OAS30Validator
from referencing import Registry, Resource

from app.config import Settings
from app.errors import ErrorCode
from app.main import create_app
from app.spotify import SpotifyService, classify, parse_track_uri
from app.storage import SessionStore
from app.totp import TotpSecretProvider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = yaml.safe_load(open(os.path.join(ROOT, "openapi.yaml")))
TOKEN = "t" * 64
AUTH = {"Authorization": f"Bearer {TOKEN}"}
_REG = Registry().with_resource("spec", Resource.opaque(SPEC))


def validate(schema_name: str, instance) -> None:
    # OAS 3.0 idiom `nullable: true` + `allOf: [$ref]` (NowPlaying.track) is rejected by strict
    # jsonschema validators; validate a null track by checking the rest of the object only.
    if schema_name == "NowPlaying" and isinstance(instance, dict) and instance.get("track") is None:
        instance = {**instance, "track": {"uri": "", "title": "", "artists": []}}
    schema = {"$ref": f"spec#/components/schemas/{schema_name}"}
    OAS30Validator(schema, registry=_REG).validate(instance)


def make(tmp_path, pairing_code=""):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    s = Settings(app_token=TOKEN, enc_key=key, pairing_code=pairing_code, data_dir=str(tmp_path),
                 db_path=str(tmp_path / "spotapi.db"), totp_cache_path=str(tmp_path / "totp.json"))
    store = SessionStore(s.db_path, key)
    svc = SpotifyService(store, TotpSecretProvider((), s.totp_cache_path), retry_after_s=7)
    return TestClient(create_app(s, service=svc), raise_server_exceptions=False), svc, store


def assert_err(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    validate("Error", body)
    assert body["ok"] is False and body["error"]["code"] == code
    return body


# ------------------------------------------------------------------ contract coverage
def test_routes_match_spec(tmp_path):
    client, _, _ = make(tmp_path)
    app_routes = set()
    for r in client.app.routes:
        for m in getattr(r, "methods", set()) - {"HEAD", "OPTIONS"}:
            app_routes.add((r.path.removeprefix("/api/v1"), m.lower()))
    spec_routes = {(p, m) for p, ops in SPEC["paths"].items() for m in ops}
    assert app_routes == spec_routes


def test_error_codes_match_spec():
    assert {c.value for c in ErrorCode} == set(SPEC["components"]["schemas"]["ErrorCode"]["enum"])


# ------------------------------------------------------------------ auth
def test_health_no_auth(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic " + TOKEN}])
@pytest.mark.parametrize("method,path", [("get", "/spotify/session"), ("post", "/play"), ("post", "/pause"),
                                         ("get", "/now-playing"), ("get", "/devices"), ("delete", "/spotify/session")])
def test_auth_rejected(tmp_path, headers, method, path):
    client, _, _ = make(tmp_path)
    r = client.request(method, "/api/v1" + path, headers=headers, json={"query": "x"})
    assert_err(r, 401, "UNAUTHORIZED")


def test_pair_disabled(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.post("/api/v1/auth/pair", json={"pairing_code": "123-456", "device_name": "Pixel"})
    assert_err(r, 404, "BAD_REQUEST")


def test_pair_enabled_and_rate_limit(tmp_path):
    client, _, _ = make(tmp_path, pairing_code="483-219")
    r = client.post("/api/v1/auth/pair", json={"pairing_code": "483-219", "device_name": "Pixel"})
    assert r.status_code == 200
    validate("PairResponse", r.json())
    assert r.json()["app_token"] == TOKEN
    assert_err(client.post("/api/v1/auth/pair", json={"device_name": "x"}), 400, "BAD_REQUEST")
    for _ in range(5):
        assert_err(client.post("/api/v1/auth/pair", json={"pairing_code": "bad", "device_name": "x"}), 401, "UNAUTHORIZED")
    r = client.post("/api/v1/auth/pair", json={"pairing_code": "483-219", "device_name": "x"})
    assert_err(r, 429, "RATE_LIMITED")
    assert int(r.headers["Retry-After"]) > 0


# ------------------------------------------------------------------ session
def test_session_missing_everywhere(tmp_path):
    client, _, _ = make(tmp_path)
    r = client.get("/api/v1/spotify/session", headers=AUTH)
    assert r.status_code == 200 and r.json()["connected"] is False
    validate("SessionStatus", r.json())
    for m, p in [("post", "/pause"), ("post", "/resume"), ("post", "/next"), ("post", "/prev"),
                 ("get", "/now-playing"), ("get", "/devices")]:
        assert_err(client.request(m, "/api/v1" + p, headers=AUTH), 403, "SPOTIFY_SESSION_MISSING")
    assert_err(client.post("/api/v1/play", headers=AUTH, json={"query": "hello"}), 403, "SPOTIFY_SESSION_MISSING")
    assert client.delete("/api/v1/spotify/session", headers=AUTH).status_code == 204


def test_session_validation(tmp_path):
    client, _, _ = make(tmp_path)
    assert_err(client.post("/api/v1/spotify/session", headers=AUTH, json={}), 400, "BAD_REQUEST")
    assert_err(client.post("/api/v1/spotify/session", headers=AUTH, json={"cookies": {"sp_t": "x"}}), 400, "BAD_REQUEST")
    assert_err(client.post("/api/v1/spotify/session", headers=AUTH, json={"cookies": {"sp_dc": "short"}}), 400, "BAD_REQUEST")


def test_store_encrypts_and_status_has_no_secrets(tmp_path):
    client, svc, store = make(tmp_path)
    secret = "AQ_SECRET_COOKIE_VALUE_1234567890"
    store.save({"sp_dc": secret}, username="u1", display_name="D", product="FREE")
    raw = open(tmp_path / "spotapi.db", "rb").read()
    assert secret.encode() not in raw
    assert store.load().cookies == {"sp_dc": secret}
    r = client.get("/api/v1/spotify/session", headers=AUTH)
    validate("SessionStatus", r.json())
    assert r.json()["connected"] is True and secret not in r.text
    store.mark_invalid()
    assert client.get("/api/v1/spotify/session", headers=AUTH).json()["connected"] is False
    assert_err(client.post("/api/v1/pause", headers=AUTH), 401, "SPOTIFY_SESSION_INVALID")
    assert client.delete("/api/v1/spotify/session", headers=AUTH).status_code == 204
    assert store.load() is None


def test_wrong_enc_key_means_missing(tmp_path):
    _, _, store = make(tmp_path)
    store.save({"sp_dc": "x" * 20}, username=None, display_name=None, product=None)
    other = SessionStore(str(tmp_path / "spotapi.db"), base64.urlsafe_b64encode(os.urandom(32)).decode())
    rec = other.load()
    assert rec.cookies is None and rec.valid is False


# ------------------------------------------------------------------ play validation
def test_play_validation(tmp_path):
    client, _, _ = make(tmp_path)
    assert_err(client.post("/api/v1/play", headers=AUTH, json={}), 400, "BAD_REQUEST")
    assert_err(client.post("/api/v1/play", headers=AUTH, json={"query": "a", "uri": "spotify:track:20jbSiX29FDX4oQxBXyUEi"}), 400, "BAD_REQUEST")
    assert_err(client.post("/api/v1/play", headers=AUTH, json={"uri": "spotify:album:1x159B5VzbDWAGBik5cr1z"}), 400, "BAD_REQUEST")
    assert_err(client.post("/api/v1/play", headers=AUTH, json={"query": "a", "index": -1}), 400, "BAD_REQUEST")


def test_parse_track_uri():
    tid = "20jbSiX29FDX4oQxBXyUEi"
    for v in (tid, f"spotify:track:{tid}", f"https://open.spotify.com/track/{tid}?si=abc",
              f"https://open.spotify.com/intl-id/track/{tid}"):
        assert parse_track_uri(v) == f"spotify:track:{tid}"


# ------------------------------------------------------------------ error mapping
def _errors():
    from spotapi.exceptions import BaseClientError, PlayerError, RequestError, WebSocketError
    return BaseClientError, PlayerError, RequestError, WebSocketError


def test_classify():
    from spotapi.exceptions import LoginError
    BaseClientError, PlayerError, RequestError, WebSocketError = _errors()
    cases = [
        (BaseClientError("Could not get session auth tokens", error="Status Code: 400, Response: {}"), "TOKEN_UNAVAILABLE", 503),
        (BaseClientError("Could not get client token", error="Status Code: 500, Response: x"), "TOKEN_UNAVAILABLE", 503),
        (PlayerError("Could not send command", error="Status Code: 404, Response: None"), "NO_ACTIVE_DEVICE", 409),
        (PlayerError("Could not send command", error="Status Code: 400, Response: None"), "COMMAND_FAILED", 502),
        (PlayerError("Could not send command", error="Status Code: 429, Response: None"), "RATE_LIMITED", 429),
        (WebSocketError("Could not connect device", error="Status Code: 401, Response: x"), "SPOTIFY_SESSION_INVALID", 401),
        (ValueError("Could not get active device ID. Please provide a device ID."), "NO_ACTIVE_DEVICE", 409),
        (RequestError("Failed to complete request.", error="timeout"), "SPOTIFY_UPSTREAM", 502),
        (KeyError("x"), "SPOTIFY_UPSTREAM", 502),
        (LoginError("Could not GET https://open.spotify.com/api/token. Status Code: 400", "Request Failed."), "TOKEN_UNAVAILABLE", 503),
        (LoginError("Could not GET https://open.spotify.com/. Status Code: 401", "Request Failed."), "SPOTIFY_SESSION_INVALID", 401),
        (LoginError("Could not GET https://open.spotify.com/. Status Code: 503", "Request Failed."), "SPOTIFY_UPSTREAM", 502),
    ]
    for exc, code, status in cases:
        e = classify(exc, 9)
        assert (e.code.value, e.status) == (code, status), exc
    assert classify(cases[4][0], 9).retry_after == 9


@pytest.mark.parametrize("raise_exc,status,code,attempts", [
    ("ratelimit", 429, "RATE_LIMITED", 1),
    ("token", 503, "TOKEN_UNAVAILABLE", 2),
    ("unauth", 401, "SPOTIFY_SESSION_INVALID", 2),
    ("nodevice", 409, "NO_ACTIVE_DEVICE", 1),
    ("cmd", 502, "COMMAND_FAILED", 1),
])
def test_endpoint_error_mapping_and_retry(tmp_path, monkeypatch, raise_exc, status, code, attempts):
    BaseClientError, PlayerError, _, _ = _errors()
    client, svc, store = make(tmp_path)
    store.save({"sp_dc": "x" * 20}, username="u", display_name=None, product=None)
    calls = {"n": 0}
    excs = {
        "ratelimit": PlayerError("Could not send command", error="Status Code: 429, Response: None"),
        "token": BaseClientError("Could not get session auth tokens", error="Status Code: 400, Response: {}"),
        "unauth": PlayerError("Could not send command", error="Status Code: 401, Response: None"),
        "nodevice": ValueError("Could not get active device ID. Please provide a device ID."),
        "cmd": PlayerError("Could not send command", error="Status Code: 400, Response: None"),
    }

    def boom(device_id):
        calls["n"] += 1
        raise excs[raise_exc]

    monkeypatch.setattr(svc, "_get_player", boom)
    monkeypatch.setattr(svc, "_ensure_login", lambda: None)
    r = client.post("/api/v1/next", headers=AUTH)
    body = assert_err(r, status, code)
    assert calls["n"] == attempts
    if code == "RATE_LIMITED":
        assert r.headers["Retry-After"] == "7" and body["error"]["retryable"] is True
    if code == "SPOTIFY_SESSION_INVALID":
        assert store.load().valid is False


def test_success_shapes_match_schemas(tmp_path, monkeypatch):
    client, svc, _ = make(tmp_path)
    track = {"uri": "spotify:track:20jbSiX29FDX4oQxBXyUEi", "title": "hate that i made you love me",
             "artists": ["Ariana Grande"], "album": "x", "duration_ms": 197949, "image_url": None}
    monkeypatch.setattr(svc, "play", lambda **kw: (track, "dev1"))
    monkeypatch.setattr(svc, "pause", lambda d=None: None)
    monkeypatch.setattr(svc, "now_playing", lambda: {"is_playing": False, "is_paused": None, "track": None,
                                                    "position_ms": None, "active_device_id": None, "device_name": None})
    monkeypatch.setattr(svc, "devices", lambda: {"active_device_id": "dev1", "devices": [
        {"id": "dev1", "name": "Web Player (Chrome)", "type": "COMPUTER", "is_active": True, "can_play": True, "volume_percent": 50}]})
    r = client.post("/api/v1/play", headers=AUTH, json={"query": "hate that i made you love me"})
    validate("PlayResponse", r.json())
    for body in (None, {}, {"device_id": "dev1"}):
        r = client.post("/api/v1/pause", headers=AUTH, json=body) if body is not None else client.post("/api/v1/pause", headers=AUTH)
        assert r.status_code == 200, r.text
        validate("OkResponse", r.json())
    validate("NowPlaying", client.get("/api/v1/now-playing", headers=AUTH).json())
    validate("DevicesResponse", client.get("/api/v1/devices", headers=AUTH).json())


def test_now_playing_parsing(tmp_path, monkeypatch):
    client, svc, store = make(tmp_path)
    dump = {"active_device_id": "dev1",
            "devices": {"dev1": {"name": "Pixel", "device_type": "smartphone", "can_play": True, "volume": 32768},
                        "hobs_abc": {"name": "phantom", "capabilities": {"hidden": True}}},
            "player_state": {"is_playing": True, "is_paused": True, "duration": "197949",
                             "position_as_of_timestamp": "1000", "timestamp": "1",
                             "track": {"uri": "spotify:track:20jbSiX29FDX4oQxBXyUEi",
                                       "metadata": {"title": "hate that i made you love me", "artist_name": "Ariana Grande",
                                                    "album_title": "A", "image_url": "spotify:image:abc"}}}}
    monkeypatch.setattr(svc, "_dump", lambda: dump)
    np = client.get("/api/v1/now-playing", headers=AUTH).json()
    validate("NowPlaying", np)
    assert np["is_playing"] is False and np["is_paused"] is True and np["position_ms"] == 1000
    assert np["track"]["artists"] == ["Ariana Grande"] and np["track"]["image_url"] == "https://i.scdn.co/image/abc"
    dv = client.get("/api/v1/devices", headers=AUTH).json()
    validate("DevicesResponse", dv)
    assert [d["id"] for d in dv["devices"]] == ["dev1"] and dv["devices"][0]["volume_percent"] == 50


# ------------------------------------------------------------------ TOTP robustness
def test_totp_fallback_chain(tmp_path, monkeypatch):
    import requests
    p = TotpSecretProvider(("https://example.invalid/x.json",), str(tmp_path / "totp.json"), timeout=0.5, retries=0)
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectTimeout("t")))
    v, _ = p.get()
    assert p.last_source == "bundled-seed" and v >= 59
    (tmp_path / "totp.json").write_text('{"99": [1, 2, 3]}')
    p.invalidate()
    v, s = p.get()
    assert (v, list(s), p.last_source) == (99, [1, 2, 3], "disk-cache")

    class R:
        ok = True
        status_code = 200
        def json(self): return {"100": [4, 5]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    p.invalidate()
    assert p.get()[0] == 100 and p.last_source == "network"
    assert '"100"' in (tmp_path / "totp.json").read_text()


# ------------------------------------------------------------------ live (network, fake cookies only)
@pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 to hit Spotify with FAKE cookies")
def test_live_fake_cookies_rejected(tmp_path):
    client, _, store = make(tmp_path)
    r = client.post("/api/v1/spotify/session", headers=AUTH, json={"cookies": {"sp_dc": "AQFAKE" + "x" * 40}})
    assert_err(r, 400, "INVALID_COOKIES")
    assert store.load() is None


# ------------------------------------------------------------------ lyrics
def test_lyrics_parsers_match_schema():
    from app.lyrics import color_lyrics_lines, parse_lrc

    data = {"lyrics": {"syncType": "LINE_SYNCED", "lines": [
        {"startTimeMs": "1000", "words": "hello"}, {"startTimeMs": "2500", "words": "world"}, {"startTimeMs": "3000", "words": ""}]}}
    lines, synced = color_lyrics_lines(data)
    assert synced and lines == [{"start_ms": 1000, "text": "hello"}, {"start_ms": 2500, "text": "world"}]
    validate("LyricsResponse", {"track_id": "x" * 22, "synced": synced, "source": "spotify", "lines": lines})

    lrc = parse_lrc("[00:01.50] a\n[00:00.20][00:03.00] b\nnot a line")
    assert [l["start_ms"] for l in lrc] == [200, 1500, 3000]
    validate("LyricsResponse", {"track_id": "x" * 22, "synced": True, "source": "lrclib", "lines": lrc})


def test_lyrics_requires_param(tmp_path):
    client, _, _ = make(tmp_path)
    assert_err(client.get("/api/v1/lyrics", headers=AUTH), 400, "BAD_REQUEST")
