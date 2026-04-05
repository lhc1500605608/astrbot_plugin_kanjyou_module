# 情绪价值提供者 (astrbot_plugin_kanjyou_module)

[![Version](https://img.shields.io/badge/version-v1.4.0-blue.svg)](https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module)
[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-green.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](LICENSE)

一个面向 AstrBot 的「闲时主动对话」插件：
在长时间无消息往来时，按人格和 Prompt 生成自然问候，帮助对话重新开始。

快速开始 • 功能总览 • 配置建议 • Debug 指南

---

## ⚠️ 使用前建议

- 建议关闭其他同类“主动对话/主动回复”功能，避免重复触发。
- 先配置白名单（私聊/群聊），再开启主动触发，避免误打扰。
- 夜间免打扰建议必开（默认 `23:30-08:00`）。

---

## 功能总览

- 分会话独立计时：私聊/群聊分别计算 idle
- 空闲触发：到达最小阈值后，概率递增触发主动问候
- 人格驱动问候：通过 `persona_id + proactive_prompt_template` 动态生成
- 模型可选：支持指定 `proactive_provider_id`，也可跟随当前会话
- 防啰嗦机制：
  - 分钟级阈值（更直观）
  - 冷却期限制
  - 每会话每日上限
  - 可选“未收到用户回复前不连续主动”
- 夜间免打扰：支持跨天时间窗
- 调试可观测：控制台输出触发决策链路与状态指示器

---

## 快速开始

1. 安装插件并重启 AstrBot。
2. 在 WebUI 插件配置中至少设置：
- `private_whitelist` / `group_whitelist`
- `persona_id`（推荐）
- `sleep_start` / `sleep_end`
3. 如需调试，打开 `debug_log=true`。
4. 在目标会话发送 `/idle_test` 验证主动发送链路。

---

## 指令

- `/idle_status` 查看当前会话状态
- `/idle_enable` 开启主动回复
- `/idle_disable` 关闭主动回复
- `/idle_wl_add_private <user_id>` 添加私聊白名单
- `/idle_wl_del_private <user_id>` 移除私聊白名单
- `/idle_wl_add_group <group_id>` 添加群聊白名单
- `/idle_wl_del_group <group_id>` 移除群聊白名单
- `/idle_sleep_set <HH:MM> <HH:MM>` 设置免打扰时段
- `/idle_test` 在当前会话发送一条测试主动问候

---

## 关键配置（WebUI）

- `enabled`: 是否启用
- `timezone`: 时区（默认 `Asia/Shanghai`）
- `sleep_start` / `sleep_end`: 免打扰时间
- `private_whitelist` / `group_whitelist`: 白名单
- `min_idle_min`: 最小 idle 阈值（分钟）
- `max_idle_min`: 最大 idle 阈值（分钟）
- `cooldown_min`: 触发后冷却（分钟）
- `max_per_session_per_day`: 单会话每日最大主动次数
- `trigger_base_prob` / `trigger_max_prob`: 触发概率区间
- `require_human_reply_before_next_proactive`: 是否等待用户回复后再下一次主动
- `persona_id`: 主动问候使用人格（`select_persona`）
- `proactive_provider_id`: 主动问候使用模型提供商（`select_provider`）
- `proactive_prompt_template`: 主动问候 Prompt 模板（支持变量）
- `fallback_proactive_text`: 模型失败时兜底文案
- `debug_log`: 控制台 debug 日志开关
- `debug_status_window_sec`: 状态摘要输出窗口（建议 `300` 或 `600`）

### Prompt 变量

- `{persona}`: 人格设定文本（解析失败时回填人格 ID）
- `{session_type}`: 会话类型（私聊/群聊）
- `{idle_minutes}`: 空闲分钟数
- `{idle_seconds}`: 空闲秒数

---

## 推荐配置（保守，不啰嗦）

- `min_idle_min = 45`
- `max_idle_min = 180`
- `cooldown_min = 90`
- `trigger_base_prob = 0.02`
- `trigger_max_prob = 0.18`
- `require_human_reply_before_next_proactive = true`
- `debug_status_window_sec = 300`

如果你希望更安静，可进一步提高 `min_idle_min/cooldown_min`，并降低 `trigger_max_prob`。

---

## Debug 指南（仅控制台）

打开 `debug_log=true` 后，可在控制台查看：

- `session skip(...)`: 本轮跳过原因
- `session trigger(success/failed)`: 主动触发结果
- `status reason=...`: 会话状态指示器，含：
  - `idle=...s`
  - `cooldown_left=...s`
  - `next_check=...`
  - `next_trigger_earliest=...`

说明：插件不会在对话消息中输出 debug，仅控制台输出。

---

## 版本与兼容

- 当前版本：`v1.4.0`
- 配置已支持旧秒级参数自动迁移到分钟级（若存在旧字段）

---

## 许可证

AGPL-3.0

## 作者

Tango

## 仓库

https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module
