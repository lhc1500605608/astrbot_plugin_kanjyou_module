"""Cross-instance authorization checks through public command entry points."""

import asyncio
from types import SimpleNamespace


def _event(instance, *, group_id="", sender_id="42", thread="42"):
    kind = "GroupMessage" if group_id else "FriendMessage"
    return SimpleNamespace(
        message_obj=SimpleNamespace(
            group_id=group_id, sender=SimpleNamespace(user_id=sender_id)
        ),
        unified_msg_origin=f"{instance}:{kind}:{group_id or thread}",
        get_sender_id=lambda: sender_id,
        get_platform_id=lambda: instance,
        plain_result=lambda text: text,
        should_call_llm=lambda value: None,
        stop_event=lambda: None,
    )


def test_group_command_grants_only_current_platform_instance(plugin):
    first = _event("onebot-primary", group_id="42")
    second = _event("onebot-secondary", group_id="42")

    async def scenario():
        async for _ in plugin._cmd_idle_wl_add_group(first, "42"):
            pass

    asyncio.run(scenario())
    assert plugin._is_whitelisted(first)
    assert not plugin._is_whitelisted(second)


def test_private_command_grants_only_current_platform_instance(plugin):
    first = _event("onebot-primary")
    second = _event("onebot-secondary")

    async def scenario():
        async for _ in plugin._cmd_idle_wl_add_private(first, "42"):
            pass

    asyncio.run(scenario())
    assert plugin._is_whitelisted(first)
    assert not plugin._is_whitelisted(second)
