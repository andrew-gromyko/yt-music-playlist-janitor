"""OAuth device-flow helpers for YouTube Data API."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional


YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class OAuthError(RuntimeError):
    pass


@dataclass
class TokenData:
    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    token_type: str
    obtained_at: float
    scope: str = ""


class OAuthDeviceClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def _post_form(self, url: str, form: Dict[str, str]) -> Dict:
        data = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None

            if isinstance(payload, dict) and payload.get("error") in {"authorization_pending", "slow_down"}:
                return payload

            raise OAuthError(f"HTTP {e.code} from OAuth endpoint: {body}") from e

    def start_device_flow(self) -> Dict:
        return self._post_form(
            DEVICE_CODE_URL,
            {
                "client_id": self.client_id,
                "scope": YOUTUBE_SCOPE,
            },
        )

    def poll_for_token(self, device_code: str, interval_seconds: int = 5) -> TokenData:
        while True:
            response = self._post_form(
                TOKEN_URL,
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )

            if "access_token" in response:
                return TokenData(
                    access_token=response["access_token"],
                    refresh_token=response.get("refresh_token"),
                    expires_in=int(response.get("expires_in", 0)),
                    token_type=response.get("token_type", "Bearer"),
                    obtained_at=time.time(),
                    scope=response.get("scope", YOUTUBE_SCOPE),
                )

            error = response.get("error")
            if error == "authorization_pending":
                time.sleep(interval_seconds)
                continue
            if error == "slow_down":
                interval_seconds += 5
                time.sleep(interval_seconds)
                continue
            raise OAuthError(f"OAuth device flow failed: {response}")

    def refresh_access_token(self, refresh_token: str) -> TokenData:
        response = self._post_form(
            TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        return TokenData(
            access_token=response["access_token"],
            refresh_token=refresh_token,
            expires_in=int(response.get("expires_in", 0)),
            token_type=response.get("token_type", "Bearer"),
            obtained_at=time.time(),
            scope=response.get("scope", YOUTUBE_SCOPE),
        )
