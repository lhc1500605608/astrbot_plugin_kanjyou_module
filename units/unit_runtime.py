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
                if self.config.get("enabled", True):
                    await self._flush_dialogue_wait_buffers()
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

        decision = await self._decision_engine(session_key, s, now, now_ts)
        self._record_decision(session_key, decision)
        self._debug_decision(
            session_key,
            {
                "outcome": "allow" if decision.get("allow") else "skip",
                "reason_codes": decision.get("reason_codes", []),
                "confidence": decision.get("confidence"),
                "mode": decision.get("mode"),
                "idle_sec": decision.get("idle_sec"),
            },
        )
        if not decision.get("allow", False):
            return bool(decision.get("state_changed", False))

        period = str(decision.get("period", self._get_period(now)))
        idle_sec = float(
            decision.get("idle_sec", now_ts - s.get("last_interaction_at", now_ts))
        )
        decay = float(decision.get("decay", self._no_reply_decay_factor(s)))
        success, sent_text = await self._unit_execute_send(
            str(decision.get("umo", s.get("unified_msg_origin"))),
            session_key,
            idle_sec,
            s,
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

    async def _decision_engine(
        self, session_key: str, s: Dict, now: datetime, now_ts: float
    ) -> Dict:
        mode = self._decision_mode()
        period = self._get_period(now)
        idle_sec = float(now_ts - s.get("last_interaction_at", now_ts))
        decay = float(self._no_reply_decay_factor(s))
        reason_codes = []
        state_changed = False

        if self._unit_gate_whitelist(session_key, s, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.99,
                ["not_in_proactive_whitelist"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )

        if self._unit_gate_next_check(session_key, s, now_ts):
            return self._decision_result(
                False,
                0.98,
                ["waiting_next_check"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )
        if self._unit_gate_cooldown(session_key, s, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.97,
                ["cooldown"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )
        if self._unit_gate_daily_limit(session_key, s, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.97,
                ["daily_limit"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )
        if self._unit_gate_pending_reply(session_key, s, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.95,
                ["await_human_reply"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )
        if self._unit_gate_period_limit(session_key, s, period, now, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.93,
                ["period_limit"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )
        if self._unit_gate_idle(session_key, s, idle_sec, decay, now, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.92,
                ["idle_not_enough"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )
        if self._unit_gate_mood(session_key, s, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.94,
                ["mood_low"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )

        # Group timing heuristic inspired by group_chat_plus.
        if session_key.startswith("group:"):
            threshold_sec = int(self._decision_group_quiet_threshold() * 60)
            if idle_sec < threshold_sec:
                self._unit_defer_session(
                    session_key,
                    s,
                    now_ts,
                    "group_active",
                    f"session skip(group_active) session={session_key} idle_sec={int(idle_sec)} quiet_threshold={threshold_sec}",
                )
                state_changed = True
                return self._decision_result(
                    False,
                    0.9,
                    ["group_active"],
                    session_key,
                    s,
                    now,
                    now_ts,
                    period,
                    idle_sec,
                    decay,
                    mode,
                    state_changed,
                )
            reason_codes.append("group_quiet")

        # Decision-mode aware probability.
        p_raw = float(self._trigger_probability(float(idle_sec), now))
        multiplier = 1.0
        if mode == "strict":
            multiplier = 0.65
        elif mode == "active":
            multiplier = 1.25
        p = max(0.0, min(1.0, p_raw * multiplier))
        roll = random.random()
        if roll >= p:
            self._unit_defer_session(
                session_key,
                s,
                now_ts,
                "probability_miss",
                (
                    f"session skip(probability) session={session_key} idle_sec={int(idle_sec)} "
                    f"p={p:.4f} roll={roll:.4f} mode={mode}"
                ),
            )
            state_changed = True
            return self._decision_result(
                False,
                round(max(0.0, p), 4),
                ["probability_miss"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )
        reason_codes.append("probability_pass")

        umo = s.get("unified_msg_origin")
        if self._unit_gate_origin(session_key, s, umo, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.98,
                ["missing_origin"],
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
            )

        # Lite decision refinement: can veto/confirm with confidence.
        lite = await self._decision_refine_by_lite_llm(
            session_key=session_key,
            unified_msg_origin=str(umo or ""),
            now=now,
            idle_sec=idle_sec,
            decay=decay,
            mood=float(s.get("mood", self._mood_initial())),
            reason_codes=reason_codes,
            mode=mode,
        )
        min_conf = self._decision_min_confidence()
        allow = True
        confidence = 0.7
        suggested_tone = self._style_hint(session_key, s, idle_sec)
        if isinstance(lite, dict):
            lite_allow = bool(lite.get("allow", True))
            lite_conf = float(lite.get("confidence", 0.0))
            lite_reasons = lite.get("reason_codes", [])
            if isinstance(lite_reasons, list):
                for code in lite_reasons[:2]:
                    c = str(code).strip()
                    if c:
                        reason_codes.append(f"lite_{c}")
            if lite_allow and lite_conf >= min_conf:
                allow = True
                confidence = lite_conf
            elif (not lite_allow) and lite_conf >= min_conf:
                allow = False
                confidence = lite_conf
                reason_codes.append("lite_veto")
            if (
                isinstance(lite.get("suggested_tone"), str)
                and lite.get("suggested_tone").strip()
            ):
                suggested_tone = lite.get("suggested_tone").strip()

        if not allow:
            self._unit_defer_session(
                session_key,
                s,
                now_ts,
                "lite_veto",
                f"session skip(lite_veto) session={session_key} mode={mode}",
            )
            state_changed = True
            return self._decision_result(
                False,
                confidence,
                reason_codes,
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
                suggested_tone,
            )

        # Last-hop safety confirmation before sending.
        ok, safety_reason = self._decision_final_safety_check(s, now, now_ts)
        if not ok:
            self._unit_defer_session(
                session_key,
                s,
                now_ts,
                safety_reason,
                f"session skip({safety_reason}) session={session_key}",
            )
            state_changed = True
            reason_codes.append(safety_reason)
            return self._decision_result(
                False,
                max(confidence, 0.9),
                reason_codes,
                session_key,
                s,
                now,
                now_ts,
                period,
                idle_sec,
                decay,
                mode,
                state_changed,
                suggested_tone,
            )

        s["decision_suggested_tone"] = suggested_tone
        return self._decision_result(
            True,
            max(confidence, 0.75),
            reason_codes or ["allow"],
            session_key,
            s,
            now,
            now_ts,
            period,
            idle_sec,
            decay,
            mode,
            state_changed,
            suggested_tone,
        )

    def _decision_result(
        self,
        allow: bool,
        confidence: float,
        reason_codes: list,
        session_key: str,
        s: Dict,
        now: datetime,
        now_ts: float,
        period: str,
        idle_sec: float,
        decay: float,
        mode: str,
        state_changed: bool,
        suggested_tone: str = "",
    ) -> Dict:
        return {
            "allow": bool(allow),
            "confidence": round(float(confidence), 4),
            "reason_codes": list(reason_codes or []),
            "suggested_tone": suggested_tone
            or self._style_hint(session_key, s, idle_sec),
            "period": period,
            "idle_sec": int(idle_sec),
            "decay": round(float(decay), 3),
            "mode": mode,
            "umo": s.get("unified_msg_origin"),
            "state_changed": bool(state_changed),
            "mood": round(float(s.get("mood", self._mood_initial())), 2),
            "next_check_at": self._fmt_ts(s.get("next_check_at", now_ts)),
            "cooldown_until": self._fmt_ts(s.get("cooldown_until", 0)),
        }

    def _decision_final_safety_check(
        self, s: Dict, now: datetime, now_ts: float
    ) -> tuple[bool, str]:
        if self._in_sleep_window(now):
            return False, "night_quiet"
        if now_ts < s.get("cooldown_until", 0):
            return False, "cooldown_guard"
        self._trim_global_send_history(now_ts)
        if len(self._global_send_history) >= self._security_global_hourly_cap():
            return False, "global_hourly_cap"
        return True, "ok"

    async def _decision_refine_by_lite_llm(
        self,
        session_key: str,
        unified_msg_origin: str,
        now: datetime,
        idle_sec: float,
        decay: float,
        mood: float,
        reason_codes: list,
        mode: str,
    ) -> Dict:
        prompt = (
            "你是主动对话决策助手。请输出严格 JSON，不要解释。\n"
            '格式：{"allow":true,"confidence":0.0,"reason_codes":["..."],"suggested_tone":"..."}\n'
            "规则：\n"
            "1) allow 只表示是否建议主动发言。\n"
            "2) confidence 范围 0-1。\n"
            "3) reason_codes 用简短英文下划线风格。\n"
            "4) suggested_tone 为一句中文语气建议，长度<=30。\n"
            f"session={session_key} now={now.strftime('%Y-%m-%d %H:%M:%S')} mode={mode}\n"
            f"idle_sec={int(idle_sec)} decay={decay:.2f} mood={mood:.2f}\n"
            f"rule_reason_codes={','.join(reason_codes) if reason_codes else '-'}\n"
        )
        obj = await self._lite_llm_json(unified_msg_origin, prompt)
        if not isinstance(obj, dict):
            return {}
        out = {
            "allow": bool(obj.get("allow", True)),
            "confidence": max(0.0, min(1.0, float(obj.get("confidence", 0.0)))),
            "reason_codes": obj.get("reason_codes", []),
            "suggested_tone": str(obj.get("suggested_tone", "")).strip(),
        }
        if not isinstance(out["reason_codes"], list):
            out["reason_codes"] = []
        out["reason_codes"] = [
            str(x).strip() for x in out["reason_codes"] if str(x).strip()
        ]
        return out

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

    def _unit_gate_whitelist(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if self._is_session_whitelisted(session_key):
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "not_in_proactive_whitelist",
            f"session skip(whitelist) session={session_key}",
        )
        return True

    def _unit_gate_next_check(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if now_ts >= s.get("next_check_at", 0):
            return False
        self._maybe_log_status(session_key, s, now_ts, "waiting_next_check")
        return True

    def _unit_defer_session(
        self, session_key: str, s: Dict, now_ts: float, reason: str, debug_msg: str
    ):
        s["next_check_at"] = now_ts + self._randomized_interval()
        self._maybe_log_status(session_key, s, now_ts, reason)

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
        s.pop("decision_suggested_tone", None)
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
                session_key, s, now_ts, "trigger_success", force=False
            )
            return

        self._global_fail_streak += 1
        if self._global_fail_streak >= self._security_max_fail_streak():
            self._global_pause_until = now_ts + self._security_fail_pause_sec()
            self._debug(
                f"trigger safety pause fail_streak={self._global_fail_streak} pause_until={self._fmt_ts(self._global_pause_until)}"
            )
        self._maybe_log_status(session_key, s, now_ts, "trigger_failed", force=False)

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
        parts = [topic]
        try:
            if self._output_segment_enabled():
                split_parts = self._trim_reply_segments(
                    self._split_reply_segments(topic)
                )
                if split_parts:
                    parts = split_parts
        except Exception:
            parts = [topic]
        try:
            for i, part in enumerate(parts):
                chain = MessageChain().message(part)
                await self.context.send_message(unified_msg_origin, chain)
                if i < len(parts) - 1:
                    await asyncio.sleep(min(1.2, 0.15 + 0.02 * len(part)))
            self._debug(
                f"send proactive ok session={session_key} parts={len(parts)} topic={topic}"
            )
            return True, topic
        except Exception:
            try:
                for i, part in enumerate(parts):
                    await self.context.send_message(unified_msg_origin, [Plain(part)])
                    if i < len(parts) - 1:
                        await asyncio.sleep(min(1.2, 0.15 + 0.02 * len(part)))
                self._debug(
                    f"send proactive ok(fallback) session={session_key} parts={len(parts)} topic={topic}"
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
