"""
session.py — Simple session manager for SpotAPI.

Usage
-----
    from spotapi.session import SpotifySession

    # First time: save your cookies (get both from browser DevTools)
    SpotifySession.setup("your_sp_dc_here", "your_sp_key_here")

    # Every time after: load the saved session
    login = SpotifySession.load()

How to get sp_dc and sp_key
----------------------------
    1. Open https://open.spotify.com in your browser and log in
    2. Press F12 → Application tab → Cookies → https://open.spotify.com
    3. Copy the value of ``sp_dc``
    4. Copy the value of ``sp_key``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from spotapi.login import Login
from spotapi.types.data import Config
from spotapi.utils.logger import NoopLogger

__all__ = ["SpotifySession"]

# Default path: ~/.config/spotapi/session.json
_DEFAULT_SESSION_DIR = Path.home() / ".config" / "spotapi"
_DEFAULT_SESSION_FILE = _DEFAULT_SESSION_DIR / "session.json"


class SpotifySession:
    """
    Manages storing and loading a Spotify session from ``sp_dc`` + ``sp_key`` cookies.

    How to get the cookies
    ----------------------
    1. Open https://open.spotify.com in your browser and log in.
    2. Press **F12** → **Application** tab → **Cookies** → ``https://open.spotify.com``
    3. Copy the value of ``sp_dc``
    4. Copy the value of ``sp_key``

    Example
    -------
    First time setup (only once per account)::

        SpotifySession.setup("AQCqbfRJ...", "07c956c0...", identifier="my_account")

    Every time after::

        login = SpotifySession.load("my_account")
        player = Player(login)
    """

    @staticmethod
    def _session_file(path: Path | str | None = None) -> Path:
        return Path(path) if path else _DEFAULT_SESSION_FILE

    @staticmethod
    def setup(
        sp_dc: str,
        sp_key: str,
        identifier: str = "default",
        *,
        path: Path | str | None = None,
    ) -> Login:
        """
        Save a Spotify session from ``sp_dc`` and ``sp_key`` cookies.

        Parameters
        ----------
        sp_dc : str
            The ``sp_dc`` cookie value copied from your browser.
        sp_key : str
            The ``sp_key`` cookie value copied from your browser.
        identifier : str, optional
            A label for this session (e.g. your email). Defaults to ``"default"``.
        path : Path or str, optional
            Custom path to save the session JSON.
            Defaults to ``~/.config/spotapi/session.json``.

        Returns
        -------
        Login
            A ready-to-use ``Login`` instance.
        """
        sp_dc = sp_dc.strip()
        sp_key = sp_key.strip()

        session_data: list[dict[str, Any]] = [
            {
                "identifier": identifier,
                "password": "",
                "cookies": {
                    "sp_dc": sp_dc,
                    "sp_key": sp_key,
                },
            }
        ]

        session_file = SpotifySession._session_file(path)
        session_file.parent.mkdir(parents=True, exist_ok=True)

        # Merge with existing sessions (replace if same identifier)
        existing: list[dict[str, Any]] = []
        if session_file.exists():
            try:
                with open(session_file, "r") as f:
                    existing = json.load(f)
                # Remove old entry for same identifier
                existing = [s for s in existing if s.get("identifier") != identifier]
            except (json.JSONDecodeError, IOError):
                existing = []

        existing.extend(session_data)
        with open(session_file, "w") as f:
            json.dump(existing, f, indent=4)

        print(f"Session '{identifier}' saved to: {session_file}")

        return SpotifySession.load(identifier, path=path)

    @staticmethod
    def load(
        identifier: str = "default",
        *,
        path: Path | str | None = None,
        cfg: Config | None = None,
    ) -> Login:
        """
        Load a saved Spotify session.

        Parameters
        ----------
        identifier : str, optional
            The session label used when calling ``setup()``. Defaults to ``"default"``.
        path : Path or str, optional
            Custom path to the session JSON file.
            Defaults to ``~/.config/spotapi/session.json``.
        cfg : Config, optional
            Custom SpotAPI config. Defaults to a silent (NoopLogger) config.

        Returns
        -------
        Login
            A ready-to-use ``Login`` instance.

        Raises
        ------
        FileNotFoundError
            If no session file exists — run ``SpotifySession.setup()`` first.
        KeyError
            If the identifier is not found in the session file.
        """
        session_file = SpotifySession._session_file(path)

        if not session_file.exists():
            raise FileNotFoundError(
                f"No session file found at {session_file}. "
                "Run SpotifySession.setup('your_sp_dc') first."
            )

        with open(session_file, "r") as f:
            sessions: list[Mapping[str, Any]] = json.load(f)

        match = next(
            (s for s in sessions if s.get("identifier") == identifier), None
        )

        if match is None:
            available = [s.get("identifier") for s in sessions]
            raise KeyError(
                f"Session '{identifier}' not found. "
                f"Available sessions: {available}"
            )

        if cfg is None:
            cfg = Config(logger=NoopLogger())

        return Login.from_cookies(match, cfg)

    @staticmethod
    def list_sessions(*, path: Path | str | None = None) -> list[str]:
        """
        List all saved session identifiers.

        Returns
        -------
        list[str]
            List of identifier strings from the session file.
        """
        session_file = SpotifySession._session_file(path)

        if not session_file.exists():
            return []

        with open(session_file, "r") as f:
            sessions: list[Mapping[str, Any]] = json.load(f)

        return [s.get("identifier", "?") for s in sessions]

    @staticmethod
    def remove(identifier: str, *, path: Path | str | None = None) -> None:
        """
        Remove a saved session by identifier.

        Parameters
        ----------
        identifier : str
            The session label to remove.
        """
        session_file = SpotifySession._session_file(path)

        if not session_file.exists():
            raise FileNotFoundError(f"No session file found at {session_file}.")

        with open(session_file, "r") as f:
            sessions: list[dict[str, Any]] = json.load(f)

        new_sessions = [s for s in sessions if s.get("identifier") != identifier]

        if len(new_sessions) == len(sessions):
            raise KeyError(f"Session '{identifier}' not found.")

        with open(session_file, "w") as f:
            json.dump(new_sessions, f, indent=4)

        print(f"Session '{identifier}' removed from {session_file}.")
