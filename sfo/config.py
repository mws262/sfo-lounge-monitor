"""Configuration: optional TOML file overlaid with environment variables.

Nothing here is required for the confirmed-working signals (lounge, security,
METAR, FAA, departures -- all keyless). Only Google Routes (drive time) needs a
credential, and it stays inert until you supply it.

Lookup order for each value: env var  >  TOML file  >  built-in default.
"""
from __future__ import annotations

import os
from typing import Any

DEFAULT_PATHS = ("config.toml", "sfo.toml")


def _load_toml(path: str) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


class Config:
    """Thin config accessor. `get('google.routes_api_key')` dotted lookups."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        data: dict = {}
        candidates = [path] if path else list(DEFAULT_PATHS)
        for p in candidates:
            if p and os.path.exists(p):
                data = _load_toml(p)
                break
        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        # Env var wins, e.g. google.routes_api_key -> SFO_GOOGLE_ROUTES_API_KEY
        env_key = "SFO_" + dotted.upper().replace(".", "_")
        if env_key in os.environ:
            return os.environ[env_key]
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    # Convenience -----------------------------------------------------------
    @property
    def google_routes_key(self) -> str | None:
        key = self.get("google.routes_api_key")
        return str(key) if key else None

    @property
    def drive_origin(self) -> str | None:
        origin = self.get("drive.origin")
        return str(origin) if origin else None
