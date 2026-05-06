"""Config storage at ~/.meshapi/config.json."""
import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".meshapi"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"

DEFAULT_CONFIG = {
    "base_url": "https://api.meshapi.ai/v1",
    "api_key": "",
    "model": "anthropic/claude-sonnet-4.5",
    "system": "You are a helpful coding assistant. Be concise.",
    "route": None,
}


def load_config() -> dict:
    CONFIG_DIR.mkdir(exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
    # MESH_API_KEY kept as fallback for one release; prefer MESHAPI_API_KEY.
    cfg["api_key"] = (
        os.getenv("MESHAPI_API_KEY")
        or os.getenv("MESH_API_KEY")
        or cfg.get("api_key", "")
    )
    cfg["base_url"] = os.getenv("MESHAPI_BASE_URL", cfg["base_url"])
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    persisted = {k: v for k, v in cfg.items() if k != "api_key"}
    CONFIG_FILE.write_text(json.dumps(persisted, indent=2))
