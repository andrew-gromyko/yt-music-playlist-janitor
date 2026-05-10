"""Credential and token storage for yt-music-dedup.

On macOS, values are stored in Keychain. On other systems, the fallback is a
user-only config file under ~/.config/yt-music-dedup.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


SERVICE = "yt-music-playlist-janitor"
CONFIG_DIR = Path(os.getenv("YT_MUSIC_PLAYLIST_JANITOR_CONFIG_DIR", "~/.config/yt-music-playlist-janitor")).expanduser()
CONFIG_FILE = CONFIG_DIR / "credentials.json"


class CredentialError(RuntimeError):
    pass


@dataclass
class ClientCredentials:
    client_id: str
    client_secret: str


def _security_cmd_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("security") is not None


def _keychain_get(account: str) -> Optional[str]:
    if not _security_cmd_available():
        return None
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def _keychain_set(account: str, value: str) -> bool:
    if not _security_cmd_available():
        return False
    proc = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", value],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _keychain_delete(account: str) -> None:
    if not _security_cmd_available():
        return
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
        capture_output=True,
        text=True,
        check=False,
    )


def _read_file_store() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CredentialError(f"Could not read {CONFIG_FILE}: invalid JSON") from e


def _write_file_store(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def get_value(key: str) -> Optional[str]:
    env_name = f"YT_MUSIC_DEDUP_{key.upper()}"
    if os.getenv(env_name):
        return os.getenv(env_name)

    from_keychain = _keychain_get(key)
    if from_keychain:
        return from_keychain

    file_data = _read_file_store()
    value = file_data.get(key)
    return str(value) if value else None


def set_value(key: str, value: str) -> None:
    if _keychain_set(key, value):
        return
    data = _read_file_store()
    data[key] = value
    _write_file_store(data)


def delete_value(key: str) -> None:
    _keychain_delete(key)
    data = _read_file_store()
    if key in data:
        del data[key]
        _write_file_store(data)


def has_client_credentials() -> bool:
    return bool(get_value("client_id") and get_value("client_secret"))


def get_client_credentials() -> Optional[ClientCredentials]:
    client_id = get_value("client_id")
    client_secret = get_value("client_secret")
    if not client_id or not client_secret:
        return None
    return ClientCredentials(client_id=client_id, client_secret=client_secret)


def save_client_credentials(client_id: str, client_secret: str) -> None:
    set_value("client_id", client_id.strip())
    set_value("client_secret", client_secret.strip())
    clear_refresh_token()


def prompt_for_client_credentials() -> ClientCredentials:
    print("First-time setup: enter your Google OAuth client credentials.")
    client_id = input("Client ID: ").strip()
    client_secret = getpass.getpass("Client secret: ").strip()
    if not client_id or not client_secret:
        raise CredentialError("Client ID and client secret are both required.")
    save_client_credentials(client_id, client_secret)
    return ClientCredentials(client_id=client_id, client_secret=client_secret)


def reset_client_credentials() -> ClientCredentials:
    return prompt_for_client_credentials()


def get_refresh_token() -> Optional[str]:
    return get_value("refresh_token")


def save_refresh_token(refresh_token: str) -> None:
    set_value("refresh_token", refresh_token.strip())


def clear_refresh_token() -> None:
    delete_value("refresh_token")
