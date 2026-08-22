"""The upgrade note: shows once, never on a fresh install, never wraps."""
import json

from meshapi import __version__, config, whatsnew


def test_current_version_has_a_note():
    # A release without a note ships silently — catch that at build time.
    assert whatsnew.note_for(__version__), f"no NOTES entry for {__version__}"


def test_note_fits_one_line():
    for version, note in whatsnew.NOTES.items():
        assert len(note) <= 70, (version, len(note))
        assert "\n" not in note


def test_unknown_version_says_nothing():
    assert whatsnew.note_for("9.9.9") == ""
    assert whatsnew.should_show({}, "9.9.9") is False


def test_shows_on_upgrade_then_never_again():
    cfg = {}                                   # config predating the feature
    assert whatsnew.should_show(cfg, __version__) is True
    cfg["last_seen_version"] = __version__
    assert whatsnew.should_show(cfg, __version__) is False


def test_fresh_install_is_silent(monkeypatch, tmp_path):
    # The note must not greet a first-time user: load_config stamps the
    # current version when it CREATES config.json.
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    cfg = config.load_config()
    assert cfg["last_seen_version"] == __version__
    assert whatsnew.should_show(cfg) is False


def test_existing_config_without_the_key_is_an_upgrade(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"model": "m"}))
    cfg = config.load_config()
    assert whatsnew.should_show(cfg) is True


def test_fit_clips_without_wrapping():
    assert whatsnew.fit("short", 40) == "short"
    out = whatsnew.fit("x" * 100, 30)
    assert len(out) <= 30 and out.endswith("…")
    assert whatsnew.fit("abc", 0) == "abc"          # degenerate width is safe


def test_mark_seen_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(config, "save_config", boom)
    cfg = {}
    whatsnew.mark_seen(cfg)                          # must not propagate
    assert cfg["last_seen_version"] == __version__
