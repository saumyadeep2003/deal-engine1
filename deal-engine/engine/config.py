"""Config loading. Thesis, sources and models are configuration, not code.

Secrets come from the environment, loaded from a local `.env` if present, so a
launchd/systemd service (which inherits no shell env) picks them up too.
"""
from __future__ import annotations
import os
from functools import lru_cache
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent

# Load .env before anything reads os.environ. Never overrides a real env var.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:  # dotenv optional — env vars still work
    pass
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = ROOT / "output"
DB_PATH = Path(os.environ.get("DEAL_ENGINE_DB", DATA_DIR / "engine.db"))

USER_AGENT = "ThirdbaseDealEngine/0.1 (research; saumya.mimo2003@gmail.com)"


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, "r") as f:
        return yaml.safe_load(f)


def thesis() -> dict:
    return load_yaml("thesis.yaml")


def sources_config() -> dict:
    return load_yaml("sources.yaml")


def models_config() -> dict:
    return load_yaml("models.yaml")


def env_key_present(env_key: str | None) -> bool:
    return bool(env_key and os.environ.get(env_key))


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


LOG_DIR = ROOT / "logs"
# Hosted mode (Render sets RENDER=true and PORT): bind publicly on the platform
# port. Locally: localhost only — fund data on a laptop is not a public site.
IS_HOSTED = bool(env("RENDER") or env("DEAL_ENGINE_HOSTED"))
WEB_HOST = env("DEAL_ENGINE_HOST", "0.0.0.0" if IS_HOSTED else "127.0.0.1")
WEB_PORT = int(env("PORT") or env("DEAL_ENGINE_PORT") or "8787")
