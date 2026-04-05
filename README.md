# astrbot_plugin_kanjyou_module

AstrBot 闲时主动回复插件：让 Bot 在长时间无消息往来时，主动抛出话题促成对话，并支持白名单与夜间免打扰。

## 功能

- 私聊/群聊分会话独立 idle 计时
- 有消息往来自动重置计时器
- 灵活触发：`min_idle_sec` 到 `max_idle_sec` 间概率递增触发
- 每会话冷却和每日上限，避免刷屏
- 白名单控制：仅对白名单私聊与群聊生效
- 夜间免打扰：支持跨天时间窗（如 `23:30-08:00`）
- 主动问候由 LLM 根据人格 + Prompt 动态生成（不再使用固定话题池）

## 安装后配置

插件参数通过 AstrBot WebUI 配置（基于 `_conf_schema.json`）。
插件目录仅会生成 `idle_state.json` 作为运行状态文件。

## 指令

- `/idle_status` 查看当前会话状态
- `/idle_enable` 开启主动回复
- `/idle_disable` 关闭主动回复
- `/idle_wl_add_private <user_id>` 添加私聊白名单
- `/idle_wl_del_private <user_id>` 移除私聊白名单
- `/idle_wl_add_group <group_id>` 添加群聊白名单
- `/idle_wl_del_group <group_id>` 移除群聊白名单
- `/idle_sleep_set <HH:MM> <HH:MM>` 设置免打扰时段
- `/idle_test` 在当前会话发送一条测试主动话题

## 核心配置项

- `enabled`: 是否启用
- `debug_log`: 是否输出 debug 决策日志
- `debug_status_window_sec`: 调试状态摘要窗口（秒），建议 300 或 600
- `timezone`: 时区（默认 `Asia/Shanghai`）
- `sleep_start`: 免打扰开始时间（HH:MM）
- `sleep_end`: 免打扰结束时间（HH:MM）
- `private_whitelist`: 私聊白名单
- `group_whitelist`: 群聊白名单
- `check_interval_sec`: 巡检周期
- `min_idle_sec`: 最小 idle 触发阈值
- `max_idle_sec`: 到达后强制触发
- `cooldown_sec`: 会话触发后的冷却时间
- `max_per_session_per_day`: 单会话每日主动触发上限
- `trigger_base_prob`: 到达最小阈值时基础触发概率
- `trigger_max_prob`: 接近最大阈值时触发概率上限
- `persona_id`: 主动问候使用的人格（WebUI 下拉选择）
- `proactive_provider_id`: 主动问候使用的模型提供商（可留空跟随会话）
- `proactive_prompt_template`: 主动问候生成 Prompt 模板（支持变量）
- `fallback_proactive_text`: 模型生成失败时的兜底文案

## 建议初始参数

- `min_idle_sec`: 900（15 分钟）
- `max_idle_sec`: 3600（60 分钟）
- `cooldown_sec`: 1200（20 分钟）
- `sleep_start/sleep_end`: `23:30` / `08:00`

## 注意事项

- 本插件依赖 AstrBot 适配器支持主动发消息能力。
- 如 `/idle_test` 失败，请先确认连接器允许 bot 主动发送。

## Debug 建议

- 在 WebUI 中将 `debug_log` 打开，然后观察日志中 `[idle-proactive][debug]` 前缀。
- 建议将 `debug_status_window_sec` 设为 `300`（5 分钟）或 `600`（10 分钟），减少刷屏。
- 关键日志会标明会话、跳过原因和触发结果，例如：
  - `session skip(cooldown)`：在冷却期内，跳过触发
  - `session skip(probability)`：本轮概率未命中
  - `session trigger(success)`：成功主动发送
  - `send proactive failed`：发送失败（适配器或权限问题）
  - `status ... next_trigger_earliest=...`：会话状态指示器（包含 idle、冷却剩余、下次检查时间、最早触发时间）

## Prompt 变量

- `{persona}`: 选中人格的设定文本（若无法解析则回填人格 ID）
- `{session_type}`: 会话类型（私聊/群聊）
- `{idle_minutes}`: 空闲分钟数
- `{idle_seconds}`: 空闲秒数
