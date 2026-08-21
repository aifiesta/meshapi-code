"""Config storage at ~/.meshapi/config.json."""
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

CONFIG_DIR = Path.home() / ".meshapi"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"
# The API key lives in its own 0600 file (like ~/.aws/credentials), NOT in
# config.json — save_config() strips api_key on every write, so a key stored
# in config.json would silently vanish on the next /model or /route change.
CREDENTIALS_FILE = CONFIG_DIR / "credentials"
# Backgrounded server pids/ports, persisted so a crashed meshapi can offer
# to clean them up on next launch (a hard kill skips atexit/SIGTERM).
SERVERS_FILE = CONFIG_DIR / "servers.json"
# Update-check cache: last known PyPI version + timestamp + which version
# the user declined (so we don't re-nag about the same release every run).
UPDATE_CHECK_FILE = CONFIG_DIR / "update_check.json"
# Tool-call failure forensics: raw arguments of every doomed/repaired call,
# so corruption can be attributed (model-side vs gateway SSE relay).
TOOLCALL_FAILURES_FILE = CONFIG_DIR / "toolcall_failures.jsonl"
# Full pre-compaction history, so compaction is recoverable instead of
# destructive: the summary left in context names this path and the model can
# read back exact code/errors it no longer has. (Claude Code does the same —
# its compact summary points at the session transcript.)
TRANSCRIPTS_DIR = CONFIG_DIR / "transcripts"
FAILURE_LOG_MAX_BYTES = 1_000_000
_RAW_ARGS_LOG_CAP = 32_768  # bound one pathological record

DEFAULT_CONFIG = {
    "base_url": "https://api.meshapi.ai/v1",
    "api_key": "",
    "model": "anthropic/claude-sonnet-4.5",
    "system": "You are a helpful coding assistant. Be concise.",
    "auto_route": False,        # model:"auto" — gateway Auto Router picks per prompt
    "repo_memory": True,        # warm-start repo map + remember notes (/memory off)
    "fallback_models": [],      # ordered `models` fallback list sent in the payload
    "reasoning_effort": None,   # high|medium|low|none, or None = not sent
    # Mesh Optimize dial (BETA). 0 = off. 0 to 0.95: how aggressively to
    # cut token spend. See /optimize in the REPL and README for details.
    "optimize": 0.0,
    # Agentic loop controls. max_hops 0 = unlimited (stall detection still
    # stops runaway loops); auto_compact keeps long turns under the model's
    # context limit; stall_policy "pause" ends the turn at the prompt after
    # repeated identical actions ignore nudges, "keep-going" nudges forever
    # (unattended runs).
    "max_hops": 0,
    "auto_compact": True,
    "stall_policy": "pause",
    # Smart routing (local table-driven picker). route_mode: "off" (pin),
    # "auto" (gateway picks — mirrors auto_route), "smart" (local pick).
    # Weights steer smart picks along each cohort's efficiency frontier.
    "route_mode": "off",
    "route_weights": {"cost": 0.5, "cap": 0.3, "speed": 0.2},
    # Effort: "auto" detects prompt difficulty (low/mid/high) and tilts the
    # weights; low|medium|high|xhigh|max forces the tilt for every prompt.
    "route_effort": "auto",
    # How the assistant writes (prose only — never tool access or scope).
    "output_style": "default",
    # Models observed rejecting reasoning_effort (with tools) — skipped
    # without a doomed retry in future sessions. Evidence-based cache.
    "reasoning_rejected_models": [],
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
    if not isinstance(url, str):
        url = str(url or "")  # a hand-edited non-string base_url must not crash
    u = url.strip().rstrip("/")
    if u.startswith("https://"):
        return u
    # http:// is allowed ONLY for a genuinely-local host. Bound the host match
    # so http://localhost.evil.com / http://127.0.0.1.attacker can't smuggle the
    # bearer key to an external host in cleartext.
    # http:// allowed ONLY for a genuinely-local host. Use the URL parser's
    # host extraction — the same host the HTTP client actually connects to —
    # so http://localhost.evil.com and http://127.0.0.1.evil.com resolve to
    # their real external hostnames and are rejected, not smuggled as "local"
    # (a prefix check let them through; a hand-split broke IPv6 [::1]).
    if u.startswith("http://"):
        try:
            host = (urlparse(u).hostname or "").lower()
        except ValueError:
            host = ""
        if host in ("localhost", "127.0.0.1", "::1"):
            return u
    print(
        f"meshapi: refusing to use base_url {url!r} — must be https:// "
        "(or http://localhost for local dev). The Authorization header "
        "carries your API key in cleartext otherwise.",
        file=sys.stderr,
    )
    sys.exit(2)


def load_api_key() -> str:
    """Read the persisted API key (single line, 0600). '' if absent."""
    try:
        return CREDENTIALS_FILE.read_text().strip()
    except OSError:
        return ""


def save_api_key(key: str) -> None:
    """Persist the API key to its own file, created 0600 from the start
    (os.open with mode, not write-then-chmod, so there's no readable window).
    """
    _secure_dir(CONFIG_DIR)
    fd = os.open(CREDENTIALS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.strip() + "\n")
    secure_file(CREDENTIALS_FILE)  # tighten a pre-existing looser file


_OLD_ROUTE_WEIGHTS = {"cost": 0.5, "cap": 0.3, "speed": 0.2}


def load_config() -> dict:
    _secure_dir(CONFIG_DIR)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    secure_file(CONFIG_FILE)
    # A corrupt/non-object config.json must NEVER brick launch (a truncated
    # write, a bad hand-edit). Every other loader here already degrades to a
    # default — load_config used to be the exception and would crash the CLI.
    try:
        loaded = json.loads(CONFIG_FILE.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("config.json is not a JSON object")
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(
            f"meshapi: ~/.meshapi/config.json is unreadable ({e}); using "
            "defaults. Fix or delete it to silence this.",
            file=sys.stderr,
        )
        loaded = {}
    cfg = {**DEFAULT_CONFIG, **loaded}
    # 0.5.9-dev migration: two short-lived route_weights shapes (cap="auto",
    # difficulty sensitivity key) collapsed back to numeric cost/cap/speed +
    # route_effort. Reset any non-numeric shape to the default.
    rw = cfg.get("route_weights") or {}
    if rw.get("cap") == "auto" or "difficulty" in rw:
        cfg["route_weights"] = dict(DEFAULT_CONFIG["route_weights"])
    # A hand-edited or stale output_style must degrade to default rather
    # than silently injecting nothing while the UI claims a style is on.
    from . import styles as _styles
    cfg["output_style"] = (_styles.normalize(cfg.get("output_style"))
                           or _styles.DEFAULT)
    # `route` (cheapest/fastest/balanced) never existed gateway-side and was
    # replaced by auto_route in 0.5.0 — drop the stale key from old configs
    # (it disappears from disk on the next save_config).
    cfg.pop("route", None)
    # Resolution order: env > credentials file > legacy hand-edited
    # config.json. MESH_API_KEY kept as fallback for one release.
    file_key = (cfg.get("api_key") or "").strip()
    cfg["api_key"] = (
        os.getenv("MESHAPI_API_KEY")
        or os.getenv("MESH_API_KEY")
        or load_api_key()
        or file_key
    )
    # Migrate a hand-edited config.json key to the credentials file so it
    # survives the api_key strip in save_config().
    if file_key and not CREDENTIALS_FILE.exists():
        try:
            save_api_key(file_key)
        except OSError:
            pass
    cfg["base_url"] = _validate_base_url(
        os.getenv("MESHAPI_BASE_URL", cfg["base_url"])
    )
    return cfg


def save_config(cfg: dict) -> None:
    _secure_dir(CONFIG_DIR)
    persisted = {k: v for k, v in cfg.items() if k != "api_key"}
    # Atomic write (temp + os.replace) like save_servers/save_update_check — a
    # bare write_text truncates first, so an interrupt mid-save leaves a
    # corrupt config.json that (before the load guard above) bricked launch.
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(persisted, indent=2))
    secure_file(tmp)  # 0600 before it becomes config.json — no readable window
    os.replace(tmp, CONFIG_FILE)


def transcript_path(session_id: str) -> "Path":
    """Path for this session's full transcript (created lazily, 0600)."""
    _secure_dir(CONFIG_DIR)
    _secure_dir(TRANSCRIPTS_DIR)
    safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")[:64]
    return TRANSCRIPTS_DIR / f"{safe or 'session'}.jsonl"


def append_transcript(session_id: str, messages: list) -> "str | None":
    """Append messages to the session transcript. Returns the path, or None.

    Best-effort and never raises: losing a transcript line must never break
    a turn. Written 0600 — it holds full file contents and shell output.
    """
    try:
        path = transcript_path(session_id)
        first = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            for m in messages:
                fh.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")
        if first:
            secure_file(path)
        return str(path)
    except Exception:
        return None


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


def save_update_check(data: dict) -> None:
    """Persist the update-check cache (`latest`, `checked_at`,
    `declined_version`). Atomic + 0600 + best-effort, like save_servers."""
    try:
        _secure_dir(CONFIG_DIR)
        persisted = {
            k: data[k]
            for k in ("latest", "checked_at", "declined_version")
            if k in data
        }
        tmp = UPDATE_CHECK_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(persisted, indent=2))
        os.replace(tmp, UPDATE_CHECK_FILE)
        secure_file(UPDATE_CHECK_FILE)
    except (OSError, TypeError):
        pass


def log_toolcall_failure(record: dict) -> None:
    """Append one JSONL forensics record. Best-effort — never raises.

    0600 from creation (os.open, no readable window). Rotation: when the
    file exceeds FAILURE_LOG_MAX_BYTES it is renamed to `.jsonl.1`
    (clobbering the previous rotation) and a fresh file starts — one
    atomic syscall, keeps a full window of history, no parsing.
    """
    try:
        _secure_dir(CONFIG_DIR)
        try:
            if TOOLCALL_FAILURES_FILE.stat().st_size > FAILURE_LOG_MAX_BYTES:
                os.replace(
                    TOOLCALL_FAILURES_FILE,
                    TOOLCALL_FAILURES_FILE.with_suffix(".jsonl.1"),
                )
        except OSError:
            pass  # missing file, racing process — carry on
        raw = record.get("raw_args")
        if isinstance(raw, str) and len(raw) > _RAW_ARGS_LOG_CAP:
            record = {
                **record,
                "raw_args": raw[:_RAW_ARGS_LOG_CAP] + f"…[+{len(raw) - _RAW_ARGS_LOG_CAP} chars]",
            }
        fd = os.open(
            TOOLCALL_FAILURES_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass  # forensics must never hurt the session


def load_update_check() -> dict:
    """Read the update-check cache. Returns {} on any failure."""
    if not UPDATE_CHECK_FILE.exists():
        return {}
    try:
        data = json.loads(UPDATE_CHECK_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
