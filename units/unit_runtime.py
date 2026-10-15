import asyncio
import random
import uuid
from datetime import datetime
from typing import Dict, Optional

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain


class _ProactiveDeliveryFailure(Exception):
    """Internal marker for an explicit send_message(False) delivery failure."""


class RuntimeUnitsMixin:
    async def _idle_loop(self):
        while True:
            try:
                await asyncio.sleep(self.config["check_interval_sec"])
                await self._check_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_error(
                    "idle_loop_error", f"idle loop error err_type={type(exc).__name__}"
                )

    async def _check_sessions(self):
        await self._flush_all_attempt_outcomes()
        if not self.config.get("enabled", True):
            self._debug_throttled("loop_plugin_disabled", "loop skip: plugin disabled")
            return

        # v2.12.0 Phase 3-B: low-frequency, fail-silent life-content refresh.
        # Runs in the plugin's own loop (never the passive reply path); the core
        # gates by min-interval / daily cap, so an extra call is a cheap no-op.
        await self._companion_maybe_refresh_life_content()

        now = self._now()
        if self._in_sleep_window(now):
            self._debug_throttled(
                "loop_sleep_window",
                f"loop skip: in sleep window now={now.strftime('%H:%M')}",
            )
            return

        now_ts = now.timestamp()
        if not self._unit_global_guard(now_ts):
            return

        async with self._lock:
            sessions = iter(list(self._sessions.items()))

        async def consume():
            for session_key, session in sessions:
                changed = await self._process_session(session_key, session, now, now_ts)
                if changed:
                    async with self._lock:
                        self._save_state()

        # A fixed worker pool bounds both queued work and generation concurrency.
        await asyncio.gather(consume(), consume())

    async def _process_session(
        self, session_key: str, s: Dict, now: datetime, now_ts: float
    ) -> bool:
        if not isinstance(s, dict) or not self._session_identity(session_key, s):
            return False
        self._unit_rollover_counters(s, now)
        self._recover_session_mood(s, now_ts)
        self._update_mood_low_streak(s)
        # Phase 2-A: settle a stale proactive receipt as ignored_proactive.
        receipt_settled = await self._settle_expired_receipt(s, now_ts)

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
            return bool(decision.get("state_changed", False)) or receipt_settled

        idle_sec = float(
            decision.get("idle_sec", now_ts - s.get("last_interaction_at", now_ts))
        )
        decay = float(decision.get("decay", self._no_reply_decay_factor(s)))
        umo = str(decision.get("umo", s.get("unified_msg_origin")) or "")
        success, sent_text = await self._unit_execute_send(
            umo,
            session_key,
            idle_sec,
            s,
        )
        if not success:
            s.pop("companion_open_thread_followup", None)
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

    # ------------------------------------------------------------------ #
    # Phase 2 send pipeline helpers
    # ------------------------------------------------------------------ #
    def _proactive_generation_timeout_sec(self) -> float:
        """Bound the whole generation pipeline without adding a config key."""
        try:
            return max(
                1.0,
                max(60.0, float(self.config.get("lite_llm_timeout_sec", 6)))
                + float(self.config.get("memory_recall_timeout_sec", 2))
                + float(self.config.get("companion_timeout_sec", 1.5)),
            )
        except (TypeError, ValueError):
            return 63.5

    def _proactive_persona_id(self) -> str:
        """Current configured persona ID only; never another session global."""
        return str(self.config.get("persona_id") or "").strip()

    def _capture_proactive_baseline(self, session_key: str, session: Dict) -> Dict:
        return {
            "session_key": session_key,
            "unified_msg_origin": str(
                (session or {}).get("unified_msg_origin")
                or self._canonical_umo_from_session_key(session_key)
                or ""
            ),
            "revision": int((session or {}).get("user_message_revision", 0) or 0),
            "last_human_at": float((session or {}).get("last_human_at", 0.0) or 0.0),
            "origin": str((session or {}).get("unified_msg_origin") or ""),
            "identity": dict((session or {}).get("identity") or {}),
        }

    def _canonical_umo_from_session_key(self, session_key: str) -> str:
        identity = self._parse_canonical_session_key(session_key)
        return identity["umo"] if identity else ""

    def _proactive_new_human_message(
        self, session_key: str, baseline: Dict, now_ts: float
    ) -> bool:
        session = self._sessions.get(session_key)
        if not isinstance(session, dict):
            return True
        # Inbound handlers advance this revision; a send never impersonates a human.
        if int(session.get("user_message_revision", 0) or 0) != int(
            baseline.get("revision", 0)
        ):
            return True
        return float(session.get("last_human_at", 0.0) or 0.0) > float(
            baseline.get("last_human_at", 0.0) or 0.0
        )

    def _proactive_delivery_gate(
        self,
        session_key: str,
        session: Dict,
        baseline: Dict,
        *,
        source: str,
        now_ts: float,
    ) -> Optional[str]:
        """Fresh, strict pre-delivery gate; None means send is still allowed."""
        if self._terminating:
            return "shutdown"
        if not isinstance(session, dict):
            return "session_missing"
        if not self._session_identity(session_key, session):
            return "identity_mismatch"
        if not self._session_state_whitelisted(session_key, session):
            return "whitelist_removed"
        current = self._sessions.get(session_key)
        if current is not session:
            return "session_replaced"
        if dict(session.get("identity") or {}) != baseline.get("identity") or str(
            session.get("unified_msg_origin") or ""
        ) != baseline.get("origin"):
            return "identity_changed"
        if self._proactive_new_human_message(session_key, baseline, now_ts):
            return "new_human_message"
        if source == "manual":
            # Explicit WebUI test intentionally bypasses schedule cooldown and
            # DND/sleep only; hard identity/whitelist/shutdown/new-human guards
            # above remain authoritative.
            return None
        if not self.config.get("enabled", True):
            return "disabled"
        if now_ts < self._global_pause_until:
            return "safety_pause"
        if self._in_sleep_window(self._now()):
            return "night_quiet"
        if now_ts < float(session.get("cooldown_until", 0.0) or 0.0):
            return "cooldown_guard"
        if int(session.get("today_proactive_count", 0) or 0) >= int(
            self._max_per_session_per_day()
        ):
            return "daily_limit"
        self._trim_global_send_history(now_ts)
        if (
            len(self._global_send_history) + self._global_send_inflight
            >= self._security_global_hourly_cap()
        ):
            return "global_hourly_cap"
        period = self._get_period(self._now())
        if self._period_quota_enabled() and period:
            counts = session.get("period_proactive_count") or {}
            if int(counts.get(period, 0)) >= self._effective_period_quota_limit(
                period, self._now()
            ):
                return "period_limit"
        if self._require_human_reply_before_next_proactive() and session.get(
            "pending_human_reply"
        ):
            return "await_human_reply"
        return None

    def _proactive_attempt_metadata(
        self,
        session_key: str,
        umo: str,
        baseline: Dict,
        *,
        source: str,
        now_ts: float,
    ) -> Dict:
        """Metadata persisted before the first external send (never text)."""
        return {
            "attempt_id": str(uuid.uuid4()),
            "session_key": session_key,
            "umo": umo,
            "source": source,
            "persona_id": self._proactive_persona_id(),
            "revision": int(baseline.get("revision", 0) or 0),
            "last_human_at": float(baseline.get("last_human_at", 0.0) or 0.0),
            "origin": str(baseline.get("origin") or ""),
            "identity": dict(baseline.get("identity") or {}),
            "started_at": now_ts,
            "state": "pending",
        }

    async def _run_proactive_generation(
        self,
        unified_msg_origin: str,
        session_key: str,
        idle_sec: float,
        session: Dict,
    ) -> str:
        """Owned, bounded generation task; cancellation propagates safely."""
        if len(self._proactive_generation_tasks) >= 8:
            raise RuntimeError("generation_queue_full")

        async def bounded_generate():
            async with self._proactive_generation_semaphore:
                return await self._generate_proactive_text(
                    unified_msg_origin, session_key, idle_sec, session
                )

        task = asyncio.create_task(bounded_generate())
        self._proactive_generation_tasks.add(task)
        try:
            return await asyncio.wait_for(
                task, timeout=self._proactive_generation_timeout_sec()
            )
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise
        finally:
            self._proactive_generation_tasks.discard(task)
            if not task.done():
                task.cancel()

    async def _cancel_proactive_generation_tasks(self) -> None:
        tasks = list(
            (self._proactive_generation_tasks | self._proactive_send_tasks)
            - {asyncio.current_task()}
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _flush_all_attempt_outcomes(self) -> None:
        for session in list(getattr(self, "_sessions", {}).values()):
            if isinstance(session, dict):
                try:
                    await self._flush_attempt_outcomes(session)
                except Exception:
                    pass

    def _remember_proactive_attempt(
        self, session: Dict, metadata: Dict, *, now_ts: float
    ) -> None:
        if not isinstance(session, dict):
            return
        # Pending outcome is keyed by attempt_id; it contains no message text.
        pending = session.setdefault("proactive_attempt_pending", {})
        if not isinstance(pending, dict):
            pending = {}
            session["proactive_attempt_pending"] = pending
        pending[metadata["attempt_id"]] = dict(metadata)
        session["proactive_attempt_inflight"] = metadata["attempt_id"]
        session["proactive_attempt_updated_at"] = now_ts

    def _finish_proactive_attempt(
        self, session: Dict, attempt_id: str, *, state: str, reason_code: str
    ) -> None:
        if not isinstance(session, dict):
            return
        pending = session.get("proactive_attempt_pending")
        if isinstance(pending, dict):
            item = pending.get(attempt_id)
            if isinstance(item, dict):
                item["state"] = state
                item["reason_code"] = reason_code
                item["finished_at"] = self._now().timestamp()
        if session.get("proactive_attempt_inflight") == attempt_id:
            session.pop("proactive_attempt_inflight", None)
            session["proactive_attempt_inflight"] = None

    async def _record_attempt_result(
        self, session: Dict, umo: str, metadata: Dict, sent: bool, reason_code: str
    ) -> bool:
        """Call the adapter immediately after the actual send outcome."""
        self._finish_proactive_attempt(
            session,
            metadata["attempt_id"],
            state="sent" if sent else "blocked",
            reason_code=reason_code,
        )
        try:
            result = await self._record_attempt_outcome(
                session,
                umo,
                event_id=metadata["attempt_id"],
                persona_id=metadata["persona_id"],
                sent=sent,
                reason_code=reason_code,
            )
        except Exception:
            result = False
        if result or not self._companion_enabled():
            pending = session.get("proactive_attempt_pending")
            if isinstance(pending, dict):
                pending.pop(metadata["attempt_id"], None)
        self._last_proactive_attempt_result = {
            "attempt_id": metadata["attempt_id"],
            "sent": sent,
            "reason_code": reason_code,
            "source": metadata.get("source", ""),
        }
        self._save_state()
        return bool(result)

    def _cleanup_expired_attempt_metadata(self, session: Dict, now_ts: float) -> None:
        pending = (
            session.get("proactive_attempt_pending")
            if isinstance(session, dict)
            else None
        )
        if not isinstance(pending, dict):
            return
        expired = []
        for attempt_id, item in pending.items():
            if not isinstance(item, dict):
                expired.append(attempt_id)
                continue
            started = float(item.get("started_at", 0.0) or 0.0)
            if now_ts - started > 300:
                expired.append(attempt_id)
        for attempt_id in expired:
            pending.pop(attempt_id, None)

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
                False,
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
        if self._unit_gate_persona_state(session_key, s, now_ts):
            state_changed = True
            return self._decision_result(
                False,
                0.94,
                ["persona_state_low"],
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
        # companion 配额软闸：只降权、绝不提升；现有安全闸始终权威。
        companion_soft = self._companion_quota_soft_gate(s)
        if companion_soft:
            p = max(0.0, p * 0.5)
        # Phase 2-C expression 软闸：proactive_bias < 0 时只降权（一次性）。
        expression_bias = self._companion_expression_proactive_soft_gate(s)
        if expression_bias is not None and expression_bias < 0:
            p = max(0.0, p * max(0.0, 1.0 + expression_bias))
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
        if companion_soft:
            reason_codes.append("companion_quota_soft")
        if expression_bias is not None and expression_bias < 0:
            reason_codes.append("companion_expression_damp")

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

        confidence = 0.7
        suggested_tone = self._style_hint(session_key, s, idle_sec)

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

        # Phase 3-C2：群聊参与闸门（companion advisory）。作为最后一跳闸门，
        # 只有前面所有闸门都放行时才读取；allow=false 时不接话并落日志。
        # 契约缺失/异常/超时一律 fail-open，回落 v2.10.2 行为。
        if session_key.startswith("group:"):
            group_allow, group_view = await self._companion_group_participation(umo)
            if not group_allow:
                group_reason = "blocked"
                participation = (
                    group_view.get("participation")
                    if isinstance(group_view, dict)
                    else None
                )
                if isinstance(participation, dict):
                    group_reason = str(participation.get("reason") or group_reason)
                code = f"companion_group_{group_reason}"
                self._debug(
                    f"group gate blocked session={session_key} reason={group_reason}"
                )
                self._unit_defer_session(
                    session_key,
                    s,
                    now_ts,
                    code,
                    (
                        f"session skip({code}) session={session_key} "
                        f"reason={group_reason}"
                    ),
                )
                state_changed = True
                reason_codes.append(code)
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

    def _unit_global_guard(self, now_ts: float) -> bool:
        if now_ts < self._global_pause_until:
            self._debug_throttled(
                "global_pause_active",
                f"loop skip: global pause active until={self._fmt_ts(self._global_pause_until)}",
            )
            return False

        self._trim_global_send_history(now_ts)
        if len(self._global_send_history) >= self._security_global_hourly_cap():
            self._debug_throttled(
                "global_hourly_cap_reached",
                f"loop skip: global hourly cap reached count={len(self._global_send_history)} cap={self._security_global_hourly_cap()}",
            )
            return False
        return True

    def _unit_rollover_counters(self, s: Dict, now: datetime):
        self._ensure_session_shape(s)
        self._rollover_daily_counter(s, now)
        self._rollover_period_counter(s, now)

    def _unit_gate_whitelist(self, session_key: str, s: Dict, now_ts: float) -> bool:
        if self._session_state_whitelisted(session_key, s):
            return False
        # Whitelist is a hard skip gate for proactive sends only.
        # Do not mutate session timing here, otherwise state will be persisted every loop.
        self._maybe_log_status(session_key, s, now_ts, "not_in_proactive_whitelist")
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

    def _unit_gate_persona_state(
        self, session_key: str, s: Dict, now_ts: float
    ) -> bool:
        if not self._persona_state_enabled():
            return False
        idle_sec = max(0.0, now_ts - float(s.get("last_interaction_at", now_ts)))
        state = self._current_persona_state(session_key, s, idle_sec)
        if not self._persona_state_suppresses_proactive(state):
            return False
        self._unit_defer_session(
            session_key,
            s,
            now_ts,
            "persona_state_low",
            (
                f"session skip(persona_state_low) session={session_key} "
                f"state={state} mood={float(s.get('mood', self._mood_initial())):.2f} "
                f"low_streak={int(s.get('mood_low_streak', 0))}"
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
        source: str = "scheduled",
    ) -> tuple[bool, str]:
        if source not in ("manual", "scheduled"):
            source = "scheduled"
        if not session_key:
            session_key = "private:" + unified_msg_origin if unified_msg_origin else ""
        session = (
            session if isinstance(session, dict) else self._sessions.get(session_key)
        )
        if not isinstance(session, dict):
            self._debug(
                f"send proactive blocked session={session_key} reason=session_missing"
            )
            return False, ""
        if unified_msg_origin != str(session.get("unified_msg_origin") or ""):
            self._debug(
                f"send proactive blocked session={session_key} reason=origin_mismatch"
            )
            return False, ""

        # Complete-UMO per-session lock. A second concurrent manual request is
        # busy; two sequential requests both revalidate under fresh state.
        lock = self._proactive_session_locks.setdefault(session_key, asyncio.Lock())
        if len(session.get("companion_attempt_receipt_pending") or {}) >= 16:
            self._debug("send proactive blocked reason=receipt_backlog_full")
            return False, ""
        owner = object()
        if session_key in self._proactive_session_inflight:
            self._debug(f"send proactive busy session={session_key} source={source}")
            return False, ""
        self._proactive_session_inflight.add(session_key)
        self._proactive_session_lock_owners[session_key] = owner
        send_task = asyncio.current_task()
        self._proactive_send_tasks.add(send_task)
        reserved = False
        try:
            if lock.locked():
                self._debug(
                    f"send proactive busy session={session_key} source={source}"
                )
                return False, ""
            async with lock:
                now_ts = self._now().timestamp()
                async with self._lock:
                    baseline = self._capture_proactive_baseline(session_key, session)
                    baseline["revision"] = (
                        int(session.get("user_message_revision", 0) or 0) + 1
                    )
                    session["user_message_revision"] = baseline["revision"]
                    self._cleanup_expired_attempt_metadata(session, now_ts)
                    self._remember_proactive_attempt(
                        session,
                        self._proactive_attempt_metadata(
                            session_key,
                            unified_msg_origin,
                            baseline,
                            source=source,
                            now_ts=now_ts,
                        ),
                        now_ts=now_ts,
                    )
                    self._save_state()
                metadata = session["proactive_attempt_pending"][
                    session["proactive_attempt_inflight"]
                ]

                try:
                    topic = await self._run_proactive_generation(
                        unified_msg_origin, session_key, idle_sec, session
                    )
                except asyncio.CancelledError:
                    await self._record_attempt_result(
                        session,
                        unified_msg_origin,
                        metadata,
                        False,
                        "generation_cancelled",
                    )
                    return False, ""
                except asyncio.TimeoutError:
                    await self._record_attempt_result(
                        session,
                        unified_msg_origin,
                        metadata,
                        False,
                        "generation_timeout",
                    )
                    return False, ""
                except Exception:
                    await self._record_attempt_result(
                        session,
                        unified_msg_origin,
                        metadata,
                        False,
                        "generation_failed",
                    )
                    return False, ""

                delivery_now_ts = self._now().timestamp()
                gate_reason = self._proactive_delivery_gate(
                    session_key,
                    session,
                    baseline,
                    source=source,
                    now_ts=delivery_now_ts,
                )
                if gate_reason:
                    await self._record_attempt_result(
                        session,
                        unified_msg_origin,
                        metadata,
                        False,
                        gate_reason,
                    )
                    self._debug(
                        f"send proactive blocked session={session_key} "
                        f"reason={gate_reason} source={source}"
                    )
                    return False, ""

                if source == "scheduled":
                    self._global_send_inflight += 1
                    reserved = True
                delivered_segments = 0

                async def _send_segment(segment: str) -> None:
                    nonlocal delivered_segments
                    chain = MessageChain().message(segment)
                    delivered = await self.context.send_message(
                        unified_msg_origin, chain
                    )
                    # AstrBot 4.23.2 send_message returns bool; False means no
                    # platform matched. Treat it as definitive failure.
                    if delivered is False:
                        self._debug(
                            "send_message returned False (platform not found) "
                            f"session={session_key}"
                        )
                        raise _ProactiveDeliveryFailure()
                    delivered_segments += 1

                sent = False
                failure_reason = "send_failed"
                try:
                    await self._dispatch_reply_segments(
                        _send_segment, topic, proactive=True
                    )
                    sent = True
                except _ProactiveDeliveryFailure:
                    failure_reason = "send_message_false"
                except Exception as initial_error:
                    try:
                        # Only a first-call payload type mismatch can safely use
                        # the legacy payload form; unknown or partial delivery
                        # is never replayed as a full message.
                        if delivered_segments or not isinstance(
                            initial_error, TypeError
                        ):
                            raise initial_error
                        delivered = await self.context.send_message(
                            unified_msg_origin, [Plain(topic)]
                        )
                        if delivered is False:
                            raise _ProactiveDeliveryFailure()
                        sent = True
                    except _ProactiveDeliveryFailure:
                        failure_reason = "send_message_false"
                    except Exception as exc:
                        self._log_error(
                            "send_failed",
                            f"send failed err_type={type(exc).__name__}",
                        )
                        if event:
                            await event.send(
                                event.plain_result(
                                    "主动消息发送失败，请检查适配器是否支持主动消息。"
                                )
                            )

                if not sent:
                    if source == "scheduled":
                        async with self._lock:
                            self._unit_finalize_result(
                                session_key,
                                session,
                                False,
                                "",
                                self._get_period(self._now()),
                                idle_sec,
                                self._no_reply_decay_factor(session),
                                self._now(),
                                delivery_now_ts,
                            )
                    await self._record_attempt_result(
                        session,
                        unified_msg_origin,
                        metadata,
                        False,
                        failure_reason,
                    )
                    return False, ""

                async with self._lock:
                    if source == "scheduled":
                        self._unit_finalize_result(
                            session_key,
                            session,
                            True,
                            topic,
                            self._get_period(self._now()),
                            idle_sec,
                            self._no_reply_decay_factor(session),
                            self._now(),
                            delivery_now_ts,
                        )
                    else:
                        session["manual_proactive_count"] = (
                            int(session.get("manual_proactive_count", 0)) + 1
                        )
                        session["last_manual_proactive_at"] = delivery_now_ts
                    self._save_state()
                await self._begin_proactive_receipt(
                    session,
                    unified_msg_origin,
                    delivery_now_ts,
                    attempt_id=metadata["attempt_id"],
                )
                if source == "scheduled":
                    await self._commit_open_thread_followup(
                        session, unified_msg_origin, delivery_now_ts
                    )

                # Adapter receipt is immediately after the actual send. Do not
                # expose or persist generated message text in attempt metadata.
                await self._record_attempt_result(
                    session,
                    unified_msg_origin,
                    metadata,
                    True,
                    "ok",
                )
                self._debug(
                    f"send proactive ok session={session_key} chars={len(topic)} "
                    f"source={source}"
                )
                self._remember_proactive_text(session_key, topic, session)
                return True, topic
        finally:
            self._proactive_send_tasks.discard(send_task)
            if reserved:
                self._global_send_inflight -= 1
            self._proactive_session_inflight.discard(session_key)
            if self._proactive_session_lock_owners.get(session_key) is owner:
                self._proactive_session_lock_owners.pop(session_key, None)
                self._proactive_session_locks.pop(session_key, None)
