"""连续消息合并（防抖）单元测试 (TMEAAA-595)。

覆盖 plan §6 的 1（连发 3 条只发 1 次请求）、3（间隔>窗口各自走）、4（@/指令/
含图片不合并）、5（异常/超时 fail-closed），以及单条逐字节、``_merge_release``
不二次吸收、群内非唤醒不挂起。并覆盖打字延长（TMEAAA-596）：信号识别/降级、
程序化延长、定时器重排、max_wait 上限。
"""

from __future__ import annotations

import asyncio

PRIVATE_UMO = "webchat!u1!c1"
GROUP_UMO = "aiocqhttp:group:g1"


class _Sender:
    def __init__(self, user_id: str):
        self.user_id = user_id


class _MessageObj:
    def __init__(self, message=None, group_id: str = "", sender_id: str = "u1"):
        self.message = list(message or [])
        self.message_str = ""
        self.group_id = group_id
        self.sender = _Sender(sender_id)


class _FakeEvent:
    def __init__(
        self,
        text: str = "",
        umo: str = PRIVATE_UMO,
        group_id: str = "",
        sender_id: str = "u1",
        components=None,
        wake: bool = True,
    ):
        self.message_str = text
        self.message_obj = _MessageObj(components, group_id, sender_id)
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = wake
        self.call_llm = False
        self._extra: dict = {}
        self._stopped = False
        self._result = None
        # 平台层「已发送」标记：AstrBot 对 stopped 事件补发空帧后会置 True。
        self._has_send_oper = False

    def get_messages(self):
        return list(self.message_obj.message)

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extra
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def set_result(self, result):
        self._result = result

    def get_result(self):
        return self._result

    def clear_result(self):
        self._result = None

    def stop_event(self):
        self._stopped = True
        self._result = "stopped"

    def continue_event(self):
        self._stopped = False
        self._result = None

    def is_stopped(self):
        return self._stopped


class _FakeQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def _make_plugin(plugin):
    queue = _FakeQueue()
    plugin.context.get_event_queue = lambda: queue
    plugin._merge_queue = queue
    # 让定时器确定性且快速（不改变生产语义，仅测试缩短等待）。
    plugin._merge_window_sec = lambda _sk: 0.05
    plugin._merge_max_wait_sec = lambda: 1.0
    return plugin


def _key(event):
    if event.message_obj.group_id:
        return f"group:{event.message_obj.group_id}"
    return f"private:{event.message_obj.sender.user_id}"


def test_merge_three_messages_into_one_request(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        e1 = _FakeEvent("你好")
        e2 = _FakeEvent("在吗")
        e3 = _FakeEvent("想聊聊")
        assert await instance._evt_merge_gate(e1) is True
        assert await instance._evt_merge_gate(e2) is True
        assert await instance._evt_merge_gate(e3) is True
        # 每次吸收都挂起，未重入队。
        assert e1.is_stopped() and e2.is_stopped() and e3.is_stopped()
        assert instance._merge_queue.items == []
        instance._release_merge_leader(_key(e1), "test")
        assert len(instance._merge_queue.items) == 1
        released = instance._merge_queue.items[0]
        assert released is e1
        assert released.message_str == "你好\n在吗\n想聊聊"
        assert released.message_obj.message[0].text == "你好\n在吗\n想聊聊"
        assert released.get_extra("_merge_release") is True
        assert not released.is_stopped()

    asyncio.run(scenario())


def test_single_message_is_byte_identical(plugin):
    instance = _make_plugin(plugin)
    original = " 单条 消息 \n第二行 "  # 提取/写回走 strip 语义

    async def scenario():
        event = _FakeEvent(original)
        assert await instance._evt_merge_gate(event) is True
        instance._release_merge_leader(_key(event), "test")
        released = instance._merge_queue.items[0]
        assert released.message_str == original.strip()
        assert released.message_obj.message[0].text == original.strip()

    asyncio.run(scenario())


def test_released_event_is_not_reabsorbed(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        event = _FakeEvent("只此一条")
        await instance._evt_merge_gate(event)
        instance._release_merge_leader(_key(event), "test")
        released = instance._merge_queue.items[0]
        # 放行给正常 pipeline，不得再次被吸收/挂起。
        assert await instance._evt_merge_gate(released) is False
        assert not released.is_stopped()
        assert instance._merge_queue.items == [released]

    asyncio.run(scenario())


def test_timer_releases_after_window(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        event = _FakeEvent("定时释放")
        assert await instance._evt_merge_gate(event) is True
        assert instance._merge_queue.items == []
        await asyncio.sleep(0.25)
        assert len(instance._merge_queue.items) == 1
        assert instance._merge_queue.items[0].message_str == "定时释放"

    asyncio.run(scenario())


def test_separate_messages_after_window_each_flow(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        first = _FakeEvent("第一条")
        assert await instance._evt_merge_gate(first) is True
        await asyncio.sleep(0.2)  # 超过窗口：第一条已释放
        assert len(instance._merge_queue.items) == 1

        second = _FakeEvent("很久以后第二条")
        assert await instance._evt_merge_gate(second) is True
        await asyncio.sleep(0.2)
        assert len(instance._merge_queue.items) == 2
        assert instance._merge_queue.items[0].message_str == "第一条"
        assert instance._merge_queue.items[1].message_str == "很久以后第二条"

    asyncio.run(scenario())


def test_command_releases_leader_and_not_stopped(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        held = _FakeEvent("普通消息")
        assert await instance._evt_merge_gate(held) is True
        command = _FakeEvent("/idle_status")
        assert await instance._evt_merge_gate(command) is False
        assert not command.is_stopped()
        # 原有 leader 被立即释放，不吞。
        assert len(instance._merge_queue.items) == 1
        assert instance._merge_queue.items[0] is held

    asyncio.run(scenario())


def test_image_message_not_merged(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        from astrbot.api.message_components import Image

        event = _FakeEvent("带图", components=[Image(file="a.png")])
        assert await instance._evt_merge_gate(event) is False
        assert not event.is_stopped()
        assert instance._merge_queue.items == []

    asyncio.run(scenario())


def test_group_non_wake_not_held(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        event = _FakeEvent("群内闲聊", umo=GROUP_UMO, group_id="g1", wake=False)
        assert await instance._evt_merge_gate(event) is False
        assert not event.is_stopped()
        assert instance._merge_queue.items == []

    asyncio.run(scenario())


def test_long_message_not_merged(plugin):
    instance = _make_plugin(plugin)
    plugin.config["merge_max_chars"] = 5

    async def scenario():
        event = _FakeEvent("这是一条很长很长的消息")
        assert await instance._evt_merge_gate(event) is False
        assert not event.is_stopped()

    asyncio.run(scenario())


def test_disabled_passthrough(plugin):
    instance = _make_plugin(plugin)
    plugin.config["merge_enabled"] = False

    async def scenario():
        event = _FakeEvent("直通")
        assert await instance._evt_merge_gate(event) is False
        assert not event.is_stopped()
        assert instance._merge_queue.items == []

    asyncio.run(scenario())


def test_gate_exception_is_fail_closed(plugin):
    instance = _make_plugin(plugin)

    def _boom(_event):
        raise RuntimeError("boom")

    instance._extract_event_text = _boom

    async def scenario():
        event = _FakeEvent("异常")
        assert await instance._evt_merge_gate(event) is False
        assert not event.is_stopped()

    asyncio.run(scenario())


def test_requeue_resets_send_flag(plugin):
    """TMEAAA-597：stopped 首轮被平台补发空帧置 _has_send_oper=True 后，重入队
    必须复位，否则 ProcessStage 跳过默认 LLM（无回复）。"""
    instance = _make_plugin(plugin)

    async def scenario():
        event = _FakeEvent("连发合并")
        await instance._evt_merge_gate(event)
        # 模拟 scheduler.execute 尾部对 stopped 事件补发空帧。
        event._has_send_oper = True
        assert event.is_stopped()
        instance._release_merge_leader(_key(event), "test")
        released = instance._merge_queue.items[0]
        assert released is event
        # 复位后 ProcessStage 的 `not event._has_send_oper` 才为真 → 触发 LLM。
        assert released._has_send_oper is False
        assert not released.is_stopped()
        assert released.get_extra("_merge_release") is True

    asyncio.run(scenario())


def test_requeue_reset_survives_missing_flag(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        event = _FakeEvent("无字段")
        await instance._evt_merge_gate(event)
        del event._has_send_oper  # 老/异形事件没有该字段
        instance._release_merge_leader(_key(event), "test")
        assert len(instance._merge_queue.items) == 1

    asyncio.run(scenario())


def test_requeue_failure_leaves_event_resumed(plugin):
    instance = _make_plugin(plugin)

    def _boom(_event, _merged):
        raise RuntimeError("requeue boom")

    instance._merge_requeue = _boom

    async def scenario():
        event = _FakeEvent("释放失败")
        await instance._evt_merge_gate(event)
        instance._release_merge_leader(_key(event), "test")
        # 释放失败时尽力恢复事件，不静默丢弃。
        assert not event.is_stopped()
        assert instance._merge_queue.items == []

    asyncio.run(scenario())


def test_group_window_config_used(plugin):
    instance = plugin
    assert instance._merge_window_sec("group:g1") == plugin.config[
        "merge_group_window_sec"
    ]
    assert instance._merge_window_sec("private:u1") == plugin.config["merge_window_sec"]


# ----------------------------------------------------------------------
# 打字延长信号（TMEAAA-596）
# ----------------------------------------------------------------------
def test_typing_signal_from_extra(plugin):
    instance = _make_plugin(plugin)
    event = _FakeEvent("hi")
    assert instance._merge_typing_signal(event) is False
    event.set_extra("user_typing", True)
    assert instance._merge_typing_signal(event) is True


def test_typing_signal_from_attributes(plugin):
    instance = _make_plugin(plugin)
    on_event = _FakeEvent("hi")
    on_event.is_typing = True
    assert instance._merge_typing_signal(on_event) is True

    on_msg = _FakeEvent("hi")
    on_msg.message_obj.composing = True
    assert instance._merge_typing_signal(on_msg) is True


def test_typing_signal_ignores_callable(plugin):
    instance = _make_plugin(plugin)
    called = {"n": 0}

    def _compose():
        called["n"] += 1
        return True

    event = _FakeEvent("hi")
    event.typing = _compose
    assert instance._merge_typing_signal(event) is False
    assert called["n"] == 0


def test_typing_signal_fail_safe(plugin):
    instance = _make_plugin(plugin)

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    event = _FakeEvent("hi")
    event.get_extra = _boom
    assert instance._merge_typing_signal(event) is False


def test_note_typing_extends_deadline(plugin):
    instance = _make_plugin(plugin)
    instance._merge_typing_extend_sec = lambda: 0.5

    async def scenario():
        event = _FakeEvent("hi")
        await instance._evt_merge_gate(event)
        key = _key(event)
        row = instance._merge_sessions[key]
        base = row["deadline"]
        assert instance._merge_note_typing(session_key=key) is True
        assert row["deadline"] > base + 0.2
        assert row["deadline"] <= row["hard_deadline"]

    asyncio.run(scenario())


def test_note_typing_capped_by_max_wait(plugin):
    instance = _make_plugin(plugin)
    instance._merge_typing_extend_sec = lambda: 999.0

    async def scenario():
        event = _FakeEvent("hi")
        await instance._evt_merge_gate(event)
        key = _key(event)
        row = instance._merge_sessions[key]
        assert instance._merge_note_typing(session_key=key) is True
        assert row["deadline"] == row["hard_deadline"]
        # 已触顶：再延长无效。
        assert instance._merge_note_typing(session_key=key) is False
        assert row["deadline"] == row["hard_deadline"]

    asyncio.run(scenario())


def test_note_typing_without_leader_returns_false(plugin):
    instance = _make_plugin(plugin)
    assert instance._merge_note_typing(session_key="private:none") is False
    assert instance._merge_note_typing(umo="webchat!u1!c1") is False


def test_note_typing_resolves_group_umo(plugin):
    instance = _make_plugin(plugin)
    instance._merge_typing_extend_sec = lambda: 0.5

    async def scenario():
        event = _FakeEvent("hi", umo="aiocqhttp:group:g1", group_id="g1")
        await instance._evt_merge_gate(event)
        assert instance._merge_note_typing(umo="aiocqhttp:group:g1") is True

    asyncio.run(scenario())


def test_gate_typing_signal_extends_deadline(plugin):
    instance = _make_plugin(plugin)
    instance._merge_typing_extend_sec = lambda: 0.5

    async def scenario():
        first = _FakeEvent("hi")
        await instance._evt_merge_gate(first)
        row = instance._merge_sessions[_key(first)]
        base = row["deadline"]

        typed = _FakeEvent("again")
        typed.set_extra("user_typing", True)
        assert await instance._evt_merge_gate(typed) is True
        assert row["deadline"] > base + 0.2

    asyncio.run(scenario())


def test_absorb_without_signal_keeps_window(plugin):
    instance = _make_plugin(plugin)
    instance._merge_typing_extend_sec = lambda: 0.5

    async def scenario():
        first = _FakeEvent("a")
        await instance._evt_merge_gate(first)
        row = instance._merge_sessions[_key(first)]
        second = _FakeEvent("b")
        assert await instance._evt_merge_gate(second) is True
        # 无信号：仅重置为窗口（≈0.05），不得延长到 extend（0.5）。
        assert row["deadline"] - row["first_at"] < 0.2

    asyncio.run(scenario())


def test_note_typing_reschedules_timer(plugin):
    instance = _make_plugin(plugin)
    instance._merge_typing_extend_sec = lambda: 0.4

    async def scenario():
        event = _FakeEvent("hi")
        await instance._evt_merge_gate(event)
        assert instance._merge_note_typing(session_key=_key(event)) is True
        await asyncio.sleep(0.15)
        # 已过原窗口(0.05)，但因延长仍未发送。
        assert instance._merge_queue.items == []
        await asyncio.sleep(0.45)
        assert len(instance._merge_queue.items) == 1
        assert instance._merge_queue.items[0].message_str == "hi"

    asyncio.run(scenario())


def test_note_typing_disabled_returns_false(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        event = _FakeEvent("hi")
        await instance._evt_merge_gate(event)
        plugin.config["merge_enabled"] = False
        assert instance._merge_note_typing(session_key=_key(event)) is False

    asyncio.run(scenario())
