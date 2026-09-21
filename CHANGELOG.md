# Changelog

All notable changes to this project will be documented in this file.

## [2.6.0] - 2026-09-21

### Added
- **Phase 2-A zero-LLM emotion event detection**: keyword events `gratitude` /
  `misunderstood` / `sudden_warmth` / `cold_shoulder` plus the proactive-receipt
  state machine settling `valued_reply` / `ignored_proactive`, reported through
  companion-core's `record_emotion_event`.
- **Single-settlement rule**: one inbound message settles at most one event. Inside
  a proactive reply window it only books `valued_reply`; otherwise the keyword
  priority `misunderstood > gratitude > sudden_warmth > cold_shoulder` picks one.
  Both proactive receipts share `dedupe_key=f"proactive:{send_ts}"` so the server
  primary key makes them truly mutually exclusive (late arrival reports
  `duplicate`, no ledger change). Dedupe key is `msg:{message_id}` or, when absent,
  `msg:{umo}:{ts}` — never the message body.
- **Phase 2-C expression consumption**: `emotion_state` / `expression` from
  `get_proactive_context` are consumed via `{emotion_state}` / `{expression_mode}`
  placeholders, with a safe appended block when the template lacks them.
- **persona_state merge (plan §4.1)**: `expression.mode` is the authoritative tier;
  `persona_state` becomes style detail — `length_range` → length bias, session
  `mood` → warmth, `suppress_proactive` only lowers proactive bias. Total numeric
  adjustment is bounded by ±0.10 and never overrides the mode.
- **Group hard suppression re-check**: group sessions allow only `放松/活泼/温暖`
  with `warmth <= 0.55`; private emotion is never injected into groups.
- **Capability negotiation + degradation**: `capabilities` (dict, reads `emotion` /
  `expression`) and `api_version` are probed before calling `record_emotion_event`;
  missing capability / version mismatch / timeout / any exception degrades silently.
- **New config group** `emotion_event` (enable toggle, reply/ignore windows,
  cold-shoulder streak, keyword overrides, reserved LLM-judge switch).

### Fixed
- `tests/test_companion_context.py` capabilities test stub used an array
  `["life_state"]`; the frozen contract defines a `dict[str, bool]` — corrected.

### Notes
- `api_version` stays `1`; `capabilities` stays a dict and only gains keys.
- **Without companion-core installed, behavior is unchanged from v2.4.0.**

## [2.5.0] - 2026-09-20

### Added
- Optional `astrbot_plugin_tcompanion_core` context consumption: read-only
  life/relationship/motivation context injected into proactive prompts, with an
  `on_proactive_outcome` receipt after send and a `quota.allow` soft gate.

## [2.4.0] - 2026-09-19

### Added
- Structured `persona_state_custom` / `persona_state_overrides` template-list forms
  with lossless migration of the legacy hand-written JSON shape.
