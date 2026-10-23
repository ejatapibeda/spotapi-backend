from __future__ import annotations

from typing import Any
from collections.abc import Mapping
from spotapi.types.annotations import enforce
from spotapi import utils
from spotapi.exceptions import UserError
from spotapi.login import Login, RECAPTCHA_SITE_KEY

__all__ = ["User", "UserError"]


@enforce
class User:
    """
    Represents a Spotify user.

    Parameters
    ----------
    login : Login
        The login object.
    """

    __slots__ = ("login", "_user_plan", "_user_info", "csrf_token")

    def __init__(self, login: Login) -> None:
        if not login.logged_in:
            raise ValueError("Must be logged in")

        self.login = login
        self._user_plan: Mapping[str, Any] | None = None
        self._user_info: Mapping[str, Any] | None = None

    @property
    def has_premium(self) -> bool:
        if self._user_plan is None:
            self._user_plan = self.get_plan_info()

        return self._user_plan["plan"]["name"] != "Spotify Free"

    @property
    def username(self) -> str:
        if self._user_info is None:
            self._user_info = self.get_user_info()

        return self._user_info["profile"]["username"]

    def get_plan_info(self) -> Mapping[str, Any]:
        """Gets user plan info via Pathfinder accountAttributes."""
        url = "https://api-partner.spotify.com/pathfinder/v1/query"
        # BaseClient is used for persisted-query hashes / auth headers
        from spotapi.client import BaseClient
        import json

        base = BaseClient(self.login.client)
        params = {
            "operationName": "accountAttributes",
            "variables": "{}",
            "extensions": json.dumps(
                {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": base.part_hash("accountAttributes"),
                    }
                }
            ),
        }
        resp = self.login.client.get(url, params=params, authenticate=True)

        if resp.fail:
            raise UserError("Could not get user plan info", error=resp.error.string)

        if not isinstance(resp.response, Mapping):
            raise UserError("Invalid JSON")

        account = (
            resp.response.get("data", {})
            .get("me", {})
            .get("account", {})
        )
        product = str(account.get("product") or "FREE").upper()
        attributes = account.get("attributes") or {}
        catalogue = attributes.get("catalogue") or product.lower()

        # Keep legacy shape expected by has_premium / callers
        plan_name = "Spotify Free" if product == "FREE" else f"Spotify {product.title()}"
        return {
            "plan": {
                "name": plan_name,
                "product": product,
                "catalogue": catalogue,
            },
            "account": account,
            "raw": resp.response,
        }

    def verify_login(self) -> bool:
        try:
            self.get_user_info()
        except Exception as e:
            if "401" in str(e):
                return False
            # Fall back to plan probe for sessions that can auth partner API only
            try:
                self.get_plan_info()
            except Exception as e2:
                if "401" in str(e2):
                    return False
                raise e
        return True

    def get_user_info(self) -> Mapping[str, Any]:
        """Gets accounts user info."""
        url = "https://www.spotify.com/api/account-settings/v1/profile"
        resp = self.login.client.get(url)

        if resp.fail:
            raise UserError("Could not get user info", error=resp.error.string)

        if not isinstance(resp.response, Mapping):
            raise UserError("Invalid JSON")

        self.csrf_token = resp.raw.headers.get("X-Csrf-Token")
        return resp.response

    def edit_user_info(self, dump: Mapping[str, Any]) -> None:
        """
        Edits account user account info.
        For this function to work, dump must be the entire profile dump.
        You can get this dump from get_user_info, then change the fields you want.
        """
        if self.login.solver is None:
            raise UserError("Captcha solver not set")

        captcha_response = self.login.solver.solve_captcha(
            "https://www.spotify.com",
            RECAPTCHA_SITE_KEY,
            "account_settings/profile_update",
            "v3",
        )

        if not captcha_response:
            raise UserError("Could not solve captcha")

        profile_dump = dump["profile"]
        dump = {
            "profile": {
                "email": profile_dump["email"],
                "gender": profile_dump["gender"],
                "birthdate": profile_dump["birthdate"],
                "country": profile_dump["country"],
            },
            "recaptcha_token": captcha_response,
            "client_nonce": utils.random_nonce(),
            "callback_url": "https://www.spotify.com/account/profile/challenge",
            "client_info": {"locale": "en_US", "capabilities": [1]},
        }

        url = "https://www.spotify.com/api/account-settings/v2/profile"

        headers = {
            "Content-Type": "application/json",
            "X-Csrf-Token": self.csrf_token,
        }

        resp = self.login.client.put(url, json=dump, headers=headers)

        if resp.fail:
            raise UserError("Could not edit  user info", error=resp.error.string)
