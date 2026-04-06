from astrbot.api.event import AstrMessageEvent


class EventUnitsMixin:
    async def _evt_on_all_message(self, event: AstrMessageEvent):
        # Command path has highest priority:
        # bypass session touching and all downstream decision environments.
        text = self._extract_event_text(event)
        if text and self._is_plugin_command_text(text):
            session_key = self._session_key(event)
            self._clear_wait_buffer_for_session(session_key)
            self._suppress_default_llm(
                event, "command_high_priority_bypass", stop_propagation=False
            )
            self._debug(
                f"skip decision env by command session={session_key or '-'} text={text}"
            )
            return

        session_key = self._session_key(event)
        if not session_key:
            self._debug("skip message: session key unavailable")
            return

        if not self._is_whitelisted(event):
            self._debug(f"skip message: not in whitelist session={session_key}")
            return

        now_ts = self._now().timestamp()
        async with self._lock:
            s = self._get_or_create_session(event)
            self._ensure_session_shape(s)
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
        await self._maybe_reply_shallow_query_with_wait(event)

    async def _evt_after_message_sent(self, event: AstrMessageEvent):
        session_key = self._session_key(event)
        if not session_key:
            return

        if not self._is_whitelisted(event):
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
