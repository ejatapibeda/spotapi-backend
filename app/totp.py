"""Robust TOTP-secret provider for spotapi.

The upstream library fetches the secret once per 15 min from a single URL with a 5s timeout
and falls back to a stale hard-coded secret (v18), which makes open.spotify.com/api/token
return 400. Here: several mirrors, per-attempt timeout + retries, last good secret cached on
disk (/data/totp_secret.json), then a bundled seed file, and only then the library fallback.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Tuple

import requests

log = logging.getLogger("spotapi-backend.totp")

_SEED_PATH = os.path.join(os.path.dirname(__file__), "totp_seed.json")
_MEM_TTL = 15 * 60
_FAIL_BACKOFF = 60  # after all mirrors fail, don't retry network for this long


def _parse(d: object) -> Tuple[int, bytearray]:
    if not isinstance(d, dict) or not d:
        raise ValueError("secret dict empty or wrong type")
    version = max(d, key=lambda k: int(k))
    lst = d[version]
    if not isinstance(lst, list) or not all(isinstance(x, int) for x in lst):
        raise ValueError("secret is not a list of ints")
    return int(version), bytearray(lst)


class TotpSecretProvider:
    def __init__(self, urls: tuple[str, ...], cache_path: str, timeout: float = 6.0, retries: int = 2) -> None:
        self.urls = urls
        self.cache_path = cache_path
        self.timeout = timeout
        self.retries = max(0, retries)
        self._lock = threading.Lock()
        self._mem: Tuple[int, bytearray] | None = None
        self._mem_exp = 0.0
        self._fail_until = 0.0
        self.last_source = "none"

    def invalidate(self) -> None:
        """Force a network refresh on next use (e.g. after a token 400)."""
        with self._lock:
            self._mem_exp = 0.0
            self._fail_until = 0.0

    def _fetch_network(self) -> dict | None:
        for url in self.urls:
            for attempt in range(self.retries + 1):
                try:
                    r = requests.get(url, timeout=(min(3.0, self.timeout), self.timeout))
                    if r.ok:
                        data = r.json()
                        _parse(data)
                        return data
                    log.warning("TOTP secret fetch %s -> HTTP %s", url.split("/")[2], r.status_code)
                except Exception as e:  # noqa: BLE001
                    log.warning("TOTP secret fetch %s failed (attempt %d): %s", url.split("/")[2], attempt + 1, type(e).__name__)
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 4))
        return None

    def _read_file(self, path: str) -> dict | None:
        try:
            with open(path) as f:
                data = json.load(f)
            _parse(data)
            return data
        except Exception:  # noqa: BLE001
            return None

    def _write_cache(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            log.warning("Could not write TOTP cache: %s", e)

    def get(self) -> Tuple[int, bytearray]:
        with self._lock:
            now = time.time()
            if self._mem and now < self._mem_exp:
                return self._mem
            data = None
            if now >= self._fail_until:
                data = self._fetch_network()
                if data is not None:
                    self._write_cache(data)
                    self.last_source = "network"
                else:
                    self._fail_until = now + _FAIL_BACKOFF
            if data is None:
                data = self._read_file(self.cache_path)
                if data is not None:
                    self.last_source = "disk-cache"
            if data is None:
                data = self._read_file(_SEED_PATH)
                if data is not None:
                    self.last_source = "bundled-seed"
            if data is None:
                from spotapi.client import _FALLBACK_SECRET  # last resort (stale)

                self.last_source = "library-fallback"
                return _FALLBACK_SECRET
            self._mem = _parse(data)
            # keep a non-network secret only briefly so we try the network again soon
            self._mem_exp = now + (_MEM_TTL if self.last_source == "network" else _FAIL_BACKOFF)
            return self._mem


def install(provider: TotpSecretProvider) -> None:
    """Monkeypatch spotapi.client so generate_totp() uses our provider."""
    import spotapi.client as sc

    sc.get_latest_totp_secret = provider.get
