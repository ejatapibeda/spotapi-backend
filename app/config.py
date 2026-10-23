"""Environment-driven settings. Secrets (APP_TOKEN, ENC_KEY, PAIRING_CODE) are never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_token: str = field(repr=False, default="")
    enc_key: str = field(repr=False, default="")
    pairing_code: str = field(repr=False, default="")
    port: int = 8080
    data_dir: str = "/data"
    db_path: str = "/data/spotapi.db"
    totp_cache_path: str = "/data/totp_secret.json"
    totp_secret_urls: tuple[str, ...] = ()
    totp_fetch_timeout: float = 6.0
    totp_fetch_retries: int = 2
    session_max_age_s: int = 45 * 60
    rate_limit_retry_after_s: int = 10
    now_playing_cache_ms: int = 1500
    log_level: str = "info"
    http_timeout_s: int = 12
    http_retries: int = 1


DEFAULT_TOTP_URLS = (
    "https://code.thetadev.de/ThetaDev/spotify-secrets/raw/branch/main/secrets/secretDict.json",
    "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/main/secrets/secretDict.json",
)


def load_settings() -> Settings:
    data_dir = os.environ.get("DATA_DIR", "/data")
    urls = tuple(
        u.strip()
        for u in os.environ.get("TOTP_SECRET_URLS", ",".join(DEFAULT_TOTP_URLS)).split(",")
        if u.strip()
    )
    return Settings(
        app_token=os.environ.get("APP_TOKEN", "").strip(),
        enc_key=os.environ.get("ENC_KEY", "").strip(),
        pairing_code=os.environ.get("PAIRING_CODE", "").strip(),
        port=_int("PORT", 8080),
        data_dir=data_dir,
        db_path=os.environ.get("DB_PATH", os.path.join(data_dir, "spotapi.db")),
        totp_cache_path=os.path.join(data_dir, "totp_secret.json"),
        totp_secret_urls=urls,
        totp_fetch_timeout=float(os.environ.get("TOTP_FETCH_TIMEOUT", "6")),
        totp_fetch_retries=_int("TOTP_FETCH_RETRIES", 2),
        session_max_age_s=_int("SESSION_MAX_AGE_S", 45 * 60),
        rate_limit_retry_after_s=_int("RATE_LIMIT_RETRY_AFTER_S", 10),
        now_playing_cache_ms=_int("NOW_PLAYING_CACHE_MS", 1500),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        http_timeout_s=_int("HTTP_TIMEOUT_S", 12),
        http_retries=_int("HTTP_RETRIES", 1),
    )
