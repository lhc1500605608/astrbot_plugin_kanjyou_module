"""v2.2.0 配置/文档/版本收敛校验 (TMEAAA-400)。

锁定版本号三处一致（metadata.yaml / config.py / README），并确保
DEFAULT_CONFIG 与 _conf_schema.json 的共享字段默认值完全一致。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "2.13.0"


def _load_config():
    spec = importlib.util.spec_from_file_location(
        "kanjyou_config_probe", ROOT / "config.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _metadata() -> dict:
    data = {}
    for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def test_config_version_is_2_2_0():
    assert _load_config().PLUGIN_VERSION == EXPECTED_VERSION


def test_version_consistent_across_config_metadata_readme():
    config_version = _load_config().PLUGIN_VERSION
    metadata_version = _metadata().get("version")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert metadata_version == f"v{config_version}"
    assert f"version-v{config_version}-blue.svg" in readme


def test_schema_defaults_match_default_config():
    config = _load_config()
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def _check(schema_node: dict, conf_node: dict, path: str = "") -> None:
        for key, field in schema_node.items():
            sub_path = f"{path}.{key}" if path else key
            if not isinstance(field, dict):
                continue
            if field.get("type") == "object":
                assert isinstance(conf_node.get(key), dict), sub_path
                _check(field.get("items", {}), conf_node[key], sub_path)
            elif key in conf_node:
                assert field.get("default") == conf_node[key], sub_path

    _check(schema, config.DEFAULT_CONFIG)


def _assert_field_contract(spec: dict, path: str) -> None:
    for required in ("type", "description", "hint", "default"):
        assert required in spec, f"{path} missing {required}"


def test_persona_state_fields_are_structured_template_lists():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    emotion_items = schema["emotion"]["items"]
    for key in ("persona_state_custom", "persona_state_overrides"):
        field = emotion_items[key]
        # `dict` type crashes AstrBot 4.23-4.27 config loading.
        assert field["type"] == "object", key
        assert field["type"] != "dict"
        assert field["default"] == {}
        assert set(field["items"]) == {"default", "states"}
        _assert_field_contract(field["items"]["default"], f"{key}.default")
        assert field["items"]["default"]["type"] == "string"
        states = field["items"]["states"]
        _assert_field_contract(states, f"{key}.states")
        assert states["type"] == "template_list"
        assert states["default"] == []
        template = states["templates"]["state"]
        assert template["name"] and template["description"]
        for name, spec in template["items"].items():
            _assert_field_contract(spec, f"{key}.states.state.{name}")
        assert '"type": "dict"' not in json.dumps(field, ensure_ascii=False)


def test_schema_has_grouped_objects_and_schema_version():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    groups = {
        key: field
        for key, field in schema.items()
        if isinstance(field, dict) and field.get("type") == "object"
    }
    assert len(groups) >= 9
    assert schema.get("schema_version", {}).get("invisible") is True
    for key, field in groups.items():
        assert field.get("items"), key
        assert field.get("description"), key
