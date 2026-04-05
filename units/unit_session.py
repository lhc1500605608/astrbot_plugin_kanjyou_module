import copy
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from config import DEFAULT_CONFIG

class SessionConfigUnitsMixin:
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
            "pending_human_reply": False,
            "no_reply_streak": 0,
            "period_counter_date": self._now().strftime("%Y-%m-%d"),
            "period_proactive_count": {"morning": 0, "afternoon": 0, "evening": 0},
            "recent_proactive_texts": [],
        }

    def _ensure_session_shape(self, session: Dict):
        if "pending_human_reply" not in session:
            session["pending_human_reply"] = False
        if "no_reply_streak" not in session:
            session["no_reply_streak"] = 0
        if "period_counter_date" not in session:
            session["period_counter_date"] = self._now().strftime("%Y-%m-%d")
        if not isinstance(session.get("period_proactive_count"), dict):
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}
        if not isinstance(session.get("recent_proactive_texts"), list):
            session["recent_proactive_texts"] = []

    def _rollover_daily_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("counter_date") != today:
            session["counter_date"] = today
            session["today_proactive_count"] = 0
            session["period_counter_date"] = today
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}

    def _rollover_period_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("period_counter_date") != today:
            session["period_counter_date"] = today
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}
            return
        if not isinstance(session.get("period_proactive_count"), dict):
            session["period_proactive_count"] = {"morning": 0, "afternoon": 0, "evening": 0}

    def _inc_period_count(self, session: Dict, period: str):
        if period not in {"morning", "afternoon", "evening"}:
            return
        counters = session.get("period_proactive_count")
        if not isinstance(counters, dict):
            counters = {"morning": 0, "afternoon": 0, "evening": 0}
        counters[period] = int(counters.get(period, 0)) + 1
        session["period_proactive_count"] = counters

    def _in_sleep_window(self, now: datetime) -> bool:
        hm = now.strftime("%H:%M")
        start = self.config.get("sleep_start")
        end = self.config.get("sleep_end")
        if not (isinstance(start, str) and isinstance(end, str) and self._is_hhmm(start) and self._is_hhmm(end)):
            start = DEFAULT_CONFIG["sleep_start"]
            end = DEFAULT_CONFIG["sleep_end"]

        if start <= end:
            if start <= hm <= end:
                return True
        else:
            # 跨天窗口，例如 23:30-08:00
            if hm >= start or hm <= end:
                return True
        return False

    def _randomized_interval(self) -> int:
        base = int(self.config["check_interval_sec"])
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
        tz = ZoneInfo(self.config["timezone"])
        return datetime.now(tz)

    def _fmt_ts(self, ts: Optional[float]) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts, ZoneInfo(self.config["timezone"])).strftime("%Y-%m-%d %H:%M:%S")

    def _trim_global_send_history(self, now_ts: float):
        window_start = now_ts - 3600
        self._global_send_history = [ts for ts in self._global_send_history if ts >= window_start]

    def _security_global_hourly_cap(self) -> int:
        return max(1, int(self.config.get("security_global_hourly_cap", DEFAULT_CONFIG["security_global_hourly_cap"])))

    def _security_max_fail_streak(self) -> int:
        return max(1, int(self.config.get("security_max_fail_streak", DEFAULT_CONFIG["security_max_fail_streak"])))

    def _security_fail_pause_sec(self) -> int:
        return max(300, int(float(self.config.get("security_fail_pause_min", DEFAULT_CONFIG["security_fail_pause_min"])) * 60))

    def _security_allow_links(self) -> bool:
        return self._to_bool(
            self.config.get("security_allow_links", DEFAULT_CONFIG["security_allow_links"]),
            DEFAULT_CONFIG["security_allow_links"],
        )

    def _security_blocked_words(self) -> List[str]:
        raw = self.config.get("security_blocked_words", DEFAULT_CONFIG["security_blocked_words"])
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            token = item.strip()
            if token:
                out.append(token)
        return out

    def _to_bool(self, value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "on"}:
                return True
            if v in {"0", "false", "no", "off"}:
                return False
        return default

    def _normalize_webui_config(self):
        changed = False
        changed = self._normalize_defaults() or changed
        changed = self._normalize_basic_layer() or changed
        changed = self._normalize_timing_layer() or changed
        changed = self._normalize_generation_layer() or changed
        changed = self._normalize_security_layer() or changed
        changed = self._normalize_debug_layer() or changed

        if changed:
            self._save_webui_config()

    def _normalize_defaults(self) -> bool:
        changed = False
        for key, value in DEFAULT_CONFIG.items():
            if self.config.get(key) is None:
                self.config[key] = copy.deepcopy(value)
                changed = True
        return changed

    def _normalize_basic_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("private_whitelist"), list):
            self.config["private_whitelist"] = copy.deepcopy(DEFAULT_CONFIG["private_whitelist"])
            changed = True
        if not isinstance(self.config.get("group_whitelist"), list):
            self.config["group_whitelist"] = copy.deepcopy(DEFAULT_CONFIG["group_whitelist"])
            changed = True

        mode = str(self.config.get("config_mode", "basic")).lower()
        if mode not in {"basic", "advanced"}:
            self.config["config_mode"] = "basic"
            changed = True

        for key in ("enabled", "lifecycle_log", "debug_log"):
            if not isinstance(self.config.get(key), bool):
                self.config[key] = self._to_bool(self.config.get(key), DEFAULT_CONFIG[key])
                changed = True
        return changed

    def _normalize_timing_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("sleep_start"), str):
            self.config["sleep_start"] = DEFAULT_CONFIG["sleep_start"]
            changed = True
        if not isinstance(self.config.get("sleep_end"), str):
            self.config["sleep_end"] = DEFAULT_CONFIG["sleep_end"]
            changed = True

        # 兼容旧版秒级配置，自动迁移为分钟配置。
        if self.config.get("min_idle_min") is None and isinstance(self.config.get("min_idle_sec"), (int, float)):
            self.config["min_idle_min"] = max(1, int(float(self.config["min_idle_sec"]) // 60))
            changed = True
        if self.config.get("max_idle_min") is None and isinstance(self.config.get("max_idle_sec"), (int, float)):
            self.config["max_idle_min"] = max(1, int(float(self.config["max_idle_sec"]) // 60))
            changed = True
        if self.config.get("cooldown_min") is None and isinstance(self.config.get("cooldown_sec"), (int, float)):
            self.config["cooldown_min"] = max(1, int(float(self.config["cooldown_sec"]) // 60))
            changed = True

        if not isinstance(self.config.get("min_idle_min"), (int, float)):
            self.config["min_idle_min"] = DEFAULT_CONFIG["min_idle_min"]
            changed = True
        if not isinstance(self.config.get("max_idle_min"), (int, float)):
            self.config["max_idle_min"] = DEFAULT_CONFIG["max_idle_min"]
            changed = True
        if not isinstance(self.config.get("cooldown_min"), (int, float)):
            self.config["cooldown_min"] = DEFAULT_CONFIG["cooldown_min"]
            changed = True

        if float(self.config["max_idle_min"]) <= float(self.config["min_idle_min"]):
            self.config["max_idle_min"] = max(
                float(self.config["min_idle_min"]) + 30, float(DEFAULT_CONFIG["max_idle_min"])
            )
            changed = True
        return changed

    def _normalize_generation_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("persona_id"), str):
            self.config["persona_id"] = DEFAULT_CONFIG["persona_id"]
            changed = True
        if not isinstance(self.config.get("proactive_provider_id"), str):
            self.config["proactive_provider_id"] = DEFAULT_CONFIG["proactive_provider_id"]
            changed = True
        if not isinstance(self.config.get("proactive_prompt_template"), str) or not self.config[
            "proactive_prompt_template"
        ].strip():
            self.config["proactive_prompt_template"] = DEFAULT_CONFIG["proactive_prompt_template"]
            changed = True
        if not isinstance(self.config.get("fallback_proactive_text"), str) or not self.config[
            "fallback_proactive_text"
        ].strip():
            self.config["fallback_proactive_text"] = DEFAULT_CONFIG["fallback_proactive_text"]
            changed = True
        if not isinstance(self.config.get("enable_holiday_perception"), bool):
            self.config["enable_holiday_perception"] = self._to_bool(
                self.config.get("enable_holiday_perception"), DEFAULT_CONFIG["enable_holiday_perception"]
            )
            changed = True
        if not isinstance(self.config.get("enable_platform_perception"), bool):
            self.config["enable_platform_perception"] = self._to_bool(
                self.config.get("enable_platform_perception"), DEFAULT_CONFIG["enable_platform_perception"]
            )
            changed = True
        if not isinstance(self.config.get("holiday_country"), str) or not self.config.get("holiday_country", "").strip():
            self.config["holiday_country"] = DEFAULT_CONFIG["holiday_country"]
            changed = True
        else:
            self.config["holiday_country"] = str(self.config["holiday_country"]).upper().strip()
        return changed

    def _normalize_security_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("security_allow_links"), bool):
            self.config["security_allow_links"] = self._to_bool(
                self.config.get("security_allow_links"), DEFAULT_CONFIG["security_allow_links"]
            )
            changed = True
        if not isinstance(self.config.get("security_blocked_words"), list):
            self.config["security_blocked_words"] = copy.deepcopy(DEFAULT_CONFIG["security_blocked_words"])
            changed = True

        if not isinstance(self.config.get("security_global_hourly_cap"), (int, float)):
            self.config["security_global_hourly_cap"] = DEFAULT_CONFIG["security_global_hourly_cap"]
            changed = True
        if int(self.config.get("security_global_hourly_cap", 0)) < 1:
            self.config["security_global_hourly_cap"] = 1
            changed = True
        if not isinstance(self.config.get("security_max_fail_streak"), (int, float)):
            self.config["security_max_fail_streak"] = DEFAULT_CONFIG["security_max_fail_streak"]
            changed = True
        if int(self.config.get("security_max_fail_streak", 0)) < 1:
            self.config["security_max_fail_streak"] = 1
            changed = True
        if not isinstance(self.config.get("security_fail_pause_min"), (int, float)):
            self.config["security_fail_pause_min"] = DEFAULT_CONFIG["security_fail_pause_min"]
            changed = True
        if float(self.config.get("security_fail_pause_min", 0)) < 5:
            self.config["security_fail_pause_min"] = 5
            changed = True
        if not isinstance(self.config.get("security_max_text_length"), (int, float)):
            self.config["security_max_text_length"] = DEFAULT_CONFIG["security_max_text_length"]
            changed = True
        if int(self.config.get("security_max_text_length", 0)) < 20:
            self.config["security_max_text_length"] = 20
            changed = True
        return changed

    def _normalize_debug_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("debug_status_window_sec"), int):
            self.config["debug_status_window_sec"] = DEFAULT_CONFIG["debug_status_window_sec"]
            changed = True
        if int(self.config.get("debug_status_window_sec", 0)) < 60:
            self.config["debug_status_window_sec"] = 60
            changed = True
        return changed

    def _save_webui_config(self):
        save_func = getattr(self.config, "save_config", None)
        if callable(save_func):
            try:
                save_func()
            except Exception as exc:
                logger.error(f"[idle-proactive] save webui config failed: {exc}")

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
        if self.config.get("debug_log", False):
            logger.debug(f"[idle-proactive] {msg}")

    def _maybe_log_status(self, session_key: str, s: Dict, now_ts: float, reason: str, force: bool = False):
        if not self.config.get("debug_log", False):
            return

        window = max(60, int(self.config.get("debug_status_window_sec", 300)))
        last = self._debug_status_last.get(session_key, 0.0)
        if not force and (now_ts - last) < window:
            return

        self._debug_status_last[session_key] = now_ts
        idle_sec = max(0, int(now_ts - s.get("last_interaction_at", now_ts)))
        cooldown_left = max(0, int(s.get("cooldown_until", 0) - now_ts))
        next_check_at = float(s.get("next_check_at", now_ts))
        next_check_in = max(0, int(next_check_at - now_ts))
        now_dt = self._now()
        min_idle_at = float(s.get("last_interaction_at", now_ts)) + float(self._effective_min_idle_sec(now_dt))
        earliest_trigger_at = max(next_check_at, float(s.get("cooldown_until", 0)), min_idle_at)
        earliest_trigger_in = max(0, int(earliest_trigger_at - now_ts))
        no_reply_streak = int(s.get("no_reply_streak", 0))
        decay = self._no_reply_decay_factor(s)

        self._debug(
            "status "
            f"reason={reason} session={session_key} "
            f"idle={idle_sec}s cooldown_left={cooldown_left}s "
            f"no_reply_streak={no_reply_streak} decay={decay:.2f} "
            f"next_check={self._fmt_ts(next_check_at)}(+{next_check_in}s) "
            f"next_trigger_earliest={self._fmt_ts(earliest_trigger_at)}(+{earliest_trigger_in}s)"
        )
