"""Phase 2-A 情绪事件探测 + Phase 2-C expression 消费（kanjyou 侧）。

零 LLM：关键词/回执状态机判定 6 类事件（`gratitude` / `misunderstood` /
`sudden_warmth` / `cold_shoulder` / `valued_reply` / `ignored_proactive`），经
companion-core 的 ``record_emotion_event`` 落库；消费 ``emotion_state`` /
``expression`` 注入 prompt，并按 plan §4.1 与 ``persona_state`` 合并。

硬约束（plan §1/§2）：

* 单条入站消息**只结算一次**：落在主动回复窗口内只记 ``valued_reply``，否则按
  ``misunderstood > gratitude > sudden_warmth > cold_shoulder`` 取一。
* 两类主动回执**共用** ``dedupe_key=f"proactive:{send_ts}"``，由 companion-core
  主键真互斥（后到者 ``duplicate``，不改账）。
* 判定键 ``msg:{message_id}``，无则 ``msg:{umo}:{ts}``；**不含消息原文**。
* 群聊一律不记账、不注入私聊情绪，expression 硬抑制为
  ``放松/活泼/温暖`` 且 ``warmth ≤ 0.55``。
* companion-core 缺失 / 版本不符 / capabilities 无 ``emotion``·``expression`` /
  超时 / 任意异常一律静默降级，行为等同 v2.4.0（fail-closed）。
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, Optional, Tuple

try:
    from ..config import DEFAULT_CONFIG_FLAT
except ImportError:
    from config import DEFAULT_CONFIG_FLAT

# 关键词事件的稳定优先级（plan §2.1.2）。
KEYWORD_EVENT_PRIORITY = ("misunderstood", "gratitude", "sudden_warmth", "cold_shoulder")
POSITIVE_EVENT_TYPES = frozenset({"gratitude", "sudden_warmth", "valued_reply"})
PROACTIVE_EVENT_TYPES = frozenset({"valued_reply", "ignored_proactive"})

# expression 档位基准（plan §3.3；与 companion-core 保持一致）。
EXPRESSION_BASELINES: Dict[str, Dict[str, object]] = {
    "回避": {"tone": "简短克制", "warmth": 0.25, "length_bias": -0.50, "proactive_bias": -0.60},
    "受伤": {"tone": "低落含蓄", "warmth": 0.30, "length_bias": -0.30, "proactive_bias": -0.40},
    "放松": {"tone": "平和自然", "warmth": 0.45, "length_bias": 0.00, "proactive_bias": 0.00},
    "活泼": {"tone": "轻快俏皮", "warmth": 0.55, "length_bias": 0.10, "proactive_bias": 0.10},
    "温暖": {"tone": "柔和体贴", "warmth": 0.70, "length_bias": 0.05, "proactive_bias": 0.05},
    "亲近": {"tone": "亲昵自然", "warmth": 0.80, "length_bias": 0.10, "proactive_bias": 0.15},
    "爱意": {"tone": "深情温柔", "warmth": 0.95, "length_bias": 0.05, "proactive_bias": 0.20},
}
GROUP_ALLOWED_MODES = frozenset({"放松", "活泼", "温暖"})
GROUP_WARMTH_CAP = 0.55

# persona_state 数值调整上限（plan §4.1）：总量 ≤ ±0.10。
PERSONA_STYLE_ADJUST_CAP = 0.10
# expression length_bias → 目标字数偏移比例（±1 映射到 ±60 字）。
EXPRESSION_LENGTH_CHARS_PER_UNIT = 60.0
LENGTH_RANGE_PATTERN = re.compile(r"^\s*(\d+)\s*[-~]\s*(\d+)\s*$")


def _text(value) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _number(value, low: float, high: float) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(low, min(high, number))


def _number_or(value, fallback: float, low: float, high: float) -> float:
    parsed = _number(value, low, high)
    return parsed if parsed is not None else float(fallback)


class EmotionEventUnitsMixin:
    # ------------------------------------------------------------------ #
    # config accessors
    # ------------------------------------------------------------------ #
    def _emotion_event_enabled(self) -> bool:
        if not self._companion_enabled():
            return False
        return self._to_bool(
            self.config.get("emotion_event_enabled"),
            DEFAULT_CONFIG_FLAT["emotion_event_enabled"],
        )

    def _emotion_reply_window_sec(self) -> int:
        try:
            value = int(
                self.config.get(
                    "emotion_reply_window_sec",
                    DEFAULT_CONFIG_FLAT["emotion_reply_window_sec"],
                )
            )
        except (TypeError, ValueError):
            value = int(DEFAULT_CONFIG_FLAT["emotion_reply_window_sec"])
        return max(3600, min(value, 86400))

    def _emotion_ignore_window_sec(self) -> int:
        try:
            value = int(
                self.config.get(
                    "emotion_ignore_window_sec",
                    DEFAULT_CONFIG_FLAT["emotion_ignore_window_sec"],
                )
            )
        except (TypeError, ValueError):
            value = int(DEFAULT_CONFIG_FLAT["emotion_ignore_window_sec"])
        return max(3600, min(value, 172800))

    def _emotion_cold_shoulder_streak(self) -> int:
        try:
            value = int(
                self.config.get(
                    "emotion_cold_shoulder_streak",
                    DEFAULT_CONFIG_FLAT["emotion_cold_shoulder_streak"],
                )
            )
        except (TypeError, ValueError):
            value = int(DEFAULT_CONFIG_FLAT["emotion_cold_shoulder_streak"])
        return max(2, min(value, 10))

    def _emotion_llm_judge_enabled(self) -> bool:
        # Reserved for a future release; v2.6.0 never runs an LLM judge.
        return self._to_bool(
            self.config.get("emotion_llm_judge_enabled"),
            DEFAULT_CONFIG_FLAT["emotion_llm_judge_enabled"],
        )

    def _emotion_keywords(self) -> Dict[str, list]:
        defaults = DEFAULT_CONFIG_FLAT["emotion_keywords"]
        result: Dict[str, list] = {key: list(words) for key, words in defaults.items()}
        raw = self.config.get("emotion_keywords")
        if isinstance(raw, dict):
            for key in result:
                value = raw.get(key)
                if not isinstance(value, (list, tuple)):
                    continue
                words = [_text(item) for item in value]
                words = [word for word in words if word]
                if words:
                    result[key] = words
        return result

    # ------------------------------------------------------------------ #
    # detection
    # ------------------------------------------------------------------ #
    def _message_dedupe_key(self, event, umo: str, now_ts: float) -> str:
        message_id = ""
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            message_id = _text(getattr(message_obj, "message_id", ""))
        if not message_id:
            message_id = _text(getattr(event, "message_id", ""))
        if message_id:
            return f"msg:{message_id}"
        return f"msg:{umo}:{int(now_ts)}"

    def _is_terse_message(self, text: str, keywords: Optional[Dict[str, list]] = None) -> bool:
        normalized = (text or "").strip()
        if not normalized or len(normalized) > 2:
            return False
        words = (keywords or self._emotion_keywords()).get("cold_shoulder", [])
        return any(word and word.lower() in normalized.lower() for word in words)

    def _detect_keyword_event(self, text: str, session: Dict) -> Optional[str]:
        """Zero-LLM keyword detection with the frozen priority order.

        Returns one event type or ``None``; mutates the cold-shoulder streak and
        the "positive interaction seen" flag on ``session``.
        """
        normalized = (text or "").strip().lower()
        if not normalized:
            return None
        keywords = self._emotion_keywords()
        for event_type in KEYWORD_EVENT_PRIORITY:
            if event_type == "cold_shoulder":
                continue
            for word in keywords.get(event_type, []):
                if word and word.lower() in normalized:
                    self._reset_cold_streak(session)
                    if event_type in POSITIVE_EVENT_TYPES:
                        session["companion_positive_seen"] = True
                    return event_type
        if self._is_terse_message(text, keywords):
            streak = int(session.get("companion_cold_streak", 0) or 0) + 1
            session["companion_cold_streak"] = streak
            if (
                streak >= self._emotion_cold_shoulder_streak()
                and session.get("companion_positive_seen")
            ):
                self._reset_cold_streak(session)
                return "cold_shoulder"
            return None
        self._reset_cold_streak(session)
        return None

    @staticmethod
    def _reset_cold_streak(session: Dict) -> None:
        session["companion_cold_streak"] = 0

    # ------------------------------------------------------------------ #
    # proactive receipt state machine
    # ------------------------------------------------------------------ #
    @staticmethod
    def _proactive_receipt_key(send_ts: float) -> str:
        return f"proactive:{int(send_ts)}"

    def _collect_inbound_emotion_event(
        self,
        session_key: str,
        session: Dict,
        text: str,
        event,
        umo: str,
        now_ts: float,
    ) -> Optional[Tuple[str, str, str]]:
        """Decide what (if anything) one inbound message settles.

        Returns ``(event_type, dedupe_key, reason)`` or ``None``. Group sessions
        are fully isolated. Inside a proactive reply window the message only
        settles ``valued_reply``; otherwise a single keyword event is chosen.
        """
        if not self._emotion_event_enabled():
            return None
        if session_key.startswith("group:"):
            return None
        if not self._is_session_whitelisted(session_key):
            return None

        pending = session.get("companion_receipt_pending")
        if isinstance(pending, dict):
            send_ts = _number(pending.get("send_ts"), 0.0, 1e18)
            if send_ts is not None and (now_ts - send_ts) <= self._emotion_reply_window_sec():
                session.pop("companion_receipt_pending", None)
                session["companion_positive_seen"] = True
                return (
                    "valued_reply",
                    self._proactive_receipt_key(send_ts),
                    "kanjyou:proactive_reply",
                )

        event_type = self._detect_keyword_event(text, session)
        if not event_type:
            return None
        return (event_type, self._message_dedupe_key(event, umo, now_ts), "kanjyou:keyword")

    async def _begin_proactive_receipt(self, session: Dict, umo: str, now_ts: float) -> None:
        """Record a new pending proactive receipt, settling any stale one first."""
        if not self._emotion_event_enabled():
            return
        if str(session.get("session_key") or "").startswith("group:"):
            return
        old = session.get("companion_receipt_pending")
        if isinstance(old, dict):
            old_ts = _number(old.get("send_ts"), 0.0, 1e18)
            old_umo = _text(old.get("umo")) or umo
            if old_ts is not None:
                await self._record_companion_emotion_event(
                    old_umo,
                    "ignored_proactive",
                    reason="kanjyou:superseded_proactive",
                    dedupe_key=self._proactive_receipt_key(old_ts),
                )
        session["companion_receipt_pending"] = {"send_ts": float(now_ts), "umo": umo}

    async def _settle_expired_receipt(self, session: Dict, now_ts: float) -> bool:
        """Settle ``ignored_proactive`` once the ignore window elapses."""
        if not self._emotion_event_enabled():
            return False
        if str(session.get("session_key") or "").startswith("group:"):
            session.pop("companion_receipt_pending", None)
            return False
        pending = session.get("companion_receipt_pending")
        if not isinstance(pending, dict):
            return False
        send_ts = _number(pending.get("send_ts"), 0.0, 1e18)
        if send_ts is None:
            session.pop("companion_receipt_pending", None)
            return True
        if (now_ts - send_ts) < self._emotion_ignore_window_sec():
            return False
        umo = _text(pending.get("umo"))
        session.pop("companion_receipt_pending", None)
        await self._record_companion_emotion_event(
            umo,
            "ignored_proactive",
            reason="kanjyou:proactive_unanswered",
            dedupe_key=self._proactive_receipt_key(send_ts),
        )
        return True

    # ------------------------------------------------------------------ #
    # reporting
    # ------------------------------------------------------------------ #
    async def _record_companion_emotion_event(
        self,
        umo: str,
        event_type: str,
        *,
        reason: str = "",
        dedupe_key: Optional[str] = None,
    ) -> bool:
        """Report one event to companion-core; silent degrade on any failure."""
        if not self._emotion_event_enabled() or not umo:
            return False
        try:
            adapter = self._companion_adapter()
        except Exception as exc:
            self._debug(f"emotion adapter init failed: {exc}")
            return False
        try:
            result = await asyncio.wait_for(
                adapter.record_emotion_event(
                    umo,
                    event_type=event_type,
                    reason=reason,
                    dedupe_key=dedupe_key,
                ),
                timeout=self._companion_timeout_sec(),
            )
        except asyncio.TimeoutError:
            self._debug(f"emotion record timeout umo={umo} type={event_type}")
            return False
        except Exception as exc:
            self._debug(f"emotion record failed umo={umo} type={event_type} err={exc}")
            return False
        return isinstance(result, dict)

    # ------------------------------------------------------------------ #
    # consumption
    # ------------------------------------------------------------------ #
    def _companion_expression(self, ctx, session_key: str) -> Optional[Dict]:
        """Sanitize ``expression`` and re-apply the group hard suppression."""
        if not isinstance(ctx, dict):
            return None
        expression = ctx.get("expression")
        if not isinstance(expression, dict):
            return None
        mode = _text(expression.get("mode"))
        base = EXPRESSION_BASELINES.get(mode)
        if base is None:
            return None
        hints = expression.get("style_hints")
        hints = hints if isinstance(hints, dict) else {}
        warmth = _number_or(hints.get("warmth"), base["warmth"], 0.0, 1.0)
        length_bias = _number_or(hints.get("length_bias"), base["length_bias"], -1.0, 1.0)
        proactive_bias = _number_or(
            hints.get("proactive_bias"), base["proactive_bias"], -1.0, 1.0
        )
        tone = _text(hints.get("tone")) or _text(base["tone"])
        suppressed = False
        if session_key.startswith("group:"):
            if mode not in GROUP_ALLOWED_MODES:
                suppressed = True
                mode = "放松"
                base = EXPRESSION_BASELINES["放松"]
                tone = _text(base["tone"])
                warmth = float(base["warmth"])
                length_bias = float(base["length_bias"])
                proactive_bias = float(base["proactive_bias"])
            warmth = min(warmth, GROUP_WARMTH_CAP)
        return {
            "mode": mode,
            "tone": tone,
            "warmth": round(warmth, 4),
            "length_bias": round(length_bias, 4),
            "proactive_bias": round(proactive_bias, 4),
            "group_suppressed": suppressed,
        }

    def _length_range_midpoint(self, length_range: str) -> Optional[float]:
        match = LENGTH_RANGE_PATTERN.match(_text(length_range))
        if not match:
            return None
        low, high = int(match.group(1)), int(match.group(2))
        if high < low:
            low, high = high, low
        return (low + high) / 2.0

    def _persona_style_adjust(
        self, persona_state: str, session: Optional[Dict]
    ) -> Tuple[float, float, bool]:
        """plan §4.1: persona_state → (warmth_ps, length_bias_ps, suppress)."""
        if not self._persona_state_enabled() or not persona_state:
            return 0.0, 0.0, False
        midpoint = self._length_range_midpoint(
            self._persona_state_length_range(persona_state)
        )
        length_bias_ps = 0.0
        if midpoint is not None:
            length_bias_ps = max(
                -PERSONA_STYLE_ADJUST_CAP,
                min(PERSONA_STYLE_ADJUST_CAP, (midpoint - 40.0) / 200.0),
            )
        mood = None
        if isinstance(session, dict):
            mood = _number(
                session.get("mood", self._mood_initial()), 0.0, 100.0
            )
        warmth_ps = 0.0
        if mood is not None:
            warmth_ps = max(
                -PERSONA_STYLE_ADJUST_CAP,
                min(PERSONA_STYLE_ADJUST_CAP, (mood - 50.0) / 500.0),
            )
        suppress = self._persona_state_suppresses_proactive(persona_state)
        return warmth_ps, length_bias_ps, suppress

    def _expression_style_merge(
        self,
        expression: Optional[Dict],
        persona_state: str,
        session: Optional[Dict],
        session_key: str,
    ) -> Optional[Dict]:
        """plan §4.1 merge: expression is authoritative, persona is detail.

        Numeric adjustment is bounded (±0.10 per persona channel) and the
        ``mode`` is never overridden.
        """
        if not expression:
            return None
        warmth_ps, length_bias_ps, suppress = self._persona_style_adjust(
            persona_state, session
        )
        warmth = max(0.0, min(1.0, float(expression["warmth"]) + warmth_ps))
        length_bias = max(
            -1.0, min(1.0, float(expression["length_bias"]) + length_bias_ps)
        )
        proactive_bias = max(-1.0, min(1.0, float(expression["proactive_bias"])))
        if suppress:
            proactive_bias = min(proactive_bias, -0.5)
        if session_key.startswith("group:"):
            warmth = min(warmth, GROUP_WARMTH_CAP)
        return {
            "mode": expression["mode"],
            "tone": expression["tone"],
            "warmth": round(warmth, 4),
            "length_bias": round(length_bias, 4),
            "proactive_bias": round(proactive_bias, 4),
            "group_suppressed": bool(expression.get("group_suppressed")),
        }

    def _expression_style_hint(self, merged: Dict) -> str:
        return (
            f"{merged['tone']}（表达档位：{merged['mode']}，"
            f"温暖度 {float(merged['warmth']):.2f}）"
        )

    def _expression_length_range(self, merged: Dict, base_range: str) -> str:
        match = LENGTH_RANGE_PATTERN.match(_text(base_range))
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if high < low:
                low, high = high, low
        else:
            low, high = 20, 60
        width = max(4, high - low)
        midpoint = (low + high) / 2.0
        target = max(
            6.0,
            min(
                120.0,
                midpoint + float(merged["length_bias"]) * EXPRESSION_LENGTH_CHARS_PER_UNIT,
            ),
        )
        new_low = max(6, round(target - width / 2.0))
        new_high = max(new_low + 4, round(target + width / 2.0))
        return f"{new_low}-{new_high}"

    def _companion_emotion_state_text(self, ctx, session_key: str) -> str:
        """Human-readable emotion line; never leaks private emotion into groups."""
        if session_key.startswith("group:"):
            return ""
        if not isinstance(ctx, dict):
            return ""
        emotion = ctx.get("emotion_state")
        if not isinstance(emotion, dict):
            return ""
        state = _text(emotion.get("state"))
        if not state:
            return ""
        parts = []
        valence = _number(emotion.get("valence"), -1.0, 1.0)
        if valence is not None:
            parts.append(f"情绪值 {valence:+.2f}")
        last_event = _text(emotion.get("last_event"))
        if last_event:
            parts.append(f"最近事件 {last_event}")
        if not parts:
            return state
        return f"{state}（{'，'.join(parts)}）"

    def _append_expression_block(
        self,
        prompt: str,
        prompt_tpl: str,
        emotion_state_text: str,
        merged: Optional[Dict],
    ) -> str:
        lines = []
        if emotion_state_text and "{emotion_state}" not in prompt_tpl:
            lines.append(f"当前情绪：{emotion_state_text}")
        if merged and "{expression_mode}" not in prompt_tpl:
            lines.append(
                f"表达档位：{merged['mode']}（{merged['tone']}，"
                f"温暖度 {float(merged['warmth']):.2f}）"
            )
        if not lines:
            return prompt
        return prompt + "\n【情绪与表达】\n" + "\n".join(lines) + "\n"

    def _companion_expression_proactive_soft_gate(
        self, session: Optional[Dict]
    ) -> Optional[float]:
        """One-shot read of the last merged ``proactive_bias`` (never boosts)."""
        if not isinstance(session, dict):
            return None
        value = session.pop("companion_expression_proactive_bias", None)
        return _number(value, -1.0, 1.0)
