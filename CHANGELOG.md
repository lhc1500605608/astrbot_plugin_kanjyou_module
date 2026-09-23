# Changelog

All notable changes to this project will be documented in this file.

## [2.11.0] - 2026-09-23

### Added
- 群聊参与：对接 companion-core 时，群内主动发言前先按该群的参与节奏（最短间隔、
  每小时上限、群繁忙）决定是否接话，命中时不发言并把原因写入日志；群消息会驱动
  companion 的群活跃计数（只上报计数与短标签，不含消息原文）。
- 群氛围：群内主动消息改为参考该群的活跃度与话题氛围生成，群聊不再注入任何私聊
  关系/情绪/生活线/画像。

### Changed
- 版本收敛到 `2.11.0`（`metadata.yaml` / `config.py` / `tests/test_version_convergence.py` / `README.md`）。

### Notes
- 需启用「陪伴」且 companion-core 提供群聊能力；未安装/异常/超时一律回落旧行为，
  私聊行为不变。

## [2.10.2] - 2026-09-23

### Changed
- 插件外显名统一为 **WarmWhisper · 暖语**（插件标识名 `astrbot_plugin_kanjyou_module`、
  行为与配置键均不变）；README 标题、插件库/面板显示名与三个页面标题同步更新。

## [2.10.1] - 2026-09-23

### Fixed
- 日志页「结果」筛选下拉原先直接显示内部英文值（`allow`/`skip`/`triggered`/`failed`），
  现按界面语言显示「英文值（中文说明）」（如 `allow（放行）`），选项取值与筛选行为不变。

## [2.10.0] - 2026-09-23

### Added
- 主动消息现在也会参考 companion-core 提供的生活细节：当天天气、当前/下一顿用餐、
  作息时段与前一天小结，作为独立字段注入 prompt（模板含 `{life_detail}` 时填入，
  否则安全追加到陪伴块）。
- 生活细节仅在私聊注入；与记忆片段按同一规整（空白/大小写归一后精确去重）合并，
  重复内容只出现一次。需启用「陪伴」且 companion-core 提供对应能力，否则行为不变。

### Changed
- 版本收敛到 `2.10.0`（`metadata.yaml` / `config.py` / `tests/test_version_convergence.py` / `README.md`）。

## [2.9.0] - 2026-09-22

### Added
- 主动消息现在也会采用 companion-core 提供的记忆片段：与插件自身的记忆召回合并，
  去重后一起注入（重复内容只出现一次，总条数上限不变）。若画像摘要可用，会作为
  一条偏好提示一并参考。需启用「陪伴」且 companion-core 提供对应能力，否则行为不变。
- 群聊仍不注入任何私聊记忆；companion-core 未提供该能力时不读取、不注入。

### Changed
- 版本收敛到 `2.9.0`（`metadata.yaml` / `config.py` / `tests/test_version_convergence.py` / `README.md`）。

## [2.8.1] - 2026-09-21

### Fixed
- 修复主动消息可能重复提到对方之前提过的事：即使处于冷却时间或已提过多次，提醒内容
  仍可能被带上。现在冷却中、已达提醒次数上限、未到沉淀时间或功能关闭时都不会再提及，
  通过闸门时也只会提一件事。
- 兜底：动机字段（`motivation.reason`）若携带任何未完话题短标签则整条丢弃，确保该标签
  只能经闸门后的续接块进入 prompt（配合 companion-core v1.2.0 的泛化 reason）。

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
