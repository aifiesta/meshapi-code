"""Image attachment loading for multimodal chat.

Builds OpenAI-compatible content parts (`{type: image_url, image_url: {url,
detail}}`) from local file paths or HTTP(S) URLs. We always base64-encode
into a `data:image/...;base64,...` URL — the Mesh API docs warn that some
providers refuse public URLs and require base64, so always-base64 is the
maximally-compatible default.

Also provides a conservative auto-detector for the main loop: scan a user
prompt for tokens that unambiguously look like image paths or URLs (start
with `/`, `~`, `./`, `../`, or `http(s)://`, end in a known image extension),
so a dragged-in file path can be attached without an explicit slash command.
"""
import base64
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Tokenizer for find_image_tokens: a single- or double-quoted span (kept
# whole, INCLUDING internal spaces) OR a run of non-whitespace. Quoted
# alternatives come first so a drag-dropped path like
# `'/Users/me/snake game/img.png'` stays one token instead of being shredded
# on the spaces by str.split().
_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\S+")

# Size guardrails. We don't refuse — vision tokens are the user's call — but we
# do report sizes back so the user sees the cost.
HARD_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MB

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class AttachmentError(Exception):
    """Raised for any user-facing failure to attach an image."""


def load_image(source: str, detail: str = "auto") -> tuple[dict, dict]:
    """Load an image from a local path or URL into a content part.

    Returns (content_part, info):
      - content_part is the dict to splice into the message's `content` array:
        `{"type":"image_url","image_url":{"url":"data:...","detail":...}}`
      - info has display metadata: `{"name", "size_bytes", "mime"}`

    Raises AttachmentError on any failure (not found, too big, wrong MIME,
    network error). Caller is expected to surface the message to the user.
    """
    if _looks_like_url(source):
        data, mime, name = _fetch_url(source)
    else:
        data, mime, name = _read_local(source)

    if len(data) > HARD_LIMIT_BYTES:
        raise AttachmentError(
            f"image too large: {len(data) // (1024*1024)} MB "
            f"(limit {HARD_LIMIT_BYTES // (1024*1024)} MB)"
        )

    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    return (
        {"type": "image_url", "image_url": {"url": data_url, "detail": detail}},
        {"name": name, "size_bytes": len(data), "mime": mime},
    )


def find_image_tokens(text: str) -> list[tuple[str, str]]:
    """Return `(raw_token, normalized)` pairs for image references in `text`.

    Strategy: be liberal about what looks like a file/URL, then verify by
    actually checking existence (local) or extension (URL). The user's
    natural workflow is "drag the file in" — terminals wrap drag-dropped
    paths in single quotes when convenient, so we strip wrapping quotes.
    A bare filename like `screenshot.png` also matches if it exists in the
    cwd. The only escape is a backtick prefix: `` `foo.png` `` is treated
    as text.

      - http(s) URLs ending in a known image extension
      - Any token that, after stripping wrapping quotes and trailing
        punctuation, resolves to an existing file with an image extension

    `raw_token` is the exact substring to find/replace in the original
    text (so quotes are preserved when stripping); `normalized` is the
    cleaned path or URL to pass into load_image().
    """
    matches: list[tuple[str, str]] = []
    for raw in _TOKEN_RE.findall(text):
        if not raw:
            continue
        # Backtick prefix = explicit "treat as text" escape.
        if raw.startswith("`"):
            continue

        token = raw
        # Strip a matching wrapping pair of single or double quotes
        # (drag-drop on macOS Terminal/iTerm2 quotes paths automatically).
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
            token = token[1:-1]
        # Strip trailing sentence punctuation but leave URL query strings.
        while token and token[-1] in ".,;:!?)":
            token = token[:-1]
        if not token:
            continue

        low = token.lower()
        if low.startswith(("http://", "https://")):
            path_part = token.split("?", 1)[0]
            if path_part.lower().endswith(IMAGE_EXTS):
                matches.append((raw, token))
            continue

        if low.endswith(IMAGE_EXTS):
            try:
                p = Path(token).expanduser()
                if p.is_file():
                    matches.append((raw, token))
            except (OSError, ValueError):
                pass
    return matches


def _looks_like_url(s: str) -> bool:
    try:
        u = urlparse(s)
    except (ValueError, AttributeError):
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


def _fetch_url(url: str) -> tuple[bytes, str, str]:
    # SSRF guard: refuse loopback/private/link-local before issuing the
    # request. Imported lazily so attachments.py doesn't pull in safety on
    # every code path.
    from .safety import is_url_safe_for_fetch

    ok, reason = is_url_safe_for_fetch(url)
    if not ok:
        raise AttachmentError(f"refusing to fetch {url}: {reason}")
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.content
            mime = r.headers.get("content-type", "").split(";")[0].strip()
    except httpx.HTTPError as e:
        raise AttachmentError(f"couldn't fetch {url}: {e}") from None

    if not mime:
        guessed, _ = mimetypes.guess_type(url)
        mime = guessed or ""
    if mime not in IMAGE_MIMES:
        raise AttachmentError(
            f"URL doesn't look like an image (mime: {mime or 'unknown'})"
        )
    name = Path(urlparse(url).path).name or "image"
    return data, mime, name


def _read_local(path_str: str) -> tuple[bytes, str, str]:
    path = Path(path_str).expanduser()
    if not path.exists():
        raise AttachmentError(f"file not found: {path}")
    if not path.is_file():
        raise AttachmentError(f"not a regular file: {path}")
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        raise AttachmentError(f"couldn't determine MIME type for {path.name}")
    if mime not in IMAGE_MIMES:
        raise AttachmentError(
            f"{path.name} isn't an image (mime: {mime}). "
            "For text documents, use /file."
        )
    try:
        data = path.read_bytes()
    except OSError as e:
        raise AttachmentError(f"can't read {path}: {e}") from None
    return data, mime, path.name
