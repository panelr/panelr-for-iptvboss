"""Settings, read once from the environment."""
import os
from dataclasses import dataclass


def _bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    boss_url: str             # where IPTV Boss's XC server answers, e.g. http://iptvboss:8001
    listen_host: str
    listen_port: int
    data_dir: str             # database and guide index live here
    api_key: str              # key a panel sends to manage picks
    enforce_streams: bool     # refuse streams outside a customer's picks
    new_categories: str       # starting rule for categories a customer hasn't decided on: show | hide
    guide_recheck_seconds: int
    request_timeout: int
    forwarded_proto: str      # X-Forwarded-Proto for IPTV Boss when the player's proxy sends none: "" | http | https

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        new = env.get("MW_NEW_CATEGORIES", "show").strip().lower()
        if new not in ("show", "hide"):
            raise SystemExit("MW_NEW_CATEGORIES must be show or hide")
        key = env.get("MW_API_KEY", "").strip()
        if len(key) < 24:
            raise SystemExit("MW_API_KEY must be set to a random value of at least 24 characters")
        value = env.get("MW_ENFORCE_STREAMS")
        enforce = True if value is None else value.strip().lower() in ("1", "true", "yes", "on")
        proto = env.get("MW_FORWARDED_PROTO", "").strip().lower()
        if proto not in ("", "http", "https"):
            raise SystemExit("MW_FORWARDED_PROTO must be http, https or empty")
        return cls(
            boss_url=env.get("MW_BOSS_URL", "http://127.0.0.1:8001").rstrip("/"),
            listen_host=env.get("MW_LISTEN_HOST", "0.0.0.0"),
            listen_port=int(env.get("MW_LISTEN_PORT", "8002")),
            data_dir=env.get("MW_DATA_DIR", "/data"),
            api_key=key,
            enforce_streams=enforce,
            new_categories=new,
            guide_recheck_seconds=int(env.get("MW_GUIDE_RECHECK_SECONDS", "60")),
            request_timeout=int(env.get("MW_REQUEST_TIMEOUT", "300")),
            forwarded_proto=proto,
        )
