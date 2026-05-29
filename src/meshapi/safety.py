"""Security guardrails for tool execution.

Three checks the rest of the CLI calls into:

    is_path_safe_for_auto_write(path, mode) -> (allowed, reason)
        Should write_file be auto-approved at this path in this mode?
        Cwd-scope applies to ACCEPT_EDITS/AUTO; the sensitive-path denylist
        applies to AUTO/ACCEPT_EDITS/BYPASS (so even YOLO mode confirms
        before touching ~/.ssh, /etc, credential files, etc.).

    is_command_safe_for_auto(cmd, mode) -> (allowed, reason)
        Does the shell command look like something AUTO/BYPASS should
        auto-execute, or does it match a destructive/exfiltration pattern
        (rm -rf /, sudo, curl | sh, ...) that should always confirm?

    is_url_safe_for_fetch(url) -> (allowed, reason)
        Does the URL resolve to a public address? Blocks loopback /
        private / link-local / reserved / multicast to prevent SSRF via
        /image when the user pastes an attacker-influenced URL.

Design notes:
- A failing safety check NEVER hard-denies the action — it only downgrades
  auto-approval to "ask the user." The user is the source of truth.
- BYPASS keeps the denylist for paths and commands but skips the cwd-scope
  check. The intent: BYPASS = "skip routine confirmations" not "the model
  can silently overwrite ~/.ssh/authorized_keys."
- We resolve symlinks and check the resolved target against the denylist
  so a symlink in cwd pointing at /etc/passwd doesn't sneak past us.
"""
import ipaddress
import re
import socket
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from .permissions import Mode

# Total bytes of attached images allowed per session before /image and
# auto-attach refuse new attachments. 20 MB per image already lives in
# attachments.HARD_LIMIT_BYTES; this is the cumulative cap.
SESSION_IMAGE_BYTE_CAP = 100 * 1024 * 1024  # 100 MB

# Sensitive path prefixes — even BYPASS asks before writing here.
_HOME = Path.home()
_SENSITIVE_PATH_PREFIXES: tuple = tuple(
    str(_HOME / sub) for sub in (
        ".ssh", ".aws", ".gnupg", ".gpg", ".docker", ".kube",
        ".npmrc", ".pypirc", ".netrc",
        ".bash_history", ".zsh_history", ".python_history",
        ".mysql_history", ".psql_history", ".sqlite_history",
        ".meshapi",  # don't let the model rewrite its own config / history
    )
) + (
    "/etc", "/sys", "/proc", "/boot",
    "/private/etc",  # macOS shadows /etc under /private
    "/usr/bin", "/usr/sbin", "/sbin", "/bin",
)

# Secret / key file extensions — anywhere in the path.
_SENSITIVE_EXT_PATTERN = re.compile(
    r"\.(pem|key|p12|pfx|crt|cer|der|asc|gpg|kdbx)$", re.IGNORECASE
)

# Destructive / exfiltration shell patterns. Pattern → human-readable reason.
# Tuned to catch shapes that are almost never intentional in an agentic dev
# loop; legitimate uses still work via the y/n confirm path.
_DANGEROUS_BASH_PATTERNS: tuple = (
    (re.compile(r"\brm\s+(-[a-zA-Z]+\s+)*[/~]"),       "rm targeting / or ~"),
    (re.compile(r"\brm\s+-[rRfF]"),                     "rm -rf / -fr"),
    (re.compile(r"\bsudo\b"),                           "sudo (privilege escalation)"),
    (re.compile(r"(?:curl|wget|fetch)\s[^|]*\|\s*(sh|bash|zsh|python|node|perl|ruby)\b"),
                                                        "piping a download into a shell"),
    (re.compile(r"\bdd\s+if="),                         "dd (raw block I/O)"),
    (re.compile(r"\bmkfs(\.[A-Za-z0-9]+)?\b"),          "mkfs (filesystem format)"),
    (re.compile(r"\bchmod\s+(-R\s+)?[+\-=0-7]+\s+[/~]"),"recursive chmod on / or ~"),
    (re.compile(r"\bchown\s+(-R\s+)?\S+\s+[/~]"),       "chown on / or ~"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),          "fork bomb"),
    (re.compile(r">\s*/dev/(sd[a-z]|nvme|disk|hd[a-z])"),"writing to raw block device"),
    (re.compile(r"\bnc(?:at)?\b[^\n]*-[lL]\b"),         "netcat listener (possible reverse shell)"),
    (re.compile(r"\b(env|printenv|history)\b[^\n]*\|[^\n]*\b(curl|wget|nc|ncat|xh|http)\b"),
                                                        "exfiltrating env/history over the network"),
    (re.compile(r"\bcat\s+/etc/(passwd|shadow|sudoers|hostname|hosts)\b"),
                                                        "reading sensitive system files"),
    (re.compile(r"\b(cat|head|tail|less|more)\s+~?/?\.(ssh|aws|gnupg|netrc)\b"),
                                                        "reading a credential directory"),
    (re.compile(r"\bssh-keygen\b[^\n]*\s-f\s+[^/\s]"),  "writing an ssh key to an implicit location"),
    (re.compile(r"\beval\s+[\"'`]?\$\(.*curl"),         "eval of a downloaded payload"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),"system shutdown / reboot"),
)


def is_path_safe_for_auto_write(
    path_str: Optional[str], mode: Mode
) -> Tuple[bool, Optional[str]]:
    """Should `write_file(path)` auto-approve in `mode`?

    DEFAULT  → always returns True (the call site confirms anyway).
    BYPASS   → True unless the path is in the sensitive denylist.
    AUTO,
    ACCEPT_EDITS → True only if the resolved path is inside cwd AND not
                   in the denylist.
    """
    if mode == Mode.DEFAULT:
        return True, None
    if not path_str:
        return False, "empty path"

    try:
        resolved = _resolve_target(path_str)
    except (OSError, ValueError, RuntimeError) as e:
        return False, f"can't resolve path: {e}"
    resolved_str = str(resolved)

    # Denylist check — applies in every auto mode, including BYPASS.
    for sensitive in _SENSITIVE_PATH_PREFIXES:
        if resolved_str == sensitive or resolved_str.startswith(sensitive + "/"):
            return False, f"path is in the sensitive denylist ({sensitive})"
    if _SENSITIVE_EXT_PATTERN.search(resolved_str):
        return False, "path has a secret/key file extension"

    # Cwd-scope check — AUTO and ACCEPT_EDITS only.
    if mode in (Mode.ACCEPT_EDITS, Mode.AUTO):
        try:
            cwd = Path.cwd().resolve()
        except OSError as e:
            return False, f"can't read cwd: {e}"
        if not _is_inside(resolved, cwd):
            return False, "path is outside the current project directory"

    return True, None


def is_path_safe_for_auto_read(
    path_str: Optional[str], mode: Mode
) -> Tuple[bool, Optional[str]]:
    """Should `read_file(path)` auto-approve in `mode`?

    Same denylist as write, but no cwd-scope (reading outside cwd is much
    more often legitimate than writing outside cwd — e.g. reading
    /usr/local/lib/.../some.py). The denylist still bites though, because
    reading a denylisted path leaks its contents to the LLM provider.
    """
    if mode == Mode.DEFAULT:
        return True, None
    if not path_str:
        return False, "empty path"
    try:
        resolved = _resolve_target(path_str)
    except (OSError, ValueError, RuntimeError) as e:
        return False, f"can't resolve path: {e}"
    resolved_str = str(resolved)
    for sensitive in _SENSITIVE_PATH_PREFIXES:
        if resolved_str == sensitive or resolved_str.startswith(sensitive + "/"):
            return False, f"path is in the sensitive denylist ({sensitive})"
    if _SENSITIVE_EXT_PATTERN.search(resolved_str):
        return False, "path has a secret/key file extension"
    return True, None


def is_command_safe_for_auto(
    cmd: Optional[str], mode: Mode
) -> Tuple[bool, Optional[str]]:
    """Should `run_bash(cmd)` auto-approve in `mode`?

    DEFAULT  → always returns True (caller confirms anyway).
    AUTO,
    BYPASS   → True unless the command matches a destructive / exfiltration
               pattern. Even YOLO BYPASS asks before `rm -rf /` and friends.
    Other    → True (these modes don't auto-approve run_bash regardless).
    """
    if mode == Mode.DEFAULT:
        return True, None
    if not cmd or not cmd.strip():
        return False, "empty command"
    if mode in (Mode.AUTO, Mode.BYPASS):
        for pattern, reason in _DANGEROUS_BASH_PATTERNS:
            if pattern.search(cmd):
                return False, reason
    return True, None


def is_url_safe_for_fetch(url: str) -> Tuple[bool, Optional[str]]:
    """Does `url` resolve only to public addresses?

    Rejects loopback / private / link-local / reserved / multicast. Re-
    resolves all addresses (any family) so DNS-rebinding to a private
    range is caught even when one A record is public.
    """
    try:
        u = urlparse(url)
    except (ValueError, AttributeError) as e:
        return False, f"can't parse URL: {e}"
    if u.scheme not in ("http", "https"):
        return False, "only http(s) URLs are allowed"
    host = u.hostname
    if not host:
        return False, "URL has no hostname"

    # If the host is itself a literal IP, check it directly.
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            return False, f"URL points at a non-public address {ip}"
    except ValueError:
        pass  # not a literal IP — resolve DNS below

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0].split("%", 1)[0]  # strip IPv6 zone-id
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            return False, f"URL resolves to non-public address {ip}"
    return True, None


# ---- helpers --------------------------------------------------------------


def _resolve_target(path_str: str) -> Path:
    """Resolve a path the user/model gave us, following symlinks where we
    can. For non-existent paths we resolve the parent and append basename
    so `~/.ssh/foo` (where foo doesn't exist) still resolves under ~/.ssh."""
    p = Path(path_str).expanduser()
    if p.exists() or p.is_symlink():
        return p.resolve()
    parent = p.parent
    try:
        return parent.resolve(strict=False) / p.name
    except (OSError, RuntimeError):
        return p.absolute()


def _is_inside(child: Path, parent: Path) -> bool:
    """True if `child` is at or below `parent` after resolution."""
    try:
        # Python 3.9+ has Path.is_relative_to. Fall back to manual check.
        return child.is_relative_to(parent)  # type: ignore[attr-defined]
    except AttributeError:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False


def _is_blocked_ip(ip) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )
