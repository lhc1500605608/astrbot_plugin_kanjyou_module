from astrbot.api.event import AstrMessageEvent


class EventUnitsMixin:
    async def _touch_session_for_command(self, event: AstrMessageEvent):
        session_key = self._session_key(event)
        if not session_key:
            return
        now_ts = self._now().timestamp()
        async with self._lock:
            s = self._get_or_create_session(event)
            self._ensure_session_shape(s)
            # Command counts as user interaction, but doesn't consume extra dialogue mood.
            s["last_human_at"] = now_ts
            s["last_interaction_at"] = now_ts
            s["pending_human_reply"] = False
            s["next_check_at"] = now_ts + self._randomized_interval()
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by command session={session_key} last_interaction={self._fmt_ts(now_ts)}"
            )

    async def _evt_on_all_message(self, event: AstrMessageEvent):
        # Command path has highest priority for this plugin only:
        # skip plugin pipelines, but do not hijack AstrBot/global command abilities.
        text = self._extract_event_text(event)
        if text and self._is_plugin_command_text(text):
            session_key = self._session_key(event)
            await self._touch_session_for_command(event)
            self._debug(
                f"skip decision env by command session={session_key or '-'} text={text}"
            )
            return
        if text and self._is_command_like_text(text):
            session_key = self._session_key(event)
            await self._touch_session_for_command(event)
            self._debug(
                f"skip plugin pipeline by external command session={session_key or '-'} text={text}"
            )
            return

        session_key = self._session_key(event)
        if not session_key:
            self._debug("skip message: session key unavailable")
            return

        now_ts = self._now().timestamp()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        decided = None
        async with self._lock:
            s = self._get_or_create_session(event)
            self._ensure_session_shape(s)
            umo = str(s.get("unified_msg_origin") or umo)
            # Phase 2-A: settle at most one emotion event for this inbound message.
            try:
                decided = self._collect_inbound_emotion_event(
                    session_key, s, text, event, umo, now_ts
                )
            except Exception as exc:
                self._debug(f"emotion detect failed session={session_key} err={exc}")
                decided = None
            self._consume_session_mood_by_dialogue(s, now_ts)
            s["last_human_at"] = now_ts
            s["last_interaction_at"] = now_ts
            s["pending_human_reply"] = False
            s["no_reply_streak"] = 0
            s["next_check_at"] = now_ts + self._randomized_interval()
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by human session={session_key} last_interaction={self._fmt_ts(now_ts)} next_check={self._fmt_ts(s['next_check_at'])}"
            )
        if decided:
            event_type, dedupe_key, reason = decided
            await self._record_companion_emotion_event(
                umo, event_type, reason=reason, dedupe_key=dedupe_key
            )

    async def _evt_after_message_sent(self, event: AstrMessageEvent):
        session_key = self._session_key(event)
        if not session_key:
            return

        now_ts = self._now().timestamp()
        async with self._lock:
            s = self._get_or_create_session(event)
            self._ensure_session_shape(s)
            self._consume_session_mood_by_dialogue(s, now_ts)
            s["last_bot_at"] = now_ts
            s["last_interaction_at"] = now_ts
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by bot session={session_key} last_interaction={self._fmt_ts(now_ts)}"
            )
