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
        platform_id: str = "webchat",
    ):
        self.message_str = text
        self.message_obj = _MessageObj(components, group_id, sender_id)
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = wake
        self.call_llm = False
        self._extra: dict = {}
        self._stopped = False
        self._result = None
        self._platform_id = platform_id
        # 平台层「已发送」标记：AstrBot 对 stopped 事件补发空帧后会置 True。
        self._has_send_oper = False

    def get_messages(self):
        return list(self.message_obj.message)

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_platform_id(self):
        return self._platform_id

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

    def should_call_llm(self, value: bool):
        self.call_llm = value

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


def _would_call_default_llm(event) -> bool:
    """计数桩：镜像 AstrBot ``process_stage/stage.py`` 的默认 LLM 触发条件。"""
    return (
        (not event.call_llm)
        and (not event._has_send_oper)
        and (not event.is_stopped())
    )


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


def test_absorbed_messages_suppress_default_llm(plugin):
    """TMEAAA-605：合并挂起须显式抑制默认 LLM；释放后恢复；只触发 1 次。"""
    instance = _make_plugin(plugin)

    async def scenario():
        events = [_FakeEvent("你好"), _FakeEvent("在吗"), _FakeEvent("想聊聊")]
        for event in events:
            assert await instance._evt_merge_gate(event) is True
            # 被吸收的每条都显式抑制默认 LLM 且已挂起。
            assert event.call_llm is True
            assert event.is_stopped()

        instance._release_merge_leader(_key(events[0]), "test")
        released = instance._merge_queue.items[0]
        assert released is events[0]
        # 释放后恢复默认 LLM 资格、事件未挂起。
        assert released.call_llm is False
        assert not released.is_stopped()
        # 合并结果只让默认 LLM 触发一次（吸收的 follower 不触发）。
        assert sum(1 for e in events if _would_call_default_llm(e)) == 1

    asyncio.run(scenario())


def test_at_component_still_merged(plugin):
    """TMEAAA-605：群聊 @bot 每条的 At 段不再阻断合并。"""
    instance = _make_plugin(plugin)

    async def scenario():
        from astrbot.api.message_components import At, Plain

        event = _FakeEvent(
            "你好",
            umo=GROUP_UMO,
            group_id="g1",
            components=[At(qq="bot"), Plain("你好")],
        )
        assert await instance._evt_merge_gate(event) is True
        assert event.is_stopped()
        assert event.call_llm is True

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
        assert event.call_llm is True  # 挂起时已显式抑制
        instance._release_merge_leader(_key(event), "test")
        # 释放失败时尽力恢复事件，不静默丢弃：call_llm 必须复位为 False 且未挂起。
        assert not event.is_stopped()
        assert event.call_llm is False
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


# ----------------------------------------------------------------------
# 补偿投递：重入队后 webchat respond 阶段丢帧（TMEAAA-608）
# ----------------------------------------------------------------------
class _LlmResult:
    """最小 MessageEventResult 替身：chain + is_llm_result()。"""

    def __init__(self, text: str = "QA608OK"):
        from astrbot.api.message_components import Plain

        self.chain = [Plain(text)]

    def is_llm_result(self) -> bool:
        return True


def _attach_send_message(instance, result=True, recorder=None):
    async def _send(umo, chain):
        if recorder is not None:
            recorder.append((umo, list(getattr(chain, "chain", []))))
        return result

    instance.context.send_message = _send


def test_requeue_disables_webchat_streaming(plugin):
    instance = _make_plugin(plugin)

    async def scenario():
        event = _FakeEvent("连发合并")
        await instance._evt_merge_gate(event)
        instance._release_merge_leader(_key(event), "test")
        released = instance._merge_queue.items[0]
        assert released.get_extra("enable_streaming") is False

    asyncio.run(scenario())


def test_deliver_proactive_on_webchat_clears_result(plugin):
    instance = _make_plugin(plugin)
    sent = []
    _attach_send_message(instance, result=True, recorder=sent)

    async def scenario():
        event = _FakeEvent("合并回复")
        event.set_extra("_merge_release", True)
        result = _LlmResult("QA608OK")
        event.set_result(result)
        assert await instance._merge_deliver_decorated_result(event) is True
        assert event.get_result() is None  # 已抑制 respond 阶段的原投递
        assert event.get_extra("_merge_proactive_delivered") is True
        assert len(sent) == 1
        assert sent[0][0] == event.unified_msg_origin
        assert sent[0][1][0].text == "QA608OK"

    asyncio.run(scenario())


def test_deliver_skips_non_webchat(plugin):
    instance = _make_plugin(plugin)
    sent = []
    _attach_send_message(instance, result=True, recorder=sent)

    async def scenario():
        event = _FakeEvent("合并回复", platform_id="aiocqhttp")
        event.set_extra("_merge_release", True)
        event.set_result(_LlmResult())
        # 非目标平台：保持原路径（respond event.send），不主动投递、不清结果。
        assert await instance._merge_deliver_decorated_result(event) is False
        assert event.get_result() is not None
        assert sent == []

    asyncio.run(scenario())


def test_deliver_skips_without_release_marker(plugin):
    instance = _make_plugin(plugin)
    sent = []
    _attach_send_message(instance, result=True, recorder=sent)

    async def scenario():
        event = _FakeEvent("普通回复")
        event.set_result(_LlmResult())
        assert await instance._merge_deliver_decorated_result(event) is False
        assert event.get_result() is not None
        assert sent == []

    asyncio.run(scenario())


def test_deliver_keeps_result_when_send_fails(plugin):
    instance = _make_plugin(plugin)
    _attach_send_message(instance, result=False)

    async def scenario():
        event = _FakeEvent("合并回复")
        event.set_extra("_merge_release", True)
        event.set_result(_LlmResult())
        # 主动投递失败 → 不吞结果，回退原路径（fail-safe）。
        assert await instance._merge_deliver_decorated_result(event) is False
        assert event.get_result() is not None
        assert event.get_extra("_merge_proactive_delivered") is None

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 引用回复不再阻断合并（TMEAAA-614）
# ----------------------------------------------------------------------
QQ_PRIVATE_UMO = "aiocqhttp:FriendMessage:10001"


def test_reply_component_still_merged(plugin):
    """QQ 私聊里「引用机器人再说话」的两条连续消息应合并成一条回复。"""
    instance = _make_plugin(plugin)

    async def scenario():
        from astrbot.api.message_components import Plain, Reply

        first = _FakeEvent(
            "你刚说的那个",
            umo=QQ_PRIVATE_UMO,
            platform_id="aiocqhttp",
            components=[Reply(id="1234"), Plain("你刚说的那个")],
        )
        second = _FakeEvent(
            "我不太明白",
            umo=QQ_PRIVATE_UMO,
            platform_id="aiocqhttp",
            components=[Reply(id="1234"), Plain("我不太明白")],
        )
        assert await instance._evt_merge_gate(first) is True
        assert await instance._evt_merge_gate(second) is True
        assert first.is_stopped() and second.is_stopped()

        instance._release_merge_leader(_key(first), "test")
        assert len(instance._merge_queue.items) == 1
        released = instance._merge_queue.items[0]
        assert released is first
        assert released.message_str == "你刚说的那个\n我不太明白"
        # 合并后重写为纯文本，合并结果只触发一次默认 LLM。
        assert len(released.message_obj.message) == 1
        assert sum(1 for e in (first, second) if _would_call_default_llm(e)) == 1

    asyncio.run(scenario())


def test_reply_component_never_blocks_merge(plugin):
    """带引用段的消息本身可合并（``_merge_has_blocking_component`` 不认它）。"""
    instance = _make_plugin(plugin)

    from astrbot.api.message_components import Plain, Reply

    event = _FakeEvent("引用一下", components=[Reply(id="9"), Plain("引用一下")])
    assert instance._merge_has_blocking_component(event) is False
    assert instance._merge_skip_reason(event, "引用一下") == ""


def test_media_components_still_block_merge(plugin):
    """图片/文件/语音/视频改写为纯文本会真丢内容，必须继续各自单独走。"""
    instance = _make_plugin(plugin)

    from astrbot.api import message_components as mc

    factories = {
        "Image": lambda: mc.Image(file="a.png"),
        "File": lambda: mc.File(file="a.pdf", name="a.pdf"),
        "Record": lambda: mc.Record(file="a.wav"),
        "Video": lambda: mc.Video(file="a.mp4"),
    }
    for name, factory in factories.items():
        event = _FakeEvent("带附件", components=[factory()])
        assert instance._merge_has_blocking_component(event) is True, name
        assert instance._merge_skip_reason(event, "带附件") == "has_media", name


def test_media_message_not_held_by_gate(plugin):
    """端到端：含媒体段的消息不挂起、不进合并队列。"""
    instance = _make_plugin(plugin)

    async def scenario():
        from astrbot.api.message_components import Image, Plain

        event = _FakeEvent(
            "看这张图", components=[Image(file="a.png"), Plain("看这张图")]
        )
        assert await instance._evt_merge_gate(event) is False
        assert not event.is_stopped()
        assert instance._merge_queue.items == []

    asyncio.run(scenario())


def test_skip_reason_codes(plugin):
    """每条不合并路径都有可观测的原因码；正常消息返回空串。"""
    instance = _make_plugin(plugin)
    plugin.config["merge_max_chars"] = 5

    cases = {
        "empty": _FakeEvent("   "),
        "command": _FakeEvent("/idle_status"),
        "too_long": _FakeEvent("这是一条很长很长的消息"),
        "not_wake": _FakeEvent("群内闲聊", umo=GROUP_UMO, group_id="g1", wake=False),
    }
    for expected, event in cases.items():
        assert instance._merge_skip_reason(event, event.message_str) == expected

    from astrbot.api.message_components import Image

    media = _FakeEvent("带图", components=[Image(file="a.png")])
    assert instance._merge_skip_reason(media, media.message_str) == "has_media"

    plain = _FakeEvent("普通消息")
    assert instance._merge_skip_reason(plain, "普通消息") == ""
    assert instance._merge_is_mergeable(plain, "普通消息") is True


def test_skip_reason_logged_in_plain_chinese(plugin):
    """跳过原因以用户可读中文写进调试日志，且按原因限流。"""
    instance = _make_plugin(plugin)
    logged: list[tuple[str, str]] = []
    instance._debug_throttled = lambda key, msg: logged.append((key, msg))

    async def scenario():
        assert await instance._evt_merge_gate(_FakeEvent("/idle_status")) is False

    asyncio.run(scenario())

    assert len(logged) == 1
    key, msg = logged[0]
    assert key == "merge_skip:command"
    # 面向用户可见的日志：不得出现内部术语。
    for term in ("Phase", "TMEAAA", "_merge", "unit_merge", "leader", "schema"):
        assert term not in msg, term
    assert "指令消息" in msg


def test_skip_reason_check_failure_is_fail_closed(plugin):
    """原因判定自身异常 → 视为可合并，绝不吞消息。"""
    instance = _make_plugin(plugin)

    def _boom(_text):
        raise RuntimeError("boom")

    instance._merge_max_chars = _boom
    assert instance._merge_skip_reason(_FakeEvent("普通消息"), "普通消息") == ""
    assert instance._merge_is_mergeable(_FakeEvent("普通消息"), "普通消息") is True
