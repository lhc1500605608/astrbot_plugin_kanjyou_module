import asyncio
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


@register("kanjyou_idle_proactive", "shangtang", "闲时主动聊天：分会话计时、白名单、夜间免打扰", "1.0.0")
class KanjyouIdleProactivePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._cfg_path = Path(__file__).parent / "idle_config.json"
        self._state_path = Path(__file__).parent / "idle_state.json"
        self._config = self._load_config()
        self._sessions: Dict[str, Dict] = self._load_state()
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._idle_loop())
        logger.info("[idle-proactive] initialized")
        self._debug("plugin initialize complete")

    async def terminate(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._save_state()
        logger.info("[idle-proactive] terminated")
        self._debug("plugin terminate complete")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
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
            s["last_human_at"] = now_ts
            s["last_interaction_at"] = now_ts
            s["next_check_at"] = now_ts + self._randomized_interval()
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by human session={session_key} last_interaction={self._fmt_ts(now_ts)} next_check={self._fmt_ts(s['next_check_at'])}"
            )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        session_key = self._session_key(event)
        if not session_key:
            return

        if not self._is_whitelisted(event):
            return

        now_ts = self._now().timestamp()
        async with self._lock:
            s = self._get_or_create_session(event)
            s["last_bot_at"] = now_ts
            s["last_interaction_at"] = now_ts
            self._sessions[session_key] = s
            self._save_state()
            self._debug(
                f"touch by bot session={session_key} last_interaction={self._fmt_ts(now_ts)}"
            )

    @filter.command("idle_status")
    async def idle_status(self, event: AstrMessageEvent):
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
            f"enabled={self._config['enabled']} | idle={idle_sec}s | "
            f"today_count={s.get('today_proactive_count', 0)} | "
            f"cooldown_until={self._fmt_ts(s.get('cooldown_until'))} | "
            f"sleep_mode={sleep_on} | debug_log={self._config.get('debug_log', False)}"
        )
        yield event.plain_result(summary)

    @filter.command("idle_enable")
    async def idle_enable(self, event: AstrMessageEvent):
        self._config["enabled"] = True
        self._save_config()
        yield event.plain_result("已开启闲时主动聊天。")

    @filter.command("idle_disable")
    async def idle_disable(self, event: AstrMessageEvent):
        self._config["enabled"] = False
        self._save_config()
        yield event.plain_result("已关闭闲时主动聊天。")

    @filter.command("idle_debug_on")
    async def idle_debug_on(self, event: AstrMessageEvent):
        self._config["debug_log"] = True
        self._save_config()
        yield event.plain_result("已开启 idle debug 日志。")

    @filter.command("idle_debug_off")
    async def idle_debug_off(self, event: AstrMessageEvent):
        self._config["debug_log"] = False
        self._save_config()
        yield event.plain_result("已关闭 idle debug 日志。")

    @filter.command("idle_wl_add_private")
    async def idle_wl_add_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self._config["private_whitelist"]
        if user_id not in wl:
            wl.append(user_id)
            self._save_config()
        yield event.plain_result(f"私聊白名单已添加: {user_id}")

    @filter.command("idle_wl_del_private")
    async def idle_wl_del_private(self, event: AstrMessageEvent, user_id: str):
        wl: List[str] = self._config["private_whitelist"]
        if user_id in wl:
            wl.remove(user_id)
            self._save_config()
        yield event.plain_result(f"私聊白名单已移除: {user_id}")

    @filter.command("idle_wl_add_group")
    async def idle_wl_add_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self._config["group_whitelist"]
        if group_id not in wl:
            wl.append(group_id)
            self._save_config()
        yield event.plain_result(f"群聊白名单已添加: {group_id}")

    @filter.command("idle_wl_del_group")
    async def idle_wl_del_group(self, event: AstrMessageEvent, group_id: str):
        wl: List[str] = self._config["group_whitelist"]
        if group_id in wl:
            wl.remove(group_id)
            self._save_config()
        yield event.plain_result(f"群聊白名单已移除: {group_id}")

    @filter.command("idle_sleep_set")
    async def idle_sleep_set(self, event: AstrMessageEvent, start_hm: str, end_hm: str):
        if not self._is_hhmm(start_hm) or not self._is_hhmm(end_hm):
            yield event.plain_result("格式错误，请使用 HH:MM，例如 /idle_sleep_set 23:30 08:00")
            return
        self._config["sleep_windows"] = [{"start": start_hm, "end": end_hm}]
        self._save_config()
        yield event.plain_result(f"免打扰时段已设置为 {start_hm}-{end_hm}")

    @filter.command("idle_test")
    async def idle_test(self, event: AstrMessageEvent):
        if not self._is_whitelisted(event):
            yield event.plain_result("当前会话不在白名单，先加入白名单再测试。")
            return
        await self._send_proactive(event.unified_msg_origin, event)
        yield event.plain_result("已发送一条主动话题测试消息。")

    async def _idle_loop(self):
        while True:
            try:
                await asyncio.sleep(self._config["check_interval_sec"])
                await self._check_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[idle-proactive] idle loop error: {exc}")

    async def _check_sessions(self):
        if not self._config.get("enabled", True):
            self._debug("loop skip: plugin disabled")
            return

        now = self._now()
        if self._in_sleep_window(now):
            self._debug(f"loop skip: in sleep window now={now.strftime('%H:%M')}")
            return

        now_ts = now.timestamp()
        changed = False

        async with self._lock:
            for session_key, s in list(self._sessions.items()):
                self._rollover_daily_counter(s, now)

                if now_ts < s.get("next_check_at", 0):
                    self._debug(
                        f"session skip(next_check) session={session_key} next_check={self._fmt_ts(s.get('next_check_at'))}"
                    )
                    continue

                if now_ts < s.get("cooldown_until", 0):
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._debug(
                        f"session skip(cooldown) session={session_key} cooldown_until={self._fmt_ts(s.get('cooldown_until'))}"
                    )
                    continue

                if s.get("today_proactive_count", 0) >= self._config["max_per_session_per_day"]:
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._debug(
                        f"session skip(limit) session={session_key} today_count={s.get('today_proactive_count', 0)}"
                    )
                    continue

                idle_sec = now_ts - s.get("last_interaction_at", now_ts)
                if idle_sec < self._config["min_idle_sec"]:
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._debug(
                        f"session skip(idle_short) session={session_key} idle_sec={int(idle_sec)} min_idle={self._config['min_idle_sec']}"
                    )
                    continue

                should_trigger = self._should_trigger(float(idle_sec))
                if not should_trigger:
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._debug(
                        f"session skip(probability) session={session_key} idle_sec={int(idle_sec)}"
                    )
                    continue

                umo = s.get("unified_msg_origin")
                if not umo:
                    s["next_check_at"] = now_ts + self._randomized_interval()
                    changed = True
                    self._debug(f"session skip(no_origin) session={session_key}")
                    continue

                success = await self._send_proactive(umo, None, session_key)
                s["next_check_at"] = now_ts + self._randomized_interval()
                if success:
                    s["last_bot_at"] = now_ts
                    s["last_interaction_at"] = now_ts
                    s["today_proactive_count"] = int(s.get("today_proactive_count", 0)) + 1
                    s["cooldown_until"] = now_ts + self._config["cooldown_sec"]
                    self._debug(
                        f"session trigger(success) session={session_key} idle_sec={int(idle_sec)} today_count={s['today_proactive_count']}"
                    )
                else:
                    self._debug(f"session trigger(failed) session={session_key}")
                changed = True

            if changed:
                self._save_state()
                self._debug("state persisted")

    async def _send_proactive(
        self, unified_msg_origin: str, event: Optional[AstrMessageEvent], session_key: str = ""
    ) -> bool:
        topic = self._pick_topic(session_key)
        try:
            chain = MessageChain().message(topic)
            await self.context.send_message(unified_msg_origin, chain)
            self._debug(f"send proactive ok session={session_key} topic={topic}")
            return True
        except Exception:
            try:
                # 兼容部分适配器对 MessageChain 构造差异
                await self.context.send_message(unified_msg_origin, [Plain(topic)])
                self._debug(f"send proactive ok(fallback) session={session_key} topic={topic}")
                return True
            except Exception as exc:
                logger.error(f"[idle-proactive] send failed: {exc}")
                self._debug(f"send proactive failed session={session_key} err={exc}")
                if event:
                    await event.send(event.plain_result("主动消息发送失败，请检查适配器是否支持主动消息。"))
                return False

    def _should_trigger(self, idle_sec: float) -> bool:
        min_idle = float(self._config["min_idle_sec"])
        max_idle = float(self._config["max_idle_sec"])

        if idle_sec >= max_idle:
            return True

        span = max(max_idle - min_idle, 1.0)
        progress = max(0.0, min(1.0, (idle_sec - min_idle) / span))
        base_prob = float(self._config["trigger_base_prob"])  # 刚到最小 idle 时也有少量概率
        max_prob = float(self._config["trigger_max_prob"])
        p = base_prob + (max_prob - base_prob) * progress
        return random.random() < p

    def _is_whitelisted(self, event: AstrMessageEvent) -> bool:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return False

        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_id = str(getattr(getattr(msg_obj, "sender", None), "user_id", "") or event.get_sender_id())

        if group_id:
            return group_id in self._config["group_whitelist"]
        return sender_id in self._config["private_whitelist"]

    def _session_key(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""

        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_id = str(getattr(getattr(msg_obj, "sender", None), "user_id", "") or event.get_sender_id())
        if group_id:
            return f"group:{group_id}"
        return f"private:{sender_id}"

    def _pick_topic(self, session_key: str) -> str:
        if session_key.startswith("private:") and self._config.get("private_topic_pool"):
            return random.choice(self._config["private_topic_pool"])
        if session_key.startswith("group:") and self._config.get("group_topic_pool"):
            return random.choice(self._config["group_topic_pool"])
        return random.choice(self._config["topic_pool"])

    def _get_or_create_session(self, event: AstrMessageEvent) -> Dict:
        key = self._session_key(event)
        now_ts = self._now().timestamp()
        old = self._sessions.get(key)
        if old:
            old["unified_msg_origin"] = event.unified_msg_origin
            return old

        return {
            "session_key": key,
            "unified_msg_origin": event.unified_msg_origin,
            "last_human_at": now_ts,
            "last_bot_at": 0.0,
            "last_interaction_at": now_ts,
            "next_check_at": now_ts + self._randomized_interval(),
            "today_proactive_count": 0,
            "counter_date": self._now().strftime("%Y-%m-%d"),
            "cooldown_until": 0.0,
        }

    def _rollover_daily_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("counter_date") != today:
            session["counter_date"] = today
            session["today_proactive_count"] = 0

    def _in_sleep_window(self, now: datetime) -> bool:
        hm = now.strftime("%H:%M")
        for wnd in self._config["sleep_windows"]:
            start = wnd["start"]
            end = wnd["end"]
            if start <= end:
                if start <= hm <= end:
                    return True
            else:
                # 跨天窗口，例如 23:30-08:00
                if hm >= start or hm <= end:
                    return True
        return False

    def _randomized_interval(self) -> int:
        base = int(self._config["check_interval_sec"])
        low = max(5, int(base * 0.85))
        high = max(low + 1, int(base * 1.25))
        return random.randint(low, high)

    def _is_hhmm(self, val: str) -> bool:
        try:
            datetime.strptime(val, "%H:%M")
            return True
        except ValueError:
            return False

    def _now(self) -> datetime:
        tz = ZoneInfo(self._config["timezone"])
        return datetime.now(tz)

    def _fmt_ts(self, ts: Optional[float]) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts, ZoneInfo(self._config["timezone"])).strftime("%Y-%m-%d %H:%M:%S")

    def _load_config(self) -> Dict:
        default = {
            "enabled": True,
            "debug_log": False,
            "timezone": "Asia/Shanghai",
            "private_whitelist": [],
            "group_whitelist": [],
            "sleep_windows": [{"start": "23:30", "end": "08:00"}],
            "check_interval_sec": 30,
            "min_idle_sec": 15 * 60,
            "max_idle_sec": 60 * 60,
            "cooldown_sec": 20 * 60,
            "max_per_session_per_day": 8,
            "trigger_base_prob": 0.08,
            "trigger_max_prob": 0.55,
            "private_topic_pool": [
                "刚好想到你了。最近有没有一件事，你其实很想做但一直没开始？",
                "我在，想听听你今天最真实的心情分数（0-10）会给几分？",
                "我们来个超轻量话题：你最近最想改变的一个小习惯是什么？",
            ],
            "group_topic_pool": [
                "大家最近有没有遇到一个值得分享的小发现？",
                "来个轻松问题：如果这周只能完成一件最重要的事，你会选什么？",
                "随机话题：最近哪个工具或方法让你效率提升最明显？",
            ],
            "topic_pool": [
                "刚刚想起一个有意思的问题：你最近有没有哪件小事让你特别开心？",
                "我在这儿，想和你继续聊聊。你最近最想推进的一件事是什么？",
                "来个轻松话题：如果今天能立刻学会一个技能，你会选哪个？",
                "我有点好奇，你最近在关注什么新鲜内容？",
                "要不要我陪你做个两分钟的小计划，把接下来要做的事理一理？",
            ],
        }

        if not self._cfg_path.exists():
            self._write_json(self._cfg_path, default)
            return default

        try:
            loaded = self._read_json(self._cfg_path)
            merged = {**default, **loaded}
            if not isinstance(merged.get("sleep_windows"), list) or not merged["sleep_windows"]:
                merged["sleep_windows"] = default["sleep_windows"]
            if not isinstance(merged.get("private_topic_pool"), list) or not merged["private_topic_pool"]:
                merged["private_topic_pool"] = default["private_topic_pool"]
            if not isinstance(merged.get("group_topic_pool"), list) or not merged["group_topic_pool"]:
                merged["group_topic_pool"] = default["group_topic_pool"]
            if not isinstance(merged.get("topic_pool"), list) or not merged["topic_pool"]:
                merged["topic_pool"] = default["topic_pool"]
            return merged
        except Exception as exc:
            logger.error(f"[idle-proactive] load config failed: {exc}")
            return default

    def _save_config(self):
        self._write_json(self._cfg_path, self._config)

    def _load_state(self) -> Dict[str, Dict]:
        if not self._state_path.exists():
            return {}
        try:
            data = self._read_json(self._state_path)
            if isinstance(data, dict):
                return data
            return {}
        except Exception as exc:
            logger.error(f"[idle-proactive] load state failed: {exc}")
            return {}

    def _save_state(self):
        self._write_json(self._state_path, self._sessions)

    def _read_json(self, path: Path):
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: Path, data):
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _debug(self, msg: str):
        if self._config.get("debug_log", False):
            logger.info(f"[idle-proactive][debug] {msg}")
