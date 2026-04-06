import asyncio
import inspect
import json
import random
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from ..config import DEFAULT_CONFIG
except ImportError:
    from config import DEFAULT_CONFIG

try:
    import chinese_calendar as _cc
except Exception:
    _cc = None


class PolicyGenerationUnitsMixin:
    _HOLIDAY_ALIASES = {
        "元旦": "元旦",
        "元旦节": "元旦",
        "春节": "春节",
        "新春": "春节",
        "清明": "清明节",
        "清明节": "清明节",
        "劳动节": "劳动节",
        "五一劳动节": "劳动节",
        "五一": "劳动节",
        "五一节": "劳动节",
        "端午": "端午节",
        "端午节": "端午节",
        "中秋": "中秋节",
        "中秋节": "中秋节",
        "国庆": "国庆节",
        "国庆节": "国庆节",
    }

    async def _generate_proactive_text(
        self,
        unified_msg_origin: str,
        session_key: str,
        idle_sec: float,
        session: Optional[Dict],
    ) -> str:
        fallback = self._sanitize_outgoing_text(
            str(
                self.config.get("fallback_proactive_text")
                or DEFAULT_CONFIG["fallback_proactive_text"]
            ).strip()
        )
        try:
            session_type = "私聊" if session_key.startswith("private:") else "群聊"
            env_perception = self._build_env_perception(unified_msg_origin, session_key)
            persona_text = await self._resolve_persona_prompt()
            style_hint = self._style_hint(session_key, session, idle_sec)
            recent_history = self._recent_history_text(session)
            prompt_tpl = str(
                self.config.get("proactive_prompt_template")
                or DEFAULT_CONFIG["proactive_prompt_template"]
            )
            prompt = prompt_tpl.format(
                persona=persona_text,
                session_type=session_type,
                env_perception=env_perception,
                idle_seconds=int(idle_sec),
                idle_minutes=max(1, int(idle_sec // 60)),
                style_hint=style_hint,
                recent_history=recent_history,
            )

            provider_id = str(self.config.get("proactive_provider_id") or "").strip()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(
                    unified_msg_origin
                )

            if not provider_id:
                self._debug("generate skip: provider_id unavailable, use fallback")
                return fallback

            completion = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt
            )
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
            self._debug(
                f"generate ok provider={provider_id} session={session_key} text={cleaned}"
            )
            return cleaned
        except Exception as exc:
            logger.error(f"[idle-proactive] generate proactive text failed: {exc}")
            self._debug(f"generate failed session={session_key} err={exc}")
            return fallback

    def _lite_llm_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("lite_llm_enabled"),
            DEFAULT_CONFIG["lite_llm_enabled"],
        )

    def _lite_llm_timeout_sec(self) -> float:
        return max(
            1.0,
            float(
                self.config.get(
                    "lite_llm_timeout_sec", DEFAULT_CONFIG["lite_llm_timeout_sec"]
                )
            ),
        )

    async def _resolve_lite_provider_id(self, unified_msg_origin: str) -> str:
        provider_id = str(self.config.get("lite_provider_id") or "").strip()
        if provider_id:
            return provider_id
        proactive_provider_id = str(
            self.config.get("proactive_provider_id") or ""
        ).strip()
        if proactive_provider_id:
            return proactive_provider_id
        try:
            return (
                await self.context.get_current_chat_provider_id(unified_msg_origin)
            ) or ""
        except Exception:
            return ""

    def _extract_json_object(self, text: str) -> Optional[dict]:
        if not isinstance(text, str):
            return None
        raw = text.strip()
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
        return None

    async def _lite_llm_json(
        self, unified_msg_origin: str, prompt: str
    ) -> Optional[dict]:
        if not self._lite_llm_enabled():
            return None
        provider_id = await self._resolve_lite_provider_id(unified_msg_origin)
        if not provider_id:
            return None
        try:
            completion = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                ),
                timeout=self._lite_llm_timeout_sec(),
            )
            text = self._completion_to_text(completion)
            obj = self._extract_json_object(text)
            if isinstance(obj, dict):
                return obj
        except Exception as exc:
            self._debug(f"lite llm parse failed: {exc}")
        return None

    async def _holiday_intent_from_lite_llm(
        self, text: str, unified_msg_origin: str, now: datetime
    ) -> dict:
        prompt = (
            "你是一个节日问答意图解析器。"
            "请把用户输入解析为严格 JSON，不要输出其他内容。\n"
            "JSON 结构：{\"intent\":\"none|today_status|countdown\",\"holiday_name\":\"\"}\n"
            "规则：\n"
            "1) intent=today_status：询问今天过什么节/今天是不是节假日/今天放假吗。\n"
            "2) intent=countdown：询问某节日还有几天。\n"
            "3) holiday_name 只填节日名；countdown 时尽量规范为：元旦/春节/清明节/劳动节/端午节/中秋节/国庆节。\n"
            f"当前本地日期：{now.strftime('%Y-%m-%d')}。\n"
            f"用户输入：{text}\n"
        )
        obj = await self._lite_llm_json(unified_msg_origin, prompt)
        if not isinstance(obj, dict):
            return {"intent": "none", "holiday_name": ""}
        intent = str(obj.get("intent", "none")).strip().lower()
        if intent not in {"none", "today_status", "countdown"}:
            intent = "none"
        holiday_name = self._normalized_holiday_name(str(obj.get("holiday_name", "")).strip())
        return {"intent": intent, "holiday_name": holiday_name}

    def _build_env_perception(self, unified_msg_origin: str, session_key: str) -> str:
        now = self._now()
        parts = [self._time_perception_text(now), self._day_perception_text(now)]
        holiday_text = self._holiday_perception_text(now)
        if holiday_text:
            parts.append(holiday_text)
        platform_text = self._platform_perception_text(unified_msg_origin, session_key)
        if platform_text:
            parts.append(platform_text)
        return "；".join(x for x in parts if x)

    def _time_perception_text(self, now: datetime) -> str:
        hm = now.strftime("%H:%M")
        hour = now.hour
        if 5 <= hour < 12:
            span = "上午"
        elif 12 <= hour < 14:
            span = "中午"
        elif 14 <= hour < 18:
            span = "下午"
        elif 18 <= hour < 23:
            span = "晚上"
        else:
            span = "深夜"
        return f"当前时间 {hm}（{span}）"

    def _day_perception_text(self, now: datetime) -> str:
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = names[now.weekday()]
        day_type = "周末" if now.weekday() >= 5 else "工作日"
        return f"{weekday}，{day_type}"

    def _holiday_perception_text(self, now: datetime) -> str:
        if not self._to_bool(
            self.config.get("enable_holiday_perception"),
            DEFAULT_CONFIG["enable_holiday_perception"],
        ):
            return ""
        country = (
            str(self.config.get("holiday_country", DEFAULT_CONFIG["holiday_country"]))
            .upper()
            .strip()
        )
        if country != "CN":
            return ""

        today = now.date()
        try:
            if self._holiday_api_enabled():
                api_text = self._holiday_text_from_cn_api(today)
                if api_text:
                    return api_text
            return self._holiday_text_from_builtin_cn(today)
        except Exception:
            return ""

    def _holiday_api_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("holiday_api_enabled"),
            DEFAULT_CONFIG["holiday_api_enabled"],
        )

    def _holiday_api_timeout_sec(self) -> float:
        return max(
            1.0,
            float(
                self.config.get(
                    "holiday_api_timeout_sec", DEFAULT_CONFIG["holiday_api_timeout_sec"]
                )
            ),
        )

    def _holiday_api_cache_ttl_sec(self) -> int:
        return max(
            60,
            int(
                self.config.get(
                    "holiday_api_cache_ttl_sec",
                    DEFAULT_CONFIG["holiday_api_cache_ttl_sec"],
                )
            ),
        )

    def _holiday_cache_get(self, key: str) -> Optional[str]:
        cache = getattr(self, "_holiday_cache", None)
        if not isinstance(cache, dict):
            return None
        row = cache.get(key)
        if not isinstance(row, dict):
            return None
        if self._now().timestamp() > float(row.get("expires_at", 0)):
            cache.pop(key, None)
            return None
        val = row.get("value")
        if isinstance(val, str):
            return val
        return None

    def _holiday_cache_set(self, key: str, value: str):
        cache = getattr(self, "_holiday_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_holiday_cache", cache)
        cache[key] = {
            "value": str(value or ""),
            "expires_at": self._now().timestamp() + self._holiday_api_cache_ttl_sec(),
        }

    def _holiday_year_cache_key(self, year: int) -> str:
        return f"cn-year:{year}"

    def _holiday_text_from_cn_api(self, day) -> str:
        cache_key = f"cn:{day.isoformat()}"
        hit = self._holiday_cache_get(cache_key)
        if hit is not None:
            return hit

        url = f"https://timor.tech/api/holiday/info/{day.isoformat()}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "astrbot-kanjyou/1.11"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._holiday_api_timeout_sec()) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._debug(f"holiday api failed, fallback to builtin: {exc}")
            return ""

        if not isinstance(payload, dict) or int(payload.get("code", -1)) != 0:
            return ""

        holiday = payload.get("holiday")
        type_info = payload.get("type") if isinstance(payload.get("type"), dict) else {}
        type_name = str(type_info.get("name", "")).strip()
        type_code = type_info.get("type")

        text = ""
        if isinstance(holiday, dict):
            name = str(holiday.get("name", "")).strip() or type_name
            if name:
                text = f"节假日：{name}"
        elif "补班" in type_name or "工作日" in type_name:
            text = "今天是工作日"
        elif "休息" in type_name or "周末" in type_name:
            text = "今天是休息日"
        elif type_code in (0, 1, 2, 3):
            # 兜底分支：无法识别 name 时，至少给出日类型判断。
            if type_code in (0, 3):
                text = "今天是工作日"
            else:
                text = "今天是休息日"

        self._holiday_cache_set(cache_key, text)
        return text

    def _holiday_year_data_from_cn_api(self, year: int):
        cache_key = self._holiday_year_cache_key(year)
        hit = self._holiday_cache_get(cache_key)
        if hit:
            try:
                return json.loads(hit)
            except Exception:
                pass
        url = f"https://timor.tech/api/holiday/year/{year}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "astrbot-kanjyou/1.11"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._holiday_api_timeout_sec()) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._debug(f"holiday year api failed: {exc}")
            return None
        if not isinstance(payload, dict) or int(payload.get("code", -1)) != 0:
            return None
        holiday = payload.get("holiday")
        if not isinstance(holiday, dict):
            return None
        try:
            self._holiday_cache_set(cache_key, json.dumps(holiday, ensure_ascii=False))
        except Exception:
            pass
        return holiday

    def _normalized_holiday_name(self, name: str) -> str:
        raw = re.sub(r"\s+", "", str(name or "").strip())
        return self._HOLIDAY_ALIASES.get(raw, raw)

    def _iter_cn_holiday_entries(self, year_holiday: dict):
        for key, info in year_holiday.items():
            if not isinstance(key, str):
                continue
            if not isinstance(info, dict):
                continue
            # timor year API commonly returns:
            # "05-01": {"holiday": true, "name": "劳动节", "date": "2026-05-01", ...}
            # Keep only real holidays, skip make-up workdays.
            if info.get("holiday") is not True:
                continue
            name = str(info.get("name", "")).strip()
            if not name:
                continue
            # Prefer full date from payload; fallback to key ("MM-DD") with unknown year.
            full_date = str(info.get("date", "")).strip()
            if full_date:
                yield full_date, name
                continue
            yield key, name

    def _find_next_cn_holiday_by_name(self, query_name: str, now: datetime):
        normalized_query = self._normalized_holiday_name(query_name)
        today = now.date()
        candidates = []
        for year in (today.year, today.year + 1):
            rows = self._holiday_year_data_from_cn_api(year)
            if not isinstance(rows, dict):
                continue
            for day_str, name in self._iter_cn_holiday_entries(rows):
                if (
                    normalized_query in self._normalized_holiday_name(name)
                    or self._normalized_holiday_name(name) in normalized_query
                ):
                    try:
                        if len(day_str) == 5 and "-" in day_str:
                            target = datetime.strptime(f"{year}-{day_str}", "%Y-%m-%d").date()
                        else:
                            target = datetime.strptime(day_str, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if target >= today:
                        candidates.append((target, name))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0]

    def _extract_event_text(self, event: AstrMessageEvent) -> str:
        for attr in ("message_str", "raw_message", "text"):
            val = getattr(event, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            for attr in ("message_str", "raw_message", "message"):
                val = getattr(msg_obj, attr, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if isinstance(val, list):
                    joined = "".join(str(x) for x in val if x is not None).strip()
                    if joined:
                        return joined
        return ""

    def _is_today_holiday_query(self, text: str) -> bool:
        t = text.replace(" ", "")
        if "今天" not in t:
            return False
        keys = (
            "什么节",
            "过什么节",
            "什么节日",
            "是不是节假日",
            "是节假日吗",
            "放假吗",
            "工作日吗",
            "休息日吗",
        )
        return any(k in t for k in keys)

    def _extract_countdown_holiday_name(self, text: str) -> str:
        t = text.replace(" ", "")
        patterns = [
            r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9]{1,16}(?:节|节日))还有几天",
            r"距离(?P<name>[\u4e00-\u9fa5A-Za-z0-9]{1,16}(?:节|节日))还有几天",
            r"(?P<name>五一|元旦|春节|清明|端午|中秋|国庆)还有几天",
            r"距离(?P<name>五一|元旦|春节|清明|端午|中秋|国庆)(?:还有几天)?",
        ]
        for p in patterns:
            m = re.search(p, t)
            if m:
                name = str(m.group("name") or "").strip()
                if name:
                    return self._normalized_holiday_name(name)
        return ""

    async def _maybe_reply_holiday_query(self, event: AstrMessageEvent):
        if not self._to_bool(
            self.config.get("holiday_qa_enabled"),
            DEFAULT_CONFIG["holiday_qa_enabled"],
        ):
            return
        if not self._to_bool(
            self.config.get("enable_holiday_perception"),
            DEFAULT_CONFIG["enable_holiday_perception"],
        ):
            return
        country = (
            str(self.config.get("holiday_country", DEFAULT_CONFIG["holiday_country"]))
            .upper()
            .strip()
        )
        if country != "CN":
            return
        text = self._extract_event_text(event)
        if not text:
            return
        now = self._now()
        parsed = await self._holiday_intent_from_lite_llm(
            text, event.unified_msg_origin, now
        )
        intent = parsed.get("intent", "none")
        holiday_name = str(parsed.get("holiday_name", "")).strip()
        if intent == "none":
            # Fallback: keep rule-based detection for robustness.
            if self._is_today_holiday_query(text):
                intent = "today_status"
            else:
                holiday_name = self._extract_countdown_holiday_name(text)
                if holiday_name:
                    intent = "countdown"

        if intent == "today_status":
            today_text = self._holiday_perception_text(now) or "今天的节假日信息暂时不可用。"
            await event.send(event.plain_result(today_text))
            return

        if intent != "countdown":
            return
        if not holiday_name:
            holiday_name = self._extract_countdown_holiday_name(text)
            if not holiday_name:
                return
        if not self._holiday_api_enabled():
            await event.send(event.plain_result("节日倒计时需要开启在线节假日接口。"))
            return
        found = self._find_next_cn_holiday_by_name(holiday_name, now)
        if not found:
            await event.send(event.plain_result(f"暂时没查到“{holiday_name}”的日期信息。"))
            return
        target_date, target_name = found
        days = (target_date - now.date()).days
        if days <= 0:
            reply = f"今天就是{target_name}，节日快乐。"
        elif days == 1:
            reply = f"{target_name}在明天（{target_date.strftime('%m-%d')}）。"
        else:
            reply = f"{target_name}还有 {days} 天（{target_date.strftime('%Y-%m-%d')}）。"
        await event.send(event.plain_result(reply))

    def _holiday_text_from_builtin_cn(self, day) -> str:
        if _cc is None:
            return ""
        detail = _cc.get_holiday_detail(day)
        on_holiday = False
        name = ""
        if isinstance(detail, tuple):
            if len(detail) >= 1:
                on_holiday = bool(detail[0])
            if len(detail) >= 2 and detail[1]:
                name = str(detail[1])
        elif detail:
            name = str(detail)
            on_holiday = _cc.is_holiday(day)

        if on_holiday:
            return f"节假日：{name or '法定假日'}"
        if _cc.is_workday(day):
            return "今天是工作日"
        return "今天是休息日"

    def _platform_perception_text(
        self, unified_msg_origin: str, session_key: str
    ) -> str:
        if not self._to_bool(
            self.config.get("enable_platform_perception"),
            DEFAULT_CONFIG["enable_platform_perception"],
        ):
            return ""
        raw = (unified_msg_origin or "").strip()
        channel = raw.split(":", 1)[0].split("/", 1)[0].lower() if raw else ""
        mapping = {
            "telegram": "Telegram",
            "discord": "Discord",
            "onebot": "QQ/OneBot",
            "qq": "QQ",
            "wechat": "微信",
            "kook": "KOOK",
        }
        channel_name = mapping.get(channel, channel or "未知平台")
        session_type = "群聊" if str(session_key).startswith("group:") else "私聊"
        return f"平台：{channel_name}，场景：{session_type}"

    def _should_trigger(self, idle_sec: float, now: datetime) -> bool:
        return random.random() < self._trigger_probability(idle_sec, now)

    def _trigger_probability(self, idle_sec: float, now: datetime) -> float:
        min_idle = float(self._effective_min_idle_sec(now))
        max_idle = float(self._effective_max_idle_sec(now))

        if idle_sec >= max_idle:
            return 1.0

        span = max(max_idle - min_idle, 1.0)
        progress = max(0.0, min(1.0, (idle_sec - min_idle) / span))
        base_prob = float(self._trigger_base_prob())  # 刚到最小 idle 时也有少量概率
        max_prob = float(self._trigger_max_prob())
        p = base_prob + (max_prob - base_prob) * progress
        return max(0.0, min(1.0, p))

    def _min_idle_sec(self) -> int:
        return max(60, int(float(self.config["min_idle_min"]) * 60))

    def _max_idle_sec(self) -> int:
        return max(
            self._min_idle_sec() + 60, int(float(self.config["max_idle_min"]) * 60)
        )

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
        factor = base**streak
        return min(max_factor, factor)

    def _style_hint(
        self, session_key: str, session: Optional[Dict], idle_sec: float
    ) -> str:
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
        sender_id = str(
            getattr(getattr(msg_obj, "sender", None), "user_id", "")
            or event.get_sender_id()
        )

        if group_id:
            return group_id in self.config["group_whitelist"]
        return sender_id in self.config["private_whitelist"]

    def _session_key(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""

        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_id = str(
            getattr(getattr(msg_obj, "sender", None), "user_id", "")
            or event.get_sender_id()
        )
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
        max_len = max(
            20,
            int(
                self.config.get(
                    "security_max_text_length",
                    DEFAULT_CONFIG["security_max_text_length"],
                )
            ),
        )
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
