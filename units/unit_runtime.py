import asyncio
import random
from datetime import datetime
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain


class RuntimeUnitsMixin:
    async def _idle_loop(self):
        while True:
            try:
                await asyncio.sleep(self.config["check_interval_sec"])
                await self._check_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[idle-proactive] idle loop error: {exc}")

    async def _check_sessions(self):
        if not self.config.get("enabled", True):
            self._debug("loop skip: plugin disabled")
            return

        now = self._now()
        if self._in_sleep_window(now):
            self._debug(f"loop skip: in sleep window now={now.strftime('%H:%M')}")
            return

        now_ts = now.timestamp()
        if not self._unit_global_guard(now_ts):
            return

        changed = False

        async with self._lock:
            for session_key, s in list(self._sessions.items()):
                session_changed = await self._process_session(
                    session_key, s, now, now_ts
                )
                changed = changed or session_changed

            if changed:
                self._save_state()
                self._debug("state persisted")

    async def _process_session(
        self, session_key: str, s: Dict, now: datetime, now_ts: float
    ) -> bool:
        self._unit_rollover_counters(s, now)
        self._recover_session_mood(s, now_ts)

        if self._unit_gate_next_check(session_key, s, now_ts):
            self._debug_decision(
                session_key, {"outcome": "skip", "reason": "next_check"}
            )
            return False
        if self._unit_gate_cooldown(session_key, s, now_ts):
            self._debug_decision(session_key, {"outcome": "skip", "reason": "cooldown"})
            return True
        if self._unit_gate_daily_limit(session_key, s, now_ts):
            self._debug_decision(
                session_key, {"outcome": "skip", "reason": "daily_limit"}
            )
            return True
        if self._unit_gate_pending_reply(session_key, s, now_ts):
            self._debug_decision(
                session_key, {"outcome": "skip", "reason": "await_human_reply"}
            )
            return True

        period = self._get_period(now)
        if self._unit_gate_period_limit(session_key, s, period, now, now_ts):
            self._debug_decision(
                session_key,
                {"outcome": "skip", "reason": "period_limit", "period": period},
            )
            return True

        idle_sec = now_ts - s.get("last_interaction_at", now_ts)
        decay = self._no_reply_decay_factor(s)
        if self._unit_gate_idle(session_key, s, idle_sec, decay, now, now_ts):
            self._debug_decision(
                session_key,
                {
                    "outcome": "skip",
                    "reason": "idle_not_enough",
                    "idle_sec": int(idle_sec),
                    "decay": round(decay, 3),
                },
            )
            return True
        if self._unit_gate_mood(session_key, s, now_ts):
            self._debug_decision(
                session_key,
                {
                    "outcome": "skip",
                    "reason": "mood_low",
                    "mood": round(float(s.get("mood", 0.0)), 2),
                    "mood_min_trigger": round(self._mood_min_trigger(), 2),
                },
            )
            return True
        blocked_by_prob, prob_info = self._unit_gate_probability(
            session_key, s, idle_sec, now, now_ts
        )
        self._debug_decision(
            session_key,
            {
                "outcome": "skip" if blocked_by_prob else "pass",
                "reason": "probability",
                "idle_sec": int(idle_sec),
                "probability": prob_info["probability"],
                "roll": prob_info["roll"],
            },
        )
        if blocked_by_prob:
            return True

        umo = s.get("unified_msg_origin")
        if self._unit_gate_origin(session_key, s, umo, now_ts):
            self._debug_decision(
                session_key, {"outcome": "skip", "reason": "missing_origin"}
            )
            return True

        success, sent_text = await self._unit_execute_send(
            umo, session_key, idle_sec, s
        )
        self._unit_finalize_result(
            session_key, s, success, sent_text, period, idle_sec, decay, now, now_ts
        )
        self._debug_decision(
            session_key,
            {
                "outcome": "triggered" if success else "failed",
                "reason": "send_proactive",
                "idle_sec": int(idle_sec),
                "decay": round(decay, 3),
                "mood": round(float(s.get("mood", 0.0)), 2),
            },
        )
        return True

    def _unit_global_guard(self, now_ts: float) -> bool:
        if now_ts < self._global_pause_until:
            self._debug(
                f"loop skip: global pause active until={self._fmt_ts(self._global_pause_until)}"
            )
            return False

        self._trim_global_send_history(now_ts)
        if len(self._global_send_history) >= self._security_global_hourly_cap():
            self._debug(
                f"loop skip: global hourly cap reached count={len(self._global_send_history)} cap={self._security_global_hourly_cap()}"
            )
            return False
        return True

    def _unit_rollover_counters(self, s: Dict, now: datetime):
        self._ensure_session_shape(s)
        self._rollover_daily_counter(s, now)
        self._rollover_period_counter(s, now)

    def _unit_gate_next_check(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if now_ts >= s.get("next_check_at", 0):
            return False
        self._maybe_log_status(session_key, s, now_ts, "waiting_next_check")
        self._debug(
            f"session skip(next_check) session={session_key} next_check={self._fmt_ts(s.get('next_check_at'))}"
        )
        return True

    def _unit_defer_session(
        self, session_key: str, s: Dict, now_ts: float, reason: str, debug_msg: str
    ):
        s["next_check_at"] = now_ts + self._randomized_interval()
        self._maybe_log_status(session_key, s, now_ts, reason)
        self._debug(debug_msg)

    def _unit_gate_cooldown(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if now_ts >= s.get("cooldown_until", 0):
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "cooldown",
            f"session skip(cooldown) session={session_key} cooldown_until={self._fmt_ts(s.get('cooldown_until'))}",
        )
        return True

    def _unit_gate_daily_limit(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if s.get("today_proactive_count", 0) < self._max_per_session_per_day():
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "daily_limit",
            f"session skip(limit) session={session_key} today_count={s.get('today_proactive_count', 0)}",
        )
        return True

    def _unit_gate_pending_reply(
        self, session_key: str, s: Dict, now_ts: float
    ) -> bool:
        if not self._require_human_reply_before_next_proactive():
            return False
        if not s.get("pending_human_reply", False):
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "await_human_reply",
            f"session skip(await_human_reply) session={session_key}",
        )
        return True

    def _unit_gate_period_limit(
        self, session_key: str, s: Dict, period: str, now: datetime, now_ts: float
    ) -> bool:
        if not self._period_quota_enabled() or not period:
            return False
        counters = s.get("period_proactive_count")
        if not isinstance(counters, dict):
            counters = {"morning": 0, "afternoon": 0, "evening": 0}
            s["period_proactive_count"] = counters
        current_period_count = int(counters.get(period, 0))
        period_limit = self._effective_period_quota_limit(period, now)
        if current_period_count < period_limit:
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            f"period_limit_{period}",
            (
                f"session skip(period_limit) session={session_key} "
                f"period={period} count={current_period_count} limit={period_limit}"
            ),
        )
        return True

    def _unit_gate_idle(
        self,
        session_key: str,
        s: Dict,
        idle_sec: float,
        decay: float,
        now: datetime,
        now_ts: float,
    ) -> bool:
        needed_idle_sec = int(self._effective_min_idle_sec(now) * decay)
        if idle_sec >= needed_idle_sec:
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "idle_not_enough",
            (
                f"session skip(idle_short) session={session_key} "
                f"idle_sec={int(idle_sec)} min_idle={needed_idle_sec} decay={decay:.2f}"
            ),
        )
        return True

    def _unit_gate_mood(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if not self._mood_enabled():
            return False
        current = float(s.get("mood", self._mood_initial()))
        if current >= self._mood_min_trigger():
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "mood_low",
            (
                f"session skip(mood_low) session={session_key} "
                f"mood={current:.2f} min_trigger={self._mood_min_trigger():.2f}"
            ),
        )
        return True

    def _unit_gate_probability(
        self, session_key: str, s: Dict, idle_sec: float, now: datetime, now_ts: float
    ) -> tuple[bool, Dict]:
        p = float(self._trigger_probability(float(idle_sec), now))
        roll = random.random()
        if roll < p:
            return False, {"probability": round(p, 4), "roll": round(roll, 4)}
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "probability_miss",
            (
                f"session skip(probability) session={session_key} idle_sec={int(idle_sec)} "
                f"p={p:.4f} roll={roll:.4f}"
            ),
        )
        return True, {"probability": round(p, 4), "roll": round(roll, 4)}

    def _unit_gate_origin(
        self, session_key: str, s: Dict, umo: str, now_ts: float
    ) -> bool:
        if umo:
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "missing_origin",
            f"session skip(no_origin) session={session_key}",
        )
        return True

    async def _unit_execute_send(
        self, umo: str, session_key: str, idle_sec: float, s: Dict
    ) -> tuple[bool, str]:
        return await self._send_proactive(umo, None, session_key, idle_sec, s)

    def _unit_finalize_result(
        self,
        session_key: str,
        s: Dict,
        success: bool,
        sent_text: str,
        period: str,
        idle_sec: float,
        decay: float,
        now: datetime,
        now_ts: float,
    ):
        s["next_check_at"] = now_ts + self._randomized_interval()
        if success:
            self._global_send_history.append(now_ts)
            self._global_fail_streak = 0
            self._consume_session_mood_by_proactive(s, now_ts)
            s["last_bot_at"] = now_ts
            s["last_interaction_at"] = now_ts
            s["today_proactive_count"] = int(s.get("today_proactive_count", 0)) + 1
            s["cooldown_until"] = now_ts + int(
                self._effective_cooldown_sec(now) * decay
            )
            s["pending_human_reply"] = True
            s["no_reply_streak"] = int(s.get("no_reply_streak", 0)) + 1
            self._inc_period_count(s, period)
            self._push_proactive_history(s, sent_text)
            self._debug(
                f"session trigger(success) session={session_key} idle_sec={int(idle_sec)} "
                f"today_count={s['today_proactive_count']} no_reply_streak={s.get('no_reply_streak', 0)} decay={decay:.2f}"
            )
            self._maybe_log_status(
                session_key, s, now_ts, "trigger_success", force=True
            )
            return

        self._global_fail_streak += 1
        if self._global_fail_streak >= self._security_max_fail_streak():
            self._global_pause_until = now_ts + self._security_fail_pause_sec()
            self._debug(
                f"trigger safety pause fail_streak={self._global_fail_streak} pause_until={self._fmt_ts(self._global_pause_until)}"
            )
        self._debug(f"session trigger(failed) session={session_key}")
        self._maybe_log_status(session_key, s, now_ts, "trigger_failed", force=True)

    async def _send_proactive(
        self,
        unified_msg_origin: str,
        event: Optional[AstrMessageEvent],
        session_key: str = "",
        idle_sec: float = 0.0,
        session: Optional[Dict] = None,
    ) -> tuple[bool, str]:
        topic = await self._generate_proactive_text(
            unified_msg_origin, session_key, idle_sec, session
        )
        try:
            chain = MessageChain().message(topic)
            await self.context.send_message(unified_msg_origin, chain)
            self._debug(f"send proactive ok session={session_key} topic={topic}")
            return True, topic
        except Exception:
            try:
                # 兼容部分适配器对 MessageChain 构造差异
                await self.context.send_message(unified_msg_origin, [Plain(topic)])
                self._debug(
                    f"send proactive ok(fallback) session={session_key} topic={topic}"
                )
                return True, topic
            except Exception as exc:
                logger.error(f"[idle-proactive] send failed: {exc}")
                self._debug(f"send proactive failed session={session_key} err={exc}")
                if event:
                    await event.send(
                        event.plain_result(
                            "主动消息发送失败，请检查适配器是否支持主动消息。"
                        )
                    )
                return False, ""
