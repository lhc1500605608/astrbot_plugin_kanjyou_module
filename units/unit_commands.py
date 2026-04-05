from typing import List

from astrbot.api.event import AstrMessageEvent


class CommandUnitsMixin:
    async def _cmd_idle_status(self, event: AstrMessageEvent):
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
        self.config["enabled"] = True
        self._save_webui_config()
        yield event.plain_result("已开启闲时主动聊天。")

    async def _cmd_idle_disable(self, event: AstrMessageEvent):
        self.config["enabled"] = False
        self._save_webui_config()
        yield event.plain_result("已关闭闲时主动聊天。")

    async def _cmd_idle_wl_add_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self.config["private_whitelist"]
        if user_id not in wl:
            wl.append(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已添加: {user_id}")

    async def _cmd_idle_wl_del_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self.config["private_whitelist"]
        if user_id in wl:
            wl.remove(user_id)
            self._save_webui_config()
        yield event.plain_result(f"私聊白名单已移除: {user_id}")

    async def _cmd_idle_wl_add_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self.config["group_whitelist"]
        if group_id not in wl:
            wl.append(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已添加: {group_id}")

    async def _cmd_idle_wl_del_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self.config["group_whitelist"]
        if group_id in wl:
            wl.remove(group_id)
            self._save_webui_config()
        yield event.plain_result(f"群聊白名单已移除: {group_id}")

    async def _cmd_idle_sleep_set(self, event: AstrMessageEvent, start_hm: str, end_hm: str):
        if not self._is_hhmm(start_hm) or not self._is_hhmm(end_hm):
            yield event.plain_result("格式错误，请使用 HH:MM，例如 /idle_sleep_set 23:30 08:00")
            return
        self.config["sleep_start"] = start_hm
        self.config["sleep_end"] = end_hm
        self._save_webui_config()
        yield event.plain_result(f"免打扰时段已设置为 {start_hm}-{end_hm}")

    async def _cmd_idle_test(self, event: AstrMessageEvent):
        if not self._is_whitelisted(event):
            yield event.plain_result("当前会话不在白名单，先加入白名单再测试。")
            return
        await self._send_proactive(event.unified_msg_origin, event, self._session_key(event), 0, None)
        yield event.plain_result("已发送一条主动话题测试消息。")
