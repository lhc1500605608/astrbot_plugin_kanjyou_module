"""AstrBot 4.23.2 compatibility audit tests (TMEAAA-395).

The tests assert the plugin only relies on the 4.23.2 API surface and that the
audited adjustments (image chain routing, metadata version declaration) hold.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _metadata() -> dict:
    data = {}
    for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def test_metadata_declares_astrbot_version():
    meta = _metadata()
    assert meta.get("astrbot_version") == ">=4.23,<5"
    assert meta.get("version") == "v2.2.0"


def test_metadata_version_matches_config():
    from astrbot_plugin_kanjyou_module.config import PLUGIN_VERSION

    assert _metadata()["version"] == f"v{PLUGIN_VERSION}"


def test_plugin_imports_and_instantiates(plugin):
    assert plugin is not None
    assert plugin.config.get("enabled") is True


def test_completion_text_prefers_completion_text_attr(plugin):
    class _Resp:
        completion_text = "  hello  "

    assert plugin._completion_to_text(_Resp()) == "hello"
    assert plugin._completion_to_text("plain string") == "plain string"


def test_session_key_private_and_group(plugin):
    class _Sender:
        user_id = "10001"

    class _Msg:
        def __init__(self, group_id=""):
            self.group_id = group_id
            self.sender = _Sender()

    class _Event:
        def __init__(self, group_id=""):
            self.message_obj = _Msg(group_id)

        def get_sender_id(self):
            return "10001"

    assert plugin._session_key(_Event()) == "private:10001"
    assert plugin._session_key(_Event(group_id="555")) == "group:555"


def test_send_image_uses_url_image_for_http(plugin):
    captured = {}

    async def _fake_t2i(_prompt):
        return "http://127.0.0.1:9966/api/file/abc.png"

    async def _fake_send(_umo, chain):
        captured["chain"] = chain
        return True

    plugin.text_to_image = _fake_t2i
    plugin.context.send_message = _fake_send

    ok = asyncio.run(plugin._send_image_reply("aiocqhttp:FriendMessage:1", "draw"))
    assert ok is True
    assert captured["chain"].last_method == "url_image"
    assert captured["chain"].chain[0].file == "http://127.0.0.1:9966/api/file/abc.png"


def test_send_image_uses_file_image_for_local_path(plugin):
    captured = {}

    async def _fake_t2i(_prompt):
        return "/tmp/render/abc.png"

    async def _fake_send(_umo, chain):
        captured["chain"] = chain
        return True

    plugin.text_to_image = _fake_t2i
    plugin.context.send_message = _fake_send

    ok = asyncio.run(plugin._send_image_reply("aiocqhttp:FriendMessage:1", "draw"))
    assert ok is True
    assert captured["chain"].last_method == "file_image"


def test_resolve_persona_prompt_reads_system_prompt(plugin):
    class _Persona:
        system_prompt = "  你是温柔的朋友  "
        prompt = None

    class _Manager:
        async def get_persona(self, persona_id):
            assert persona_id == "p1"
            return _Persona()

    plugin.config["persona_id"] = "p1"
    plugin.context.persona_manager = _Manager()

    assert asyncio.run(plugin._resolve_persona_prompt()) == "你是温柔的朋友"


def test_resolve_persona_prompt_falls_back_when_missing(plugin):
    class _Manager:
        async def get_persona(self, persona_id):
            raise ValueError("missing")

    plugin.config["persona_id"] = "nope"
    plugin.context.persona_manager = _Manager()

    assert asyncio.run(plugin._resolve_persona_prompt()) == "人格ID: nope"
