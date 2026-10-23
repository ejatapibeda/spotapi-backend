# spotapi-backend

A small, **self-hosted, single-user** HTTP backend that lets the
[Kiko Companion](https://github.com/ejatapibeda/kiko-companion) Android app (or any HTTP client)
control Spotify: play by search or URI, pause/resume/skip, now playing with album art, Connect
devices, and synced lyrics.

It wraps [SpotAPI](https://github.com/ejatapibeda/SpotAPI) (a fork of
[Aran404/SpotAPI](https://github.com/Aran404/SpotAPI)), which talks to Spotify's **web-player API**.
That means **no Spotify Premium and no Spotify developer app are needed**. You log in with your
normal account and the backend uses the web-player cookies.

> [!WARNING]
> This is **unofficial**. It uses Spotify's private web-player API, which Spotify can change at
> any time (expect occasional breakage), and using it may violate Spotify's Terms of Service.
> Use it with **your own account**, at your own risk, for personal/educational purposes.

## Features

- FastAPI in one Docker container, **amd64 and arm64** (Raspberry Pi 4/5, Armbian boards, x86 servers).
- Bearer-token auth (`APP_TOKEN`) with an optional one-time **pairing code** flow.
- Spotify cookies stored **encrypted** (Fernet, `ENC_KEY`) in SQLite under `./data`. They are never returned by the API.
- `POST /play` with a free-text `query` (search happens on the server) or a track `uri`.
- `GET /now-playing` (title, artists, album, art URL, position, duration, device) and `GET /devices`.
- `GET /lyrics`: Spotify's own (color-lyrics) lyrics, with a fallback to [lrclib.net](https://lrclib.net). Line-synced when available.
- Robust TOTP-secret handling (several mirrors, disk cache, bundled seed), automatic session rebuild, one retry on transient errors.
- One consistent error envelope with machine-readable codes and `Retry-After` on rate limits.
- Hardened container: read-only rootfs, non-root user, `no-new-privileges`, healthcheck, log rotation.

Full contract: [`openapi.yaml`](./openapi.yaml) (OpenAPI 3.0).

## Requirements

- Docker + Docker Compose v2, on a Linux host with **glibc**. `tls_client` loads a prebuilt Go
  shared library, so Alpine/musl images do not work; the Dockerfile uses `python:3.12-slim`.
- A Spotify account (Free works).
- **A Spotify Connect device that is online**: Spotify open on a phone, PC, web player or speaker.
  The backend can control playback but cannot start a Spotify client from nothing.
- HTTPS in front of it if a phone will use it (Android blocks cleartext HTTP). See [Exposing it over HTTPS](#exposing-it-over-https).

## Quick start

```bash
git clone https://github.com/ejatapibeda/spotapi-backend.git
cd spotapi-backend

cp .env.example .env
chmod 600 .env

# 1) APP_TOKEN: the bearer token clients send
sed -i "s|^APP_TOKEN=.*|APP_TOKEN=$(openssl rand -hex 32)|" .env
# 2) ENC_KEY: Fernet key (32 random bytes, url-safe base64) that encrypts the stored Spotify cookies
sed -i "s|^ENC_KEY=.*|ENC_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" .env
# 3) run the container as you, so it can write ./data
sed -i "s|^PUID=.*|PUID=$(id -u)|; s|^PGID=.*|PGID=$(id -g)|" .env
# 4) recommended when you put a proxy/Tailscale Serve in front: only listen on localhost
sed -i "s|^BIND_ADDR=.*|BIND_ADDR=127.0.0.1|" .env

mkdir -p data
docker compose up -d --build
docker compose logs -f            # Ctrl+C to stop following

curl -s http://127.0.0.1:8080/api/v1/health
# {"ok":true,"version":"1.0.2"}
```

Check auth. The first call should return 401 and the second `{"connected":false,...}`:

```bash
curl -s http://127.0.0.1:8080/api/v1/spotify/session
curl -s -H "Authorization: Bearer $(grep ^APP_TOKEN= .env | cut -d= -f2)" \
     http://127.0.0.1:8080/api/v1/spotify/session
```

Update: `git pull && docker compose up -d --build`. Stop: `docker compose down`. Your session stays in `./data`.

**arm64:** nothing special. Build on the device itself (`docker compose up -d --build`). The build
fails early if the `tls_client` native library can't load on your architecture. 32-bit ARM (armv7)
is not supported by `tls_client`.

## Configuration (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_TOKEN` | **yes** | | Static bearer token, at least 16 chars. Use `openssl rand -hex 32`. Required on every endpoint except `/health` and `/auth/pair`. |
| `ENC_KEY` | **yes** | | Fernet key used to encrypt the stored cookies. If you change or lose it, the stored session can't be read; just log in again. |
| `PORT` | no | `8080` | Port inside and outside the container. |
| `BIND_ADDR` | no | `0.0.0.0` | Host interface to publish on. Use `127.0.0.1` behind Tailscale Serve or a reverse proxy, or your Tailscale IP (`tailscale ip -4`) for tailnet-only access. |
| `PAIRING_CODE` | no | empty | If set, `POST /auth/pair` with this code returns `APP_TOKEN`. Empty disables pairing (the endpoint returns 404). |
| `PUID` / `PGID` | no | `1000` | uid/gid the container runs as. Must be able to write `./data`. |
| `LOG_LEVEL` | no | `info` | `debug`, `info`, `warning` or `error`. Secrets are never logged. |
| `SESSION_MAX_AGE_S` | no | `2700` | Rebuild the in-memory Spotify session objects after this many seconds. |
| `RATE_LIMIT_RETRY_AFTER_S` | no | `10` | `Retry-After` value sent with `RATE_LIMITED`. |
| `NOW_PLAYING_CACHE_MS` | no | `1500` | Tiny cache so client polling doesn't hammer Spotify. |
| `HTTP_TIMEOUT_S` | no | `12` | Per-request timeout towards Spotify. |
| `HTTP_RETRIES` | no | `1` | Extra attempts per request towards Spotify. |
| `TOTP_FETCH_TIMEOUT` / `TOTP_FETCH_RETRIES` | no | `6` / `2` | Timeout and retries when fetching the web-player TOTP secret. |
| `TOTP_SECRET_URLS` | no | two public mirrors | Comma-separated list of URLs serving the web-player TOTP secret dict. |
| `DB_PATH` | no | `/data/spotapi.db` | SQLite path inside the container. |

> **About `app/totp_seed.json`:** Spotify's web player derives its access-token request from a
> TOTP secret that is **public** (it ships in the web player's JavaScript and is mirrored by
> community projects). The backend fetches the newest one from `TOTP_SECRET_URLS`, caches it in
> `data/totp_secret.json`, and only falls back to this bundled seed when every mirror is down. It is
> not a credential and contains nothing account-specific.

## Authentication

Every endpoint except `GET /health` and `POST /auth/pair` needs:

```
Authorization: Bearer <APP_TOKEN>
```

There are two ways to get the token into a client:

1. **Static token:** type or paste `APP_TOKEN` directly into the client (for example Kiko's settings on the phone).
   Don't paste it into chats, issues or screenshots.
2. **Pairing code (no copy/paste):** set `PAIRING_CODE=483-219` (any string), `docker compose up -d`,
   enter the code in the client, which calls `POST /auth/pair` and receives the token. Then **clear `PAIRING_CODE` and restart**.
   Wrong codes are rate-limited: 5 failures per 10 minutes, then `429`.

If the token leaks, generate a new `APP_TOKEN`, run `docker compose up -d`, and enter the new token in the client.

## Connecting your Spotify account

The backend needs your Spotify **web-player cookies**. `sp_dc` is required. `sp_key`, `sp_t` and
any other `sp_*` cookies are optional and passed along. It does **not** need your password.

- **With Kiko Companion:** Settings → Spotify → *Connect Spotify account* opens an in-app WebView
  where you log in to Spotify. Kiko reads the cookies, sends them **once** to `POST /spotify/session`,
  and clears the WebView cookies.
- **Manually** (any client): log in at <https://open.spotify.com> in a desktop browser, open
  DevTools → Application/Storage → Cookies → `https://open.spotify.com`, copy the value of `sp_dc`, then:

  ```bash
  curl -s -X POST http://127.0.0.1:8080/api/v1/spotify/session \
    -H "Authorization: Bearer $APP_TOKEN" -H "Content-Type: application/json" \
    -d '{"cookies":{"sp_dc":"<paste sp_dc here>"}}'
  # {"connected":true,"username":"31abc...","display_name":"Jane","product":"FREE","checked_at":"..."}
  ```

The server validates the cookies against Spotify (`Login.from_cookies` + `verify_login`), stores them
encrypted in `data/spotapi.db`, and only ever returns the status above. `sp_dc` is valid for
months, but when Spotify expires it you'll get `SPOTIFY_SESSION_INVALID`, so log in again.
Logging out with `DELETE /spotify/session` deletes the stored cookies.

## API reference

Base URL: `https://<your-host>/api/v1` (or `http://127.0.0.1:8080/api/v1` locally).

| Method | Path | Auth | Body / query | Response |
|---|---|---|---|---|
| GET | `/health` | no | | `{"ok":true,"version":"1.0.2"}` |
| POST | `/auth/pair` | no | `{"pairing_code":"483-219","device_name":"My phone"}` | `{"app_token":"…","expires_at":null}`. 404 if pairing is disabled. |
| GET | `/spotify/session` | yes | | `{"connected":bool,"username","display_name","product","checked_at"}` |
| POST | `/spotify/session` | yes | `{"cookies":{"sp_dc":"…","sp_key":"…","sp_t":"…"}}` | session status (as above) |
| DELETE | `/spotify/session` | yes | | `204` |
| POST | `/play` | yes | `{"query":"…"}` **or** `{"uri":"spotify:track:…"}`, optional `index` (0–99, which search result), `device_id` | `{"ok":true,"track":{…},"device_id":"…"}` |
| POST | `/pause` `/resume` `/next` `/prev` | yes | optional `{"device_id":"…"}` | `{"ok":true}` |
| GET | `/now-playing` | yes | | see below |
| GET | `/devices` | yes | | `{"active_device_id":"…","devices":[{id,name,type,is_active,can_play,volume_percent}]}` |
| GET | `/lyrics` | yes | `?uri=spotify:track:ID` or `?track_id=ID` | see below. 404 `NO_RESULTS` if no lyrics are found. |

There is no separate search endpoint. **Search is done by `POST /play` with `query`**: the server
searches tracks and plays result number `index` (default 0, the top hit). `uri` accepts
`spotify:track:ID`, an `open.spotify.com/track/ID` URL or a bare ID, for tracks only.

### Examples

```bash
H="Authorization: Bearer $APP_TOKEN"
B=http://127.0.0.1:8080/api/v1

curl -s -X POST $B/play -H "$H" -H 'Content-Type: application/json' -d '{"query":"bohemian rhapsody"}'
```
```json
{"ok":true,"device_id":"a1b2c3...","track":{"uri":"spotify:track:4u7EnebtmKWzUH433cf5Qv","title":"Bohemian Rhapsody",
 "artists":["Queen"],"album":"A Night At The Opera","duration_ms":354320,"image_url":"https://i.scdn.co/image/..."}}
```

```bash
curl -s $B/now-playing -H "$H"
```
```json
{"is_playing":true,"is_paused":false,"position_ms":42000,"active_device_id":"a1b2c3...","device_name":"Web Player (Chrome)",
 "track":{"uri":"spotify:track:4u7EnebtmKWzUH433cf5Qv","title":"Bohemian Rhapsody","artists":["Queen"],
          "album":"A Night At The Opera","duration_ms":354320,"image_url":"https://i.scdn.co/image/..."}}
```
If no device is active you get `200` with `"track":null,"active_device_id":null`.

```bash
curl -s "$B/lyrics?uri=spotify:track:4u7EnebtmKWzUH433cf5Qv" -H "$H"
```
```json
{"track_id":"4u7EnebtmKWzUH433cf5Qv","synced":true,"source":"spotify",
 "lines":[{"start_ms":0,"text":"♪"},{"start_ms":12340,"text":"Is this the real life?"}]}
```
`source` is `spotify` or `lrclib`. When `synced` is `false`, every `start_ms` is `0`.

### Errors

Every error has the same shape:

```json
{"ok":false,"error":{"code":"NO_ACTIVE_DEVICE","message":"…","retryable":true}}
```

| HTTP | Code | Meaning / what to do |
|---|---|---|
| 400 | `BAD_REQUEST`, `INVALID_COOKIES` | Fix the request, or the cookies were rejected by Spotify |
| 401 | `UNAUTHORIZED` | Wrong or missing `APP_TOKEN` |
| 401 | `SPOTIFY_SESSION_INVALID` | Spotify cookies expired. Log in again. |
| 403 | `SPOTIFY_SESSION_MISSING` | No Spotify account connected yet |
| 404 | `NO_RESULTS` | Search found nothing or there are no lyrics |
| 409 | `NO_ACTIVE_DEVICE` | Open Spotify on any device, play something once, then retry |
| 429 | `RATE_LIMITED` | Wait `Retry-After` seconds |
| 502 | `SPOTIFY_UPSTREAM`, `COMMAND_FAILED` | Spotify hiccup. Retryable. |
| 503 | `TOKEN_UNAVAILABLE` | Couldn't get a web-player token (TOTP). Retryable. |

## Exposing it over HTTPS

Phones need HTTPS. Pick one option.

### Tailscale Serve (recommended, private)

Only devices on your tailnet can reach it, and you get a real certificate automatically.

```bash
# .env: BIND_ADDR=127.0.0.1
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
tailscale serve status
# Base URL: https://<machine>.<tailnet>.ts.net/api/v1
```
Install Tailscale on the phone as well and log in to the same tailnet.

### Tailscale Funnel (public internet)

```bash
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8080
```

> [!CAUTION]
> Funnel makes the API reachable by **anyone on the internet**. The bearer token is then your
> only protection. Use a long random `APP_TOKEN` (`openssl rand -hex 32`), keep `PAIRING_CODE`
> **empty** except during pairing, never share the token, and rotate it if in doubt.

### Reverse proxy / Cloudflare Tunnel (public)

Any TLS-terminating proxy works. For example, with Caddy (automatic Let's Encrypt):

```
spotify.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

The container runs uvicorn with `--proxy-headers`. The same warning as Funnel applies: the endpoint is public.
Cloudflare Tunnel (`cloudflared tunnel --url http://127.0.0.1:8080`) also works.

## Using it with Kiko Companion

1. In Kiko, go to **Settings → Spotify → Spotify source → spotapi**.
2. **Base URL:** `https://<your-host>/api/v1`.
3. Authenticate with the pairing code or by pasting `APP_TOKEN` (see [Authentication](#authentication)).
4. Tap **Connect Spotify account**, log in in the WebView, and the cookies go to `POST /spotify/session`.
5. Open Spotify on any device and play something once. After that, say "play …" to Kiko.

The full app-side documentation is in [kiko-companion/docs/spotify.md](https://github.com/ejatapibeda/kiko-companion/blob/main/docs/spotify.md).

## Troubleshooting

- **`NO_ACTIVE_DEVICE` (409):** no Spotify client is online and active. Open Spotify (phone, desktop or
  <https://open.spotify.com>), play anything once, then retry. Use `GET /devices` to see what the backend sees,
  and pass `device_id` to transfer playback to a specific online device.
- **First request after start or after idle is slow / client times out:** the first call builds
  the Spotify session (TOTP secret fetch, access token, player websocket), which can take 10–20 s
  on small ARM boards. Kiko times out after 15 s. Just retry; later calls are fast. Watch progress with `docker compose logs -f`.
- **Player calls fail with websocket/connect errors on IPv4-only hosts:** `dealer.spotify.com` has only
  IPv6 (AAAA) records. The vendored SpotAPI first asks Spotify's apresolve for IPv4 dealers.
  As a fallback, `docker-compose.yml` pins `dealer.spotify.com` to two IPv4 anycast dealers via `extra_hosts`.
  If those IPs ever stop working, remove the block or look up current ones (`dig +short A dealer.spotify.com` from a resolver that returns them, or apresolve).
- **`TOKEN_UNAVAILABLE` (503):** the TOTP mirrors were unreachable and the cached/bundled secret is stale.
  Check outbound internet access, or set `TOTP_SECRET_URLS` to a mirror that is up to date.
- **`SPOTIFY_SESSION_INVALID`:** the cookies expired or you logged out everywhere. Connect the account again.
- **Container restarts / `permission denied` on `/data`:** `PUID`/`PGID` must own `./data` (`sudo chown -R $(id -u):$(id -g) data`).
- **Build fails at `import tls_client`:** you're on an unsupported architecture or a musl-based image.
- **Free accounts:** Spotify may restrict some controls (ads, shuffle), especially on mobile clients.

## Limitations

- Single user and **one worker/one container**. Player state lives in memory, so don't scale it.
- `/play` plays tracks only (no albums or playlists). It works like the library: add to queue, then skip.
- Unofficial API: GraphQL hashes, the TOTP scheme or endpoints can change without notice.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx pyyaml openapi-schema-validator
.venv/bin/python -m pytest -q tests                  # offline, no Spotify account needed
LIVE=1 .venv/bin/python -m pytest -q tests -k live   # hits Spotify with FAKE cookies (expects INVALID_COOKIES)

APP_TOKEN=$(openssl rand -hex 32) ENC_KEY=$(openssl rand -base64 32 | tr '+/' '-_') \
DATA_DIR=./data PYTHONPATH=vendor/spotapi \
  .venv/bin/uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8080
```

| Path | Contents |
|---|---|
| `app/main.py` | Routes, bearer auth, pairing, error envelope |
| `app/spotify.py` | Session/player cache (one instance plus a lock, auto-rebuild), search, now playing, devices, lyrics, error mapping |
| `app/storage.py` | SQLite plus Fernet-encrypted cookie storage |
| `app/totp.py` | TOTP secret provider (mirrors, disk cache, bundled seed) |
| `app/lyrics.py` | Spotify color-lyrics parser and lrclib fallback |
| `app/config.py`, `app/errors.py` | Settings from env and error codes |
| `vendor/spotapi/` | Vendored SpotAPI (GPL-3.0), from [ejatapibeda/SpotAPI@10e3802](https://github.com/ejatapibeda/SpotAPI/tree/10e380237aa3ba9dc8c4cf0a66eef2e6c5766563) |
| `openapi.yaml` | API contract (tests check that routes and schemas match it) |

**Vendored vs pip:** SpotAPI is vendored so `docker compose up` just works with a known-good version.
If you'd rather install it with pip, drop `vendor/`, remove `PYTHONPATH=/app/vendor/spotapi` and `COPY vendor/` from the
Dockerfile, and add this to `requirements.txt`:

```
spotapi @ git+https://github.com/ejatapibeda/SpotAPI@10e380237aa3ba9dc8c4cf0a66eef2e6c5766563
```

(The fork includes the fixes this backend relies on: optional `MetadataMap` fields and IPv4 dealer resolution.)

## License & credits

**GPL-3.0** (see [LICENSE](./LICENSE)). This repository bundles SpotAPI, which is GPL-3.0, so the
combined work is distributed under GPL-3.0 as well.

- [Aran404/SpotAPI](https://github.com/Aran404/SpotAPI): the original library (GPL-3.0).
- [ejatapibeda/SpotAPI](https://github.com/ejatapibeda/SpotAPI): fork with the patches used here.
- [lrclib.net](https://lrclib.net): free lyrics fallback.
- [Kiko Companion](https://github.com/ejatapibeda/kiko-companion): the Android app this was built for.

Not affiliated with or endorsed by Spotify. "Spotify" is a trademark of Spotify AB.

---

## Ringkasan (Bahasa Indonesia)

Backend kecil self-hosted (FastAPI + Docker) supaya app **Kiko Companion** bisa mengontrol Spotify tanpa Premium dan tanpa developer app.

1. `cp .env.example .env`, isi `APP_TOKEN` (`openssl rand -hex 32`) dan `ENC_KEY`, lalu `docker compose up -d --build`.
2. Pasang HTTPS, paling aman pakai `tailscale serve`. Funnel atau reverse proxy juga bisa, tapi endpoint-nya jadi publik.
3. Di Kiko: Pengaturan → Spotify → spotapi, isi Base URL, lalu pakai pairing code atau tempel APP_TOKEN langsung di HP (**jangan** kirim token lewat chat). Setelah itu login Spotify di WebView.
4. Buka Spotify di salah satu device dan putar sekali, supaya ada device aktif.
