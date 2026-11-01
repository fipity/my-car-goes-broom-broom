from carscout.core.config import get_settings, load_sources
from carscout.normalise.mapping import normalise_make, normalise_model, normalise_state
from carscout.sources.base import SearchParams, load_plugin, plugin_exists


def test_sources_yaml_loaded_with_defaults():
    cfg = load_sources()
    assert set(cfg) == {"demo", "carsales", "facebook"}
    assert cfg["demo"].delay == 0 and cfg["demo"].options["count"] == 5
    s = get_settings()
    assert cfg["carsales"].delay == s.default_delay_seconds  # falls back to .env default
    assert cfg["carsales"].implemented is False
    assert cfg["facebook"].enabled is False


def test_enabled_sources_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("ENABLED_SOURCES", "facebook")
    get_settings.cache_clear()
    cfg = load_sources()
    assert cfg["facebook"].enabled and not cfg["demo"].enabled


def test_plugin_registry():
    assert plugin_exists("demo") and not plugin_exists("nope")
    assert load_plugin(load_sources()["demo"]).name == "demo"


def test_search_params_from_dict_ignores_junk():
    p = SearchParams.from_dict({"make": "Toyota", "states": "vic, nsw", "colour": "red", "km_max": ""})
    assert p.make == "Toyota" and p.states == ["VIC", "NSW"] and p.km_max is None
    assert p.to_dict() == {"make": "Toyota", "states": ["VIC", "NSW"], "max_results": 200}


def test_make_aliases():
    assert normalise_make("VW") == "Volkswagen"
    assert normalise_make("volkswagon") == "Volkswagen"
    assert normalise_make("mercedes benz") == "Mercedes-Benz"
    assert normalise_make("TOYOTA") == "Toyota"
    assert normalise_make("bmw") == "BMW"
    assert normalise_make("some new brand") == "Some New Brand"
    assert normalise_make("  ") is None


def test_model_aliases():
    assert normalise_model("Toyota", "hilux") == "HiLux"
    assert normalise_model("Mazda", "CX5") == "CX-5"
    assert normalise_model("Mazda", "mazda3") == "3"
    assert normalise_model("Hyundai", "i30") == "i30"
    assert normalise_model("Unknown", "corolla cross") == "Corolla Cross"


def test_state():
    assert normalise_state("Victoria") == "VIC" and normalise_state("nsw") == "NSW"
    assert normalise_state("Narnia") is None
