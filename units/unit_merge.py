"""暖语连续消息合并（防抖）单元 (TMEAAA-595)。

在最早的消息事件阶段做 per-session trailing debounce：首个「可合并」消息作为
leader 被 ``stop_event()`` 挂起，窗口内到达的后续消息被吸收并重置计时；静默
到期后把合并文本写回 leader 事件并重新入队，走 AstrBot 正常 pipeline（含人格、
记忆注入与工具），**绝不接管生成**。

fail-closed：门内任何异常一律不 stop、放行；定时器异常立即释放；插件
``terminate()`` 取消任务并尽力释放所有挂起 leader。绝不吞消息。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File, Image, Plain, Record, Reply, Video

try:
    from ..config import DEFAULT_CONFIG_FLAT
except ImportError:
    from config import DEFAULT_CONFIG_FLAT

# 含这些消息段的消息不参与合并（各自单独走）。文本段（含 @ 本身在 message_str
# 里的占位）可安全合并；仅非文本段因重入队改写为纯 Plain 会丢内容而排除。
_NON_MERGE_COMPONENT_TYPES = (Image, File, Record, Video, Reply)

# 释放标记：重入队的事件带此 extra，合并门必须跳过（避免二次吸收）。
_MERGE_RELEASE_EXTRA = "_merge_release"

# 主动投递标记：重入队结果已由 context.send_message 送出，避免重复投递。
_MERGE_DELIVERED_EXTRA = "_merge_proactive_delivered"

# 需要补偿投递的平台：这些平台的请求结束后 back queue 会被移除，respond 阶段
# 的 event.send/send_streaming 会静默丢帧（TMEAAA-608）。
_MERGE_DELIVER_PLATFORMS = ("webchat",)

# 打字信号 extra 约定（优先复用）；AstrBot 4.28.1 无入站打字事件，无信号即降级。
_MERGE_TYPING_EXTRA = "user_typing"

# 事件/消息对象上可能的打字字段名（只读，不调用可调用对象，避免副作用）。
_MERGE_TYPING_FIELDS = ("user_typing", "is_typing", "typing", "is_composing", "composing")


class MergeUnitsMixin:
    """连续消息合并（防抖）核心。"""

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _merge_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("merge_enabled"), DEFAULT_CONFIG_FLAT["merge_enabled"]
        )

    def _merge_window_sec(self, session_key: str) -> int:
        key = (
            "merge_group_window_sec"
            if str(session_key).startswith("group:")
            else "merge_window_sec"
        )
        return max(1, int(self.config.get(key, DEFAULT_CONFIG_FLAT[key]) or 1))

    def _merge_max_wait_sec(self) -> int:
        return max(
            1,
            int(
                self.config.get("merge_max_wait_sec")
                or DEFAULT_CONFIG_FLAT["merge_max_wait_sec"]
            ),
        )

    def _merge_max_chars(self) -> int:
        return max(
            1,
            int(
                self.config.get("merge_max_chars")
                or DEFAULT_CONFIG_FLAT["merge_max_chars"]
            ),
        )

    def _merge_typing_extend_sec(self) -> float:
        try:
            value = self.config.get("merge_typing_extend_sec")
        except Exception:
            value = None
        if value is None:
            value = DEFAULT_CONFIG_FLAT["merge_typing_extend_sec"]
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return float(DEFAULT_CONFIG_FLAT["merge_typing_extend_sec"])

    # ------------------------------------------------------------------
    # 会话态
    # ------------------------------------------------------------------
    def _merge_state(self) -> Dict[str, Dict[str, Any]]:
        state = getattr(self, "_merge_sessions", None)
        if not isinstance(state, dict):
            state = {}
            self._merge_sessions = state
        return state

    # ------------------------------------------------------------------
    # 打字信号（可测接口 + 降级）
    # ------------------------------------------------------------------
    def _merge_typing_signal(self, event: AstrMessageEvent) -> bool:
        """读事件上的打字/正在输入信号；无则 False（fail-safe）。

        优先复用 ``event.get_extra("user_typing")`` 约定，其次读 event 及
        ``message_obj`` 上 typing/composing 类字段。只读属性值，不调用可调用对象
        （避免触发副作用）；任何异常一律返回 False。AstrBot 4.28.1 无入站打字
        事件，故默认不生效。
        """
        try:
            if event is None:
                return False
            get_extra = getattr(event, "get_extra", None)
            if callable(get_extra):
                try:
                    if get_extra(_MERGE_TYPING_EXTRA):
                        return True
                except Exception:
                    pass
            for owner in (event, getattr(event, "message_obj", None)):
                if owner is None:
                    continue
                for name in _MERGE_TYPING_FIELDS:
                    try:
                        value = getattr(owner, name, None)
                    except Exception:
                        continue
                    if value is None or callable(value):
                        continue
                    if value:
                        return True
            return False
        except Exception as exc:
            self._debug(f"merge typing signal error err={exc}")
            return False

    def _merge_note_typing(
        self,
        umo: str | None = None,
        session_key: str | None = None,
    ) -> bool:
        """程序化延长当前挂起 leader 的窗口（供适配/其它插件/QA 调用）。

        新截止 = ``min(max(当前截止, now + merge_typing_extend_sec),
        hard_deadline)``；受 ``merge_max_wait_sec`` 上限约束，绝不突破。每次延长
        都会重排定时器。无挂起 leader / 无可延长空间 / 异常 → False。
        """
        try:
            if not self._merge_enabled():
                return False
            key = self._merge_resolve_key(umo, session_key)
            if not key:
                return False
            state = self._merge_state()
            row = state.get(key)
            if not isinstance(row, dict):
                return False
            now_ts = self._now().timestamp()
            hard_deadline = float(row.get("hard_deadline", 0.0) or 0.0)
            current = float(row.get("deadline", 0.0) or 0.0)
            target = now_ts + self._merge_typing_extend_sec()
            new_deadline = min(max(current, target), hard_deadline)
            if new_deadline <= current:
                return False
            row["deadline"] = new_deadline
            self._schedule_merge_flush(key)
            self._debug(
                f"merge typing extend session={key} "
                f"deadline={self._fmt_ts(new_deadline)}"
            )
            return True
        except Exception as exc:
            self._debug(f"merge note typing error err={exc}")
            return False

    def _merge_resolve_key(
        self, umo: str | None, session_key: str | None
    ) -> str:
        """由显式 session_key 或 umo 解析合并会话键（best-effort）。"""
        key = str(session_key or "").strip()
        if key:
            return key
        raw = str(umo or "").strip()
        if not raw:
            return ""
        if raw.startswith("private:") or raw.startswith("group:"):
            return raw
        state = self._merge_state()
        if raw in state:
            return raw
        if ":group:" in raw:
            return f"group:{raw.rsplit(':', 1)[-1]}"
        if "!group!" in raw:
            return f"group:{raw.rsplit('!', 1)[-1]}"
        return ""

    # ------------------------------------------------------------------
    # 默认 LLM 抑制开关
    # ------------------------------------------------------------------
    def _merge_set_call_llm(self, event: AstrMessageEvent, value: bool) -> None:
        """显式设置事件是否触发默认 LLM（兼容 property/method 两种 API）。

        AstrBot 4.28.1 ``process_stage/stage.py`` 判定 ``not event.call_llm`` 才走
        默认 LLM，故 ``True`` = 跳过默认 LLM（挂起/吸收），``False`` = 恢复。显式
        设置而非依赖「stopped result」副作用。任何异常一律忽略（fail-safe）。
        """
        set_call = getattr(event, "should_call_llm", None)
        if callable(set_call):
            try:
                set_call(value)
            except Exception:
                pass
        try:
            if hasattr(event, "call_llm"):
                event.call_llm = value
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 补偿投递：重入队后 respond 阶段在 webchat 上会丢帧
    # ------------------------------------------------------------------
    def _merge_platform_id(self, event: AstrMessageEvent) -> str:
        """取平台 id（兼容 get_platform_id / get_platform_name / 缺失）。"""
        for name in ("get_platform_id", "get_platform_name"):
            getter = getattr(event, name, None)
            if not callable(getter):
                continue
            try:
                value = getter()
            except Exception:
                continue
            if value:
                return str(value)
        return ""

    def _merge_needs_proactive_delivery(self, event: AstrMessageEvent) -> bool:
        """重入队的合并事件在该平台上必须改走主动路径（否则静默丢帧）。"""
        if not self._merge_is_release(event):
            return False
        return self._merge_platform_id(event) in _MERGE_DELIVER_PLATFORMS

    def _merge_is_release(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_extra(_MERGE_RELEASE_EXTRA))
        except Exception:
            return False

    def _merge_build_chain(self, source: Any) -> MessageChain:
        """由结果/链对象构造 MessageChain（兼容不同构造签名）。"""
        components = list(getattr(source, "chain", None) or [])
        try:
            return MessageChain(chain=components)
        except Exception:
            chain = MessageChain()
            try:
                chain.chain = components
            except Exception:
                pass
            return chain

    async def _merge_deliver_chain(
        self, event: AstrMessageEvent, source: Any
    ) -> bool:
        """重入队事件在 webchat 上改用 ``context.send_message`` 主动投递。

        webchat 请求结束后其 back queue 已被 Live Chat 服务移除，respond 阶段的
        ``event.send``/``send_streaming`` 会静默丢帧；主动路径会广播会话订阅并落库。
        返回 True 表示已由主动路径处理（调用方不应再 event.send）；非合并 / 非目标
        平台 / 无 umo / 投递失败 → False，调用方回退原路径（fail-safe，绝不吞消息）。
        """
        if not self._merge_needs_proactive_delivery(event):
            return False
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return False
        sender = getattr(self.context, "send_message", None)
        if not callable(sender):
            return False
        try:
            delivered = await sender(umo, self._merge_build_chain(source))
        except Exception as exc:
            self._debug(f"merge proactive deliver failed: {exc}")
            return False
        return bool(delivered)

    async def _merge_deliver_decorated_result(self, event: AstrMessageEvent) -> bool:
        """发送前钩子：重入队合并结果在 webchat 上改走主动路径并抑制原结果。

        仅在结果仍未被分段逻辑接管时调用；成功投递后清空结果，避免 respond 阶段
        再走一次注定丢失的 ``event.send``。失败一律返回 False（保留结果，走原路径）。
        """
        try:
            if event.get_extra(_MERGE_DELIVERED_EXTRA):
                return False
            if event.get_extra("_kanjyou_output_seg_done"):
                # 分段逻辑已接管并投递过本事件，避免重复投递。
                return False
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return False
            is_llm = getattr(result, "is_llm_result", None)
            if callable(is_llm) and not is_llm():
                return False
            if not await self._merge_deliver_chain(event, result):
                return False
            event.set_extra(_MERGE_DELIVERED_EXTRA, True)
            event.clear_result()
            try:
                await self._evt_after_message_sent(event)
            except Exception:
                pass
            self._debug(
                f"merge proactive deliver session={self._session_key(event) or '-'} "
                f"comps={len(result.chain)}"
            )
            return True
        except Exception as exc:
            self._debug(f"merge proactive deliver hook failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # 合并门：返回 True 表示本条消息已被消费（调用方应直接 return）
    # ------------------------------------------------------------------
    async def _evt_merge_gate(self, event: AstrMessageEvent) -> bool:
        try:
            return self._merge_gate(event)
        except Exception as exc:
            # fail-closed：绝不因为异常而吞掉消息。
            self._debug(
                f"merge gate error session={self._session_key(event) or '-'} err={exc}"
            )
            return False

    def _merge_gate(self, event: AstrMessageEvent) -> bool:
        if not self._merge_enabled():
            return False
        if event.get_extra(_MERGE_RELEASE_EXTRA):
            # 重入队的 leader 事件，直接放行给正常 pipeline。
            return False

        session_key = self._session_key(event)
        if not session_key:
            return False
        state = self._merge_state()

        text = self._extract_event_text(event)
        if not self._merge_is_mergeable(event, text):
            # 不合并：若当前有挂起 leader，先立即释放，再让本条正常走（保序）。
            if session_key in state:
                self._release_merge_leader(session_key, "exempt")
            return False

        now_ts = self._now().timestamp()
        row = state.get(session_key)
        if isinstance(row, dict):
            hard_deadline = float(row.get("hard_deadline", 0.0) or 0.0)
            if now_ts >= hard_deadline:
                # 防御：触顶但定时器未及释放 → 连同本条立即释放，只发一次。
                parts = row.get("parts")
                if isinstance(parts, list) and text:
                    parts.append(text)
                self._release_merge_leader(session_key, "hard_deadline")
                return True
            parts = row.get("parts")
            if not isinstance(parts, list):
                parts = []
                row["parts"] = parts
            if text:
                parts.append(text)
            row["deadline"] = min(
                now_ts + self._merge_window_sec(session_key), hard_deadline
            )
            if self._merge_typing_signal(event):
                # 携带打字信号 → 在当前窗口基础上再延长（受 max_wait 约束）。
                self._merge_note_typing(session_key=session_key)
            else:
                self._schedule_merge_flush(session_key)
            self._merge_set_call_llm(event, True)
            event.stop_event()
            self._debug(
                f"merge absorb session={session_key} parts={len(parts)} "
                f"deadline={self._fmt_ts(row['deadline'])}"
            )
            return True

        window = self._merge_window_sec(session_key)
        hard_deadline = now_ts + self._merge_max_wait_sec()
        state[session_key] = {
            "leader_event": event,
            "parts": [text],
            "first_at": now_ts,
            "deadline": min(now_ts + window, hard_deadline),
            "hard_deadline": hard_deadline,
            "task": None,
        }
        if self._merge_typing_signal(event):
            self._merge_note_typing(session_key=session_key)
        else:
            self._schedule_merge_flush(session_key)
        self._merge_set_call_llm(event, True)
        event.stop_event()
        self._debug(f"merge leader session={session_key} text={text}")
        return True

    def _merge_is_mergeable(self, event: AstrMessageEvent, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if self._is_command_like_text(t) or self._is_plugin_command_text(t):
            return False
        if len(t) > self._merge_max_chars():
            return False
        if not getattr(event, "is_at_or_wake_command", False):
            # 群内未 @/未唤醒：本就不回，绝不挂起。
            return False
        return not self._merge_has_blocking_component(event)

    def _merge_has_blocking_component(self, event: AstrMessageEvent) -> bool:
        messages = None
        getter = getattr(event, "get_messages", None)
        if callable(getter):
            try:
                messages = getter()
            except Exception:
                messages = None
        if messages is None:
            messages = getattr(getattr(event, "message_obj", None), "message", None)
        if not isinstance(messages, list):
            return False
        return any(isinstance(seg, _NON_MERGE_COMPONENT_TYPES) for seg in messages)

    # ------------------------------------------------------------------
    # 定时器 + 释放 + 重入队
    # ------------------------------------------------------------------
    def _schedule_merge_flush(self, session_key: str) -> None:
        state = self._merge_state()
        row = state.get(session_key)
        if not isinstance(row, dict):
            return
        old = row.get("task")
        if isinstance(old, asyncio.Task) and not old.done():
            old.cancel()

        async def _runner():
            try:
                deadline = float(row.get("deadline", 0.0) or 0.0)
                delay = max(0.05, deadline - self._now().timestamp())
                await asyncio.sleep(delay)
                self._release_merge_leader(session_key, "window")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._debug(f"merge timer error session={session_key} err={exc}")
                try:
                    self._release_merge_leader(session_key, "timer_error")
                except Exception:
                    pass

        try:
            row["task"] = asyncio.create_task(_runner())
        except Exception as exc:
            self._debug(f"merge schedule failed session={session_key} err={exc}")
            self._release_merge_leader(session_key, "schedule_error")

    def _release_merge_leader(self, session_key: str, reason: str) -> bool:
        state = self._merge_state()
        row = state.pop(session_key, None)
        if not isinstance(row, dict):
            return False
        task = row.get("task")
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
        parts = row.get("parts")
        if not isinstance(parts, list):
            parts = []
        merged = "\n".join(str(p) for p in parts if str(p))
        event = row.get("leader_event")
        self._debug(
            f"merge release session={session_key} reason={reason} parts={len(parts)}"
        )
        if event is None:
            return True
        try:
            self._merge_requeue(event, merged)
        except Exception as exc:
            # 尽力保持事件可继续，绝不静默丢弃。
            self._debug(f"merge requeue failed session={session_key} err={exc}")
            try:
                event.continue_event()
                event.clear_result()
                self._merge_reset_send_flags(event)
                self._merge_set_call_llm(event, False)
            except Exception:
                pass
            return False
        return True

    def _merge_reset_send_flags(self, event: AstrMessageEvent) -> None:
        """复位「已发送」标记，使重入队事件重新具备触发默认 LLM 的资格。

        AstrBot 4.28.1 会对 *stopped* 的 webchat/wecom 事件在 pipeline 尾部补发一个
        空帧（``scheduler.py`` 的 ``await event.send(None)``），它把
        ``_has_send_oper`` 置 True；重入队时若不复位，
        ``process_stage/stage.py`` 的 ``not event._has_send_oper`` 为假 → 默认 LLM 被
        跳过、无回复（TMEAAA-597）。只复位发送标记，不动 ``call_llm``/结果等状态。
        用 ``setattr`` 访问平台私有标记，缺失/异常一律忽略（fail-safe）。
        """
        try:
            setattr(event, "_has_send_oper", False)
        except Exception:
            pass

    def _merge_requeue(self, event: AstrMessageEvent, merged: str) -> None:
        event.continue_event()
        event.clear_result()
        self._merge_reset_send_flags(event)
        # 恢复默认 LLM 资格：挂起时显式置 True，重入队必须复位否则消息被吞。
        self._merge_set_call_llm(event, False)
        if self._merge_platform_id(event) in _MERGE_DELIVER_PLATFORMS:
            # 延迟合并回复不再流式：webchat 重入队后流式帧同样会丢，且流式结果
            # 不触发 on_decorating_result，无法改走主动路径（TMEAAA-608）。
            try:
                event.set_extra("enable_streaming", False)
            except Exception:
                pass
        try:
            event.message_str = merged
        except Exception:
            pass
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            try:
                msg_obj.message = [Plain(merged)]
            except Exception:
                pass
            try:
                msg_obj.message_str = merged
            except Exception:
                pass
        event.set_extra(_MERGE_RELEASE_EXTRA, True)
        self.context.get_event_queue().put_nowait(event)

    def _merge_shutdown(self) -> None:
        state = getattr(self, "_merge_sessions", None)
        if not isinstance(state, dict):
            return
        for session_key in list(state.keys()):
            try:
                self._release_merge_leader(session_key, "shutdown")
            except Exception as exc:
                self._debug(
                    f"merge shutdown release failed session={session_key} err={exc}"
                )
