"""Configuration management.

Loads from config/default.yaml and an optional project-local .env, with values
ultimately read from the process environment. Secrets are never stored in the
checked-in YAML configuration.
"""

import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class Config:
    """Kassandra configuration loaded from YAML + .env + environment overrides."""

    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or Path(__file__).parent.parent.parent / "config" / "default.yaml"
        self._data: dict[str, Any] = {}
        self._load()
        self._load_env()

    def _load(self) -> None:
        if self._config_path.exists():
            with open(self._config_path) as f:
                self._data = yaml.safe_load(f) or {}

    def _load_env(self) -> None:
        # Load only the project-local file; operator-wide credential locations
        # are deliberately outside the public application's configuration path.
        project_env = Path(self._data.get("paths", {}).get("project_root", ".")) / ".env"
        if project_env.exists():
            load_dotenv(project_env, override=True)

    @property
    def db_path(self) -> Path:
        """Main SQLite state database path."""
        project_root = Path(self._data.get("paths", {}).get("project_root", "."))
        return (project_root / "data" / "state.db").resolve()

    @property
    def evidence_dir(self) -> Path:
        """Content-addressed evidence storage directory."""
        project_root = Path(self._data.get("paths", {}).get("project_root", "."))
        return (project_root / "data" / "evidence").resolve()

    @property
    def portfolio_dir(self) -> Path:
        """Portfolio data directory."""
        project_root = Path(self._data.get("paths", {}).get("project_root", "."))
        return (project_root / "data" / "portfolio").resolve()

    @property
    def companies_house_api_key(self) -> str:
        """Companies House API key — from env ONLY, never from config."""
        return os.environ.get("COMPANIES_HOUSE_API_KEY", "")

    @property
    def api_requests_per_second(self) -> float:
        return float(self._data.get("rates", {}).get("companies_house", {}).get("requests_per_second", 1.0))

    @property
    def api_max_per_day(self) -> int:
        return int(self._data.get("rates", {}).get("companies_house", {}).get("max_per_day", 500))

    @property
    def content_hash_algorithm(self) -> str:
        return self._data.get("evidence", {}).get("hash_algorithm", "sha256")

    def get(self, *keys: str, default: Any = None) -> Any:
        """Deep access into config dict."""
        node = self._data
        for key in keys:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                return default
            if node is None:
                return default
        return node


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
