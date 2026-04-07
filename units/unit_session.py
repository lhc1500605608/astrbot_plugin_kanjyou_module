import copy
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from ..config import DEFAULT_CONFIG
except ImportError:
    from config import DEFAULT_CONFIG

_GLOBAL_DEBUG_THROTTLE: dict[str, float] = {}


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
            "mood": float(self._mood_initial()),
            "mood_updated_at": now_ts,
        }

    def _ensure_session_shape(self, session: Dict):
        if "pending_human_reply" not in session:
            session["pending_human_reply"] = False
        if "no_reply_streak" not in session:
            session["no_reply_streak"] = 0
        if "period_counter_date" not in session:
            session["period_counter_date"] = self._now().strftime("%Y-%m-%d")
        if not isinstance(session.get("period_proactive_count"), dict):
            session["period_proactive_count"] = {
                "morning": 0,
                "afternoon": 0,
                "evening": 0,
            }
        if not isinstance(session.get("recent_proactive_texts"), list):
            session["recent_proactive_texts"] = []
        if "mood" not in session and isinstance(session.get("energy"), (int, float)):
            session["mood"] = float(session.get("energy"))
        if "mood_updated_at" not in session and isinstance(
            session.get("energy_updated_at"), (int, float)
        ):
            session["mood_updated_at"] = float(session.get("energy_updated_at"))
        if not isinstance(session.get("mood"), (int, float)):
            session["mood"] = float(self._mood_initial())
        if not isinstance(session.get("mood_updated_at"), (int, float)):
            session["mood_updated_at"] = self._now().timestamp()

    def _rollover_daily_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("counter_date") != today:
            session["counter_date"] = today
            session["today_proactive_count"] = 0
            session["period_counter_date"] = today
            session["period_proactive_count"] = {
                "morning": 0,
                "afternoon": 0,
                "evening": 0,
            }

    def _rollover_period_counter(self, session: Dict, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if session.get("period_counter_date") != today:
            session["period_counter_date"] = today
            session["period_proactive_count"] = {
                "morning": 0,
                "afternoon": 0,
                "evening": 0,
            }
            return
        if not isinstance(session.get("period_proactive_count"), dict):
            session["period_proactive_count"] = {
                "morning": 0,
                "afternoon": 0,
                "evening": 0,
            }

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
        if not (
            isinstance(start, str)
            and isinstance(end, str)
            and self._is_hhmm(start)
            and self._is_hhmm(end)
        ):
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
        return datetime.fromtimestamp(ts, ZoneInfo(self.config["timezone"])).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def _trim_global_send_history(self, now_ts: float):
        window_start = now_ts - 3600
        self._global_send_history = [
            ts for ts in self._global_send_history if ts >= window_start
        ]

    def _mood_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("mood_enabled"), DEFAULT_CONFIG["mood_enabled"]
        )

    def _mood_initial(self) -> float:
        return max(
            0.0,
            min(
                100.0,
                float(self.config.get("mood_initial", DEFAULT_CONFIG["mood_initial"])),
            ),
        )

    def _mood_min_trigger(self) -> float:
        return max(
            0.0,
            min(
                100.0,
                float(
                    self.config.get(
                        "mood_min_trigger", DEFAULT_CONFIG["mood_min_trigger"]
                    )
                ),
            ),
        )

    def _mood_cost_on_proactive(self) -> float:
        return max(
            0.0,
            min(
                100.0,
                float(
                    self.config.get(
                        "mood_cost_on_proactive",
                        DEFAULT_CONFIG["mood_cost_on_proactive"],
                    )
                ),
            ),
        )

    def _mood_cost_on_dialogue(self) -> float:
        return max(
            0.0,
            min(
                100.0,
                float(
                    self.config.get(
                        "mood_cost_on_dialogue", DEFAULT_CONFIG["mood_cost_on_dialogue"]
                    )
                ),
            ),
        )

    def _mood_recover_per_min(self) -> float:
        return max(
            0.0,
            float(
                self.config.get(
                    "mood_recover_per_min", DEFAULT_CONFIG["mood_recover_per_min"]
                )
            ),
        )

    def _mood_clamp(self, value: float) -> float:
        return max(0.0, min(100.0, float(value)))

    def _recover_session_mood(self, s: Dict, now_ts: float):
        if not self._mood_enabled():
            return
        last = float(s.get("mood_updated_at", now_ts))
        if now_ts <= last:
            return
        recover = ((now_ts - last) / 60.0) * self._mood_recover_per_min()
        if recover <= 0:
            return
        s["mood"] = self._mood_clamp(
            float(s.get("mood", self._mood_initial())) + recover
        )
        s["mood_updated_at"] = now_ts

    def _consume_session_mood_by_dialogue(self, s: Dict, now_ts: float):
        if not self._mood_enabled():
            return
        self._recover_session_mood(s, now_ts)
        s["mood"] = self._mood_clamp(
            float(s.get("mood", self._mood_initial())) - self._mood_cost_on_dialogue()
        )
        s["mood_updated_at"] = now_ts

    def _consume_session_mood_by_proactive(self, s: Dict, now_ts: float):
        if not self._mood_enabled():
            return
        self._recover_session_mood(s, now_ts)
        s["mood"] = self._mood_clamp(
            float(s.get("mood", self._mood_initial())) - self._mood_cost_on_proactive()
        )
        s["mood_updated_at"] = now_ts

    # Backward compatibility for legacy runtime calls that still reference energy_* APIs.
    def _energy_enabled(self) -> bool:
        return self._mood_enabled()

    def _energy_initial(self) -> float:
        return self._mood_initial()

    def _energy_min_trigger(self) -> float:
        return self._mood_min_trigger()

    def _energy_cost_on_proactive(self) -> float:
        return self._mood_cost_on_proactive()

    def _energy_recover_per_min(self) -> float:
        return self._mood_recover_per_min()

    def _recover_session_energy(self, s: Dict, now_ts: float):
        self._recover_session_mood(s, now_ts)

    def _consume_session_energy_by_dialogue(self, s: Dict, now_ts: float):
        self._consume_session_mood_by_dialogue(s, now_ts)

    def _consume_session_energy_by_proactive(self, s: Dict, now_ts: float):
        self._consume_session_mood_by_proactive(s, now_ts)

    def _boost_energy_by_human(self, s: Dict, now_ts: float):
        # Legacy compatibility: old event units may still call this method.
        # Keep behavior aligned with the new mood system: dialogue consumes mood.
        self._consume_session_mood_by_dialogue(s, now_ts)

    def _security_global_hourly_cap(self) -> int:
        return max(
            1,
            int(
                self.config.get(
                    "security_global_hourly_cap",
                    DEFAULT_CONFIG["security_global_hourly_cap"],
                )
            ),
        )

    def _security_max_fail_streak(self) -> int:
        return max(
            1,
            int(
                self.config.get(
                    "security_max_fail_streak",
                    DEFAULT_CONFIG["security_max_fail_streak"],
                )
            ),
        )

    def _security_fail_pause_sec(self) -> int:
        return max(
            300,
            int(
                float(
                    self.config.get(
                        "security_fail_pause_min",
                        DEFAULT_CONFIG["security_fail_pause_min"],
                    )
                )
                * 60
            ),
        )

    def _security_allow_links(self) -> bool:
        return self._to_bool(
            self.config.get(
                "security_allow_links", DEFAULT_CONFIG["security_allow_links"]
            ),
            DEFAULT_CONFIG["security_allow_links"],
        )

    def _security_blocked_words(self) -> List[str]:
        raw = self.config.get(
            "security_blocked_words", DEFAULT_CONFIG["security_blocked_words"]
        )
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

    def _debug_decision_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("debug_decision_log"), DEFAULT_CONFIG["debug_decision_log"]
        )

    def _decision_mode(self) -> str:
        mode = (
            str(self.config.get("decision_mode", DEFAULT_CONFIG["decision_mode"]))
            .strip()
            .lower()
        )
        if mode not in {"balanced", "strict", "active"}:
            return "balanced"
        return mode

    def _decision_min_confidence(self) -> float:
        return max(
            0.0,
            min(
                1.0,
                float(
                    self.config.get(
                        "decision_min_confidence",
                        DEFAULT_CONFIG["decision_min_confidence"],
                    )
                ),
            ),
        )

    def _decision_group_quiet_threshold(self) -> int:
        return max(
            1,
            int(
                self.config.get(
                    "decision_group_quiet_threshold",
                    DEFAULT_CONFIG["decision_group_quiet_threshold"],
                )
            ),
        )

    def _decision_trace_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("decision_trace_enabled"),
            DEFAULT_CONFIG["decision_trace_enabled"],
        )

    def _quality_trace_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("quality_trace_enabled"),
            DEFAULT_CONFIG["quality_trace_enabled"],
        )

    def _record_decision(self, session_key: str, payload: Dict):
        decision = dict(payload or {})
        decision["session"] = session_key
        decision["at"] = self._now().strftime("%Y-%m-%d %H:%M:%S")
        self._decision_last[session_key] = decision
        if not self._decision_trace_enabled():
            return
        self._decision_trace.append(decision)
        limit = 100
        if len(self._decision_trace) > limit:
            self._decision_trace = self._decision_trace[-limit:]

    def _decision_last_for_session(self, session_key: str) -> Optional[Dict]:
        row = self._decision_last.get(session_key)
        if isinstance(row, dict):
            return row
        return None

    def _quality_bump(self, key: str, n: int = 1):
        if not self._quality_trace_enabled():
            return
        if not isinstance(self._quality_trace, dict):
            self._quality_trace = {}
        self._quality_trace[key] = int(self._quality_trace.get(key, 0)) + max(0, int(n))

    def _decision_status_summary(self) -> str:
        trace_count = (
            len(self._decision_trace) if isinstance(self._decision_trace, list) else 0
        )
        sessions = (
            len(self._decision_last) if isinstance(self._decision_last, dict) else 0
        )
        q = self._quality_trace if isinstance(self._quality_trace, dict) else {}
        shallow_hits = int(q.get("shallow_hit", 0))
        shallow_fallback = int(q.get("shallow_fallback", 0))
        lite_ok = int(q.get("lite_ok", 0))
        lite_fail = int(q.get("lite_fail", 0))
        return (
            f"mode={self._decision_mode()} min_conf={self._decision_min_confidence():.2f} "
            f"sessions={sessions} trace={trace_count} "
            f"shallow_hit={shallow_hits} shallow_fallback={shallow_fallback} "
            f"lite_ok={lite_ok} lite_fail={lite_fail}"
        )

    def _debug_decision(self, session_key: str, payload: Dict):
        if not self.config.get("debug_log", False):
            return
        if not self._debug_decision_enabled():
            return
        now_ts = self._now().timestamp()
        window = max(60, int(self.config.get("debug_status_window_sec", 300)))
        key = f"decision:{session_key}"
        last = self._debug_status_last.get(key, 0.0)
        if (now_ts - last) < window:
            return
        self._debug_status_last[key] = now_ts
        normalized = {"session": session_key}
        normalized.update(payload or {})
        try:
            line = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            line = str(normalized)
        self._debug(f"decision {line}")

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
            self.config["private_whitelist"] = copy.deepcopy(
                DEFAULT_CONFIG["private_whitelist"]
            )
            changed = True
        if not isinstance(self.config.get("group_whitelist"), list):
            self.config["group_whitelist"] = copy.deepcopy(
                DEFAULT_CONFIG["group_whitelist"]
            )
            changed = True

        # Backward compatibility: migrate legacy config_mode -> advanced_enabled.
        if self.config.get("advanced_enabled") is None:
            mode = str(self.config.get("config_mode", "basic")).lower()
            self.config["advanced_enabled"] = mode == "advanced"
            changed = True
        if not isinstance(self.config.get("advanced_enabled"), bool):
            self.config["advanced_enabled"] = self._to_bool(
                self.config.get("advanced_enabled"), DEFAULT_CONFIG["advanced_enabled"]
            )
            changed = True

        for key in ("enabled", "lifecycle_log", "debug_log"):
            if not isinstance(self.config.get(key), bool):
                self.config[key] = self._to_bool(
                    self.config.get(key), DEFAULT_CONFIG[key]
                )
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
        if self.config.get("min_idle_min") is None and isinstance(
            self.config.get("min_idle_sec"), (int, float)
        ):
            self.config["min_idle_min"] = max(
                1, int(float(self.config["min_idle_sec"]) // 60)
            )
            changed = True
        if self.config.get("max_idle_min") is None and isinstance(
            self.config.get("max_idle_sec"), (int, float)
        ):
            self.config["max_idle_min"] = max(
                1, int(float(self.config["max_idle_sec"]) // 60)
            )
            changed = True
        if self.config.get("cooldown_min") is None and isinstance(
            self.config.get("cooldown_sec"), (int, float)
        ):
            self.config["cooldown_min"] = max(
                1, int(float(self.config["cooldown_sec"]) // 60)
            )
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
                float(self.config["min_idle_min"]) + 30,
                float(DEFAULT_CONFIG["max_idle_min"]),
            )
            changed = True
        return changed

    def _normalize_generation_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("persona_id"), str):
            self.config["persona_id"] = DEFAULT_CONFIG["persona_id"]
            changed = True
        if not isinstance(self.config.get("proactive_provider_id"), str):
            self.config["proactive_provider_id"] = DEFAULT_CONFIG[
                "proactive_provider_id"
            ]
            changed = True
        if not isinstance(self.config.get("lite_llm_enabled"), bool):
            self.config["lite_llm_enabled"] = self._to_bool(
                self.config.get("lite_llm_enabled"),
                DEFAULT_CONFIG["lite_llm_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("lite_provider_id"), str):
            self.config["lite_provider_id"] = DEFAULT_CONFIG["lite_provider_id"]
            changed = True
        if not isinstance(self.config.get("lite_llm_timeout_sec"), (int, float)):
            self.config["lite_llm_timeout_sec"] = DEFAULT_CONFIG["lite_llm_timeout_sec"]
            changed = True
        if float(self.config.get("lite_llm_timeout_sec", 0)) < 1:
            self.config["lite_llm_timeout_sec"] = 1
            changed = True
        mode = (
            str(self.config.get("decision_mode", DEFAULT_CONFIG["decision_mode"]))
            .strip()
            .lower()
        )
        if mode not in {"balanced", "strict", "active"}:
            self.config["decision_mode"] = DEFAULT_CONFIG["decision_mode"]
            changed = True
        else:
            self.config["decision_mode"] = mode
        if not isinstance(self.config.get("decision_min_confidence"), (int, float)):
            self.config["decision_min_confidence"] = DEFAULT_CONFIG[
                "decision_min_confidence"
            ]
            changed = True
        self.config["decision_min_confidence"] = max(
            0.0, min(1.0, float(self.config.get("decision_min_confidence")))
        )
        if not isinstance(
            self.config.get("decision_group_quiet_threshold"), (int, float)
        ):
            self.config["decision_group_quiet_threshold"] = DEFAULT_CONFIG[
                "decision_group_quiet_threshold"
            ]
            changed = True
        if int(self.config.get("decision_group_quiet_threshold", 0)) < 1:
            self.config["decision_group_quiet_threshold"] = 1
            changed = True
        if not isinstance(self.config.get("decision_trace_enabled"), bool):
            self.config["decision_trace_enabled"] = self._to_bool(
                self.config.get("decision_trace_enabled"),
                DEFAULT_CONFIG["decision_trace_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("quality_trace_enabled"), bool):
            self.config["quality_trace_enabled"] = self._to_bool(
                self.config.get("quality_trace_enabled"),
                DEFAULT_CONFIG["quality_trace_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("dialogue_wait_enabled"), bool):
            self.config["dialogue_wait_enabled"] = self._to_bool(
                self.config.get("dialogue_wait_enabled"),
                DEFAULT_CONFIG["dialogue_wait_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("dialogue_wait_timeout_sec"), (int, float)):
            self.config["dialogue_wait_timeout_sec"] = DEFAULT_CONFIG[
                "dialogue_wait_timeout_sec"
            ]
            changed = True
        if int(self.config.get("dialogue_wait_timeout_sec", 0)) < 1:
            self.config["dialogue_wait_timeout_sec"] = 1
            changed = True
        if not isinstance(self.config.get("dialogue_wait_max_merge"), (int, float)):
            self.config["dialogue_wait_max_merge"] = DEFAULT_CONFIG[
                "dialogue_wait_max_merge"
            ]
            changed = True
        if int(self.config.get("dialogue_wait_max_merge", 0)) < 1:
            self.config["dialogue_wait_max_merge"] = 1
            changed = True
        if not isinstance(self.config.get("output_segment_enabled"), bool):
            self.config["output_segment_enabled"] = self._to_bool(
                self.config.get("output_segment_enabled"),
                DEFAULT_CONFIG["output_segment_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("output_segment_max_parts"), (int, float)):
            self.config["output_segment_max_parts"] = DEFAULT_CONFIG[
                "output_segment_max_parts"
            ]
            changed = True
        if int(self.config.get("output_segment_max_parts", 0)) < 1:
            self.config["output_segment_max_parts"] = 1
            changed = True
        if not isinstance(self.config.get("output_segment_max_chars"), (int, float)):
            self.config["output_segment_max_chars"] = DEFAULT_CONFIG[
                "output_segment_max_chars"
            ]
            changed = True
        if int(self.config.get("output_segment_max_chars", 0)) < 10:
            self.config["output_segment_max_chars"] = 10
            changed = True
        if not isinstance(self.config.get("holiday_qa_main_llm_enabled"), bool):
            self.config["holiday_qa_main_llm_enabled"] = self._to_bool(
                self.config.get("holiday_qa_main_llm_enabled"),
                DEFAULT_CONFIG["holiday_qa_main_llm_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("proactive_lite_refine_enabled"), bool):
            self.config["proactive_lite_refine_enabled"] = self._to_bool(
                self.config.get("proactive_lite_refine_enabled"),
                DEFAULT_CONFIG["proactive_lite_refine_enabled"],
            )
            changed = True
        if (
            not isinstance(self.config.get("proactive_prompt_template"), str)
            or not self.config["proactive_prompt_template"].strip()
        ):
            self.config["proactive_prompt_template"] = DEFAULT_CONFIG[
                "proactive_prompt_template"
            ]
            changed = True
        if (
            not isinstance(self.config.get("fallback_proactive_text"), str)
            or not self.config["fallback_proactive_text"].strip()
        ):
            self.config["fallback_proactive_text"] = DEFAULT_CONFIG[
                "fallback_proactive_text"
            ]
            changed = True
        if not isinstance(self.config.get("enable_holiday_perception"), bool):
            self.config["enable_holiday_perception"] = self._to_bool(
                self.config.get("enable_holiday_perception"),
                DEFAULT_CONFIG["enable_holiday_perception"],
            )
            changed = True
        if not isinstance(self.config.get("holiday_qa_enabled"), bool):
            self.config["holiday_qa_enabled"] = self._to_bool(
                self.config.get("holiday_qa_enabled"),
                DEFAULT_CONFIG["holiday_qa_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("enable_platform_perception"), bool):
            self.config["enable_platform_perception"] = self._to_bool(
                self.config.get("enable_platform_perception"),
                DEFAULT_CONFIG["enable_platform_perception"],
            )
            changed = True
        if (
            not isinstance(self.config.get("holiday_country"), str)
            or not self.config.get("holiday_country", "").strip()
        ):
            self.config["holiday_country"] = DEFAULT_CONFIG["holiday_country"]
            changed = True
        else:
            self.config["holiday_country"] = (
                str(self.config["holiday_country"]).upper().strip()
            )
        if not isinstance(self.config.get("holiday_api_enabled"), bool):
            self.config["holiday_api_enabled"] = self._to_bool(
                self.config.get("holiday_api_enabled"),
                DEFAULT_CONFIG["holiday_api_enabled"],
            )
            changed = True
        if not isinstance(self.config.get("holiday_api_timeout_sec"), (int, float)):
            self.config["holiday_api_timeout_sec"] = DEFAULT_CONFIG[
                "holiday_api_timeout_sec"
            ]
            changed = True
        if float(self.config.get("holiday_api_timeout_sec", 0)) < 1:
            self.config["holiday_api_timeout_sec"] = 1
            changed = True
        if not isinstance(self.config.get("holiday_api_cache_ttl_sec"), (int, float)):
            self.config["holiday_api_cache_ttl_sec"] = DEFAULT_CONFIG[
                "holiday_api_cache_ttl_sec"
            ]
            changed = True
        if int(self.config.get("holiday_api_cache_ttl_sec", 0)) < 60:
            self.config["holiday_api_cache_ttl_sec"] = 60
            changed = True
        return changed

    def _normalize_security_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("security_allow_links"), bool):
            self.config["security_allow_links"] = self._to_bool(
                self.config.get("security_allow_links"),
                DEFAULT_CONFIG["security_allow_links"],
            )
            changed = True
        # Backward compatibility: migrate legacy energy_* keys to mood_* when mood_* is missing.
        legacy_map = {
            "energy_enabled": "mood_enabled",
            "energy_initial": "mood_initial",
            "energy_min_trigger": "mood_min_trigger",
            "energy_cost_on_proactive": "mood_cost_on_proactive",
            "energy_recover_per_min": "mood_recover_per_min",
        }
        for old_key, new_key in legacy_map.items():
            if (
                self.config.get(new_key) is None
                and self.config.get(old_key) is not None
            ):
                self.config[new_key] = self.config.get(old_key)
                changed = True

        if not isinstance(self.config.get("mood_enabled"), bool):
            self.config["mood_enabled"] = self._to_bool(
                self.config.get("mood_enabled"), DEFAULT_CONFIG["mood_enabled"]
            )
            changed = True
        if not isinstance(self.config.get("debug_decision_log"), bool):
            self.config["debug_decision_log"] = self._to_bool(
                self.config.get("debug_decision_log"),
                DEFAULT_CONFIG["debug_decision_log"],
            )
            changed = True
        if not isinstance(self.config.get("security_blocked_words"), list):
            self.config["security_blocked_words"] = copy.deepcopy(
                DEFAULT_CONFIG["security_blocked_words"]
            )
            changed = True

        if not isinstance(self.config.get("security_global_hourly_cap"), (int, float)):
            self.config["security_global_hourly_cap"] = DEFAULT_CONFIG[
                "security_global_hourly_cap"
            ]
            changed = True
        if int(self.config.get("security_global_hourly_cap", 0)) < 1:
            self.config["security_global_hourly_cap"] = 1
            changed = True
        if not isinstance(self.config.get("security_max_fail_streak"), (int, float)):
            self.config["security_max_fail_streak"] = DEFAULT_CONFIG[
                "security_max_fail_streak"
            ]
            changed = True
        if int(self.config.get("security_max_fail_streak", 0)) < 1:
            self.config["security_max_fail_streak"] = 1
            changed = True
        if not isinstance(self.config.get("security_fail_pause_min"), (int, float)):
            self.config["security_fail_pause_min"] = DEFAULT_CONFIG[
                "security_fail_pause_min"
            ]
            changed = True
        if float(self.config.get("security_fail_pause_min", 0)) < 5:
            self.config["security_fail_pause_min"] = 5
            changed = True
        if not isinstance(self.config.get("security_max_text_length"), (int, float)):
            self.config["security_max_text_length"] = DEFAULT_CONFIG[
                "security_max_text_length"
            ]
            changed = True
        if int(self.config.get("security_max_text_length", 0)) < 20:
            self.config["security_max_text_length"] = 20
            changed = True
        if not isinstance(self.config.get("mood_initial"), (int, float)):
            self.config["mood_initial"] = DEFAULT_CONFIG["mood_initial"]
            changed = True
        if not isinstance(self.config.get("mood_min_trigger"), (int, float)):
            self.config["mood_min_trigger"] = DEFAULT_CONFIG["mood_min_trigger"]
            changed = True
        if not isinstance(self.config.get("mood_cost_on_proactive"), (int, float)):
            self.config["mood_cost_on_proactive"] = DEFAULT_CONFIG[
                "mood_cost_on_proactive"
            ]
            changed = True
        if not isinstance(self.config.get("mood_cost_on_dialogue"), (int, float)):
            self.config["mood_cost_on_dialogue"] = DEFAULT_CONFIG[
                "mood_cost_on_dialogue"
            ]
            changed = True
        if not isinstance(self.config.get("mood_recover_per_min"), (int, float)):
            self.config["mood_recover_per_min"] = DEFAULT_CONFIG["mood_recover_per_min"]
            changed = True
        self.config["mood_initial"] = self._mood_clamp(
            self.config.get("mood_initial", DEFAULT_CONFIG["mood_initial"])
        )
        self.config["mood_min_trigger"] = self._mood_clamp(
            self.config.get("mood_min_trigger", DEFAULT_CONFIG["mood_min_trigger"])
        )
        self.config["mood_cost_on_proactive"] = self._mood_clamp(
            self.config.get(
                "mood_cost_on_proactive", DEFAULT_CONFIG["mood_cost_on_proactive"]
            )
        )
        self.config["mood_cost_on_dialogue"] = self._mood_clamp(
            self.config.get(
                "mood_cost_on_dialogue", DEFAULT_CONFIG["mood_cost_on_dialogue"]
            )
        )
        self.config["mood_recover_per_min"] = max(
            0.0,
            float(
                self.config.get(
                    "mood_recover_per_min", DEFAULT_CONFIG["mood_recover_per_min"]
                )
            ),
        )
        if self.config["mood_min_trigger"] > self.config["mood_initial"]:
            self.config["mood_initial"] = self.config["mood_min_trigger"]
            changed = True
        return changed

    def _normalize_debug_layer(self) -> bool:
        changed = False
        if not isinstance(self.config.get("debug_status_window_sec"), int):
            self.config["debug_status_window_sec"] = DEFAULT_CONFIG[
                "debug_status_window_sec"
            ]
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

    def _debug_throttled(self, key: str, msg: str):
        if not self.config.get("debug_log", False):
            return
        now_ts = self._now().timestamp()
        window = max(60, int(self.config.get("debug_status_window_sec", 300)))
        throttle_key = f"idle-proactive:{key}"
        last = _GLOBAL_DEBUG_THROTTLE.get(throttle_key, 0.0)
        if (now_ts - last) < window:
            return
        _GLOBAL_DEBUG_THROTTLE[throttle_key] = now_ts
        logger.debug(f"[idle-proactive] {msg}")

    def _run_startup_config_checks(self):
        issues = []
        enabled = bool(self.config.get("enabled", True))
        private_wl = self.config.get("private_whitelist", [])
        group_wl = self.config.get("group_whitelist", [])
        sleep_start = str(self.config.get("sleep_start", "") or "")
        sleep_end = str(self.config.get("sleep_end", "") or "")
        proactive_provider = str(
            self.config.get("proactive_provider_id", "") or ""
        ).strip()
        dialogue_wait_enabled = bool(self.config.get("dialogue_wait_enabled", False))

        if enabled and not private_wl and not group_wl:
            issues.append(
                (
                    "WARN",
                    "proactive_whitelist_empty: 插件白名单为空，主动问候将不会触发。",
                )
            )

        if not self._is_hhmm(sleep_start) or not self._is_hhmm(sleep_end):
            issues.append(
                (
                    "WARN",
                    f"sleep_window_invalid: sleep_start={sleep_start} sleep_end={sleep_end}，将回退默认时段。",
                )
            )
        elif sleep_start == sleep_end:
            issues.append(
                (
                    "WARN",
                    f"sleep_window_same_edge: sleep_start==sleep_end=={sleep_start}，建议避免相同起止时间。",
                )
            )

        if dialogue_wait_enabled:
            issues.append(
                (
                    "INFO",
                    "dialogue_wait_ignored: 当前插件为主动问候模式，对话等待参数不参与主动触发决策。",
                )
            )

        if not proactive_provider:
            issues.append(
                (
                    "INFO",
                    "proactive_provider_fallback: proactive_provider_id 为空，将在发送时跟随会话 provider。",
                )
            )

        mode = str(self.config.get("decision_mode", "balanced"))
        min_conf = float(self.config.get("decision_min_confidence", 0.6))
        if mode == "strict" and min_conf >= 0.9:
            issues.append(
                (
                    "WARN",
                    f"decision_too_strict: mode={mode} min_conf={min_conf:.2f}，主动触发概率可能极低。",
                )
            )

        self._debug(
            f"init-check summary enabled={enabled} private_wl={len(private_wl) if isinstance(private_wl, list) else 0} "
            f"group_wl={len(group_wl) if isinstance(group_wl, list) else 0} issues={len(issues)}"
        )
        for level, message in issues:
            self._debug(f"init-check {level} {message}")

    def _maybe_log_status(
        self, session_key: str, s: Dict, now_ts: float, reason: str, force: bool = False
    ):
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
        min_idle_at = float(s.get("last_interaction_at", now_ts)) + float(
            self._effective_min_idle_sec(now_dt)
        )
        earliest_trigger_at = max(
            next_check_at, float(s.get("cooldown_until", 0)), min_idle_at
        )
        earliest_trigger_in = max(0, int(earliest_trigger_at - now_ts))
        no_reply_streak = int(s.get("no_reply_streak", 0))
        decay = self._no_reply_decay_factor(s)
        mood = float(s.get("mood", self._mood_initial()))

        self._debug(
            "status "
            f"reason={reason} session={session_key} "
            f"idle={idle_sec}s cooldown_left={cooldown_left}s "
            f"no_reply_streak={no_reply_streak} decay={decay:.2f} mood={mood:.2f} "
            f"next_check={self._fmt_ts(next_check_at)}(+{next_check_in}s) "
            f"next_trigger_earliest={self._fmt_ts(earliest_trigger_at)}(+{earliest_trigger_in}s)"
        )
