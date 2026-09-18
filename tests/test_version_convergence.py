"""v2.2.0 配置/文档/版本收敛校验 (TMEAAA-400)。

锁定版本号三处一致（metadata.yaml / config.py / README），并确保
DEFAULT_CONFIG 与 _conf_schema.json 的共享字段默认值完全一致。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "2.2.0"


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
    mismatches = {
        key: (field.get("default"), config.DEFAULT_CONFIG[key])
        for key, field in schema.items()
        if key in config.DEFAULT_CONFIG
        and field.get("default") != config.DEFAULT_CONFIG[key]
    }
    assert mismatches == {}
