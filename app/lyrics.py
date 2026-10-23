"""Lirik lagu: ambil dari endpoint color-lyrics internal Spotify (sumber sama dgn app Spotify,
aslinya Musixmatch), lalu fallback ke lrclib.net (gratis, tanpa key) bila Spotify tak punya.

Kontrak keluaran (dipakai /lyrics di main.py):
    {"track_id": "<22>", "synced": bool, "source": "spotify"|"lrclib", "lines": [{"start_ms": int, "text": str}, ...]}
Baris diurutkan menaik berdasarkan start_ms. Bila tak sinkron, start_ms = 0 untuk semua baris.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("spotapi-backend.lyrics")

_LRC_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def color_lyrics_lines(data: dict) -> tuple[list[dict], bool]:
    """Parse balasan JSON color-lyrics Spotify → (lines, synced)."""
    lyr = (data or {}).get("lyrics") or {}
    sync_type = str(lyr.get("syncType") or "").upper()
    synced = sync_type == "LINE_SYNCED"
    out: list[dict] = []
    for ln in lyr.get("lines") or []:
        text = (ln.get("words") or "").strip()
        start = _int(ln.get("startTimeMs"), 0) if synced else 0
        out.append({"start_ms": start, "text": text})
    # Buang baris kosong beruntun di ujung; pertahankan placeholder "♪" dari Spotify.
    while out and not out[-1]["text"]:
        out.pop()
    if not any(l["text"] for l in out):
        return [], False
    return out, synced


def parse_lrc(text: str) -> list[dict]:
    """Parse lirik LRC '[mm:ss.xx] teks' → list baris ter-sinkron (bisa banyak timestamp per baris)."""
    lines: list[dict] = []
    for raw in (text or "").splitlines():
        stamps = list(_LRC_RE.finditer(raw))
        if not stamps:
            continue
        body = raw[stamps[-1].end():].strip()
        for m in stamps:
            mm, ss, frac = m.group(1), m.group(2), m.group(3) or "0"
            ms = (_int(mm) * 60 + _int(ss)) * 1000 + _int(frac.ljust(3, "0")[:3])
            lines.append({"start_ms": ms, "text": body})
    lines.sort(key=lambda x: x["start_ms"])
    if not any(l["text"] for l in lines):
        return []
    return lines


def fetch_lrclib(meta: dict, timeout: float = 8.0) -> tuple[list[dict], bool] | None:
    """Fallback lrclib.net. [meta] butuh title + artists; album & duration_ms opsional."""
    import requests

    title = (meta.get("title") or "").strip()
    artists = meta.get("artists") or []
    if not title or not artists:
        return None
    params = {"track_name": title, "artist_name": artists[0]}
    if meta.get("album"):
        params["album_name"] = meta["album"]
    if meta.get("duration_ms"):
        params["duration"] = int(round(meta["duration_ms"] / 1000))
    headers = {"User-Agent": "spotapi-backend (https://github.com/ejatapibeda/spotapi-backend)"}
    try:
        r = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=timeout)
        if r.status_code == 404:
            # coba /api/search bila /get gagal cocok persis
            rs = requests.get("https://lrclib.net/api/search", params={"q": f"{title} {artists[0]}"},
                              headers=headers, timeout=timeout)
            if rs.status_code != 200:
                return None
            arr = rs.json() or []
            d = arr[0] if arr else None
        elif r.status_code == 200:
            d = r.json()
        else:
            return None
    except Exception as e:  # noqa: BLE001
        log.info("lrclib fetch gagal: %s", type(e).__name__)
        return None
    if not d:
        return None
    synced_lrc = d.get("syncedLyrics")
    plain = d.get("plainLyrics")
    if synced_lrc:
        parsed = parse_lrc(synced_lrc)
        if parsed:
            return parsed, True
    if plain:
        return [{"start_ms": 0, "text": l.strip()} for l in plain.splitlines()], False
    return None
