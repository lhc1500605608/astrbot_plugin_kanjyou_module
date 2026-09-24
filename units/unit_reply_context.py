"""被动回复的主动消息上下文注入（TMEAAA-580）。

背景：主动消息经 `context.send_message` 发出，**不进入 AstrBot 会话历史**；
用户回复该主动内容时，被动生成请求里没有"刚说过什么"的信号，容易接不上话题。

本模块在插件内闭环修复（不依赖核心、不新增 companion 契约）：

- 主动发送成功时记下文案与时间（`_remember_proactive_text`）。
- 用户下一条消息到达时（`_arm_reply_context`，早于会话状态清理），若仍在
  「待回应时间窗」内，则把该主动文案暂存为待注入上下文；非当前消息即失效。
- 被动 `on_llm_request`（`_reply_context_on_llm_request`）把这段上下文作为
  **临时** 用户内容块追加到本轮请求，注入后立即清除，绝不重复注入。

群聊按群自身会话处理，只注入该群自己的主动文案，私聊内容不会进入群。
"""

from __future__ import annotations

from typing import Dict

# 视为「用户这条是在回应主动消息」的最长时间窗（秒）。
REPLY_CONTEXT_WINDOW_SEC = 2 * 3600

# 注入文本的最大长度，避免超长主动文案撑大请求。
REPLY_CONTEXT_MAX_CHARS = 600

_CONTEXT_KEYS = (
    "pending_proactive_text",
    "pending_proactive_at",
    "reply_context_text",
    "reply_context_at",
    "reply_context_llm_pending",
)


class ReplyContextUnitsMixin:
    # ------------------------------------------------------------ 主动发送侧
    def _remember_proactive_text(
        self, session_key: str, text: str, session: Dict | None = None
    ) -> None:
        """主动消息发送成功后记录文案，作为下一条被动回复的上下文候选。"""
        text = str(text or "").strip()
        if not session_key or not text:
            return
        targets = []
        live = self._sessions.get(session_key)
        if isinstance(live, dict):
            targets.append(live)
        if isinstance(session, dict) and session not in targets:
            targets.append(session)
        now_ts = self._now().timestamp()
        for s in targets:
            s["pending_proactive_text"] = text
            s["pending_proactive_at"] = now_ts
            # 新的一条主动消息覆盖旧的待注入上下文。
            s.pop("reply_context_text", None)
            s.pop("reply_context_at", None)
            s.pop("reply_context_llm_pending", None)

    # ------------------------------------------------------------ 入站捕获侧
    def _clear_reply_context(self, session: Dict) -> None:
        if not isinstance(session, dict):
            return
        for key in _CONTEXT_KEYS:
            session.pop(key, None)

    def _reply_context_window_sec(self) -> float:
        return float(REPLY_CONTEXT_WINDOW_SEC)

    def _arm_reply_context(
        self, session_key: str, session: Dict, now_ts: float
    ) -> None:
        """入站消息到达时（清理会话状态前）决定本轮是否注入主动上下文。

        只认「主动消息后第一条用户消息」：无论是否命中，都会消费掉待回应文案，
        避免后续消息被误贴。
        """
        if not isinstance(session, dict):
            return
        text = str(session.get("pending_proactive_text") or "").strip()
        at = float(session.get("pending_proactive_at") or 0.0)
        for key in (
            "pending_proactive_text",
            "pending_proactive_at",
            "reply_context_text",
            "reply_context_at",
            "reply_context_llm_pending",
        ):
            session.pop(key, None)
        if not text or not session_key:
            return
        if at <= 0 or at > now_ts or (now_ts - at) > self._reply_context_window_sec():
            return
        session["reply_context_text"] = text
        session["reply_context_at"] = at
        session["reply_context_llm_pending"] = True

    # ------------------------------------------------------------ 生成请求侧
    def _build_reply_context_block(self, text: str, is_group: bool) -> str:
        target = "群里" if is_group else "TA"
        snippet = text.strip()
        if len(snippet) > REPLY_CONTEXT_MAX_CHARS:
            snippet = snippet[:REPLY_CONTEXT_MAX_CHARS]
        return (
            "<reply_context>\n"
            f"你刚才主动对{target}说过：「{snippet}」\n"
            f"{target}这条消息很可能是在回应上面这句。若确实是在回应，请顺着这个话题"
            "自然接上，不要表现得像没说过，也不要用“你说的是什么/哪件事”这类反问。\n"
            "若这条明显与上面无关，就按当下内容正常回复即可。\n"
            "</reply_context>"
        )

    async def _reply_context_on_llm_request(self, event, req) -> None:
        """被动生成前注入待回应主动上下文（一次性，注入后清除）。"""
        try:
            session_key = self._session_key(event)
            if not session_key:
                return
            text = ""
            is_group = session_key.startswith("group:")
            async with self._lock:
                session = self._sessions.get(session_key)
                if not isinstance(session, dict) or not session.get(
                    "reply_context_llm_pending"
                ):
                    return
                text = str(session.get("reply_context_text") or "").strip()
                session["reply_context_llm_pending"] = False
                session.pop("reply_context_text", None)
                session.pop("reply_context_at", None)
                self._save_state()
            if not text:
                return
            parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(parts, list):
                return
            try:
                from astrbot.core.agent.message import TextPart
            except Exception:
                return
            block = self._build_reply_context_block(text, is_group)
            part = TextPart(text=block)
            mark = getattr(part, "mark_as_temp", None)
            if callable(mark):
                part = mark()
            parts.append(part)
            self._debug(
                f"inject reply context session={session_key} group={is_group} "
                f"chars={len(block)}"
            )
        except Exception as exc:
            self._debug(f"inject reply context failed err={exc}")
