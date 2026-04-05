# 情绪价值提供者

[![Version](https://img.shields.io/badge/version-v1.11.0-blue.svg)](https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module)
[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-green.svg)](https://github.com/AstrBotDevs/AstrBot)

一个 AstrBot 闲时主动对话插件：
当会话长时间无消息时，按人格与 Prompt 生成自然问候，帮助重新开启对话。

## 功能

- 私聊/群聊分会话独立计时
- 分钟级触发阈值与冷却控制（防啰嗦）
- 人格 + Prompt 动态生成主动问候
- 环境感知：时间段、星期/工作日、节假日（CN）与平台场景
- 夜间免打扰（支持跨天）
- 白名单控制（仅指定私聊/群聊生效）
- 控制台 Debug 状态指示器
- 内置高级策略：分时段配额、未回复衰减、周末模式、近期问候去重
- 安全与控制层：全局频率闸门、失败熔断暂停、禁用词过滤、链接开关、长度限制
- 情绪值系统：主动与对话都会消耗情绪值，随时间恢复
- 结构化决策日志：输出触发链路关键决策（JSON）

## 双层配置模式

- `config_mode=basic`（默认，推荐）：只需配置核心参数，插件自动使用内置稳妥策略
- `config_mode=advanced`：开启高级参数自定义（配额/衰减/周末/去重等）

## 配置执行单元

- 基础层：开关、白名单、模式与基础布尔修正
- 时间层：免打扰 + idle/cooldown 分钟参数与旧版迁移
- 生成层：人格、Provider、Prompt 与兜底文案
- 安全层：频率闸门、失败熔断、禁用词、链接、长度限制
- 调试层：debug 窗口与日志相关参数

## 脚本结构

- `main.py`：插件入口、生命周期、消息事件钩子
- `config.py`：默认配置、策略常量、执行顺序、版本
- `units/unit_commands.py`：管理指令单元（管理员命令）
- `units/unit_events.py`：消息事件钩子单元（会话触达更新）
- `units/unit_runtime.py`：闲时巡检调度与触发执行单元
- `units/unit_advanced.py`：高级参数策略单元（advanced 模式）
- `units/unit_generation.py`：触发策略 + 主动文案生成单元
- `units/unit_session.py`：会话状态、时间/安全/配置标准化单元

## 快速使用

1. 安装并启用插件
2. 在 WebUI 配置白名单、`persona_id`、免打扰时间
3. 按需开启 `debug_log`
4. 发送 `/idle_test` 验证主动消息链路

## 精简配置（WebUI）

- `enabled`
- `config_mode`
- `lifecycle_log`
- `timezone`
- `sleep_start` / `sleep_end`
- `private_whitelist` / `group_whitelist`
- `min_idle_min` / `max_idle_min` / `cooldown_min`
- `persona_id` / `proactive_provider_id`
- `proactive_prompt_template` / `fallback_proactive_text`
- `enable_holiday_perception` / `holiday_country` / `enable_platform_perception`
- `security_global_hourly_cap` / `security_max_fail_streak` / `security_fail_pause_min`
- `security_allow_links` / `security_blocked_words` / `security_max_text_length`
- `mood_enabled` / `mood_initial` / `mood_min_trigger`
- `mood_cost_on_proactive` / `mood_cost_on_dialogue` / `mood_recover_per_min`
- `debug_log` / `debug_decision_log` / `debug_status_window_sec`

## 推荐参数（保守）

- `config_mode = basic`
- `min_idle_min = 45`
- `max_idle_min = 180`
- `cooldown_min = 90`
- `security_global_hourly_cap = 6`
- `security_max_fail_streak = 3`
- `security_fail_pause_min = 180`
- `mood_initial = 70`
- `mood_min_trigger = 35`
- `holiday_country = CN`
- `debug_status_window_sec = 300`

## 指令

- 所有插件指令默认仅 AstrBot 管理员可调用（自动使用 AstrBot 全局管理员配置）
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
