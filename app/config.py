"""Application configuration loaded from .env and apps.yaml."""

from pathlib import Path
from functools import lru_cache

import yaml
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    # MySQL (IT Master DB - Read Only)
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "it_master")

    # SQLite (Auth Local DB)
    SQLITE_PATH: str = os.getenv("SQLITE_PATH", str(BASE_DIR / "auth_local.db"))

    # RSA Keys
    PRIVATE_KEY_PATH: str = os.getenv("PRIVATE_KEY_PATH", str(BASE_DIR / "keys" / "private.pem"))
    PUBLIC_KEY_PATH: str = os.getenv("PUBLIC_KEY_PATH", str(BASE_DIR / "keys" / "public.pem"))

    # Teams Webhook
    TEAMS_WEBHOOK_URL: str = os.getenv("TEAMS_WEBHOOK_URL", "")

    # Server
    AUTH_CENTER_BASE_URL: str = os.getenv("AUTH_CENTER_BASE_URL", "http://localhost:8000")

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
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
        with open(apps_file) as f:
            data = yaml.safe_load(f)
        _apps_cache = {app["app_id"]: app for app in data.get("apps", [])}
        _apps_mtime = mtime
    return _apps_cache
