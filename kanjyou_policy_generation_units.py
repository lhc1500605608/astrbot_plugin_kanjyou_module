import inspect
import random
from datetime import datetime
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from kanjyou_constants import DEFAULT_CONFIG

class PolicyGenerationUnitsMixin:
    async def _generate_proactive_text(
        self, unified_msg_origin: str, session_key: str, idle_sec: float, session: Optional[Dict]
    ) -> str:
        fallback = self._sanitize_outgoing_text(
            str(self.config.get("fallback_proactive_text") or DEFAULT_CONFIG["fallback_proactive_text"]).strip()
        )
        try:
            session_type = "私聊" if session_key.startswith("private:") else "群聊"
            persona_text = await self._resolve_persona_prompt()
            style_hint = self._style_hint(session_key, session, idle_sec)
            recent_history = self._recent_history_text(session)
            prompt_tpl = str(
                self.config.get("proactive_prompt_template") or DEFAULT_CONFIG["proactive_prompt_template"]
            )
            prompt = prompt_tpl.format(
                persona=persona_text,
                session_type=session_type,
                idle_seconds=int(idle_sec),
                idle_minutes=max(1, int(idle_sec // 60)),
                style_hint=style_hint,
                recent_history=recent_history,
            )

            provider_id = str(self.config.get("proactive_provider_id") or "").strip()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(unified_msg_origin)

            if not provider_id:
                self._debug("generate skip: provider_id unavailable, use fallback")
                return fallback

            completion = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            text = self._completion_to_text(completion)
            if not text:
                self._debug("generate empty completion, use fallback")
                return fallback
            cleaned = self._clean_generated_text(text)
            if not self._is_safe_proactive_text(cleaned):
                self._debug("generate unsafe text blocked, use fallback")
                return fallback
            if self._is_repetitive(cleaned, session):
                self._debug("generate repetitive text, use fallback")
                return fallback
            self._debug(f"generate ok provider={provider_id} session={session_key} text={cleaned}")
            return cleaned
        except Exception as exc:
            logger.error(f"[idle-proactive] generate proactive text failed: {exc}")
            self._debug(f"generate failed session={session_key} err={exc}")
            return fallback

    def _should_trigger(self, idle_sec: float, now: datetime) -> bool:
        min_idle = float(self._effective_min_idle_sec(now))
        max_idle = float(self._effective_max_idle_sec(now))

        if idle_sec >= max_idle:
            return True

        span = max(max_idle - min_idle, 1.0)
        progress = max(0.0, min(1.0, (idle_sec - min_idle) / span))
        base_prob = float(self._trigger_base_prob())  # 刚到最小 idle 时也有少量概率
        max_prob = float(self._trigger_max_prob())
        p = base_prob + (max_prob - base_prob) * progress
        return random.random() < p

    def _min_idle_sec(self) -> int:
        return max(60, int(float(self.config["min_idle_min"]) * 60))

    def _max_idle_sec(self) -> int:
        return max(self._min_idle_sec() + 60, int(float(self.config["max_idle_min"]) * 60))

    def _effective_max_idle_sec(self, now: datetime) -> int:
        base = float(self._max_idle_sec())
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return int(base)
        mul = max(1.0, float(self._weekend_min_idle_multiplier()))
        return int(base * mul)

    def _cooldown_sec(self) -> int:
        return max(60, int(float(self.config["cooldown_min"]) * 60))

    def _is_weekend(self, now: datetime) -> bool:
        # Monday=0 ... Sunday=6
        return now.weekday() >= 5

    def _effective_min_idle_sec(self, now: datetime) -> int:
        base = float(self._min_idle_sec())
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return int(base)
        mul = max(1.0, float(self._weekend_min_idle_multiplier()))
        return int(base * mul)

    def _effective_cooldown_sec(self, now: datetime) -> int:
        base = float(self._cooldown_sec())
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return int(base)
        mul = max(1.0, float(self._weekend_cooldown_multiplier()))
        return int(base * mul)

    def _get_period(self, now: datetime) -> str:
        hm = now.strftime("%H:%M")
        if "06:00" <= hm <= "11:59":
            return "morning"
        if "12:00" <= hm <= "17:59":
            return "afternoon"
        if "18:00" <= hm <= "22:59":
            return "evening"
        return "offhours"

    def _get_period_quota_limit(self, period: str) -> int:
        mapping = {
            "morning": int(self._period_quota_morning_max()),
            "afternoon": int(self._period_quota_afternoon_max()),
            "evening": int(self._period_quota_evening_max()),
            "offhours": 0,
        }
        return max(0, mapping.get(period, 0))

    def _effective_period_quota_limit(self, period: str, now: datetime) -> int:
        limit = self._get_period_quota_limit(period)
        if not self._weekend_mode_enabled() or not self._is_weekend(now):
            return limit
        mul = max(0.0, float(self._weekend_quota_multiplier()))
        return max(0, int(limit * mul))

    def _no_reply_decay_factor(self, session: Dict) -> float:
        if not self._no_reply_decay_enabled():
            return 1.0
        streak = max(0, int(session.get("no_reply_streak", 0)))
        if streak <= 0:
            return 1.0
        base = max(1.0, float(self._no_reply_decay_factor_base()))
        max_factor = max(1.0, float(self._no_reply_decay_max_factor()))
        factor = base ** streak
        return min(max_factor, factor)

    def _style_hint(self, session_key: str, session: Optional[Dict], idle_sec: float) -> str:
        if session_key.startswith("group:"):
            if idle_sec > 6 * 3600:
                return "群聊里简短自然、轻松抛题，不要过度热情"
            return "群聊里简短友好、克制不刷屏"
        streak = int((session or {}).get("no_reply_streak", 0))
        if streak >= 2:
            return "私聊里温和克制、低打扰、避免连续追问"
        if idle_sec > 4 * 3600:
            return "私聊里温暖真诚、轻柔开启话题"
        return "私聊里自然亲切、像朋友一样"

    def _recent_history_text(self, session: Optional[Dict]) -> str:
        history = (session or {}).get("recent_proactive_texts", [])
        if not isinstance(history, list) or not history:
            return "无"
        return "\n".join(f"- {str(x)}" for x in history[-5:])

    def _push_proactive_history(self, session: Dict, text: str):
        if not self._quality_dedupe_enabled():
            return
        if not text:
            return
        history = session.get("recent_proactive_texts")
        if not isinstance(history, list):
            history = []
        history.append(text.strip())
        size = max(1, int(self._quality_history_size()))
        session["recent_proactive_texts"] = history[-size:]

    def _is_repetitive(self, text: str, session: Optional[Dict]) -> bool:
        if not self._quality_dedupe_enabled():
            return False
        if not text:
            return True
        history = (session or {}).get("recent_proactive_texts", [])
        if not isinstance(history, list):
            return False
        normalized = "".join(text.lower().split())
        for h in history:
            hn = "".join(str(h).lower().split())
            if normalized == hn:
                return True
        return False

    def _is_whitelisted(self, event: AstrMessageEvent) -> bool:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return False

        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_id = str(getattr(getattr(msg_obj, "sender", None), "user_id", "") or event.get_sender_id())

        if group_id:
            return group_id in self.config["group_whitelist"]
        return sender_id in self.config["private_whitelist"]

    def _session_key(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""

        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_id = str(getattr(getattr(msg_obj, "sender", None), "user_id", "") or event.get_sender_id())
        if group_id:
            return f"group:{group_id}"
        return f"private:{sender_id}"

    async def _resolve_persona_prompt(self) -> str:
        persona_id = str(self.config.get("persona_id") or "").strip()
        if not persona_id:
            return "未指定人格，请保持温暖、真诚、自然。"
        try:
            manager = getattr(self.context, "persona_manager", None)
            if not manager:
                return f"人格ID: {persona_id}"
            get_func = getattr(manager, "get_persona", None)
            if not callable(get_func):
                return f"人格ID: {persona_id}"
            persona = get_func(persona_id)
            if inspect.isawaitable(persona):
                persona = await persona
            if not persona:
                return f"人格ID: {persona_id}"
            for field in ("prompt", "system_prompt", "content", "description"):
                val = getattr(persona, field, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            return str(persona)
        except Exception as exc:
            self._debug(f"resolve persona failed id={persona_id} err={exc}")
            return f"人格ID: {persona_id}"

    def _completion_to_text(self, completion: object) -> str:
        text = getattr(completion, "completion_text", None)
        if isinstance(text, str):
            return text.strip()
        if isinstance(completion, str):
            return completion.strip()
        return str(completion).strip()

    def _clean_generated_text(self, text: str) -> str:
        cleaned = self._sanitize_outgoing_text(text.strip().strip('"').strip("'"))
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if lines:
            cleaned = lines[0]
        max_len = max(20, int(self.config.get("security_max_text_length", DEFAULT_CONFIG["security_max_text_length"])))
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len].rstrip("，,。.!?？") + "。"
        return cleaned

    def _sanitize_outgoing_text(self, text: str) -> str:
        cleaned = text or ""
        if not self._security_allow_links():
            lowered = cleaned.lower()
            if "http://" in lowered or "https://" in lowered or "www." in lowered:
                cleaned = "刚刚想到你，今天有哪件小事值得被夸一下？"
        return cleaned.strip()

    def _is_safe_proactive_text(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if not self._security_allow_links():
            if "http://" in lowered or "https://" in lowered or "www." in lowered:
                return False
        blocked_words = self._security_blocked_words()
        for w in blocked_words:
            if w and w in text:
                return False
        return True
