import uuid
import time
from spotapi.utils import random_hex_string
from spotapi.exceptions import PlayerError
from spotapi.playlist import PublicPlaylist
from spotapi.types.annotations import enforce
from spotapi.status import PlayerStatus
from spotapi.login import Login
from spotapi.song import Song
from typing import List, Optional

__all__ = ["Player", "PlayerStatus", "PlayerError"]


@enforce
class Player(PlayerStatus):
    """
    A class used to interact with the Spotify player.

    Parameters
    ----------
    login : Login
        The login instance used for authentication.
    device_id : str, optional
        The device ID to connect to for the player.
    """

    __slots__ = (
        "active_id",
        "device_id",
        "r_state",
        "_transfered",
    )

    def __init__(self, login: Login, device_id: str | None = None) -> None:
        super().__init__(login, None)

        _devices = self.device_ids
        _active_id = _devices.active_device_id

        if _active_id is None:
            if not device_id:
                raise ValueError(
                    "Could not get active device ID. Please provide a device ID."
                )
            self.active_id = device_id
        else:
            self.active_id = device_id if device_id else _active_id

        self.r_state = self.state

        # play_origin.device_identifier is only populated by the web player.
        # Desktop/mobile clients leave it None.
        _origin_device_id: str | None = None
        if self.r_state.play_origin is not None:
            _origin_device_id = self.r_state.play_origin.device_identifier

        # _ws_phantom is the random device_id set by WebsocketStreamer.__init__()
        # via register_device(). At this point self.device_id still holds that
        # value because Player.__slots__ device_id hasn't been assigned yet.
        # The "from" device for all commands must be the phantom WS device so
        # Spotify routes responses back through our websocket connection.
        _ws_phantom: str = self.device_id
        self.device_id = _ws_phantom

        # origin = real device that currently owns playback; used only for the
        # initial transfer so the active device stays in control.
        _origin = _origin_device_id if _origin_device_id else self.active_id
        self.transfer_player(_origin, self.active_id)

    def transfer_player(self, from_device_id: str, to_device_id: str) -> None:
        """Transfers the player streamer from one device to another."""
        url = f"https://gue1-spclient.spotify.com/connect-state/v1/connect/transfer/from/{from_device_id}/to/{to_device_id}"
        payload = {
            "transfer_options": {
                "restore_paused": "pause" if self.state.is_paused else "resume"
            },
            "command_id": random_hex_string(32),  # This is random for some reason
        }
        resp = self.client.post(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlayerError("Could not transfer player", error=resp.error.string)

        self._transfered = True

    def _run_command(
        self, from_device_id: str, to_device_id: str, payload: dict
    ) -> None:
        url = f"https://gue1-spclient.spotify.com/connect-state/v1/player/command/from/{from_device_id}/to/{to_device_id}"
        resp = self.client.post(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlayerError("Could not send command", error=resp.error.string)

    def run_command(self, from_device_id: str, to_device_id: str, command: str) -> None:
        """Sends a generic command to the player."""
        payload = {
            "command": {
                "logging_params": {
                    "page_instance_ids": [],
                    "interaction_ids": [],
                    "command_id": random_hex_string(32),
                },
                "endpoint": command,
            }
        }
        self._run_command(from_device_id, to_device_id, payload)

    def _seek_to(
        self, from_device_id: str, to_device_id: str, position_ms: int
    ) -> None:
        payload = {
            "command": {
                "value": position_ms,
                "endpoint": "seek_to",
                "logging_params": {"command_id": random_hex_string(32)},
            }
        }
        self._run_command(from_device_id, to_device_id, payload)

    def _set_shuffle(self, from_device_id: str, to_device_id: str, value: bool) -> None:
        payload = {
            "command": {
                "value": value,
                "logging_params": {
                    "page_instance_ids": [],
                    "interaction_ids": [],
                    "command_id": random_hex_string(32),
                },
                "endpoint": "set_shuffling_context",
            }
        }
        self._run_command(from_device_id, to_device_id, payload)

    def _set_volume(
        self, from_device_id: str, to_device_id: str, volume_percent: float
    ) -> None:
        if volume_percent < 0.0 or volume_percent > 1.0:
            raise ValueError("Volume must be a percent and between 0 and 1.0")

        sixteen_bit_rep = int(volume_percent * 65535)
        url = f"https://gue1-spclient.spotify.com/connect-state/v1/connect/volume/from/{from_device_id}/to/{to_device_id}"
        payload = {"volume": sixteen_bit_rep}
        resp = self.client.put(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlayerError("Could not set volume", error=resp.error.string)

    def _repeat_track(
        self, from_device_id: str, to_device_id: str, value: bool
    ) -> None:
        payload = {
            "command": {
                "repeating_context": value,
                "repeating_track": value,
                "endpoint": "set_options",
                "logging_params": {"command_id": random_hex_string(32)},
            }
        }
        self._run_command(from_device_id, to_device_id, payload)

    def _add_to_queue(self, from_device_id: str, to_device_id: str, track: str) -> None:
        if track.startswith("spotify:track:"):
            track = track.split(":")[-1]

        payload = {
            "command": {
                "track": {
                    "uri": f"spotify:track:{track}",
                    "metadata": {"is_queued": "true"},
                    "provider": "queue",
                },
                "endpoint": "add_to_queue",
                "logging_params": {"command_id": random_hex_string(32)},
            }
        }
        self._run_command(from_device_id, to_device_id, payload)

    def _play_song(
        self,
        from_device_id: str,
        to_device_id: str,
        track: str,
        playlist: str,
        track_uid: str,
    ) -> None:
        if track.startswith("spotify:track:"):
            track = track.split(":")[-1]

        if playlist.startswith("spotify:playlist:"):
            playlist = playlist.split(":")[-1]

        payload = {
            "command": {
                "context": {
                    "uri": f"spotify:playlist:{playlist}",
                    "url": f"context://spotify:playlist:{playlist}",
                    "metadata": {},
                },
                "play_origin": {
                    "feature_identifier": "playlist",
                    "feature_version": "web-player_2024-08-20_1724112418648_eba321c",
                    "referrer_identifier": "home",
                },
                "options": {
                    "license": "tft",
                    "skip_to": {
                        "track_uid": track_uid,
                        "track_index": 1,
                        "track_uri": f"spotify:track:{track_uid}",
                    },
                    "player_options_override": {},
                },
                "logging_params": {
                    "page_instance_ids": [
                        str(uuid.uuid4()),
                    ],
                    "interaction_ids": [
                        str(uuid.uuid4()),
                    ],
                    "command_id": random_hex_string(32),
                },
                "endpoint": "play",
            },
        }
        self._run_command(from_device_id, to_device_id, payload)

    def set_shuffle(self, value: bool, /) -> None:
        """
        Modifies the shuffle state of the player.

        Parameters
        ----------
        value : bool
            The new shuffle state.
        """
        self._set_shuffle(self.device_id, self.active_id, value)

    def seek_to(self, position_ms: int, /) -> None:
        """Seeks to a specific position in the player."""
        self._seek_to(self.device_id, self.active_id, position_ms)

    def restart_song(self) -> None:
        """Restarts the current song."""
        self.seek_to(0)

    def pause(self) -> None:
        """Pauses the player."""
        self.run_command(self.device_id, self.active_id, "pause")

    def resume(self) -> None:
        """Resumes the player."""
        self.run_command(self.device_id, self.active_id, "resume")

    def skip_next(self) -> None:
        """Skips to the next track."""
        self.run_command(self.device_id, self.active_id, "skip_next")

    def skip_prev(self) -> None:
        """Skips to the previous track."""
        self.run_command(self.device_id, self.active_id, "skip_prev")

    def add_to_queue(self, track: str, /) -> None:
        """Adds a track to the player's queue."""
        self._add_to_queue(self.device_id, self.active_id, track)

    def play_track(self, track: str, playlist: str, /) -> None:
        """
        Overrides the player with a new track.

        Parameters
        ----------
        track : str
            The track uri to play.
        playlist : str
            The playlist uri to play the track from.
        """
        if track.startswith("spotify:track:"):
            track = track.split(":")[-1]

        _playlist = PublicPlaylist(playlist).paginate_playlist()

        uids: List[str] = []
        for playlist_chunk in _playlist:
            items = playlist_chunk["items"]
            extended_uids, stop = Song.parse_playlist_items(
                items,
                song_id=track,
                all_instances=True,
            )
            uids.extend(extended_uids)

            if stop:
                _playlist.close()
                break

        self._play_song(self.device_id, self.active_id, track, playlist, uids[0])

    def repeat_track(self, value: bool, /) -> None:
        """
        Repeats the current track.

        Parameters
        ----------
        value : bool
            Whether to repeat the track or disable repeating.
        """
        self._repeat_track(self.device_id, self.active_id, value)

    def set_volume(self, volume_percent: float, /) -> None:
        """
        Sets the volume of the player.

        Parameters
        ----------
        volume_percent : float
            The volume to set the player to. Must be a percent representation.
        """
        self._set_volume(self.device_id, self.active_id, volume_percent)

    def fade_in_volume(
        self,
        volume_percent: float,
        /,
        duration_ms: int = 500,
        *,
        request_time_ms: int | None = None,
    ) -> None:
        """
        Slowly increases or decreases the volume of the player.

        Parameters
        ----------
        volume_percent : float
            The target volume to set the player to. Must be between 0.0 and 1.0.
        duration_ms : int
            The duration of the fade in in milliseconds. Defaults to 500ms.
        request_time_ms : int
            If not None, it will account for the request time in the duration_ms.
        """
        if volume_percent < 0.0 or volume_percent > 1.0:
            raise ValueError("Volume must be between 0.0 and 1.0")

        if request_time_ms is not None:
            effective_duration_ms = duration_ms - request_time_ms
        else:
            effective_duration_ms = duration_ms

        if effective_duration_ms < 0:
            raise ValueError("Effective duration must be positive")

        target_volume = volume_percent * 65535
        current_volume = self.device_ids.devices[self.active_id].volume

        if current_volume == target_volume:
            return

        fade_steps = 100
        step_duration = effective_duration_ms / fade_steps
        volume_step = (target_volume - current_volume) / fade_steps

        for _ in range(fade_steps):
            current_volume += volume_step
            current_volume = max(0, min(current_volume, 65535))
            self._set_volume(self.device_id, self.active_id, (current_volume / 65535))
            time.sleep(step_duration / 1000)

        # Ensure the final volume is set
        self._set_volume(self.device_id, self.active_id, volume_percent)

    def play_search(self, query: str, /, *, index: int = 0) -> str:
        """
        Searches for a track and immediately queues and plays it.

        Parameters
        ----------
        query : str
            The search query (song name, artist, etc.).
        index : int, optional
            Which result from the search to pick. Defaults to 0 (first result).

        Returns
        -------
        str
            The Spotify track URI that was queued and played.

        Raises
        ------
        ValueError
            If no results are found for the given query.
        """
        # Import here to avoid circular imports (Public -> Song -> player chain)
        from spotapi.public import Public

        results = Public.song_search(query)
        first_chunk = next(results, None)
        results.close()

        if not first_chunk:
            raise ValueError(f"No search results found for query: {query!r}")

        items = list(first_chunk)
        if index >= len(items):
            raise ValueError(
                f"Index {index} out of range: only {len(items)} results returned."
            )

        track_uri: str = items[index]["item"]["data"]["uri"]

        self._add_to_queue(self.device_id, self.active_id, track_uri)
        self.run_command(self.device_id, self.active_id, "skip_next")

        return track_uri

    def play_playlist_search(self, query: str, /, *, index: int = 0) -> str:
        """
        Searches for a playlist and immediately plays it.

        Parameters
        ----------
        query : str
            The search query for the playlist.
        index : int, optional
            Which result from the search to pick. Defaults to 0 (first result).

        Returns
        -------
        str
            The Spotify playlist URI that was played.

        Raises
        ------
        ValueError
            If no results are found for the given query, or if the playlist is empty.
        """
        from spotapi.public import Public

        results = Public.playlist_search(query)
        first_chunk = next(results, None)
        results.close()

        if not first_chunk:
            raise ValueError(f"No search results found for query: {query!r}")

        items = list(first_chunk)
        if index >= len(items):
            raise ValueError(
                f"Index {index} out of range: only {len(items)} results returned."
            )

        playlist_uri: str = items[index]["data"]["uri"]
        playlist_id = playlist_uri.split("playlist:")[-1]

        # To play a playlist directly, we need a track inside it to start with
        _playlist = PublicPlaylist(playlist_id).paginate_playlist()
        first_playlist_chunk = next(_playlist, None)
        _playlist.close()

        if not first_playlist_chunk or not first_playlist_chunk["items"]:
            raise ValueError(f"Playlist {playlist_uri} is empty.")

        first_track_uri = first_playlist_chunk["items"][0]["itemV2"]["data"]["uri"]
        first_track_uid = first_playlist_chunk["items"][0]["uid"]
        
        self._play_song(self.device_id, self.active_id, first_track_uri, playlist_uri, first_track_uid)

        return playlist_uri
