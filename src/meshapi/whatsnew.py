"""One-line 'what changed' note on the first launch after an upgrade.

Rules that make this a note and not nagware:
  * shown ONCE per version, then persisted (`last_seen_version`)
  * never shown on a fresh install — there is nothing "new" on day one, so
    load_config stamps the current version when it creates config.json
  * a version with no entry here shows nothing at all
  * one line, so it can never push the banner off a short terminal

Keep NOTES entries in the user's language ("what can I now do"), not commit
language. Newest first is irrelevant — lookup is by exact version.
"""
from __future__ import annotations

from . import __version__

# version -> single-line summary. Written for someone upgrading FROM the
# previous PyPI release, so it may span several internal versions.
NOTES = {
    # 0.5.9 was built but never published — it has no note because no user
    # can ever be upgrading TO it. 0.5.10 is what 0.5.8 users receive.
    "0.5.10": "free per-prompt routing · /output styles · unlimited hops",
}


def fit(note: str, width: int) -> str:
    """Clip *note* so the banner line can never wrap.

    The promise is ONE line; enforcing it here rather than trusting every
    future note to be short keeps a long entry from pushing the banner off
    a small terminal.
    """
    if width <= 0 or len(note) <= width:
        return note
    return note[:max(1, width - 1)].rstrip(" ·-—") + "…"


def note_for(version: str = None) -> str:
    """The one-liner for *version*, or '' when there is nothing to say."""
    return NOTES.get(version or __version__, "")


def should_show(cfg: dict, version: str = None) -> bool:
    """True only on the first launch of a version the user hasn't seen.

    An absent `last_seen_version` means the config predates this feature —
    i.e. a genuine upgrade — so the note SHOULD fire. Fresh installs are
    stamped at creation time instead, which is what keeps them quiet.
    """
    version = version or __version__
    if not note_for(version):
        return False
    return cfg.get("last_seen_version") != version


def mark_seen(cfg: dict, version: str = None) -> None:
    """Persist that this version's note has been shown. Never raises."""
    cfg["last_seen_version"] = version or __version__
    try:
        from .config import save_config
        save_config(cfg)
    except Exception:
        pass
