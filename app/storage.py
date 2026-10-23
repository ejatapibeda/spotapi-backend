"""SQLite store for the single Spotify session. Cookies are Fernet-encrypted at rest."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken


@dataclass
class SessionRecord:
    cookies: dict[str, str] | None  # None if it could not be decrypted (ENC_KEY changed)
    username: str | None
    display_name: str | None
    product: str | None
    checked_at: str | None
    valid: bool


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SessionStore:
    def __init__(self, db_path: str, enc_key: str) -> None:
        try:
            self._fernet = Fernet(enc_key.encode())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "ENC_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)."
            ) from e
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS spotify_session (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cookies_enc BLOB NOT NULL,
                    username TEXT, display_name TEXT, product TEXT,
                    checked_at TEXT, valid INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )"""
            )
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)

    def save(self, cookies: dict[str, str], *, username: str | None, display_name: str | None,
             product: str | None) -> SessionRecord:
        enc = self._fernet.encrypt(json.dumps(cookies).encode())
        now = utcnow_iso()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO spotify_session (id, cookies_enc, username, display_name, product, checked_at, valid, created_at)
                   VALUES (1, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(id) DO UPDATE SET cookies_enc=excluded.cookies_enc, username=excluded.username,
                     display_name=excluded.display_name, product=excluded.product,
                     checked_at=excluded.checked_at, valid=1, created_at=excluded.created_at""",
                (enc, username, display_name, product, now, now),
            )
        return SessionRecord(cookies, username, display_name, product, now, True)

    def load(self) -> SessionRecord | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT cookies_enc, username, display_name, product, checked_at, valid FROM spotify_session WHERE id=1"
            ).fetchone()
        if not row:
            return None
        try:
            cookies = json.loads(self._fernet.decrypt(row[0]).decode())
        except (InvalidToken, ValueError):
            cookies = None
        return SessionRecord(cookies, row[1], row[2], row[3], row[4], bool(row[5]) and cookies is not None)

    def mark_invalid(self) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE spotify_session SET valid=0, checked_at=? WHERE id=1", (utcnow_iso(),))

    def delete(self) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM spotify_session WHERE id=1")
