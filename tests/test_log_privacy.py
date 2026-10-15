"""Debug sinks receive only structural diagnostics for synthetic sensitive inputs."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from test_isolation import _message
from test_merge import _make_plugin
from test_platform_whitelist_boundary import _event


@pytest.mark.parametrize("text", ["PRIVATE_BODY_SYNTHETIC", "/external token=EXTERNAL_ARGUMENT_SECRET", "/idle_status PLUGIN_ARGUMENT_SECRET"])
def test_inbound_logs_omit_full_message_and_command_arguments(plugin, caplog, text):
    plugin.config["debug_log"] = True
    event = _event("A")
    event.message_str = text
    plugin.config["merge_enabled"] = False
    with caplog.at_level(logging.DEBUG, logger="astrbot"):
        asyncio.run(plugin._evt_on_all_message(event))
    assert text not in caplog.text
    assert "ARGUMENT_SECRET" not in caplog.text
    assert caplog.records


def test_merge_leader_logs_omit_body(plugin, caplog):
    plugin.config["debug_log"] = True
    instance = _make_plugin(plugin)

    async def scenario():
        event = _message("A:GroupMessage:42", text="MERGE_BODY_SECRET")
        await instance._evt_merge_gate(event)
        instance._merge_shutdown()

    with caplog.at_level(logging.DEBUG, logger="astrbot"):
        asyncio.run(scenario())
    assert "MERGE_BODY_SECRET" not in caplog.text
    assert "merge leader" in caplog.text


@pytest.mark.parametrize("failure", [False, True])
def test_generation_and_send_logs_omit_result_and_exception_contents(plugin, caplog, failure):
    plugin.config["debug_log"] = True
    plugin.config["holiday_api_enabled"] = False
    plugin.config["proactive_prompt_template"] = "safe prompt"
    plugin.config["proactive_segment_enabled"] = False
    async def provider(_umo):
        return "synthetic-provider"
    async def generate(**_kwargs):
        if failure:
            raise RuntimeError("PROVIDER_ERROR_SECRET")
        return SimpleNamespace(completion_text="GENERATED_CONTENT_SECRET")
    async def send(_umo, _chain):
        return True
    plugin.context.get_current_chat_provider_id = provider
    plugin.context.llm_generate = generate
    plugin.context.send_message = send
    with caplog.at_level(logging.DEBUG, logger="astrbot"):
        asyncio.run(plugin._send_proactive("A:FriendMessage:42", None, "private:A:FriendMessage:42", 100, {}))
        plugin._debug_lite_llm_issue_once(RuntimeError("LITE_ERROR_SECRET"))
    for marker in ("GENERATED_CONTENT_SECRET", "PROVIDER_ERROR_SECRET", "LITE_ERROR_SECRET"):
        assert marker not in caplog.text
    assert caplog.records
