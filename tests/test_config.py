"""Tests for meshapi.config.

CRITICAL: config.py binds CONFIG_DIR / CONFIG_FILE / CREDENTIALS_FILE at
import time to the *real* ~/.meshapi. Every test that exercises load/save
MUST route those module globals at a throwaway tmp_path first (the
`config_paths` fixture below) so the user's real config is never read,
written, or migrated. The env vars that override key/base_url resolution are
also cleared so a developer's shell can't leak into an assertion.
"""
import json

import pytest

from meshapi import config


# --------------------------------------------------------------------------
# _validate_base_url  (pure function — no filesystem)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://api.meshapi.ai/v1",
        "https://example.com",
        "http://localhost",
        "http://localhost:8080",
        "http://127.0.0.1",
    ],
)
def test_validate_base_url_accepts_https_and_local(url):
    # Returns the url (trailing slash stripped), never exits.
    assert config._validate_base_url(url) == url


def test_validate_base_url_strips_trailing_slash():
    assert config._validate_base_url("https://api.meshapi.ai/v1/") == (
        "https://api.meshapi.ai/v1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.evil.com",   # suffix smuggling
        "http://127.0.0.1.evil.com",   # suffix smuggling
        "http://localhostx",           # prefix collision, not a real local host
        "http://evil.com",             # plain external over cleartext http
    ],
)
def test_validate_base_url_rejects_cleartext_external(url):
    # A non-local http:// host must hard-exit rather than ship the bearer
    # key in cleartext to somewhere off-machine.
    with pytest.raises(SystemExit) as ei:
        config._validate_base_url(url)
    assert ei.value.code == 2


@pytest.mark.parametrize("bad", [123, ["x"], {"a": 1}, 3.14])
def test_validate_base_url_non_string_coerces_then_exits(bad):
    # Regression: a hand-edited non-string base_url must coerce (str()) and
    # SystemExit — never AttributeError from calling .strip() on a non-str.
    with pytest.raises(SystemExit) as ei:
        config._validate_base_url(bad)
    assert ei.value.code == 2


def test_validate_base_url_non_string_does_not_raise_attributeerror():
    # Explicitly assert the failure mode is SystemExit, not AttributeError.
    with pytest.raises(SystemExit):
        try:
            config._validate_base_url(123)
        except AttributeError:  # pragma: no cover - would be the regression
            pytest.fail("_validate_base_url raised AttributeError on non-str")


def test_validate_base_url_ipv6_loopback_accepted():
    # Regression: http://[::1] (IPv6 loopback) is a genuine local host and must
    # be accepted. Previously rejected because the :-split broke the bracketed
    # form ('[::1]'.split(':')[0] == '['); fixed to strip the brackets first.
    assert config._validate_base_url("http://[::1]") == "http://[::1]"
    assert config._validate_base_url("http://[::1]:8080/v1") == "http://[::1]:8080/v1"


# --------------------------------------------------------------------------
# Fixtures: sandbox the module-level paths + clear env overrides
# --------------------------------------------------------------------------

@pytest.fixture
def config_paths(tmp_path, monkeypatch):
    """Point config.py's module globals at a fresh tmp dir and clear the
    env vars that feed key/base_url resolution. Yields the .meshapi dir."""
    cfg_dir = tmp_path / ".meshapi"
    cfg_dir.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_dir / "config.json")
    monkeypatch.setattr(config, "CREDENTIALS_FILE", cfg_dir / "credentials")
    for var in ("MESHAPI_API_KEY", "MESH_API_KEY", "MESHAPI_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return cfg_dir


def _write_config(cfg_dir, text):
    (cfg_dir / "config.json").write_text(text)


# --------------------------------------------------------------------------
# load_config — corruption resilience
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "corrupt",
    [
        "{bad json",   # not valid JSON
        "[]",          # valid JSON, wrong type (list)
        '"x"',         # valid JSON, wrong type (str)
        "42",          # valid JSON, wrong type (int)
        "null",        # valid JSON, wrong type (None)
        "",            # empty file
    ],
    ids=["truncated", "list", "string", "int", "null", "empty"],
)
def test_load_config_survives_corrupt_file(config_paths, corrupt):
    # A corrupt/non-object config.json must NEVER brick launch: it degrades
    # to the merged defaults instead of raising.
    _write_config(config_paths, corrupt)
    cfg = config.load_config()
    assert isinstance(cfg, dict)
    assert cfg["model"] == config.DEFAULT_CONFIG["model"]
    # A full default set survives the fallback merge.
    assert cfg["base_url"] == config.DEFAULT_CONFIG["base_url"]
    assert cfg["system"] == config.DEFAULT_CONFIG["system"]


def test_load_config_missing_file_is_created(config_paths):
    assert not (config_paths / "config.json").exists()
    cfg = config.load_config()
    assert (config_paths / "config.json").exists()
    assert cfg["model"] == config.DEFAULT_CONFIG["model"]
    # The freshly written file is itself valid JSON.
    on_disk = json.loads((config_paths / "config.json").read_text())
    assert on_disk["model"] == config.DEFAULT_CONFIG["model"]


def test_load_config_valid_file_overrides_defaults(config_paths):
    _write_config(
        config_paths,
        json.dumps({"model": "openai/gpt-4o-mini", "optimize": 0.5}),
    )
    cfg = config.load_config()
    assert cfg["model"] == "openai/gpt-4o-mini"     # overridden
    assert cfg["optimize"] == 0.5                    # overridden
    # Untouched keys fall back to defaults (merge, not replace).
    assert cfg["system"] == config.DEFAULT_CONFIG["system"]


def test_load_config_drops_stale_route_key(config_paths):
    # `route` (cheapest/fastest/balanced) never existed gateway-side; it must
    # be stripped from an old hand-edited/legacy config.
    _write_config(
        config_paths,
        json.dumps({"model": "x/y", "route": "cheapest"}),
    )
    cfg = config.load_config()
    assert "route" not in cfg
    assert cfg["model"] == "x/y"


# --------------------------------------------------------------------------
# load_config — API key resolution order
#   env MESHAPI_API_KEY > env MESH_API_KEY > credentials file > config.json
# --------------------------------------------------------------------------

def test_key_resolution_config_json_only(config_paths, monkeypatch):
    monkeypatch.delenv("MESHAPI_API_KEY", raising=False)
    monkeypatch.delenv("MESH_API_KEY", raising=False)
    _write_config(config_paths, json.dumps({"api_key": "rsk_fromconfig"}))
    cfg = config.load_config()
    assert cfg["api_key"] == "rsk_fromconfig"
    # It is migrated into the credentials file so it survives the api_key
    # strip on the next save_config.
    creds = config_paths / "credentials"
    assert creds.exists()
    assert creds.read_text().strip() == "rsk_fromconfig"


def test_key_resolution_credentials_beats_config_json(config_paths):
    _write_config(config_paths, json.dumps({"api_key": "rsk_fromconfig"}))
    (config_paths / "credentials").write_text("rsk_fromcreds\n")
    cfg = config.load_config()
    assert cfg["api_key"] == "rsk_fromcreds"


def test_key_resolution_mesh_api_key_beats_credentials(config_paths, monkeypatch):
    (config_paths / "credentials").write_text("rsk_fromcreds\n")
    monkeypatch.setenv("MESH_API_KEY", "rsk_meshenv")
    cfg = config.load_config()
    assert cfg["api_key"] == "rsk_meshenv"


def test_key_resolution_meshapi_api_key_wins(config_paths, monkeypatch):
    (config_paths / "credentials").write_text("rsk_fromcreds\n")
    monkeypatch.setenv("MESH_API_KEY", "rsk_meshenv")
    monkeypatch.setenv("MESHAPI_API_KEY", "rsk_primary")
    cfg = config.load_config()
    assert cfg["api_key"] == "rsk_primary"


def test_base_url_env_override_applied(config_paths, monkeypatch):
    monkeypatch.setenv("MESHAPI_BASE_URL", "https://gateway.example.com/v1")
    cfg = config.load_config()
    assert cfg["base_url"] == "https://gateway.example.com/v1"


# --------------------------------------------------------------------------
# save_config
# --------------------------------------------------------------------------

def test_save_config_never_persists_api_key(config_paths):
    config.save_config(
        {"model": "x/y", "api_key": "rsk_secret", "optimize": 0.25}
    )
    written = json.loads((config_paths / "config.json").read_text())
    assert "api_key" not in written
    assert written["model"] == "x/y"
    assert written["optimize"] == 0.25


def test_save_config_round_trips_other_keys(config_paths):
    payload = {
        "model": "anthropic/claude-opus-4.8",
        "base_url": "https://api.meshapi.ai/v1",
        "system": "custom system",
        "auto_route": True,
        "fallback_models": ["a/b", "c/d"],
        "reasoning_effort": "high",
        "optimize": 0.9,
        "api_key": "rsk_should_vanish",
    }
    config.save_config(payload)
    cfg = config.load_config()  # reloads from disk
    for k, v in payload.items():
        if k == "api_key":
            continue  # stripped on save, re-resolved on load
        assert cfg[k] == v


def test_save_config_is_atomic_no_tmp_leftover(config_paths):
    config.save_config({"model": "x/y"})
    # os.replace leaves no ".tmp" behind.
    assert not (config_paths / "config.json.tmp").exists()
    assert not (config_paths / "config.json.json.tmp").exists()
    # And the result is valid JSON.
    json.loads((config_paths / "config.json").read_text())


def test_save_config_result_is_valid_json_object(config_paths):
    config.save_config({"model": "x/y", "optimize": 0.1})
    on_disk = json.loads((config_paths / "config.json").read_text())
    assert isinstance(on_disk, dict)
    assert on_disk["model"] == "x/y"


def test_loop_control_defaults():
    """0.5.9 agentic-loop keys ship with immortal-by-default values."""
    from meshapi.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["max_hops"] == 0          # unlimited
    assert DEFAULT_CONFIG["auto_compact"] is True
    assert DEFAULT_CONFIG["stall_policy"] == "pause"


def test_smart_routing_defaults():
    from meshapi.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["route_mode"] == "off"
    w = DEFAULT_CONFIG["route_weights"]
    assert set(w) == {"cost", "cap", "speed"}
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert DEFAULT_CONFIG["route_effort"] == "auto"


def test_interim_route_weight_shapes_migrate(tmp_path, monkeypatch):
    """Two short-lived dev shapes (cap="auto", difficulty key) must reset to
    the numeric default; an explicit numeric choice is preserved."""
    import json
    from meshapi import config as cfgmod
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    for interim in ({"cost": 0.6, "cap": "auto", "speed": 0.4},
                    {"cost": 0.6, "cap": "auto", "speed": 0.4, "difficulty": 0.5}):
        (tmp_path / "config.json").write_text(json.dumps({"route_weights": interim}))
        cfg = cfgmod.load_config()
        assert cfg["route_weights"] == cfgmod.DEFAULT_CONFIG["route_weights"]
    (tmp_path / "config.json").write_text(json.dumps(
        {"route_weights": {"cost": 0.2, "cap": 0.7, "speed": 0.1}}))
    cfg = cfgmod.load_config()
    assert cfg["route_weights"]["cap"] == 0.7
