"""Metadata-only diagnostics with compatibility for existing string/tuple callers."""

import math
import re

REASONS = frozenset(
    {
        "ok",
        "unknown",
        "provider_missing",
        "empty_completion",
        "unsafe_content",
        "repetitive_content",
        "provider_error",
        "provider_timeout",
        "generation_cancelled",
        "generation_timeout",
        "generation_failed",
        "generation_queue_full",
        "session_missing",
        "origin_mismatch",
        "receipt_backlog_full",
        "busy",
        "shutdown",
        "identity_mismatch",
        "whitelist_removed",
        "session_replaced",
        "identity_changed",
        "new_human_message",
        "disabled",
        "safety_pause",
        "night_quiet",
        "cooldown_guard",
        "daily_limit",
        "global_hourly_cap",
        "period_limit",
        "await_human_reply",
        "send_message_false",
        "send_failed",
        "send_unknown",
        "partial_delivery",
        "delivery_cancelled",
    }
)
ENUMS = {
    "source": {"manual", "scheduled"},
    "status": {"completed", "blocked", "busy", "failed", "cancelled", "unknown"},
    "generation_status": {
        "not_started",
        "generated",
        "fallback",
        "timeout",
        "failed",
        "cancelled",
        "unknown",
    },
    "delivery_status": {"not_attempted", "sent", "failed", "unknown", "cancelled"},
    "receipt_status": {
        "disabled",
        "acknowledged",
        "pending",
        "rejected",
        "not_attempted",
    },
}


def safe_attempt(value):
    """Allow only defined fields/codes; legacy or untrusted state cannot echo text."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, allowed in ENUMS.items():
        item = value.get(key)
        result[key] = (
            item
            if isinstance(item, str) and item in allowed
            else "unknown"
            if "unknown" in allowed
            else "not_attempted"
            if "not_attempted" in allowed
            else "pending"
        )
    for key in ("reason_code", "generation_reason"):
        item = value.get(key)
        result[key] = item if isinstance(item, str) and item in REASONS else "unknown"
    event_id = value.get("attempt_id")
    result["attempt_id"] = (
        event_id
        if isinstance(event_id, str) and re.fullmatch(r"[a-fA-F0-9-]{36}", event_id)
        else ""
    )
    elapsed = value.get("elapsed_ms", 0)
    result["elapsed_ms"] = (
        min(3_600_000, max(0, int(elapsed)))
        if isinstance(elapsed, (int, float)) and math.isfinite(elapsed)
        else 0
    )
    return result


class GeneratedText(str):
    def __new__(cls, text, status="generated", reason="ok"):
        obj = super().__new__(cls, text)
        obj.generation_status = status
        obj.generation_reason = reason
        return obj


class ProactiveSendResult(tuple):
    def __new__(cls, sent, text, attempt):
        obj = super().__new__(cls, (bool(sent), text))
        obj.attempt = safe_attempt(attempt)
        return obj
