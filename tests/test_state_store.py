import json
import os

from src.state_store import load_injection_active, save_injection_active


def test_load_returns_none_when_file_missing(tmp_path):
    config_path = str(tmp_path / "config.json")
    assert load_injection_active(config_path) is None


def test_save_then_load_roundtrips(tmp_path):
    config_path = str(tmp_path / "config.json")
    save_injection_active(config_path, True)
    assert load_injection_active(config_path) is True
    save_injection_active(config_path, False)
    assert load_injection_active(config_path) is False


def test_state_file_lives_next_to_config(tmp_path):
    config_path = str(tmp_path / "config.json")
    save_injection_active(config_path, True)
    assert os.path.exists(str(tmp_path / "state.json"))


def test_load_returns_none_on_corrupt_json(tmp_path):
    config_path = str(tmp_path / "config.json")
    state_path = tmp_path / "state.json"
    state_path.write_text("not valid json{{{", encoding="utf-8")
    assert load_injection_active(config_path) is None


def test_load_returns_none_when_key_missing_or_wrong_type(tmp_path):
    config_path = str(tmp_path / "config.json")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"injection_active": "yes"}), encoding="utf-8")
    assert load_injection_active(config_path) is None
    state_path.write_text(json.dumps({"other_key": True}), encoding="utf-8")
    assert load_injection_active(config_path) is None
