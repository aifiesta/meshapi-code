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
from pathlib import Path
from urllib.parse import urlparse

import httpx

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


def find_image_tokens(text: str) -> list[str]:
    """Return tokens in `text` that look like image paths or URLs.

    Conservative on purpose — only matches:
      - http(s) URLs ending in a known image extension
      - Local paths starting with `/`, `~/`, `./`, or `../` (and ending in an
        image extension, AND pointing at an existing file)

    Bare filenames (`foo.png`) are NOT matched: too ambiguous with filenames
    mentioned in conversation. Tokens wrapped in backticks or quotes are
    skipped (user's escape hatch).

    Trailing sentence punctuation (`.,;:!?)`) is trimmed before matching.
    """
    matches: list[str] = []
    for raw in text.split():
        if not raw or raw[0] in "`\"'":
            continue
        token = raw
        while token and token[-1] in ".,;:!?)":
            token = token[:-1]
        if not token:
            continue
        low = token.lower()
        if low.startswith(("http://", "https://")):
            path_part = token.split("?", 1)[0]
            if path_part.lower().endswith(IMAGE_EXTS):
                matches.append(token)
            continue
        if token.startswith(("/", "~/", "./", "../")):
            if low.endswith(IMAGE_EXTS):
                try:
                    p = Path(token).expanduser()
                    if p.is_file():
                        matches.append(token)
                except OSError:
                    pass
    return matches


def _looks_like_url(s: str) -> bool:
    try:
        u = urlparse(s)
    except (ValueError, AttributeError):
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


def _fetch_url(url: str) -> tuple[bytes, str, str]:
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
