"""Phase 2-B 未完话题提取 + 自然续接（kanjyou 侧）。

零 LLM：从入站消息中按 ``commitment > plan > pending_question`` 规则提取
「对方提过、还没继续」的事，经 companion-core 的 ``record_open_thread`` 落库
（只存短标签，绝不存原文）；生成主动消息时按冷却/沉淀时间/情绪档位挑选**至多
1 条**自然续接，发送成功后 ``mark_thread_followup`` 回执。

硬约束（plan §3/§5）：

* 单条入站消息**至多提取 1 条**；``label`` 经短标签化（≤40 字、折叠空白）。
* 群聊一律 return（不提取、不消费）；未启用 companion/无 capability/超时/异常
  一律静默降级，行为等同 v2.7.3（fail-closed）。
* 续接闸门全满足才注入：开关、``status=open`` 且 ``confidence≥0.6``、
  冷却 24h、``followup_count < 2``、距 ``last_seen`` ≥ 6h、``quota.allow``、
  ``unanswered_streak < 3``、``expression.proactive_bias ≥ 0``。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Dict, Optional

try:
    from ..config import DEFAULT_CONFIG_FLAT
except ImportError:
    from config import DEFAULT_CONFIG_FLAT

OPEN_THREAD_KINDS = ("commitment", "plan", "pending_question", "topic")
OPEN_THREAD_CONFIDENCE = {
    "commitment": 0.8,
    "plan": 0.8,
    "pending_question": 0.6,
    "topic": 0.7,
}
OPEN_THREAD_MIN_CONFIDENCE = 0.6
OPEN_THREAD_FOLLOWUP_MAX = 2
OPEN_THREAD_UNANSWERED_STREAK_MAX = 3
OPEN_THREAD_LABEL_MAX = 40
OPEN_THREAD_BLOCK_HEADER = "【未完话题】"

# 入站规则（plan §3）。
COMMITMENT_KEYWORDS = (
    "回头",
    "下次",
    "改天",
    "待会",
    "一会儿",
    "明天",
    "后天",
    "说好",
    "答应",
    "稍后",
    "补给你",
    "发你",
    "给你",
    "再说",
    "记得",
)
PLAN_KEYWORDS = ("打算", "计划", "准备")
# 完成语：命中即对最近一条未完话题闭闸。
COMPLETION_KEYWORDS = ("已发", "发你了", "给你了", "说过了", "弄好了")
# assistant 侧明确延后语（plan §3，仅回复侧）。
ASSISTANT_TOPIC_KEYWORDS = ("下次再聊", "回头说", "以后再说", "改天聊")

_CLAUSE_SPLIT_RE = re.compile(r"[\s，。！？!?；;、,]+")


def _text(value) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _int_or(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or(value, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def sanitize_open_thread_label(text, max_len: int = OPEN_THREAD_LABEL_MAX) -> str:
    """把命中子句压成短标签：折叠空白、去首尾标点、超长截断。"""
    cleaned = " ".join(str(text or "").split())
    cleaned = cleaned.strip("，。！？!?,.;；、:：\"'“”‘’()（）[]【】<>《》 ")
    if not cleaned:
        return ""
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def _split_clauses(text: str) -> list:
    return [clause.strip() for clause in _CLAUSE_SPLIT_RE.split(text or "") if clause.strip()]


class OpenThreadUnitsMixin:
    # ------------------------------------------------------------------ #
    # config accessors
    # ------------------------------------------------------------------ #
    def _open_thread_followup_enabled(self) -> bool:
        if not self._companion_enabled():
            return False
        return self._to_bool(
            self.config.get("open_thread_followup_enabled"),
            DEFAULT_CONFIG_FLAT["open_thread_followup_enabled"],
        )

    def _open_thread_cooldown_hours(self) -> int:
        value = _int_or(
            self.config.get(
                "open_thread_followup_cooldown_hours",
                DEFAULT_CONFIG_FLAT["open_thread_followup_cooldown_hours"],
            ),
            int(DEFAULT_CONFIG_FLAT["open_thread_followup_cooldown_hours"]),
        )
        return max(1, min(value, 168))

    def _open_thread_min_age_hours(self) -> int:
        value = _int_or(
            self.config.get(
                "open_thread_followup_min_age_hours",
                DEFAULT_CONFIG_FLAT["open_thread_followup_min_age_hours"],
            ),
            int(DEFAULT_CONFIG_FLAT["open_thread_followup_min_age_hours"]),
        )
        return max(1, min(value, 72))

    # ------------------------------------------------------------------ #
    # extraction (zero LLM)
    # ------------------------------------------------------------------ #
    def _open_thread_label_for(self, text: str, keywords) -> str:
        clauses = _split_clauses(text)
        lowered = [clause.lower() for clause in clauses]
        for keyword in keywords:
            key = keyword.lower()
            for clause, low in zip(clauses, lowered):
                if key in low:
                    label = sanitize_open_thread_label(clause)
                    if label:
                        return label
        return ""

    def _extract_inbound_open_thread(self, text: str) -> Optional[Dict]:
        """One inbound message yields at most one unfinished item."""
        normalized = (text or "").strip()
        if not normalized:
            return None
        label = self._open_thread_label_for(normalized, COMMITMENT_KEYWORDS)
        if label:
            return {
                "kind": "commitment",
                "label": label,
                "confidence": OPEN_THREAD_CONFIDENCE["commitment"],
                "reason": "kanjyou:rule",
            }
        label = self._open_thread_label_for(normalized, PLAN_KEYWORDS)
        if label:
            return {
                "kind": "plan",
                "label": label,
                "confidence": OPEN_THREAD_CONFIDENCE["plan"],
                "reason": "kanjyou:rule",
            }
        if ("？" in normalized or "?" in normalized) and len(normalized) >= 6:
            label = sanitize_open_thread_label(normalized)
            if label:
                return {
                    "kind": "pending_question",
                    "label": label,
                    "confidence": OPEN_THREAD_CONFIDENCE["pending_question"],
                    "reason": "kanjyou:rule",
                }
        return None

    def _is_open_thread_completion(self, text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        return any(keyword in normalized for keyword in COMPLETION_KEYWORDS)

    def _extract_assistant_open_topic(self, text: str) -> Optional[Dict]:
        label = self._open_thread_label_for(text, ASSISTANT_TOPIC_KEYWORDS)
        if not label:
            return None
        return {
            "kind": "topic",
            "label": label,
            "confidence": OPEN_THREAD_CONFIDENCE["topic"],
            "reason": "kanjyou:assistant",
        }

    def _collect_inbound_open_thread(
        self, session_key: str, text: str, umo: str
    ) -> Optional[Dict]:
        """Decide what one private inbound message does for open threads.

        Returns ``{"action": "close"}`` for a completion phrase or
        ``{"action": "record", ...}`` for a fresh item; ``None`` when out of
        scope (disabled / group / not whitelisted / nothing matched).
        """
        if not self._open_thread_followup_enabled() or not umo:
            return None
        if str(session_key or "").startswith("group:"):
            return None
        if not self._is_session_whitelisted(session_key):
            return None
        if self._is_open_thread_completion(text):
            return {"action": "close"}
        item = self._extract_inbound_open_thread(text)
        if not item:
            return None
        return {"action": "record", **item}

    # ------------------------------------------------------------------ #
    # reporting
    # ------------------------------------------------------------------ #
    async def _record_companion_open_thread(
        self,
        umo: str,
        *,
        kind: str,
        label: str,
        confidence: float,
        reason: str = "",
        source: str = "",
    ) -> bool:
        """Report one unfinished item; silent degrade on any failure."""
        if not self._open_thread_followup_enabled() or not umo or not label:
            return False
        try:
            adapter = self._companion_adapter()
        except Exception as exc:
            self._debug(f"open thread adapter init failed: {exc}")
            return False
        try:
            result = await asyncio.wait_for(
                adapter.record_open_thread(
                    umo,
                    label=label,
                    kind=kind,
                    reason=reason,
                    confidence=confidence,
                    source=source,
                ),
                timeout=self._companion_timeout_sec(),
            )
        except asyncio.TimeoutError:
            self._debug(f"open thread record timeout umo={umo} kind={kind}")
            return False
        except Exception as exc:
            self._debug(f"open thread record failed umo={umo} kind={kind} err={exc}")
            return False
        return isinstance(result, dict)

    async def _close_companion_open_thread(self, umo: str, thread_id: str) -> bool:
        if not self._open_thread_followup_enabled() or not umo or not thread_id:
            return False
        try:
            adapter = self._companion_adapter()
        except Exception as exc:
            self._debug(f"open thread close adapter init failed: {exc}")
            return False
        try:
            result = await asyncio.wait_for(
                adapter.close_open_thread(umo, thread_id, reason="answered"),
                timeout=self._companion_timeout_sec(),
            )
        except asyncio.TimeoutError:
            self._debug(f"open thread close timeout umo={umo}")
            return False
        except Exception as exc:
            self._debug(f"open thread close failed umo={umo} err={exc}")
            return False
        return isinstance(result, dict)

    async def _mark_open_thread_followup(self, umo: str, thread_id: str) -> bool:
        if not self._open_thread_followup_enabled() or not umo or not thread_id:
            return False
        try:
            adapter = self._companion_adapter()
        except Exception as exc:
            self._debug(f"open thread followup adapter init failed: {exc}")
            return False
        try:
            result = await asyncio.wait_for(
                adapter.mark_thread_followup(umo, thread_id),
                timeout=self._companion_timeout_sec(),
            )
        except asyncio.TimeoutError:
            self._debug(f"open thread followup timeout umo={umo}")
            return False
        except Exception as exc:
            self._debug(f"open thread followup failed umo={umo} err={exc}")
            return False
        return isinstance(result, dict)

    async def _apply_inbound_open_thread(self, umo: str, decision: Optional[Dict]) -> bool:
        """Execute an extracted inbound decision (record or close)."""
        if not decision or not umo:
            return False
        action = str(decision.get("action") or "")
        if action == "record":
            return await self._record_companion_open_thread(
                umo,
                kind=str(decision.get("kind") or ""),
                label=str(decision.get("label") or ""),
                confidence=float(decision.get("confidence") or 0.0),
                reason=str(decision.get("reason") or ""),
                source="kanjyou:rule",
            )
        if action == "close":
            thread_id = await self._latest_open_thread_id(umo)
            if not thread_id:
                return False
            return await self._close_companion_open_thread(umo, thread_id)
        return False

    async def _latest_open_thread_id(self, umo: str) -> str:
        """Newest open/stale thread id for a private scope; ``""`` on failure."""
        if not self._open_thread_followup_enabled() or not umo:
            return ""
        try:
            adapter = self._companion_adapter()
        except Exception as exc:
            self._debug(f"open thread list adapter init failed: {exc}")
            return ""
        try:
            rows = await asyncio.wait_for(
                adapter.open_threads(umo, limit=3),
                timeout=self._companion_timeout_sec(),
            )
        except asyncio.TimeoutError:
            self._debug(f"open thread list timeout umo={umo}")
            return ""
        except Exception as exc:
            self._debug(f"open thread list failed umo={umo} err={exc}")
            return ""
        if not isinstance(rows, (list, tuple)):
            return ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") not in ("open", "stale"):
                continue
            thread_id = _text(row.get("thread_id"))
            if thread_id:
                return thread_id
        return ""

    # ------------------------------------------------------------------ #
    # assistant topic hook
    # ------------------------------------------------------------------ #
    async def _maybe_record_assistant_open_topic(self, event) -> bool:
        """Record a ``topic`` when the assistant defers the talk for later."""
        try:
            if not self._open_thread_followup_enabled():
                return False
            session_key = self._session_key(event)
            if not session_key or session_key.startswith("group:"):
                return False
            if not self._is_session_whitelisted(session_key):
                return False
            result = event.get_result()
            if result is None:
                return False
            chain = getattr(result, "chain", None)
            text = self._plain_chain_text(chain) if chain else ""
            if not text:
                return False
            item = self._extract_assistant_open_topic(text)
            if not item:
                return False
            umo = _text(getattr(event, "unified_msg_origin", ""))
            if not umo:
                return False
            return await self._record_companion_open_thread(
                umo, source="kanjyou:assistant", **item
            )
        except Exception as exc:
            self._debug(f"open thread assistant topic failed: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # consumption
    # ------------------------------------------------------------------ #
    def _open_thread_age_seconds(self, value, now_ts: float) -> Optional[float]:
        raw = _text(value)
        if not raw:
            return None
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
        try:
            return now_ts - moment.timestamp()
        except Exception:
            return None

    def _select_open_thread_followup(
        self,
        ctx,
        session_key: str,
        session,
        now_ts: float,
        expression: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Pick at most one follow-up candidate; ``None`` when gated out."""
        if not self._open_thread_followup_enabled():
            return None
        if str(session_key or "").startswith("group:"):
            return None
        if not isinstance(ctx, dict):
            return None
        details = ctx.get("open_thread_details")
        if not isinstance(details, (list, tuple)):
            return None

        streak = _int_or(ctx.get("unanswered_streak"), 0)
        if streak >= OPEN_THREAD_UNANSWERED_STREAK_MAX:
            return None
        quota = ctx.get("quota")
        if isinstance(quota, dict) and quota.get("allow") is False:
            return None
        if expression is None:
            expression = self._companion_expression(ctx, session_key)
        if isinstance(expression, dict):
            if _float_or(expression.get("proactive_bias"), 0.0) < 0:
                return None

        cooldown_sec = self._open_thread_cooldown_hours() * 3600
        min_age_sec = self._open_thread_min_age_hours() * 3600
        cooldown = session.get("companion_open_thread_cooldown") if isinstance(session, dict) else None
        cooldown = cooldown if isinstance(cooldown, dict) else {}

        for row in details:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") != "open":
                continue
            confidence = row.get("confidence")
            if confidence is None:
                continue
            confidence = _float_or(confidence, 0.0)
            if confidence < OPEN_THREAD_MIN_CONFIDENCE:
                continue
            thread_id = _text(row.get("thread_id"))
            label = _text(row.get("label"))
            if not thread_id or not label:
                continue
            if _int_or(row.get("followup_count"), 0) >= OPEN_THREAD_FOLLOWUP_MAX:
                continue
            last_sent = cooldown.get(thread_id)
            if isinstance(last_sent, (int, float)) and not isinstance(last_sent, bool):
                if (now_ts - float(last_sent)) < cooldown_sec:
                    continue
            age = self._open_thread_age_seconds(row.get("last_seen"), now_ts)
            if age is None or age < min_age_sec:
                continue
            return {
                "thread_id": thread_id,
                "label": label,
                "kind": _text(row.get("kind")) or "topic",
            }
        return None

    @staticmethod
    def _open_thread_followup_text(selected) -> str:
        label = _text((selected or {}).get("label"))
        if not label:
            return ""
        return (
            f"对方之前提过、还没继续的一件事：{label}。"
            "如果此刻自然，就随口提一句，像忽然想起来；"
            "不要催办、不要追问进度，本次只提这一件。"
        )

    @staticmethod
    def _append_open_thread_block(prompt: str, prompt_tpl: str, text: str) -> str:
        if not text or "{open_thread_followup}" in prompt_tpl:
            return prompt
        return prompt + f"\n{OPEN_THREAD_BLOCK_HEADER}\n{text}\n"

    async def _commit_open_thread_followup(self, session, umo: str, now_ts: float) -> bool:
        """Send receipt: bump the followed-up thread + local cooldown stamp."""
        if not isinstance(session, dict):
            return False
        pending = session.pop("companion_open_thread_followup", None)
        if not isinstance(pending, dict):
            return False
        thread_id = _text(pending.get("thread_id"))
        target_umo = _text(pending.get("umo")) or umo
        if not thread_id or not target_umo:
            return False
        if not await self._mark_open_thread_followup(target_umo, thread_id):
            return False
        cooldown = session.get("companion_open_thread_cooldown")
        if not isinstance(cooldown, dict):
            cooldown = {}
            session["companion_open_thread_cooldown"] = cooldown
        cooldown[thread_id] = float(now_ts)
        return True
