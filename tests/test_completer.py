"""Tests for the fuzzy tab-completion (slash commands + model ids)."""
from prompt_toolkit.document import Document

from meshapi.completer import SlashCompleter, _ranked, fuzzy_rank


def test_fuzzy_rank_tiers():
    assert fuzzy_rank("qw", "qwen") == 0        # prefix
    assert fuzzy_rank("gpt", "openai/gpt-4o") == 1   # substring
    assert fuzzy_rank("gpt4m", "openai/gpt-4o-mini") == 2  # subsequence
    assert fuzzy_rank("zzq", "qwen") is None     # no match
    assert fuzzy_rank("", "anything") == 0       # empty query = prefix of all


def test_fuzzy_rank_case_insensitive():
    assert fuzzy_rank("QW", "qwen") == 0
    assert fuzzy_rank("GPT", "openai/gpt-4o") == 1


def test_ranked_orders_prefix_before_substring_before_subsequence():
    cands = ["openai/gpt-4o-mini", "gpt-fast", "a-g-p-t-x", "nomatch"]
    ranked = _ranked("gpt", cands)
    assert "nomatch" not in ranked
    # prefix (gpt-fast) before substring (openai/gpt-4o-mini) before subsequence
    assert ranked.index("gpt-fast") < ranked.index("openai/gpt-4o-mini")
    assert ranked.index("openai/gpt-4o-mini") < ranked.index("a-g-p-t-x")


def test_ranked_empty_query_returns_all_sorted():
    assert _ranked("", ["b", "a", "c"]) == ["a", "b", "c"]


def _complete(text, state):
    comp = SlashCompleter(state)
    return [c.text for c in comp.get_completions(Document(text), None)]


def test_complete_command_names():
    out = _complete("/mod", {"models_cache": []})
    assert "/model" in out and "/models" in out


def test_complete_model_ids_from_cache():
    state = {"models_cache": [
        {"id": "qwen/qwen-max"}, {"id": "qwen/qwen-plus"},
        {"id": "openai/gpt-4o-mini"},
    ]}
    out = _complete("/model qw", state)
    assert out and all(o.startswith("qwen/") for o in out)
    assert "openai/gpt-4o-mini" not in out


def test_complete_arg_choices_for_route():
    out = _complete("/route ", {"models_cache": []})
    assert set(out) == {"auto", "off", "preview"}


def test_non_slash_input_yields_nothing():
    assert _complete("hello world", {"models_cache": []}) == []


def test_completer_never_raises_on_empty_state():
    # A missing models_cache must not crash the completer (it returns no ids).
    comp = SlashCompleter({})
    # non-fetching path: command-name completion still works
    assert any(c.text == "/help" for c in comp.get_completions(Document("/hel"), None))


def test_loop_control_commands_complete():
    from meshapi.completer import COMMANDS, _ARG_CHOICES
    for cmd in ("/hops", "/compact", "/stall"):
        assert cmd in COMMANDS
    assert "off" in _ARG_CHOICES["/hops"]
    assert "now" in _ARG_CHOICES["/compact"]
    assert "keep-going" in _ARG_CHOICES["/stall"]
