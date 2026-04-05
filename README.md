# 情绪价值提供者

[![Version](https://img.shields.io/badge/version-v1.6.0-blue.svg)](https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module)
[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-green.svg)](https://github.com/AstrBotDevs/AstrBot)

一个 AstrBot 闲时主动对话插件：
当会话长时间无消息时，按人格与 Prompt 生成自然问候，帮助重新开启对话。

## 功能

- 私聊/群聊分会话独立计时
- 分钟级触发阈值与冷却控制（防啰嗦）
- 周末模式（周六/周日自动调节频率）
- 人格 + Prompt 动态生成主动问候
- 对话质量优化：近期问候去重 + 语气风格自适应
- 夜间免打扰（支持跨天）
- 白名单控制（仅指定私聊/群聊生效）
- 控制台 Debug 状态指示器

## 快速使用

1. 安装并启用插件
2. 在 WebUI 配置白名单、`persona_id`、免打扰时间
3. 按需开启 `debug_log`
4. 发送 `/idle_test` 验证主动消息链路

## 关键配置

- `min_idle_min` / `max_idle_min` / `cooldown_min`
- `trigger_base_prob` / `trigger_max_prob`
- `require_human_reply_before_next_proactive`
- `period_quota_enabled` / `period_quota_morning_max` / `period_quota_afternoon_max` / `period_quota_evening_max`
- `no_reply_decay_enabled` / `no_reply_decay_factor` / `no_reply_decay_max_factor`
- `weekend_mode_enabled` / `weekend_min_idle_multiplier` / `weekend_cooldown_multiplier` / `weekend_quota_multiplier`
- `quality_dedupe_enabled` / `quality_history_size`
- `persona_id` / `proactive_provider_id`
- `proactive_prompt_template` / `fallback_proactive_text`

## 推荐参数（保守）

- `min_idle_min = 45`
- `max_idle_min = 180`
- `cooldown_min = 90`
- `trigger_base_prob = 0.02`
- `trigger_max_prob = 0.18`
- `require_human_reply_before_next_proactive = true`
- `period_quota_enabled = true`（上午/下午/晚间默认各 1 次）
- `no_reply_decay_enabled = true`
- `no_reply_decay_factor = 1.6`
- `no_reply_decay_max_factor = 4.0`
- `weekend_mode_enabled = true`
- `weekend_min_idle_multiplier = 1.25`
- `weekend_cooldown_multiplier = 1.35`
- `weekend_quota_multiplier = 0.8`
- `quality_dedupe_enabled = true`
- `quality_history_size = 6`

## 指令

- 管理指令默认仅 AstrBot 管理员可调用（自动使用 AstrBot 全局管理员配置）
- `/idle_status`
- `/idle_enable` / `/idle_disable`
- `/idle_wl_add_private <user_id>` / `/idle_wl_del_private <user_id>`
- `/idle_wl_add_group <group_id>` / `/idle_wl_del_group <group_id>`
- `/idle_sleep_set <HH:MM> <HH:MM>`
- `/idle_test`

---

## 许可证

AGPL-3.0

---

作者：Tango  
仓库：https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module
