"""Single-user Spotify service on top of the vendored spotapi library.

One Login + one Player/PlayerStatus are cached in-process and guarded by one lock
(tls_client sessions and the dealer websocket are not shared safely across threads).
Everything is rebuilt after SESSION_MAX_AGE_S (instance-level access tokens expire ~1h)
or after an auth/connection failure, and the failed call is retried once.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, TypeVar

from .errors import ApiError, ErrorCode
from .lyrics import color_lyrics_lines, fetch_lrclib
from .storage import SessionStore
from .totp import TotpSecretProvider

log = logging.getLogger("spotapi-backend.spotify")
T = TypeVar("T")

_STATUS_RE = re.compile(r"Status Code: (\d{3})")
_TRACK_RE = re.compile(r"^(?:spotify:track:|https?://open\.spotify\.com/(?:intl-[a-z-]+/)?track/)?([A-Za-z0-9]{22})(?:[?#].*)?$")


def _patch_spotapi_signals() -> None:
    """WebsocketStreamer.__init__ calls signal.signal(SIGINT, ...) which raises outside the
    main thread (FastAPI runs sync handlers in a threadpool) and would os._exit on Ctrl+C.
    Replace the module's `signal` with a no-op shim; uvicorn owns signal handling."""
    import signal as _signal

    import spotapi.websocket as ws

    class _SignalShim:
        SIGINT = _signal.SIGINT

        @staticmethod
        def signal(*_a: Any, **_k: Any) -> None:
            return None

    ws.signal = _SignalShim  # type: ignore[assignment]


def clear_spotapi_token_caches() -> None:
    import spotapi.client as sc

    sc._session_cache = {}
    sc._session_cache_expiry = -1
    sc._client_token_cache = None
    sc._client_token_expiry = -1


def status_code_of(exc: BaseException) -> int | None:
    for s in (getattr(exc, "error", None), str(exc)):
        if s:
            m = _STATUS_RE.search(str(s))
            if m:
                return int(m.group(1))
    return None


def parse_track_uri(uri: str) -> str:
    m = _TRACK_RE.match(uri.strip())
    if not m:
        raise ApiError(ErrorCode.BAD_REQUEST, "uri must be a Spotify track URI, track URL or 22-char track id")
    return f"spotify:track:{m.group(1)}"


def _image_url(v: str | None) -> str | None:
    if not v:
        return None
    if v.startswith("spotify:image:"):
        return "https://i.scdn.co/image/" + v.split(":")[-1]
    return v if v.startswith("http") else None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def track_from_pathfinder(d: dict) -> dict:
    """Track (contract) from a pathfinder Track object (search item data or trackUnion)."""
    artists: list[str] = []
    for key in ("artists", "firstArtist", "otherArtists"):
        for a in ((d.get(key) or {}).get("items") or []):
            name = (a.get("profile") or {}).get("name")
            if name and name not in artists:
                artists.append(name)
    album = d.get("albumOfTrack") or {}
    sources = ((album.get("coverArt") or {}).get("sources")) or []
    image = None
    if sources:
        image = max(sources, key=lambda s: s.get("width") or 0).get("url")
    return {
        "uri": d.get("uri") or "",
        "title": d.get("name") or "",
        "artists": artists,
        "album": album.get("name"),
        "duration_ms": _int_or_none((d.get("duration") or {}).get("totalMilliseconds")),
        "image_url": image,
    }


def classify(exc: BaseException, retry_after: int) -> ApiError:
    """Map spotapi / transport exceptions to the contract error envelope."""
    from spotapi.exceptions import (
        BaseClientError, LoginError, PlayerError, RequestError, SongError, UserError, WebSocketError,
    )

    if isinstance(exc, ApiError):
        return exc
    st = status_code_of(exc)
    msg = str(exc)
    if st == 429:
        return ApiError(ErrorCode.RATE_LIMITED, "Spotify rate limit hit, retry later", retry_after=retry_after)
    if st == 401:
        return ApiError(ErrorCode.SPOTIFY_SESSION_INVALID, "Spotify session expired or revoked; log in again in the app")
    # Login sets client.fail_exception=LoginError, so a failed GET of the token endpoint surfaces
    # as "LoginError: Could not GET https://open.spotify.com/api/token. Status Code: 400".
    token_hit = "open.spotify.com/api/token" in msg or "clienttoken.spotify.com" in msg
    if token_hit or (isinstance(exc, BaseClientError) and ("auth tokens" in msg or "client token" in msg)):
        return ApiError(ErrorCode.TOKEN_UNAVAILABLE, "Could not obtain a Spotify access token (TOTP/token endpoint); retry shortly")
    if isinstance(exc, ValueError) and ("active device" in msg.lower() or "player state" in msg.lower()):
        return ApiError(ErrorCode.NO_ACTIVE_DEVICE, "No active Spotify Connect device. Open Spotify on a device and start playback once, then retry")
    if isinstance(exc, PlayerError):
        if st == 404:
            return ApiError(ErrorCode.NO_ACTIVE_DEVICE, "Target device is no longer available. Open Spotify on a device, then retry")
        return ApiError(ErrorCode.COMMAND_FAILED, f"Spotify rejected the player command (HTTP {st or '?'})")
    if isinstance(exc, (WebSocketError, RequestError, SongError, BaseClientError, LoginError, UserError,
                        ConnectionError, OSError, TimeoutError)):
        return ApiError(ErrorCode.SPOTIFY_UPSTREAM, f"Spotify upstream error ({type(exc).__name__}{', HTTP ' + str(st) if st else ''})")
    return ApiError(ErrorCode.SPOTIFY_UPSTREAM, f"Unexpected upstream error ({type(exc).__name__})")


def _is_transient(exc: BaseException) -> bool:
    from spotapi.exceptions import WebSocketError

    try:
        from websockets.exceptions import ConnectionClosed
    except Exception:  # noqa: BLE001
        ConnectionClosed = ()  # type: ignore
    # RequestError/TLSClientExeption are NOT retried here: the HTTP client already retried them.
    return isinstance(exc, (WebSocketError, ConnectionClosed, ConnectionError, BrokenPipeError))


class SpotifyService:
    def __init__(self, store: SessionStore, totp: TotpSecretProvider, *, max_age_s: int = 2700,
                 retry_after_s: int = 10, now_playing_cache_ms: int = 1500,
                 http_timeout_s: int = 12, http_retries: int = 1) -> None:
        _patch_spotapi_signals()
        self.store = store
        self.totp = totp
        self.max_age_s = max_age_s
        self.retry_after_s = retry_after_s
        self.np_cache_ms = now_playing_cache_ms
        self.http_timeout_s = http_timeout_s
        self.http_retries = http_retries
        self.lock = threading.RLock()
        self._login = None
        self._status = None  # PlayerStatus (reader when no Player exists)
        self._player = None  # Player (needs an active/target device)
        self._song = None  # Song bound to the login client (search + track info, caches JS hashes)
        self._built_at = 0.0
        self._track_cache: "OrderedDict[str, dict]" = OrderedDict()
        self._np_cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------------ lifecycle
    def _close_ws(self) -> None:
        for obj in (self._player, self._status):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._player = None
        self._status = None

    def reset(self, *, drop_login: bool = False) -> None:
        with self.lock:
            self._close_ws()
            clear_spotapi_token_caches()
            self._np_cache = None
            if self._song is not None:
                from spotapi.types.alias import _Undefined

                b = self._song.base
                b.access_token = _Undefined
                b.client_token = _Undefined
                b.client_id = _Undefined
            if drop_login:
                self._login = None
                self._song = None
                self._track_cache.clear()
            self._built_at = 0.0

    def _make_login(self, cookies: dict[str, str], identifier: str | None):
        from spotapi.http.request import TLSClient
        from spotapi.login import Login
        from spotapi.types.data import Config
        from spotapi.utils.logger import NoopLogger

        client = TLSClient("chrome_120", "", auto_retries=self.http_retries)
        client.timeout_seconds = self.http_timeout_s  # tls_client default is 30s per attempt
        cfg = Config(logger=NoopLogger(), client=client)
        return Login.from_cookies({"identifier": identifier or "webview-session", "cookies": cookies}, cfg)

    def _ensure_login(self):
        if self._built_at and time.time() - self._built_at > self.max_age_s:
            log.info("Session objects older than %ss, rebuilding", self.max_age_s)
            self.reset()
        if self._login is None:
            rec = self.store.load()
            if rec is None or rec.cookies is None:
                raise ApiError(ErrorCode.SPOTIFY_SESSION_MISSING, "No Spotify session stored. Log in to Spotify in the app first")
            if not rec.valid:
                raise ApiError(ErrorCode.SPOTIFY_SESSION_INVALID, "Stored Spotify session is invalid; log in again in the app")
            clear_spotapi_token_caches()
            self._login = self._make_login(rec.cookies, rec.username)
        if not self._built_at:
            self._built_at = time.time()
        return self._login

    def _get_song(self):
        if self._song is None:
            from spotapi.song import Song

            self._song = Song(client=self._ensure_login().client)
        return self._song

    def _rebind_auth(self) -> None:
        """Every BaseClient() re-binds login.client.authenticate; make sure the newest
        Player/PlayerStatus base (fresh tokens) is the one in charge."""
        for obj in (self._player, self._status):
            if obj is not None:
                obj.client.authenticate = lambda kw, b=obj.base: b._auth_rule(kw)
                return

    def _reader(self):
        if self._player is not None:
            return self._player
        if self._status is None:
            from spotapi.status import PlayerStatus

            self._status = PlayerStatus(self._ensure_login())
        return self._status

    def _get_player(self, device_id: str | None):
        from spotapi.player import Player

        login = self._ensure_login()
        if self._player is None:
            reader = self._reader()
            reader.renew_state()
            dump = reader._device_dump or {}
            active = dump.get("active_device_id")
            known = set((dump.get("devices") or {}).keys())
            if not active and not device_id:
                raise ApiError(ErrorCode.NO_ACTIVE_DEVICE, "No active Spotify Connect device. Open Spotify on a device and start playback once, then retry")
            if device_id and device_id not in known:
                raise ApiError(ErrorCode.NO_ACTIVE_DEVICE, "device_id is not an online Spotify Connect device (see /devices)")
            # One websocket is enough: drop the reader, the Player is also a PlayerStatus.
            if self._status is not None:
                try:
                    self._status.close()
                except Exception:  # noqa: BLE001
                    pass
                self._status = None
            self._player = Player(login, device_id=device_id)
            return self._player

        p = self._player
        p.renew_state()
        dump = p._device_dump or {}
        active = dump.get("active_device_id")
        if device_id:
            if device_id not in (dump.get("devices") or {}):
                raise ApiError(ErrorCode.NO_ACTIVE_DEVICE, "device_id is not an online Spotify Connect device (see /devices)")
            if device_id != active:
                p.transfer_player(active or device_id, device_id)
            p.active_id = device_id
        else:
            if not active:
                raise ApiError(ErrorCode.NO_ACTIVE_DEVICE, "No active Spotify Connect device. Open Spotify on a device and start playback once, then retry")
            p.active_id = active
        return p

    def _call(self, fn: Callable[[], T]) -> T:
        with self.lock:
            for attempt in (0, 1):
                try:
                    out = fn()
                    return out
                except ApiError:
                    raise
                except Exception as e:  # noqa: BLE001
                    err = classify(e, self.retry_after_s)
                    log.warning("Spotify call failed (attempt %d): %s -> %s", attempt + 1, type(e).__name__, err.code.value)
                    retry = attempt == 0 and (
                        err.code in (ErrorCode.SPOTIFY_SESSION_INVALID, ErrorCode.TOKEN_UNAVAILABLE)
                        or _is_transient(e)
                    )
                    if retry:
                        if err.code == ErrorCode.TOKEN_UNAVAILABLE:
                            self.totp.invalidate()
                        self.reset()
                        continue
                    if err.code == ErrorCode.SPOTIFY_SESSION_INVALID:
                        self.store.mark_invalid()
                        self.reset(drop_login=True)
                    elif err.code in (ErrorCode.SPOTIFY_UPSTREAM, ErrorCode.COMMAND_FAILED, ErrorCode.NO_ACTIVE_DEVICE):
                        self.reset()
                    raise err
            raise AssertionError("unreachable")

    # ------------------------------------------------------------------ session
    @staticmethod
    def _pathfinder(base, operation: str, variables: dict | None = None) -> dict:
        params = {
            "operationName": operation,
            "variables": json.dumps(variables or {}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": base.part_hash(operation)}}),
        }
        resp = base.client.get("https://api-partner.spotify.com/pathfinder/v1/query", params=params, authenticate=True)
        if resp.fail:
            from spotapi.exceptions import UserError

            raise UserError(f"pathfinder {operation} failed", error=resp.error.string)
        return resp.response if isinstance(resp.response, dict) else {}

    def connect(self, cookies: dict[str, str]):
        """Validate cookies against Spotify, then store them encrypted and swap the live session."""
        from spotapi.song import Song
        from spotapi.user import User

        with self.lock:
            # Global spotapi token caches would otherwise hand the *old* account's token out.
            self.reset(drop_login=True)
            try:
                login = self._make_login(cookies, None)
                user = User(login)
                if not user.verify_login():
                    raise ApiError(ErrorCode.INVALID_COOKIES, "Spotify rejected these cookies (not logged in)")
                username = display_name = product = None
                try:
                    prof = (user.get_user_info() or {}).get("profile") or {}
                    username = prof.get("username")
                    display_name = prof.get("display_name") or prof.get("name")
                except Exception as e:  # noqa: BLE001
                    log.info("profile lookup failed: %s", type(e).__name__)
                song = Song(client=login.client)
                try:
                    acc = ((self._pathfinder(song.base, "accountAttributes").get("data") or {}).get("me") or {}).get("account") or {}
                    product = str(acc.get("product") or "").upper() or None
                except Exception as e:  # noqa: BLE001
                    if status_code_of(e) == 401:
                        raise ApiError(ErrorCode.INVALID_COOKIES, "Spotify rejected these cookies (401)")
                    log.info("plan lookup failed: %s", type(e).__name__)
                if not display_name:
                    try:
                        me = ((self._pathfinder(song.base, "profileAttributes").get("data") or {}).get("me") or {}).get("profile") or {}
                        display_name = me.get("name")
                        username = username or me.get("username")
                    except Exception as e:  # noqa: BLE001
                        log.info("profileAttributes lookup failed: %s", type(e).__name__)
            except ApiError:
                self.reset(drop_login=True)
                raise
            except Exception as e:  # noqa: BLE001
                self.reset(drop_login=True)
                err = classify(e, self.retry_after_s)
                if err.code == ErrorCode.SPOTIFY_SESSION_INVALID:
                    err = ApiError(ErrorCode.INVALID_COOKIES, "Spotify rejected these cookies (401)")
                if err.code == ErrorCode.TOKEN_UNAVAILABLE:
                    self.totp.invalidate()
                raise err
            rec = self.store.save(cookies, username=username, display_name=display_name, product=product)
            self._login = login
            self._song = song
            self._built_at = time.time()
            return rec

    def disconnect(self) -> None:
        with self.lock:
            self.store.delete()
            self.reset(drop_login=True)

    # ------------------------------------------------------------------ player
    def _command(self, name: str, device_id: str | None) -> None:
        def run():
            p = self._get_player(device_id)
            self._rebind_auth()
            getattr(p, name)()
            self._np_cache = None

        self._call(run)

    def pause(self, device_id: str | None = None) -> None:
        self._command("pause", device_id)

    def resume(self, device_id: str | None = None) -> None:
        self._command("resume", device_id)

    def next(self, device_id: str | None = None) -> None:
        self._command("skip_next", device_id)

    def prev(self, device_id: str | None = None) -> None:
        self._command("skip_prev", device_id)

    def _track_info(self, uri: str) -> dict | None:
        if uri in self._track_cache:
            self._track_cache.move_to_end(uri)
            return self._track_cache[uri]
        song = self._get_song()
        self._rebind_auth()
        data = (song.get_track_info(uri.split(":")[-1]).get("data") or {}).get("trackUnion") or {}
        if not data.get("uri"):
            return None
        t = track_from_pathfinder(data)
        self._track_cache[uri] = t
        while len(self._track_cache) > 256:
            self._track_cache.popitem(last=False)
        return t

    def lyrics(self, uri_or_id: str) -> dict:
        """Ambil lirik: color-lyrics internal Spotify dulu, fallback lrclib.net."""
        track_uri = parse_track_uri(uri_or_id) if uri_or_id else None
        if not track_uri or not track_uri.startswith("spotify:track:"):
            raise ApiError(ErrorCode.BAD_REQUEST, "uri/track_id lagu tidak valid")
        track_id = track_uri.split(":")[-1]

        def run():
            # 1) endpoint color-lyrics internal Spotify (pakai token sesi yang sama)
            song = self._get_song()
            self._rebind_auth()
            url = f"https://spclient.wg.spotify.com/color-lyrics/v2/track/{track_id}"
            resp = song.base.client.get(
                url,
                authenticate=True,
                params={"format": "json", "vocalRemoval": "false", "market": "from_token"},
                headers={"App-Platform": "WebPlayer"},
            )
            if resp.status_code == 200 and isinstance(resp.response, dict):
                lines, synced = color_lyrics_lines(resp.response)
                if lines:
                    return {"track_id": track_id, "synced": synced, "source": "spotify", "lines": lines}
            elif resp.status_code == 401:
                # token basi -> lempar biar _call refresh sesi & coba ulang sekali
                raise RuntimeError("color-lyrics Status Code: 401")
            # 404 atau 200-tanpa-lirik -> lanjut fallback lrclib

            # 2) fallback lrclib.net (butuh metadata lagu)
            try:
                meta = self._track_info(track_uri)
            except Exception:  # noqa: BLE001  metadata best-effort
                meta = None
            if meta:
                got = fetch_lrclib(meta)
                if got:
                    lines, synced = got
                    return {"track_id": track_id, "synced": synced, "source": "lrclib", "lines": lines}
            raise ApiError(ErrorCode.NO_RESULTS, "Lirik tidak tersedia untuk lagu ini")

        return self._call(run)

    def play(self, *, query: str | None, uri: str | None, index: int, device_id: str | None) -> tuple[dict, str]:
        track_uri = parse_track_uri(uri) if uri else None

        def run():
            p = self._get_player(device_id)
            self._rebind_auth()
            if track_uri:
                try:
                    track = self._track_info(track_uri)
                except Exception as e:  # noqa: BLE001  metadata is best-effort
                    if status_code_of(e) in (401, 429):
                        raise
                    track = None
                track = track or {"uri": track_uri, "title": "", "artists": [], "album": None,
                                  "duration_ms": None, "image_url": None}
            else:
                song = self._get_song()
                self._rebind_auth()
                res = song.query_songs(query, limit=max(10, index + 1))
                items = (((res.get("data") or {}).get("searchV2") or {}).get("tracksV2") or {}).get("items") or []
                if index >= len(items):
                    raise ApiError(ErrorCode.NO_RESULTS, f"No track found for query (results: {len(items)})")
                d = (items[index].get("item") or {}).get("data") or {}
                if not d.get("uri"):
                    raise ApiError(ErrorCode.NO_RESULTS, "Search result has no playable track")
                track = track_from_pathfinder(d)
            self._rebind_auth()
            p.add_to_queue(track["uri"])
            p.skip_next()
            self._np_cache = None
            return track, str(p.active_id)

        return self._call(run)

    def _dump(self) -> dict:
        r = self._reader()
        self._rebind_auth()
        r.renew_state()
        return r._device_dump or {}

    def _phantom_ids(self) -> set[str]:
        ids = set()
        for obj in (self._player, self._status):
            if obj is not None:
                ids.add(str(obj.device_id))
                ids.add("hobs_" + str(obj.device_id))
        return ids

    def devices(self) -> dict:
        def run():
            dump = self._dump()
            active = dump.get("active_device_id")
            phantom = self._phantom_ids()
            out = []
            for did, d in (dump.get("devices") or {}).items():
                caps = d.get("capabilities") or {}
                if did in phantom or did.startswith("hobs_") or caps.get("hidden"):
                    continue
                vol = d.get("volume")
                out.append({
                    "id": did,
                    "name": d.get("name") or did,
                    "type": str(d.get("device_type") or "UNKNOWN").upper(),
                    "is_active": did == active,
                    "can_play": bool(d.get("can_play", caps.get("can_be_player", False))),
                    "volume_percent": max(0, min(100, round(int(vol) * 100 / 65535))) if vol is not None else None,
                })
            return {"active_device_id": active if active and active not in phantom else None, "devices": out}

        return self._call(run)

    def now_playing(self) -> dict:
        def run():
            now = time.time()
            if self._np_cache and (now - self._np_cache[0]) * 1000 < self.np_cache_ms:
                return self._np_cache[1]
            dump = self._dump()
            active = dump.get("active_device_id")
            st = dump.get("player_state") or {}
            if not active:
                res = {"is_playing": False, "is_paused": None, "track": None, "position_ms": None,
                       "active_device_id": None, "device_name": None}
                self._np_cache = (now, res)
                return res
            dev = (dump.get("devices") or {}).get(active) or {}
            t = st.get("track") or {}
            md = t.get("metadata") or {}
            uri = t.get("uri")
            track = None
            if uri:
                artists = [v for k, v in sorted(md.items()) if re.fullmatch(r"artist_name(:\d+)?", k) and v]
                track = {
                    "uri": uri,
                    "title": md.get("title") or "",
                    "artists": artists,
                    "album": md.get("album_title"),
                    "duration_ms": _int_or_none(st.get("duration")),
                    "image_url": _image_url(md.get("image_xlarge_url") or md.get("image_large_url") or md.get("image_url")),
                }
                if uri.startswith("spotify:track:") and (not track["title"] or not track["artists"]):
                    try:
                        info = self._track_info(uri)
                    except Exception as e:  # noqa: BLE001
                        if status_code_of(e) in (401, 429):
                            raise
                        info = None
                    if info:
                        track = {**info, **{k: v for k, v in track.items() if v}}
            playing = bool(st.get("is_playing"))
            paused = st.get("is_paused")
            pos = _int_or_none(st.get("position_as_of_timestamp"))
            ts = _int_or_none(st.get("timestamp"))
            if pos is not None and ts and playing and not paused:
                speed = float(st.get("playback_speed") or 1.0)
                pos = int(pos + max(0, now * 1000 - ts) * speed)
            dur = track["duration_ms"] if track else None
            if pos is not None and dur:
                pos = max(0, min(pos, dur))
            res = {
                "is_playing": playing and not bool(paused),
                "is_paused": bool(paused) if paused is not None else None,
                "track": track,
                "position_ms": pos,
                "active_device_id": active,
                "device_name": dev.get("name"),
            }
            self._np_cache = (now, res)
            return res

        return self._call(run)
