"""FastAPI app implementing openapi.yaml (SpotAPI Wrapper Backend v1)."""
from __future__ import annotations

import hmac
import logging
import sys
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings, load_settings
from .errors import ApiError, ErrorCode

log = logging.getLogger("spotapi-backend")


# ---------------------------------------------------------------- request models
class PairRequest(BaseModel):
    pairing_code: str = Field(min_length=1, max_length=128)
    device_name: str = Field(min_length=1, max_length=128)


class SessionCookies(BaseModel):
    model_config = ConfigDict(extra="allow")
    sp_dc: str = Field(min_length=10, max_length=4096)
    sp_key: Optional[str] = Field(default=None, max_length=4096)
    sp_t: Optional[str] = Field(default=None, max_length=4096)


class SessionCreateRequest(BaseModel):
    cookies: SessionCookies

    @field_validator("cookies")
    @classmethod
    def _limit_extra(cls, v: SessionCookies) -> SessionCookies:
        extra = v.model_extra or {}
        if len(extra) > 50:
            raise ValueError("too many cookies")
        for k, val in extra.items():
            if not isinstance(val, str) or len(k) > 128 or len(val) > 4096:
                raise ValueError("cookie values must be strings (<=4096 chars)")
        return v

    def cookie_dict(self) -> dict[str, str]:
        d = {"sp_dc": self.cookies.sp_dc}
        if self.cookies.sp_key:
            d["sp_key"] = self.cookies.sp_key
        if self.cookies.sp_t:
            d["sp_t"] = self.cookies.sp_t
        for k, v in (self.cookies.model_extra or {}).items():
            if v:
                d[k] = v
        return d


class PlayRequest(BaseModel):
    query: Optional[str] = Field(default=None, max_length=500)
    uri: Optional[str] = Field(default=None, max_length=500)
    index: int = Field(default=0, ge=0, le=99)
    device_id: Optional[str] = Field(default=None, max_length=128)


class DeviceCommandRequest(BaseModel):
    device_id: Optional[str] = Field(default=None, max_length=128)


# ---------------------------------------------------------------- helpers
def _err_response(err: ApiError) -> JSONResponse:
    return JSONResponse(err.body(), status_code=err.status, headers=err.headers())


class _PairLimiter:
    """5 failed pairing attempts per 10 minutes, then 429."""

    def __init__(self, limit: int = 5, window: int = 600) -> None:
        self.limit, self.window = limit, window
        self.fails: list[float] = []
        self.lock = threading.Lock()

    def check(self) -> None:
        with self.lock:
            now = time.time()
            self.fails = [t for t in self.fails if now - t < self.window]
            if len(self.fails) >= self.limit:
                wait = int(self.window - (now - self.fails[0])) + 1
                raise ApiError(ErrorCode.RATE_LIMITED, "Too many pairing attempts", retry_after=wait)

    def fail(self) -> None:
        with self.lock:
            self.fails.append(time.time())


def create_app(settings: Settings | None = None, service=None) -> FastAPI:
    settings = settings or load_settings()
    if len(settings.app_token) < 16:
        raise RuntimeError("APP_TOKEN env var is required (>=16 chars). Generate one with: openssl rand -hex 32")

    if service is None:
        if not settings.enc_key:
            raise RuntimeError("ENC_KEY env var is required (Fernet key). See README.")
        from .spotify import SpotifyService
        from .storage import SessionStore
        from .totp import TotpSecretProvider, install

        totp = TotpSecretProvider(settings.totp_secret_urls, settings.totp_cache_path,
                                  settings.totp_fetch_timeout, settings.totp_fetch_retries)
        install(totp)
        store = SessionStore(settings.db_path, settings.enc_key)
        service = SpotifyService(store, totp, max_age_s=settings.session_max_age_s,
                                 retry_after_s=settings.rate_limit_retry_after_s,
                                 now_playing_cache_ms=settings.now_playing_cache_ms,
                                 http_timeout_s=settings.http_timeout_s, http_retries=settings.http_retries)

    app = FastAPI(title="SpotAPI Wrapper Backend", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.settings = settings
    limiter = _PairLimiter()
    token_bytes = settings.app_token.encode()

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return _err_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return _err_response(ApiError(ErrorCode.BAD_REQUEST, f"Invalid request: {loc or 'body'}: {first.get('msg', 'invalid')}"))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = ErrorCode.UNAUTHORIZED if exc.status_code == 401 else ErrorCode.BAD_REQUEST
        return _err_response(ApiError(code, str(exc.detail), status=exc.status_code, retryable=False))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("Unhandled error: %s", type(exc).__name__)
        return _err_response(ApiError(ErrorCode.SPOTIFY_UPSTREAM, "Internal error", status=500))

    def require_token(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        scheme, _, tok = auth.partition(" ")
        if scheme.lower() != "bearer" or not tok or not hmac.compare_digest(tok.strip().encode(), token_bytes):
            raise ApiError(ErrorCode.UNAUTHORIZED, "Missing or invalid app token")

    public = APIRouter(prefix="/api/v1")
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])

    @public.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @public.post("/auth/pair")
    def pair(body: PairRequest):
        if not settings.pairing_code:
            raise ApiError(ErrorCode.BAD_REQUEST, "Pairing is disabled on this server; use the static APP_TOKEN", status=404)
        limiter.check()
        if not hmac.compare_digest(body.pairing_code.strip().encode(), settings.pairing_code.encode()):
            limiter.fail()
            raise ApiError(ErrorCode.UNAUTHORIZED, "Invalid pairing code")
        log.info("Paired device %r", body.device_name[:64])
        return {"app_token": settings.app_token, "expires_at": None}

    def _status_body(rec) -> dict:
        if rec is None:
            return {"connected": False, "username": None, "display_name": None, "product": None, "checked_at": None}
        return {"connected": bool(rec.valid), "username": rec.username, "display_name": rec.display_name,
                "product": rec.product, "checked_at": rec.checked_at}

    @api.get("/spotify/session")
    def session_get():
        return _status_body(service.store.load())

    @api.post("/spotify/session")
    def session_post(body: SessionCreateRequest):
        return _status_body(service.connect(body.cookie_dict()))

    @api.delete("/spotify/session", status_code=204)
    def session_delete():
        service.disconnect()
        return Response(status_code=204)

    @api.post("/play")
    def play(body: PlayRequest):
        q = (body.query or "").strip() or None
        u = (body.uri or "").strip() or None
        if not q and not u:
            raise ApiError(ErrorCode.BAD_REQUEST, "Provide either query or uri")
        if q and u:
            raise ApiError(ErrorCode.BAD_REQUEST, "Provide either query or uri, not both")
        track, device_id = service.play(query=q, uri=u, index=body.index, device_id=body.device_id or None)
        return {"ok": True, "track": track, "device_id": device_id}

    def _device(body: Optional[DeviceCommandRequest]) -> Optional[str]:
        return (body.device_id or None) if body else None

    @api.post("/pause")
    def pause(body: Optional[DeviceCommandRequest] = None):
        service.pause(_device(body))
        return {"ok": True}

    @api.post("/resume")
    def resume(body: Optional[DeviceCommandRequest] = None):
        service.resume(_device(body))
        return {"ok": True}

    @api.post("/next")
    def next_(body: Optional[DeviceCommandRequest] = None):
        service.next(_device(body))
        return {"ok": True}

    @api.post("/prev")
    def prev(body: Optional[DeviceCommandRequest] = None):
        service.prev(_device(body))
        return {"ok": True}

    @api.get("/now-playing")
    def now_playing():
        return service.now_playing()

    @api.get("/lyrics")
    def lyrics(uri: Optional[str] = Query(default=None),
               track_id: Optional[str] = Query(default=None)):
        key = uri or track_id
        if not key:
            raise ApiError(ErrorCode.BAD_REQUEST, "Sertakan ?uri= atau ?track_id=")
        return service.lyrics(key)

    @api.get("/devices")
    def devices():
        return service.devices()

    app.include_router(public)
    app.include_router(api)
    return app


def _setup_logging(level: str) -> None:
    logging.basicConfig(stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def app_factory() -> FastAPI:
    s = load_settings()
    _setup_logging(s.log_level)
    return create_app(s)
