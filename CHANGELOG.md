# Changelog

All notable changes to this project will be documented in this file.

## [2.8.0] - 2026-09-21

### Added
- **未完话题续接**：零 LLM 从私聊入站消息里提取「对方提过、还没继续」的事
  （承诺/计划/悬而未决的提问），上报 companion-core；生成主动消息时至多挑选 1 条
  自然提起，发送成功后回执计数。默认开启，可在配置中关闭或调整冷却/沉淀时间。
  需启用「陪伴」且 companion-core 提供对应能力，否则行为不变。

### Changed
- 版本收敛到 `2.8.0`（`metadata.yaml` / `config.py` / `tests/test_version_convergence.py` / `README.md`）。

## [2.7.3] - 2026-09-21

### Fixed
- Passive segmentation no longer merges the model's own explicit line breaks
  back into a single message: when the reply already contains `||` or two or
  more non-empty lines, the complexity budget acts only as an upper bound
  (`max_parts`) and never collapses the parts to one.
- `||` is now treated as an explicit delimiter during the lossless check, so
  pipe-separated replies are actually split instead of falling back to one
  message.

## [2.7.2] - 2026-09-21

### Changed
- Config/UI copy cleanup (TMEAAA-488): removed internal implementation terms and
  version-history wording from `_conf_schema.json` and `README.md` (Phase labels,
  "behavior unchanged from vX.Y.Z", internal event/capability names, `clamp`
  ranges), trimmed over-long `hint`s into user-friendly one-liners.
- README: replaced the version-by-version "新增能力" history with a single
  non-versioned 进阶能力 overview.
- No key names, defaults, or runtime behavior changed.

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
