import asyncio
import inspect
import json
import random
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

try:
    from ..config import DEFAULT_CONFIG
except ImportError:
    from config import DEFAULT_CONFIG

try:
    import chinese_calendar as _cc
except Exception:
    _cc = None


class PolicyGenerationUnitsMixin:
    _IDLE_COMMANDS = {
        "idle_status",
        "idle_enable",
        "idle_disable",
        "idle_wl_add_private",
        "idle_wl_del_private",
        "idle_wl_add_group",
        "idle_wl_del_group",
        "idle_sleep_set",
        "idle_test",
        "idle_decision_status",
        "idle_decision_last",
    }

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
            prompt = await self._maybe_refine_proactive_prompt_with_lite(
                prompt=prompt,
                unified_msg_origin=unified_msg_origin,
                session_type=session_type,
                idle_sec=idle_sec,
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
            cleaned = await self._optimize_output_segments(cleaned, unified_msg_origin)
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

    def _holiday_qa_main_llm_enabled(self) -> bool:
        # Simplified behavior: once lite LLM is enabled, holiday QA goes through main LLM rendering.
        return self._lite_llm_enabled()

    def _proactive_lite_refine_enabled(self) -> bool:
        # Simplified behavior: once lite LLM is enabled, prompt refining is enabled together.
        return self._lite_llm_enabled()

    def _lite_llm_timeout_sec(self) -> float:
        return max(
            1.0,
            float(
                self.config.get(
                    "lite_llm_timeout_sec", DEFAULT_CONFIG["lite_llm_timeout_sec"]
                )
            ),
        )

    def _dialogue_wait_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("dialogue_wait_enabled"),
            DEFAULT_CONFIG["dialogue_wait_enabled"],
        )

    def _dialogue_wait_timeout_sec(self) -> int:
        return max(
            5,
            int(
                self.config.get(
                    "dialogue_wait_timeout_sec",
                    DEFAULT_CONFIG["dialogue_wait_timeout_sec"],
                )
            ),
        )

    def _dialogue_wait_max_merge(self) -> int:
        return max(
            1,
            int(
                self.config.get(
                    "dialogue_wait_max_merge",
                    DEFAULT_CONFIG["dialogue_wait_max_merge"],
                )
            ),
        )

    def _output_segment_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("output_segment_enabled"),
            DEFAULT_CONFIG["output_segment_enabled"],
        )

    def _output_segment_max_parts(self) -> int:
        return max(
            1,
            int(
                self.config.get(
                    "output_segment_max_parts",
                    DEFAULT_CONFIG["output_segment_max_parts"],
                )
            ),
        )

    def _output_segment_max_chars(self) -> int:
        return max(
            10,
            int(
                self.config.get(
                    "output_segment_max_chars",
                    DEFAULT_CONFIG["output_segment_max_chars"],
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

    async def _resolve_main_provider_id(self, unified_msg_origin: str) -> str:
        try:
            provider = await self.context.get_current_chat_provider_id(
                unified_msg_origin
            )
            if provider:
                return str(provider).strip()
        except Exception:
            pass
        provider = str(self.config.get("proactive_provider_id") or "").strip()
        return provider

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
                self._quality_bump("lite_ok")
                return obj
            self._quality_bump("lite_fail")
        except Exception as exc:
            self._quality_bump("lite_fail")
            self._debug_lite_llm_issue_once(exc)
        return None

    async def _lite_llm_text(self, unified_msg_origin: str, prompt: str) -> str:
        if not self._lite_llm_enabled():
            return ""
        provider_id = await self._resolve_lite_provider_id(unified_msg_origin)
        if not provider_id:
            return ""
        try:
            completion = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                ),
                timeout=self._lite_llm_timeout_sec(),
            )
            text = self._completion_to_text(completion).strip()
            if text:
                self._quality_bump("lite_ok")
            else:
                self._quality_bump("lite_fail")
            return text
        except Exception as exc:
            self._quality_bump("lite_fail")
            self._debug_lite_llm_issue_once(exc)
            return ""

    def _debug_lite_llm_issue_once(self, exc: Exception):
        # Some adapters/providers may return unsupported response objects intermittently.
        # Keep fallback behavior, and avoid flooding debug logs with the same known issue.
        message = str(exc)
        now_ts = self._now().timestamp()
        last_ts = float(getattr(self, "_lite_llm_issue_last_ts", 0.0) or 0.0)
        if "Unsupported response type" in message:
            window = max(300, int(self.config.get("debug_status_window_sec", 300)))
            if now_ts - last_ts < window:
                return
            setattr(self, "_lite_llm_issue_last_ts", now_ts)
            self._debug(
                "lite llm unavailable on this adapter/provider response type; fallback engaged"
            )
            return
        self._debug(f"lite llm failed: {message}")

    async def _main_llm_text(self, unified_msg_origin: str, prompt: str) -> str:
        provider_id = await self._resolve_main_provider_id(unified_msg_origin)
        if not provider_id:
            return ""
        try:
            completion = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            text = self._completion_to_text(completion).strip()
            if text:
                self._quality_bump("main_rewrite_ok")
            else:
                self._quality_bump("main_rewrite_fallback")
            return text
        except Exception as exc:
            self._quality_bump("main_rewrite_fallback")
            self._debug(f"main llm text failed: {exc}")
            return ""

    async def _maybe_refine_proactive_prompt_with_lite(
        self,
        prompt: str,
        unified_msg_origin: str,
        session_type: str,
        idle_sec: float,
    ) -> str:
        if not self._proactive_lite_refine_enabled():
            return prompt
        lite_prompt = (
            "你是对话提示词优化助手。"
            "请在不改变目标任务的前提下，将输入 Prompt 优化为更自然、清晰、可执行的版本。"
            "要求：\n"
            "1) 保留原始目标与限制。\n"
            "2) 输出中文。\n"
            "3) 只输出优化后的 Prompt 正文，不要解释。\n"
            f"会话类型：{session_type}；idle 秒数：{int(idle_sec)}。\n"
            "原始 Prompt：\n"
            f"{prompt}"
        )
        refined = await self._lite_llm_text(unified_msg_origin, lite_prompt)
        if not refined:
            return prompt
        # Avoid overlong/unstable rewrite.
        refined = refined.strip()
        if len(refined) < 30:
            return prompt
        if len(refined) > 6000:
            return prompt
        return refined

    async def _optimize_output_segments(
        self, text: str, unified_msg_origin: str
    ) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return cleaned
        if not self._output_segment_enabled():
            return cleaned
        max_chars = self._output_segment_max_chars()
        parts = self._split_reply_segments(cleaned)
        if not parts:
            return cleaned
        parts = self._trim_reply_segments(parts)
        if not parts:
            return cleaned
        out = "\n".join(parts)
        if len("".join(parts)) > max_chars * 2:
            return cleaned
        return out

    def _complexity_level(self, text: str) -> str:
        t = (text or "").strip()
        if not t:
            return "simple"
        hard_words = (
            "原理",
            "为什么",
            "区别",
            "方案",
            "架构",
            "优化",
            "报错",
            "异常",
            "实现",
            "步骤",
            "详细",
            "深入",
        )
        score = sum(1 for w in hard_words if w in t)
        if len(t) > 60 or score >= 3:
            return "hard"
        if len(t) > 25 or score >= 1:
            return "complex"
        return "simple"

    def _reply_policy_hint(self, user_text: str) -> str:
        level = self._complexity_level(user_text)
        max_parts = self._output_segment_max_parts()
        max_chars = self._output_segment_max_chars()
        if level == "simple":
            return "问题偏简单：用1句回复，尽量不超过28字，不要过度解释。"
        if level == "complex":
            return (
                f"问题中等复杂：分2句回复，必要时3句；总字数尽量不超过{max_chars}字。"
                "每句短一点，符合中文聊天习惯。"
            )
        return (
            f"问题较复杂：分2到{max_parts}句回复，先结论后说明。"
            f"总字数尽量不超过{max_chars}字。"
            "如纯文字仍难解释，请走图片说明模式。"
        )

    async def _lite_reply_plan(
        self, user_text: str, unified_msg_origin: str
    ) -> Dict[str, object]:
        # Lite model only decides structure budget; it must not rewrite response text.
        fallback_level = self._complexity_level(user_text)
        fallback_budget = (
            1
            if fallback_level == "simple"
            else (2 if fallback_level == "complex" else 3)
        )
        if not self._lite_llm_enabled():
            return {
                "sentence_budget": fallback_budget,
                "need_image": False,
                "source": "heuristic",
            }

        prompt = (
            "你是回复结构决策器。你只负责决定回复句数预算和是否建议用图解释，不负责改写内容。"
            "输出严格 JSON，不要解释。\n"
            '格式：{"sentence_budget":1,"need_image":false,"confidence":0.0}\n'
            "规则：\n"
            "1) 简单问题：sentence_budget=1。\n"
            "2) 中等问题：sentence_budget=2 或 3。\n"
            f"3) 复杂问题：sentence_budget=3 到 {self._output_segment_max_parts()}。\n"
            "4) 仅当纯文字难以解释时，need_image=true。\n"
            f"用户输入：{user_text}\n"
        )
        obj = await self._lite_llm_json(unified_msg_origin, prompt)
        if not isinstance(obj, dict):
            return {
                "sentence_budget": fallback_budget,
                "need_image": False,
                "source": "heuristic",
            }
        try:
            budget = int(obj.get("sentence_budget", fallback_budget))
        except Exception:
            budget = fallback_budget
        budget = max(1, min(int(self._output_segment_max_parts()), budget))
        need_image = bool(obj.get("need_image", False))
        return {"sentence_budget": budget, "need_image": need_image, "source": "lite"}

    def _parse_image_mode(self, text: str) -> Tuple[bool, str]:
        raw = (text or "").strip()
        if not raw:
            return False, ""
        m = re.search(r"\[\[IMAGE\]\]\s*(.*)", raw, flags=re.S)
        if not m:
            return False, ""
        prompt = m.group(1).strip()
        if not prompt:
            return False, ""
        return True, prompt

    def _split_reply_segments(self, text: str) -> list[str]:
        raw = (text or "").strip()
        if not raw:
            return []
        if "||" in raw:
            parts = [p.strip() for p in raw.split("||") if p.strip()]
            if parts:
                return parts
        lines = [p.strip() for p in raw.splitlines() if p.strip()]
        if len(lines) > 1:
            return lines
        chunks = re.split(r"(?<=[。！？!?；;])", raw)
        parts = [c.strip() for c in chunks if c and c.strip()]
        if len(parts) > 1:
            return parts
        # Soft split fallback for models that rarely use strong punctuation.
        soft = re.split(r"(?<=[，,])", raw)
        soft_parts = [c.strip() for c in soft if c and c.strip()]
        if len(soft_parts) > 1:
            return soft_parts
        # Hard split fallback: still no separator, split by configured char budget.
        budget = max(10, int(self._output_segment_max_chars()))
        if len(raw) <= budget:
            return [raw]
        out = []
        i = 0
        while i < len(raw):
            out.append(raw[i : i + budget].strip())
            i += budget
        return [x for x in out if x]

    def _trim_reply_segments(self, parts: list[str]) -> list[str]:
        if not parts:
            return []
        max_parts = self._output_segment_max_parts()
        if len(parts) <= max_parts:
            return parts
        kept = parts[: max_parts - 1]
        tail = "".join(parts[max_parts - 1 :]).strip()
        if tail:
            kept.append(tail)
        return [p for p in kept if p]

    async def _dispatch_reply_segments(
        self,
        send_reply: Callable[[str], Awaitable[None]],
        text: str,
        sentence_budget: Optional[int] = None,
    ):
        msg = (text or "").strip()
        if not msg:
            return
        if not self._output_segment_enabled():
            await send_reply(msg)
            return
        parts = self._split_reply_segments(msg)
        if isinstance(sentence_budget, int) and sentence_budget > 0:
            cap = max(
                1, min(int(self._output_segment_max_parts()), int(sentence_budget))
            )
            if len(parts) > cap:
                parts = parts[: cap - 1] + ["".join(parts[cap - 1 :]).strip()]
        parts = self._trim_reply_segments(parts)
        if not parts:
            await send_reply(msg)
            return
        if len(parts) == 1:
            await send_reply(parts[0])
            return
        for i, p in enumerate(parts):
            await send_reply(p)
            if i < len(parts) - 1:
                await asyncio.sleep(min(1.2, 0.15 + 0.02 * len(p)))

    async def _send_image_reply(
        self, unified_msg_origin: str, image_prompt: str
    ) -> bool:
        prompt = (image_prompt or "").strip()
        if not prompt:
            return False
        try:
            image_url = await self.text_to_image(prompt)
            try:
                chain = MessageChain().file_image(image_url)
                await self.context.send_message(unified_msg_origin, chain)
                return True
            except Exception:
                await self._send_text_to_origin(
                    unified_msg_origin, f"这部分更适合看图说明：{image_url}"
                )
                return True
        except Exception as exc:
            self._debug(f"text_to_image failed: {exc}")
            return False

    async def _holiday_intent_from_lite_llm(
        self, text: str, unified_msg_origin: str, now: datetime
    ) -> dict:
        prompt = (
            "你是一个节日问答意图解析器。"
            "请把用户输入解析为严格 JSON，不要输出其他内容。\n"
            'JSON 结构：{"intent":"none|today_status|countdown","holiday_name":""}\n'
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
        holiday_name = self._normalized_holiday_name(
            str(obj.get("holiday_name", "")).strip()
        )
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
            with urllib.request.urlopen(
                req, timeout=self._holiday_api_timeout_sec()
            ) as resp:
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
            with urllib.request.urlopen(
                req, timeout=self._holiday_api_timeout_sec()
            ) as resp:
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
                            target = datetime.strptime(
                                f"{year}-{day_str}", "%Y-%m-%d"
                            ).date()
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

    def _is_command_like_text(self, text: str) -> bool:
        t = (text or "").strip()
        return bool(t) and t[0] in {"/", "!", "！", "／"}

    def _command_token(self, text: str) -> str:
        t = (text or "").strip().lower()
        if not t:
            return ""
        token = t.split()[0]
        token = token.lstrip("/!！／")
        return token.strip()

    def _is_plugin_command_text(self, text: str) -> bool:
        token = self._command_token(text)
        if not token:
            return False
        if token in self._IDLE_COMMANDS:
            return True
        return token.startswith("idle_")

    def _clear_wait_buffer_for_session(self, session_key: str):
        if not session_key:
            return
        if isinstance(getattr(self, "_dialogue_wait_buffers", None), dict):
            self._dialogue_wait_buffers.pop(session_key, None)
        tasks = getattr(self, "_dialogue_wait_tasks", None)
        if isinstance(tasks, dict):
            task = tasks.pop(session_key, None)
            if task and not task.done():
                task.cancel()

    def _suppress_default_llm(
        self, event: AstrMessageEvent, reason: str, stop_propagation: bool = False
    ):
        # AstrBot event control: prevent default LLM from replying this event.
        # Compatible with both property and method style APIs across versions.
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
        if stop_propagation:
            try:
                stop = getattr(event, "stop_event", None)
                if callable(stop):
                    stop()
            except Exception:
                pass
        self._debug(f"dialogue wait suppress default llm reason={reason}")

    def _join_dialogue_parts(self, parts) -> str:
        if not isinstance(parts, list):
            return ""
        cleaned = [str(x).strip() for x in parts if str(x).strip()]
        if not cleaned:
            return ""
        return " ".join(cleaned).strip()

    def _event_is_inputting(self, event: AstrMessageEvent) -> bool:
        # Best-effort adapter compatibility for typing/input state events.
        candidates = [event, getattr(event, "message_obj", None)]
        keys = (
            "is_typing",
            "typing",
            "is_inputting",
            "inputting",
            "is_composing",
            "composing",
        )
        for obj in candidates:
            if obj is None:
                continue
            for key in keys:
                try:
                    val = getattr(obj, key, None)
                except Exception:
                    val = None
                if isinstance(val, bool) and val:
                    return True
        return False

    async def _schedule_dialogue_wait_flush(self, session_key: str, delay_sec: int):
        tasks = getattr(self, "_dialogue_wait_tasks", None)
        if not isinstance(tasks, dict):
            self._dialogue_wait_tasks = {}
            tasks = self._dialogue_wait_tasks
        old = tasks.get(session_key)
        if old and not old.done():
            old.cancel()

        async def _runner():
            try:
                await asyncio.sleep(max(1, int(delay_sec)))
                await self._flush_dialogue_wait_session(session_key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._debug(
                    f"dialogue wait timer failed session={session_key} err={exc}"
                )
            finally:
                cur = self._dialogue_wait_tasks.get(session_key)
                if cur is task:
                    self._dialogue_wait_tasks.pop(session_key, None)

        task = asyncio.create_task(_runner())
        tasks[session_key] = task

    async def _flush_dialogue_wait_session(self, session_key: str):
        row = self._dialogue_wait_buffers.get(session_key)
        if not isinstance(row, dict):
            return
        now_ts = self._now().timestamp()
        deadline_at = float(row.get("deadline_at", 0.0) or 0.0)
        if deadline_at > now_ts:
            return
        self._dialogue_wait_buffers.pop(session_key, None)
        merged_text = self._join_dialogue_parts(row.get("parts", []))
        umo = str(row.get("unified_msg_origin", "")).strip()
        if not merged_text or not umo:
            return
        self._debug(
            f"dialogue wait flush timeout session={session_key} merged={merged_text}"
        )
        await self._handle_wait_merged_text(
            user_text=merged_text,
            unified_msg_origin=umo,
            send_reply=lambda reply, u=umo: self._send_text_to_origin(u, reply),
        )

    async def _flush_dialogue_wait_buffers(self):
        if not self._dialogue_wait_enabled():
            return
        if not isinstance(getattr(self, "_dialogue_wait_buffers", None), dict):
            return
        now_ts = self._now().timestamp()
        to_flush = []
        for session_key, row in list(self._dialogue_wait_buffers.items()):
            if not isinstance(row, dict):
                self._dialogue_wait_buffers.pop(session_key, None)
                continue
            deadline_at = float(row.get("deadline_at", 0.0) or 0.0)
            if deadline_at > 0 and now_ts >= deadline_at:
                to_flush.append((session_key, row))

        for session_key, row in to_flush:
            await self._flush_dialogue_wait_session(session_key)

    async def _send_text_to_origin(self, unified_msg_origin: str, text: str):
        msg = (text or "").strip()
        if not msg:
            return
        try:
            chain = MessageChain().message(msg)
            await self.context.send_message(unified_msg_origin, chain)
            return
        except Exception:
            pass
        try:
            await self.context.send_message(unified_msg_origin, [Plain(msg)])
            return
        except Exception as exc:
            self._debug(f"dialogue wait send fallback failed: {exc}")

    async def _maybe_reply_shallow_query_with_wait(self, event: AstrMessageEvent):
        text = self._extract_event_text(event)
        is_inputting = self._event_is_inputting(event)
        if text and (
            self._is_command_like_text(text) or self._is_plugin_command_text(text)
        ):
            session_key = self._session_key(event)
            self._clear_wait_buffer_for_session(session_key)
            self._debug(
                f"dialogue wait bypass command session={session_key or '-'} text={text}"
            )
            return
        if not self._dialogue_wait_enabled():
            await self._maybe_reply_shallow_query(event)
            return

        session_key = self._session_key(event)
        if not session_key:
            await self._maybe_reply_shallow_query(event)
            return

        now_ts = self._now().timestamp()
        timeout_sec = self._dialogue_wait_timeout_sec()
        max_merge = self._dialogue_wait_max_merge()
        row = self._dialogue_wait_buffers.get(session_key)

        if not isinstance(row, dict):
            row = {
                "parts": [],
                "unified_msg_origin": event.unified_msg_origin,
                "last_input_at": now_ts,
                "deadline_at": now_ts + timeout_sec,
            }

        parts = row.get("parts", [])
        if not isinstance(parts, list):
            parts = []
        if text:
            parts.append(text)
            row["parts"] = parts[-max_merge:]
            row["unified_msg_origin"] = event.unified_msg_origin
        else:
            row["parts"] = parts[-max_merge:]
        row["last_input_at"] = now_ts
        row["deadline_at"] = now_ts + timeout_sec
        self._dialogue_wait_buffers[session_key] = row

        self._suppress_default_llm(event, "window_waiting", stop_propagation=True)
        await self._schedule_dialogue_wait_flush(session_key, timeout_sec)
        if is_inputting:
            self._debug(
                f"dialogue wait inputting session={session_key} deadline={self._fmt_ts(row['deadline_at'])}"
            )
            return
        if text:
            self._debug(
                f"dialogue wait buffered session={session_key} parts={len(row['parts'])} deadline={self._fmt_ts(row['deadline_at'])}"
            )
            return
        self._debug(
            f"dialogue wait refreshed by status session={session_key} deadline={self._fmt_ts(row['deadline_at'])}"
        )

    async def _maybe_reply_shallow_query(self, event: AstrMessageEvent):
        text = self._extract_event_text(event)
        if not text:
            return
        await self._maybe_reply_shallow_query_text(
            user_text=text,
            unified_msg_origin=event.unified_msg_origin,
            send_reply=lambda reply: event.send(event.plain_result(reply)),
        )

    async def _maybe_reply_shallow_query_text(
        self,
        user_text: str,
        unified_msg_origin: str,
        send_reply: Callable[[str], Awaitable[None]],
    ) -> bool:
        # Unified shallow-task channel: holiday/time/date/polite ping.
        if await self._maybe_reply_holiday_query_text(
            text=user_text,
            unified_msg_origin=unified_msg_origin,
            send_reply=send_reply,
        ):
            self._quality_bump("shallow_hit")
            return True
        if not self._lite_llm_enabled():
            return False
        text = (user_text or "").strip()
        if not text:
            return False
        now = self._now()
        lowered = text.lower()
        intent = "none"
        if any(k in lowered for k in ("几点", "时间", "现在几点")):
            intent = "time"
        elif any(k in lowered for k in ("星期几", "周几", "周几了", "今天周几")):
            intent = "weekday"
        elif any(k in lowered for k in ("在吗", "在不在", "hello", "hi", "你好")):
            intent = "ping"
        else:
            obj = await self._lite_llm_json(
                unified_msg_origin,
                (
                    '你是浅任务意图分类器。输出严格JSON: {"intent":"none|time|weekday|ping"}。\n'
                    f"用户输入：{text}\n"
                ),
            )
            if isinstance(obj, dict):
                raw_intent = str(obj.get("intent", "none")).strip().lower()
                if raw_intent in {"none", "time", "weekday", "ping"}:
                    intent = raw_intent
        if intent == "none":
            self._quality_bump("shallow_fallback")
            return False
        if intent == "time":
            fact = f"现在时间是 {now.strftime('%H:%M')}。"
        elif intent == "weekday":
            names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            fact = f"今天是{names[now.weekday()]}。"
        else:
            fact = "我在，有什么我可以帮你的？"
        reply = await self._render_holiday_final_reply(
            user_text=text,
            fact_text=fact,
            unified_msg_origin=unified_msg_origin,
        )
        await self._dispatch_reply_segments(send_reply, reply)
        self._quality_bump("shallow_hit")
        return True

    async def _handle_wait_merged_text(
        self,
        user_text: str,
        unified_msg_origin: str,
        send_reply: Callable[[str], Awaitable[None]],
    ) -> bool:
        # Time-window merge priority chain:
        # 1) main LLM first for natural continuation
        # 2) fallback to shallow channel when main unavailable
        merged_reply = await self._render_wait_merged_main_reply(
            user_text=user_text,
            unified_msg_origin=unified_msg_origin,
        )
        if merged_reply:
            plan = await self._lite_reply_plan(user_text, unified_msg_origin)
            is_image, image_prompt = self._parse_image_mode(merged_reply)
            if is_image or bool(plan.get("need_image", False)):
                if not image_prompt:
                    image_prompt = f"用信息图解释：{user_text}"
                sent = await self._send_image_reply(unified_msg_origin, image_prompt)
                if sent:
                    self._quality_bump("main_rewrite_ok")
                    return True
            await self._dispatch_reply_segments(
                send_reply,
                merged_reply,
                sentence_budget=int(plan.get("sentence_budget", 0) or 0),
            )
            self._quality_bump("main_rewrite_ok")
            return True
        handled = await self._maybe_reply_shallow_query_text(
            user_text=user_text,
            unified_msg_origin=unified_msg_origin,
            send_reply=send_reply,
        )
        if handled:
            return True
        self._debug("dialogue wait merged text not handled: both main/shallow empty")
        return False

    async def _render_wait_merged_main_reply(
        self, user_text: str, unified_msg_origin: str
    ) -> str:
        text = (user_text or "").strip()
        if not text:
            return ""
        plan = await self._lite_reply_plan(text, unified_msg_origin)
        budget = max(1, int(plan.get("sentence_budget", 1) or 1))
        policy = self._reply_policy_hint(text)
        prompt = (
            "你是聊天助手。用户可能分两次输入，这里已经合并成一条完整输入。"
            "请直接给出自然、连贯、简洁的中文回复。\n"
            "要求：\n"
            "1) 优先接住用户意图，不要复读原话。\n"
            "2) 不要编造事实，不输出推理过程。\n"
            "3) 语气自然、不过度啰嗦，贴近中国人聊天习惯。\n"
            f"4) {policy}\n"
            f"5) 本次句数预算为 {budget} 句，尽量贴合预算。\n"
            "6) 如纯文字仍难解释，请输出：[[IMAGE]] 后接一行中文画面描述词。\n"
            "7) 普通情况只输出回复正文；多句时使用 || 分隔，不要换行。\n"
            f"用户合并输入：{text}\n"
        )
        reply = await self._main_llm_text(unified_msg_origin, prompt)
        if not reply:
            return ""
        cleaned = self._sanitize_outgoing_text(reply)
        if not cleaned:
            return ""
        return await self._optimize_output_segments(cleaned, unified_msg_origin)

    async def _maybe_reply_holiday_query(self, event: AstrMessageEvent) -> bool:
        text = self._extract_event_text(event)
        if not text:
            return False
        return await self._maybe_reply_holiday_query_text(
            text=text,
            unified_msg_origin=event.unified_msg_origin,
            send_reply=lambda reply: event.send(event.plain_result(reply)),
        )

    async def _maybe_reply_holiday_query_text(
        self,
        text: str,
        unified_msg_origin: str,
        send_reply: Callable[[str], Awaitable[None]],
    ) -> bool:
        # Holiday QA is an internal side-feature bound to lite LLM.
        if not self._lite_llm_enabled():
            return False
        if not self._to_bool(
            self.config.get("enable_holiday_perception"),
            DEFAULT_CONFIG["enable_holiday_perception"],
        ):
            return False
        country = (
            str(self.config.get("holiday_country", DEFAULT_CONFIG["holiday_country"]))
            .upper()
            .strip()
        )
        if country != "CN":
            return False
        text = (text or "").strip()
        if not text:
            return False
        now = self._now()
        parsed = await self._holiday_intent_from_lite_llm(text, unified_msg_origin, now)
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
            fact_text = (
                self._holiday_perception_text(now) or "今天的节假日信息暂时不可用。"
            )
            reply = await self._render_holiday_final_reply(
                user_text=text,
                fact_text=fact_text,
                unified_msg_origin=unified_msg_origin,
            )
            await self._dispatch_reply_segments(send_reply, reply)
            return True

        if intent != "countdown":
            return False
        if not holiday_name:
            holiday_name = self._extract_countdown_holiday_name(text)
            if not holiday_name:
                return False
        if not self._holiday_api_enabled():
            await send_reply("节日倒计时需要开启在线节假日接口。")
            return True
        found = self._find_next_cn_holiday_by_name(holiday_name, now)
        if not found:
            reply = await self._render_holiday_final_reply(
                user_text=text,
                fact_text=f"暂时没查到“{holiday_name}”的日期信息。",
                unified_msg_origin=unified_msg_origin,
            )
            await self._dispatch_reply_segments(send_reply, reply)
            return True
        target_date, target_name = found
        days = (target_date - now.date()).days
        if days <= 0:
            fact_text = f"今天就是{target_name}，节日快乐。"
        elif days == 1:
            fact_text = f"{target_name}在明天（{target_date.strftime('%m-%d')}）。"
        else:
            fact_text = (
                f"{target_name}还有 {days} 天（{target_date.strftime('%Y-%m-%d')}）。"
            )
        reply = await self._render_holiday_final_reply(
            user_text=text,
            fact_text=fact_text,
            unified_msg_origin=unified_msg_origin,
        )
        await self._dispatch_reply_segments(send_reply, reply)
        return True

    async def _render_holiday_final_reply(
        self, user_text: str, fact_text: str, unified_msg_origin: str
    ) -> str:
        if not self._holiday_qa_main_llm_enabled():
            return fact_text
        plan = await self._lite_reply_plan(user_text, unified_msg_origin)
        budget = max(1, int(plan.get("sentence_budget", 1) or 1))
        policy = self._reply_policy_hint(user_text)
        prompt = (
            "你是聊天助手。请根据用户原话和已知事实，生成一条自然、简洁、有温度的中文回复。\n"
            "要求：\n"
            "1) 忠实于已知事实，不要编造。\n"
            "2) 不需要解释推理过程。\n"
            "3) 贴近中国人聊天语境，不端着。\n"
            f"4) {policy}\n"
            f"5) 本次句数预算为 {budget} 句，尽量贴合预算。\n"
            "6) 普通情况只输出正文；多句时使用 || 分隔，不要换行。\n"
            f"用户原话：{user_text}\n"
            f"已知事实：{fact_text}\n"
        )
        text = await self._main_llm_text(unified_msg_origin, prompt)
        if not text:
            return fact_text
        cleaned = self._sanitize_outgoing_text(text)
        if not cleaned:
            return fact_text
        cleaned = await self._optimize_output_segments(cleaned, unified_msg_origin)
        return cleaned

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
        override = str((session or {}).get("decision_suggested_tone", "")).strip()
        if override:
            return override[:30]
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

    def _is_session_whitelisted(self, session_key: str) -> bool:
        key = str(session_key or "").strip()
        if not key:
            return False
        if key.startswith("group:"):
            gid = key.split(":", 1)[1]
            return gid in self.config.get("group_whitelist", [])
        if key.startswith("private:"):
            uid = key.split(":", 1)[1]
            return uid in self.config.get("private_whitelist", [])
        return False

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
