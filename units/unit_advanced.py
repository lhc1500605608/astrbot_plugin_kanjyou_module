from config import INTERNAL_POLICY


class AdvancedPolicyUnitsMixin:
    def _policy(self, key: str):
        default = INTERNAL_POLICY[key]
        if not self._advanced_mode():
            return default
        raw = self.config.get(key)
        if raw is None:
            return default
        return self._resolve_advanced_policy_value(key, raw, default)

    def _resolve_advanced_policy_value(self, key: str, raw, default):
        try:
            if key in {
                "require_human_reply_before_next_proactive",
                "period_quota_enabled",
                "no_reply_decay_enabled",
                "weekend_mode_enabled",
                "quality_dedupe_enabled",
            }:
                return self._to_bool(raw, default)
            if key == "max_per_session_per_day":
                return max(1, int(raw))
            if key in {"trigger_base_prob", "trigger_max_prob"}:
                return min(1.0, max(0.0, float(raw)))
            if key in {
                "period_quota_morning_max",
                "period_quota_afternoon_max",
                "period_quota_evening_max",
            }:
                return max(0, int(raw))
            if key in {"no_reply_decay_factor", "no_reply_decay_max_factor"}:
                return max(1.0, float(raw))
            if key in {"weekend_min_idle_multiplier", "weekend_cooldown_multiplier"}:
                return max(1.0, float(raw))
            if key == "weekend_quota_multiplier":
                return max(0.0, float(raw))
            if key == "quality_history_size":
                return max(1, int(raw))
        except Exception:
            return default
        return default

    def _advanced_mode(self) -> bool:
        # Preferred flag: advanced_enabled (bool). Legacy fallback: config_mode == advanced.
        if self.config.get("advanced_enabled") is not None:
            return self._to_bool(self.config.get("advanced_enabled"), False)
        return str(self.config.get("config_mode", "basic")).lower() == "advanced"

    def _max_per_session_per_day(self) -> int:
        return int(self._policy("max_per_session_per_day"))

    def _trigger_base_prob(self) -> float:
        return float(self._policy("trigger_base_prob"))

    def _trigger_max_prob(self) -> float:
        return max(self._trigger_base_prob(), float(self._policy("trigger_max_prob")))

    def _require_human_reply_before_next_proactive(self) -> bool:
        return bool(self._policy("require_human_reply_before_next_proactive"))

    def _period_quota_enabled(self) -> bool:
        return bool(self._policy("period_quota_enabled"))

    def _period_quota_morning_max(self) -> int:
        return int(self._policy("period_quota_morning_max"))

    def _period_quota_afternoon_max(self) -> int:
        return int(self._policy("period_quota_afternoon_max"))

    def _period_quota_evening_max(self) -> int:
        return int(self._policy("period_quota_evening_max"))

    def _no_reply_decay_enabled(self) -> bool:
        return bool(self._policy("no_reply_decay_enabled"))

    def _no_reply_decay_factor_base(self) -> float:
        return float(self._policy("no_reply_decay_factor"))

    def _no_reply_decay_max_factor(self) -> float:
        return float(self._policy("no_reply_decay_max_factor"))

    def _weekend_mode_enabled(self) -> bool:
        return bool(self._policy("weekend_mode_enabled"))

    def _weekend_min_idle_multiplier(self) -> float:
        return float(self._policy("weekend_min_idle_multiplier"))

    def _weekend_cooldown_multiplier(self) -> float:
        return float(self._policy("weekend_cooldown_multiplier"))

    def _weekend_quota_multiplier(self) -> float:
        return float(self._policy("weekend_quota_multiplier"))

    def _quality_dedupe_enabled(self) -> bool:
        return bool(self._policy("quality_dedupe_enabled"))

    def _quality_history_size(self) -> int:
        return int(self._policy("quality_history_size"))
