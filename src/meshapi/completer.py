"""Fuzzy tab-completion for the prompt: slash commands and their arguments.

`/model qw` pops a menu of every qwen model; `gpt4m` finds
`openai/gpt-4o-mini`. Ranking: prefix > substring > subsequence, then
alphabetical. The model catalog loads lazily and SILENTLY on first use (the
completer runs inside a ThreadedCompleter, so the one-time ~300ms fetch
never blocks a keystroke; failures just mean no model suggestions).
Non-slash input yields nothing — normal prompts never see a menu.
"""
from prompt_toolkit.completion import Completer, Completion

from .commands import fetch_models_quiet

# command -> menu hint. Single source for name completion; keep in sync with
# /help when adding commands.
COMMANDS = {
    "/model": "switch model (fuzzy: qw, gpt4m…)",
    "/models": "browse the catalog",
    "/route": "auto | off | preview",
    "/mode": "default | accept-edits | auto | bypass",
    "/fallback": "ordered fallback models | off",
    "/reasoning": "high | medium | low | none | off",
    "/file": "add a text file to context",
    "/image": "attach an image",
    "/clear-attach": "drop queued attachments",
    "/system": "set system prompt",
    "/cost": "session spend",
    "/optimize": "token savings dial (beta)",
    "/login": "set or replace the API key",
    "/update": "check PyPI for a newer meshapi",
    "/clear": "reset conversation",
    "/help": "all commands",
    "/exit": "quit",
}

_ARG_CHOICES = {
    "/route": ("auto", "off", "preview"),
    "/mode": ("default", "accept-edits", "auto", "bypass"),
    "/reasoning": ("high", "medium", "low", "none", "off"),
}


def fuzzy_rank(query: str, candidate: str) -> "int | None":
    """0 = prefix, 1 = substring, 2 = subsequence, None = no match."""
    q, c = query.lower(), candidate.lower()
    if not q:
        return 0
    if c.startswith(q):
        return 0
    if q in c:
        return 1
    it = iter(c)
    if all(ch in it for ch in q):
        return 2
    return None


def _ranked(query: str, candidates) -> list:
    hits = []
    for cand in candidates:
        r = fuzzy_rank(query, cand)
        if r is not None:
            hits.append((r, cand))
    return [c for _, c in sorted(hits)]


class SlashCompleter(Completer):
    """Context-aware completions driven by the live REPL state."""

    def __init__(self, state: dict) -> None:
        self._state = state

    def _model_ids(self) -> list:
        models = self._state.get("models_cache")
        if models is None:
            # First use: fetch once, silently. We're on the ThreadedCompleter
            # worker, so blocking here never freezes typing; the flag stops
            # parallel fetches from rapid keystrokes.
            if not self._state.get("_models_fetching"):
                self._state["_models_fetching"] = True
                try:
                    models = fetch_models_quiet(self._state)
                finally:
                    self._state["_models_fetching"] = False
        return [
            m.get("id") for m in (models or ())
            if isinstance(m, dict) and m.get("id")
        ]

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            # completing the command name itself
            for cmd in _ranked(text[1:], [c[1:] for c in COMMANDS]):
                cmd = "/" + cmd
                yield Completion(
                    cmd, start_position=-len(text), display_meta=COMMANDS[cmd]
                )
            return
        cmd = text.split()[0]
        # the token being typed (empty right after a space)
        token = "" if text.endswith(" ") else text.split()[-1]
        if cmd in ("/model", "/fallback"):
            for mid in _ranked(token, self._model_ids()):
                yield Completion(mid, start_position=-len(token))
        elif cmd in _ARG_CHOICES:
            for choice in _ranked(token, _ARG_CHOICES[cmd]):
                yield Completion(choice, start_position=-len(token))
