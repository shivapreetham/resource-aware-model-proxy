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
