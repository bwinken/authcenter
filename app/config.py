"""Application configuration loaded from .env and apps.yaml."""

import re
from pathlib import Path
from functools import lru_cache
from urllib.parse import quote_plus

import yaml
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    # MSSQL (IT Master DB - Read Only)
    MSSQL_HOST: str = os.getenv("MSSQL_HOST", "localhost")
    MSSQL_PORT: int = int(os.getenv("MSSQL_PORT", "1433"))
    MSSQL_USER: str = os.getenv("MSSQL_USER", "sa")
    MSSQL_PASSWORD: str = os.getenv("MSSQL_PASSWORD", "")
    MSSQL_DATABASE: str = os.getenv("MSSQL_DATABASE", "it_master")
    MSSQL_DRIVER: str = os.getenv("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")
    _raw_mssql_table: str = os.getenv("MSSQL_TABLE", "staff")

    @property
    def MSSQL_TABLE(self) -> str:
        table = self._raw_mssql_table
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", table):
            raise ValueError(f"MSSQL_TABLE contains invalid characters: {table!r}")
        return table

    # SQLite (Auth Local DB)
    SQLITE_PATH: str = os.getenv("SQLITE_PATH", str(BASE_DIR / "auth_local.db"))

    # RSA Keys
    PRIVATE_KEY_PATH: str = os.getenv("PRIVATE_KEY_PATH", str(BASE_DIR / "keys" / "private.pem"))
    PUBLIC_KEY_PATH: str = os.getenv("PUBLIC_KEY_PATH", str(BASE_DIR / "keys" / "public.pem"))

    # Teams Webhook
    TEAMS_WEBHOOK_URL: str = os.getenv("TEAMS_WEBHOOK_URL", "")

    # HTTP Proxy (for outbound requests like Teams Webhook)
    HTTP_PROXY: str = os.getenv("HTTP_PROXY", "")

    # Server
    AUTH_CENTER_BASE_URL: str = os.getenv("AUTH_CENTER_BASE_URL", "http://localhost:8000")

    # Super Admin
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
    SUPER_ADMIN_EMPLOYEES: list[str] = [
        name.strip().lower()
        for name in os.getenv("SUPER_ADMIN_EMPLOYEES", "").split(",")
        if name.strip()
    ]

    @property
    def mssql_url(self) -> str:
        driver = self.MSSQL_DRIVER.replace(" ", "+")
        password = quote_plus(self.MSSQL_PASSWORD)
        return (
            f"mssql+aioodbc://{self.MSSQL_USER}:{password}"
            f"@{self.MSSQL_HOST}:{self.MSSQL_PORT}/{self.MSSQL_DATABASE}"
            f"?driver={driver}"
        )

    @property
    def sqlite_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.SQLITE_PATH}"

    @property
    def private_key(self) -> str:
        return Path(self.PRIVATE_KEY_PATH).read_text()

    @property
    def public_key(self) -> str:
        return Path(self.PUBLIC_KEY_PATH).read_text()


@lru_cache
def get_settings() -> Settings:
    return Settings()


_apps_cache: dict[str, dict] = {}
_apps_mtime: float = 0.0


def load_registered_apps() -> dict[str, dict]:
    """Load registered apps from config/apps.yaml with file mtime caching.

    Re-reads the file only when its modification time changes.
    """
    global _apps_cache, _apps_mtime
    apps_file = BASE_DIR / "config" / "apps.yaml"
    mtime = apps_file.stat().st_mtime
    if mtime != _apps_mtime:
        with open(apps_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        _apps_cache = {app["app_id"]: app for app in data.get("apps", [])}
        _apps_mtime = mtime
    return _apps_cache


def save_registered_apps(apps_dict: dict[str, dict]) -> None:
    """Write apps dict back to config/apps.yaml and update cache."""
    global _apps_cache, _apps_mtime
    apps_file = BASE_DIR / "config" / "apps.yaml"
    apps_list = []
    for app_id, info in apps_dict.items():
        entry = {"app_id": app_id}
        for key in ("client_secret", "redirect_uri", "name", "allowed_orgs", "default_level", "token_expire_hours", "app_url"):
            if key in info:
                entry[key] = info[key]
        apps_list.append(entry)
    with open(apps_file, "w", encoding="utf-8") as f:
        yaml.dump({"apps": apps_list}, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    _apps_cache = apps_dict
    _apps_mtime = apps_file.stat().st_mtime
