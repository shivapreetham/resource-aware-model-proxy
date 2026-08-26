import pytest

from ramp.config import Config, ConfigError

BASE = {
    "backend": "mock",
    "tiers": [
        {"name": "small", "est_ram_mb": 1000},
        {"name": "big", "est_ram_mb": 6000},
    ],
}


def test_tiers_sorted_best_first():
    cfg = Config.from_dict(dict(BASE))
    assert [t.name for t in cfg.tiers] == ["big", "small"]


def test_defaults_applied():
    cfg = Config.from_dict(dict(BASE))
    assert cfg.listen_port == 8090
    assert cfg.safety_margin_mb == 1536.0
    assert cfg.hysteresis.downgrade_after_samples == 2


def test_missing_tiers_rejected():
    with pytest.raises(ConfigError):
        Config.from_dict({"backend": "mock", "tiers": []})


def test_duplicate_names_rejected():
    with pytest.raises(ConfigError):
        Config.from_dict(
            {
                "backend": "mock",
                "tiers": [
                    {"name": "x", "est_ram_mb": 1},
                    {"name": "x", "est_ram_mb": 2},
                ],
            }
        )


def test_llama_backend_requires_model_path():
    with pytest.raises(ConfigError):
        Config.from_dict({"backend": "llama", "tiers": [{"name": "a", "est_ram_mb": 100}]})


def test_bad_backend_rejected():
    with pytest.raises(ConfigError):
        Config.from_dict({"backend": "wat", "tiers": BASE["tiers"]})


def test_est_ram_required_positive():
    with pytest.raises(ConfigError):
        Config.from_dict({"backend": "mock", "tiers": [{"name": "a", "est_ram_mb": 0}]})


# -- self-proxy guard ----------------------------------------------------
#
# Regression: transparent mode relocated Ollama to another port, but a
# ramp.yaml in the working directory still named the old one. RAMP found
# nothing there, started a *new* Ollama on the port it was about to bind,
# and then died with "address already in use". The arrangement is now
# rejected up front with an explanation.


def test_proxying_to_our_own_port_is_rejected():
    cfg = Config.from_dict({
        "backend": "ollama",
        "listen": {"port": 11434},
        "ollama_url": "http://127.0.0.1:11434",
        "tiers": [{"name": "t", "model": "m", "est_ram_mb": 100}],
    })
    with pytest.raises(ConfigError, match="proxying to itself"):
        cfg.validate()


def test_localhost_and_127_are_treated_as_the_same_host():
    cfg = Config.from_dict({
        "backend": "ollama",
        "listen": {"host": "127.0.0.1", "port": 9000},
        "ollama_url": "http://localhost:9000",
        "tiers": [{"name": "t", "model": "m", "est_ram_mb": 100}],
    })
    with pytest.raises(ConfigError, match="proxying to itself"):
        cfg.validate()


def test_different_ports_are_fine():
    cfg = Config.from_dict({
        "backend": "ollama",
        "listen": {"port": 11434},
        "ollama_url": "http://127.0.0.1:11435",
        "tiers": [{"name": "t", "model": "m", "est_ram_mb": 100}],
    })
    cfg.validate()


def test_the_guard_only_applies_to_the_ollama_backend():
    """Other backends spawn their own child on a free port, so sharing a
    number with the listen port means nothing."""
    cfg = Config.from_dict({
        "backend": "mock",
        "listen": {"port": 11434},
        "tiers": [{"name": "t", "est_ram_mb": 100}],
    })
    cfg.validate()
