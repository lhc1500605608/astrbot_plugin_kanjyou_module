# astrbot_plugin_kanjyou_module

AstrBot 闲时主动回复插件：让 Bot 在长时间无消息往来时，主动抛出话题促成对话，并支持白名单与夜间免打扰。

## 功能

- 私聊/群聊分会话独立 idle 计时
- 有消息往来自动重置计时器
- 灵活触发：`min_idle_sec` 到 `max_idle_sec` 间概率递增触发
- 每会话冷却和每日上限，避免刷屏
- 白名单控制：仅对白名单私聊与群聊生效
- 夜间免打扰：支持跨天时间窗（如 `23:30-08:00`）

## 安装后配置

插件启动后会在插件目录生成：

- `idle_config.json`：配置文件
- `idle_state.json`：运行状态文件

你可以先通过命令管理，也可以直接改 `idle_config.json`。

## 指令

- `/idle_status` 查看当前会话状态
- `/idle_enable` 开启主动回复
- `/idle_disable` 关闭主动回复
- `/idle_debug_on` 开启调试日志
- `/idle_debug_off` 关闭调试日志
- `/idle_wl_add_private <user_id>` 添加私聊白名单
- `/idle_wl_del_private <user_id>` 移除私聊白名单
- `/idle_wl_add_group <group_id>` 添加群聊白名单
- `/idle_wl_del_group <group_id>` 移除群聊白名单
- `/idle_sleep_set <HH:MM> <HH:MM>` 设置免打扰时段
- `/idle_test` 在当前会话发送一条测试主动话题

## 核心配置项

- `enabled`: 是否启用
- `debug_log`: 是否输出 debug 决策日志
- `timezone`: 时区（默认 `Asia/Shanghai`）
- `private_whitelist`: 私聊白名单
- `group_whitelist`: 群聊白名单
- `sleep_windows`: 免打扰时段数组
- `check_interval_sec`: 巡检周期
- `min_idle_sec`: 最小 idle 触发阈值
- `max_idle_sec`: 到达后强制触发
- `cooldown_sec`: 会话触发后的冷却时间
- `max_per_session_per_day`: 单会话每日主动触发上限
- `trigger_base_prob`: 到达最小阈值时基础触发概率
- `trigger_max_prob`: 接近最大阈值时触发概率上限
- `private_topic_pool`: 私聊专用话题池（更拟人、更贴近）
- `group_topic_pool`: 群聊专用话题池（更简短、更克制）
- `topic_pool`: 主动话题池

## 建议初始参数

- `min_idle_sec`: 900（15 分钟）
- `max_idle_sec`: 3600（60 分钟）
- `cooldown_sec`: 1200（20 分钟）
- `sleep_windows`: `23:30-08:00`

## 注意事项

- 本插件依赖 AstrBot 适配器支持主动发消息能力。
- 如 `/idle_test` 失败，请先确认连接器允许 bot 主动发送。

## Debug 建议

- 先执行 `/idle_debug_on`，然后观察日志中 `[idle-proactive][debug]` 前缀。
- 关键日志会标明会话、跳过原因和触发结果，例如：
  - `session skip(cooldown)`：在冷却期内，跳过触发
  - `session skip(probability)`：本轮概率未命中
  - `session trigger(success)`：成功主动发送
  - `send proactive failed`：发送失败（适配器或权限问题）
