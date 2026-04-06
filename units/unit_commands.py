from typing import List

from astrbot.api.event import AstrMessageEvent


class CommandUnitsMixin:
    def _selfcheck_flag(self, ok: bool) -> str:
        return "OK" if ok else "WARN"

    def _shield_command_from_llm(self, event: AstrMessageEvent):
        try:
            set_call = getattr(event, "should_call_llm", None)
            if callable(set_call):
                set_call(False)
        except Exception:
            pass
        try:
            if hasattr(event, "call_llm"):
                event.call_llm = False
        except Exception:
            pass
        try:
            stop = getattr(event, "stop_event", None)
            if callable(stop):
                stop()
        except Exception:
            pass

    async def _cmd_idle_status(self, event: AstrMessageEvent):
        self._shield_command_from_llm(event)
        now = self._now()
        session_key = self._session_key(event)
        if not session_key:
            yield event.plain_result("当前会话无法识别，无法查看 idle 状态。")
            return

        s = self._sessions.get(session_key)
        if not s:
            yield event.plain_result("当前会话还没有记录。先聊一句再查看状态。")
            return

        idle_sec = int(now.timestamp() - s.get("last_interaction_at", now.timestamp()))
        sleep_on = self._in_sleep_window(now)
        summary = (
            f"enabled={self.config['enabled']} | idle={idle_sec}s | "
            f"today_count={s.get('today_proactive_count', 0)} | "
            f"cooldown_until={self._fmt_ts(s.get('cooldown_until'))} | "
            f"sleep_mode={sleep_on}"
        )
        yield event.plain_result(summary)

    async def _cmd_idle_enable(self, event: AstrMessageEvent):
        self._shield_command_from_llm(event)
        self.config["enabled"] = True
        self._save_webui_config()
        yield event.plain_result("已开启闲时主动聊天。")

    async def _cmd_idle_disable(self, event: AstrMessageEvent):
        self._shield_command_from_llm(event)
        self.config["enabled"] = False
        self._save_webui_config()
        yield event.plain_result("已关闭闲时主动聊天。")

    async def _cmd_idle_wl_add_private(self, event: AstrMessageEvent, user_id: str):
        self._shield_command_from_llm(event)
        wl: List[str] = self.config["private_whitelist"]
        if user_id not in wl:
            wl.append(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已添加: {user_id}")

    async def _cmd_idle_wl_del_private(self, event: AstrMessageEvent, user_id: str):
        self._shield_command_from_llm(event)
        wl: List[str] = self.config["private_whitelist"]
        if user_id in wl:
            wl.remove(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已移除: {user_id}")

    async def _cmd_idle_wl_add_group(self, event: AstrMessageEvent, group_id: str):
        self._shield_command_from_llm(event)
        wl: List[str] = self.config["group_whitelist"]
        if group_id not in wl:
            wl.append(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已添加: {group_id}")

    async def _cmd_idle_wl_del_group(self, event: AstrMessageEvent, group_id: str):
        self._shield_command_from_llm(event)
        wl: List[str] = self.config["group_whitelist"]
        if group_id in wl:
            wl.remove(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已移除: {group_id}")

    async def _cmd_idle_sleep_set(
        self, event: AstrMessageEvent, start_hm: str, end_hm: str
    ):
        self._shield_command_from_llm(event)
        if not self._is_hhmm(start_hm) or not self._is_hhmm(end_hm):
            yield event.plain_result(
                "格式错误，请使用 HH:MM，例如 /idle_sleep_set 23:30 08:00"
            )
            return
        self.config["sleep_start"] = start_hm
        self.config["sleep_end"] = end_hm
        self._save_webui_config()
        yield event.plain_result(f"免打扰时段已设置为 {start_hm}-{end_hm}")

    async def _cmd_idle_test(self, event: AstrMessageEvent):
        self._shield_command_from_llm(event)
        if not self._is_whitelisted(event):
            yield event.plain_result("当前会话不在白名单，先加入白名单再测试。")
            return
        await self._send_proactive(
            event.unified_msg_origin, event, self._session_key(event), 0, None
        )
        yield event.plain_result("已发送一条主动话题测试消息。")

    async def _cmd_idle_decision_status(self, event: AstrMessageEvent):
        self._shield_command_from_llm(event)
        status = self._decision_status_summary()
        yield event.plain_result(status)

    async def _cmd_idle_decision_last(self, event: AstrMessageEvent):
        self._shield_command_from_llm(event)
        session_key = self._session_key(event)
        if not session_key:
            yield event.plain_result("当前会话无法识别。")
            return
        last = self._decision_last_for_session(session_key)
        if not last:
            yield event.plain_result("当前会话暂无决策记录。")
            return
        summary = (
            f"session={session_key} allow={last.get('allow')} "
            f"confidence={last.get('confidence')} "
            f"reason={','.join(last.get('reason_codes', [])[:3]) or '-'} "
            f"mode={last.get('mode', '-')}"
        )
        yield event.plain_result(summary)

    async def _cmd_idle_selfcheck(self, event: AstrMessageEvent):
        self._shield_command_from_llm(event)
        now = self._now()
        session_key = self._session_key(event)
        allow_session = bool(session_key) and self._is_whitelisted(event)
        wait_enabled = bool(self.config.get("dialogue_wait_enabled", True))
        wait_sec = int(self.config.get("dialogue_wait_timeout_sec", 4) or 4)
        seg_enabled = bool(self.config.get("output_segment_enabled", True))
        seg_parts = int(self.config.get("output_segment_max_parts", 4) or 4)
        seg_chars = int(self.config.get("output_segment_max_chars", 120) or 120)
        sleep_start = str(self.config.get("sleep_start", "23:30"))
        sleep_end = str(self.config.get("sleep_end", "08:00"))
        in_sleep = self._in_sleep_window(now)
        buffers = (
            len(self._dialogue_wait_buffers)
            if isinstance(self._dialogue_wait_buffers, dict)
            else 0
        )
        wait_tasks = (
            len(self._dialogue_wait_tasks)
            if isinstance(self._dialogue_wait_tasks, dict)
            else 0
        )
        image_capable = callable(getattr(self, "text_to_image", None))
        lite_enabled = self._to_bool(
            self.config.get("lite_llm_enabled"),
            True,
        )

        provider_id = ""
        if session_key:
            try:
                provider_id = (
                    await self.context.get_current_chat_provider_id(
                        event.unified_msg_origin
                    )
                ) or ""
                provider_id = str(provider_id).strip()
            except Exception:
                provider_id = ""

        checks = []
        checks.append(
            (
                "plugin_enabled",
                bool(self.config.get("enabled", True)),
                f"enabled={self.config.get('enabled', True)}",
            )
        )
        checks.append(
            (
                "session_whitelist",
                allow_session,
                f"session={session_key or '-'} whitelisted={allow_session}",
            )
        )
        checks.append(
            (
                "wait_window",
                wait_enabled and 1 <= wait_sec <= 10,
                f"enabled={wait_enabled} timeout={wait_sec}s (建议 3-5s)",
            )
        )
        checks.append(
            (
                "segment_policy",
                seg_enabled and seg_parts >= 1 and seg_chars >= 30,
                f"enabled={seg_enabled} parts={seg_parts} chars={seg_chars}",
            )
        )
        checks.append(
            (
                "sleep_window",
                self._is_hhmm(sleep_start) and self._is_hhmm(sleep_end),
                f"{sleep_start}-{sleep_end} in_sleep={in_sleep}",
            )
        )
        checks.append(
            (
                "main_provider",
                bool(provider_id),
                f"provider={'set' if provider_id else 'missing'}",
            )
        )
        checks.append(
            (
                "image_fallback",
                image_capable,
                f"text_to_image={'ready' if image_capable else 'missing'}",
            )
        )
        checks.append(
            (
                "lite_llm",
                True,
                f"enabled={lite_enabled} (当前仅用于浅任务/辅助能力，不负责分段)",
            )
        )

        warn_count = sum(1 for _, ok, _ in checks if not ok)
        header = (
            f"[idle-selfcheck] {self._selfcheck_flag(warn_count == 0)} "
            f"warn={warn_count} now={now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        lines = [header]
        for name, ok, desc in checks:
            lines.append(f"- {name}: {self._selfcheck_flag(ok)} | {desc}")
        lines.append(
            f"- runtime: wait_buffers={buffers} wait_tasks={wait_tasks} loop_alive={bool(self._loop_task and not self._loop_task.done())}"
        )
        lines.append("建议：如果 session_whitelist/WARN，先把当前会话加入白名单再测。")
        yield event.plain_result("\n".join(lines))
