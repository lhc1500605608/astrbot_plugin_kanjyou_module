# 情绪价值提供者

[![Version](https://img.shields.io/badge/version-v2.1.3-blue.svg)](https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module)
[![AstrBot](https://img.shields.io/badge/AstrBot-plugin-green.svg)](https://github.com/AstrBotDevs/AstrBot)

一个面向 AstrBot 的闲时主动聊天插件。  
核心目标是：只在合适时机主动开场，减少打扰，提升对话自然度。

## 功能特性

- 私聊/群聊分会话独立计时
- 白名单控制（插件白名单仅控制“是否允许主动问候”）
- 夜间免打扰（支持跨天）
- 主动前决策层（置信度、原因码、最后安全确认）
- 轻量 LLM 辅助主模型（浅任务处理、主动问候预加工）
- 情绪值系统（按会话消耗与恢复）
- 管理员指令控制（自动继承 AstrBot 管理员权限）
- 低打扰 Debug 日志（默认不刷屏）

## 安装方式

1. 下载或克隆仓库到 AstrBot 插件目录  
2. 在 AstrBot WebUI 启用插件  
3. 填写白名单与基础时间参数后开始使用

## WebUI 核心配置

- `enabled`：插件总开关
- `advanced_enabled`：高级配置开关
- `private_whitelist` / `group_whitelist`：主动问候会话白名单
- `sleep_start` / `sleep_end`：夜间免打扰
- `min_idle_min` / `max_idle_min` / `cooldown_min`：触发与冷却
- `persona_id` / `proactive_provider_id`：人格与主模型
- `lite_llm_enabled`：轻量模型辅助开关
- `debug_log`：调试日志开关

## 管理指令

以下指令仅管理员可用：

- `/idle_status`
- `/idle_enable` / `/idle_disable`
- `/idle_wl_add_private <user_id>` / `/idle_wl_del_private <user_id>`
- `/idle_wl_add_group <group_id>` / `/idle_wl_del_group <group_id>`
- `/idle_sleep_set <HH:MM> <HH:MM>`
- `/idle_test`
- `/idle_decision_status`
- `/idle_decision_last`

## 许可证

本项目采用 **GNU Affero General Public License v3.0 (AGPL-3.0)** 许可。

- 你可以在遵守 AGPL-3.0 的前提下使用、修改和分发本项目。
- 若你基于本项目提供网络服务（SaaS/机器人服务等），需按 AGPL 要求公开对应修改源码。
- 完整许可证文本见仓库根目录 [LICENSE](LICENSE) 文件。

---

作者：Tango  
仓库：https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module
