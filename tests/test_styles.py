"""Output style: normalization, prompt injection, and the live-apply seam."""
import pytest

from meshapi import config, styles
from meshapi.tools import build_system_prompt


def test_every_style_is_well_formed():
    for value in styles.ORDER:
        label, desc, text = styles.STYLES[value]
        assert label and desc
        assert styles.normalize(value) == value
        if value != styles.DEFAULT:
            assert text.startswith("OUTPUT STYLE")


def test_default_injects_nothing():
    assert styles.block("default") == ""
    assert "OUTPUT STYLE" not in build_system_prompt({"output_style": "default"})


@pytest.mark.parametrize("raw,expected", [
    ("Concise", "concise"), ("BRIEF", "concise"), ("terse", "concise"),
    ("verbose", "explanatory"), ("teaching", "learning"),
    ("  default  ", "default"), ("normal", "default"),
])
def test_aliases_normalize(raw, expected):
    assert styles.normalize(raw) == expected


@pytest.mark.parametrize("bad", [None, "", "zzz", 5, [], {"a": 1}])
def test_unknown_values_are_none_never_raise(bad):
    assert styles.normalize(bad) is None
    assert styles.block(bad) == ""


def test_style_block_lands_in_the_system_prompt():
    p = build_system_prompt({"system": "base", "output_style": "concise"})
    assert "OUTPUT STYLE — CONCISE" in p
    # LAST wins on cheap models — the style must not be buried mid-prompt.
    assert p.rstrip().endswith(styles.block("concise").rstrip())


def test_concise_never_licenses_shrinking_the_work():
    # A style is about prose. If "be brief" reads as "write less code" the
    # quality guard starts firing on style, not on model failure.
    text = styles.block("concise").lower()
    assert "prose only" in text
    assert "placeholder" in text


def test_learning_never_licenses_leaving_work_undone():
    assert "never leave work undone" in styles.block("learning").lower()


def test_bad_config_value_migrates_to_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"output_style": "hand-edited-junk"}')
    cfg = config.load_config()
    assert cfg["output_style"] == styles.DEFAULT


def test_set_style_rewrites_the_live_system_message(monkeypatch, tmp_path):
    # The system prompt is built once at session start; without this the
    # setting would look applied and do nothing until /clear.
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    from meshapi import commands
    cfg = dict(config.DEFAULT_CONFIG)
    state = {"cfg": cfg,
             "messages": [{"role": "system", "content": build_system_prompt(cfg)}]}
    commands.handle_command("/output explanatory", state)
    assert "OUTPUT STYLE — EXPLANATORY" in state["messages"][0]["content"]
    assert state["cfg"]["output_style"] == "explanatory"
    commands.handle_command("/output default", state)
    assert "OUTPUT STYLE" not in state["messages"][0]["content"]


def test_rejected_value_leaves_state_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    from meshapi import commands
    cfg = dict(config.DEFAULT_CONFIG, output_style="concise")
    state = {"cfg": cfg, "messages": [{"role": "system", "content": "x"}]}
    commands.handle_command("/output nonsense", state)
    assert state["cfg"]["output_style"] == "concise"
    assert state["messages"][0]["content"] == "x"


def test_style_is_registered_everywhere():
    from meshapi.completer import COMMANDS
    from meshapi.commands import resolve_command
    from meshapi.cli import LIVE_CONTROL_COMMANDS
    assert "/output" in COMMANDS
    assert "/output" in LIVE_CONTROL_COMMANDS
    assert resolve_command("/out")[0] == "/output"


def test_no_tool_names_in_style_prose():
    # Naming tools in prose tips Anthropic models into XML tool-use mode.
    for value in styles.ORDER:
        text = styles.block(value)
        for name in ("read_file", "write_file", "run_bash", "start_server",
                     "web_search", "create_plan", "update_step", "ask_user"):
            assert name not in text


# ---- each style must FORBID or REQUIRE something the others don't ----
# Regression: the first version described intent ("as few words as the
# question allows") and all three styles produced the same numbered list
# with an "Overall..." summary. Vague guidance at the end of a ~2.5k-token
# system prompt is indistinguishable from no guidance on a cheap model.

def test_concise_forbids_lists_and_summaries():
    t = styles.block("concise").lower()
    assert "list" in t and "unless the user" in t      # lists gated, not banned
    assert "in summary" in t and "overall" in t        # names the exact closers
    assert "80 words" in t                             # a checkable budget


def test_explanatory_requires_an_explicit_tradeoff():
    t = styles.block("explanatory").lower()
    assert "trade-off" in t
    assert "at least one" in t                         # a requirement, not a hint
    assert "feature list" in t                         # names what it replaces


def test_learning_requires_a_concrete_try_this_line():
    t = styles.block("learning")
    assert "Try this:" in t
    assert "analogy" in t.lower() and "worked example" in t.lower()


def test_the_three_styles_are_mutually_distinguishable():
    # No two style texts may be near-duplicates: each must contain a rule
    # absent from the others, which is what makes one answer tell them apart.
    marks = {"concise": "80 words",
             "explanatory": "trade-off",
             "learning": "Try this:"}
    for name, mark in marks.items():
        assert mark.lower() in styles.block(name).lower()
        for other in marks:
            if other != name:
                assert mark.lower() not in styles.block(other).lower(), (name, other)
