"""Config storage at ~/.meshapi/config.json."""
import json
import os
import stat
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".meshapi"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"
# Backgrounded server pids/ports, persisted so a crashed meshapi can offer
# to clean them up on next launch (a hard kill skips atexit/SIGTERM).
SERVERS_FILE = CONFIG_DIR / "servers.json"

DEFAULT_CONFIG = {
    "base_url": "https://api.meshapi.ai/v1",
    "api_key": "",
    "model": "anthropic/claude-sonnet-4.5",
    "system": "You are a helpful coding assistant. Be concise.",
    "route": None,
    # Mesh Optimize dial (BETA). 0 = off. 0 to 0.95: how aggressively to
    # cut token spend. See /optimize in the REPL and README for details.
    "optimize": 0.0,
}

_DIR_MODE = stat.S_IRWXU                       # 0700
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR       # 0600


def _secure_dir(path: Path) -> None:
    path.mkdir(exist_ok=True)
    try:
        path.chmod(_DIR_MODE)
    except OSError:
        pass  # best-effort on non-POSIX or weird filesystems


def secure_file(path: Path) -> None:
    """Tighten an existing file's permissions to 0600. Public so cli.py
    can apply it to the prompt_toolkit history file."""
    try:
        if path.exists():
            path.chmod(_FILE_MODE)
    except OSError:
        pass


def _validate_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.startswith("https://"):
        return u
    if u.startswith(("http://localhost", "http://127.0.0.1")):
        return u  # local dev/proxy is the only http:// allowed
    print(
        f"meshapi: refusing to use base_url {url!r} — must be https:// "
        "(or http://localhost for local dev). The Authorization header "
        "carries your API key in cleartext otherwise.",
        file=sys.stderr,
    )
    sys.exit(2)


def load_config() -> dict:
    _secure_dir(CONFIG_DIR)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    secure_file(CONFIG_FILE)
    cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
    # MESH_API_KEY kept as fallback for one release; prefer MESHAPI_API_KEY.
    cfg["api_key"] = (
        os.getenv("MESHAPI_API_KEY")
        or os.getenv("MESH_API_KEY")
        or cfg.get("api_key", "")
    )
    cfg["base_url"] = _validate_base_url(
        os.getenv("MESHAPI_BASE_URL", cfg["base_url"])
    )
    return cfg


def save_config(cfg: dict) -> None:
    _secure_dir(CONFIG_DIR)
    persisted = {k: v for k, v in cfg.items() if k != "api_key"}
    CONFIG_FILE.write_text(json.dumps(persisted, indent=2))
    secure_file(CONFIG_FILE)


def save_servers(servers: list) -> None:
    """Persist a list of `{pid, port, cmd, url}` dicts for crash recovery.

    Written atomically (temp + rename) at 0600 alongside the config. Best-
    effort — failures are swallowed so a broken servers.json never blocks
    starting a fresh REPL.
    """
    try:
        _secure_dir(CONFIG_DIR)
        serializable = [
            {
                "pid": s.get("pid"),
                "port": s.get("port"),
                "cmd": s.get("cmd"),
                "url": s.get("url"),
            }
            for s in (servers or [])
            if isinstance(s, dict)
        ]
        tmp = SERVERS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(serializable, indent=2))
        os.replace(tmp, SERVERS_FILE)
        secure_file(SERVERS_FILE)
    except OSError:
        pass


def load_servers() -> list:
    """Read persisted server records. Returns [] on any failure."""
    if not SERVERS_FILE.exists():
        return []
    try:
        data = json.loads(SERVERS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def clear_servers_file() -> None:
    """Drop the persisted servers file. Best-effort."""
    try:
        if SERVERS_FILE.exists():
            SERVERS_FILE.unlink()
    except OSError:
        pass
