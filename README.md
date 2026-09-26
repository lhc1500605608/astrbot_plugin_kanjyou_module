# WarmWhisper · 暖语

<div align="center">
  <img src="./logo.png" alt="WarmWhisper · 暖语" width="180">
</div>

[![Version](https://img.shields.io/badge/version-v2.13.0-blue.svg)](https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.23%2C%3C5-green.svg)](https://github.com/AstrBotDevs/AstrBot)

[**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 的闲时主动聊天插件：只在合适时机主动开场，减少打扰，让对话更像真人。

## 它能做什么

- **闲时主动问候**：私聊 / 群聊独立计时，按空闲与冷却条件触发，支持白名单。
- **时间与节日感知**：按本地时段调整语气，内置中国节假日感知（可选在线接口）。
- **更像真人**：主动与被动回复都可按语义拆成多条、按打字节奏逐条发送。
- **情绪调制**：根据会话情绪切换语气、字数与主动程度，状态内容可在 WebUI 配置。
- **长期记忆**：生成主动消息前从记忆插件召回相关内容并自然融入（默认仅私聊）。
- **陪伴上下文**：可选对接 `Hearthlight · 守灯`，使用其关系 / 生活 / 动机上下文与生活细节。
- **群聊参与**：对接守灯时，按群活跃度与节奏自然接话，不过度打扰。
- **未完话题**：记住对方提过、尚未继续的事，在合适时机自然提起一次。

## 安装

1. 在 AstrBot 插件市场安装，或克隆本仓库到 AstrBot 的插件目录。
2. 在 AstrBot WebUI 启用插件。
3. 填写白名单与基础时间参数后即可使用。

## 常用配置

- `enabled`：插件总开关；`advanced_enabled`：高级配置开关。
- `private_whitelist` / `group_whitelist`：允许主动问候的会话白名单。
- `sleep_start` / `sleep_end`：夜间免打扰（支持跨天）。
- `min_idle_min` / `max_idle_min` / `cooldown_min`：触发与冷却。
- `persona_id` / `proactive_provider_id`：人格与主模型。
- `enable_holiday_perception` / `holiday_api_enabled`：节假日感知。
- 记忆与陪伴对接、情绪状态集等高级项在 WebUI 分组内按需开启（默认低打扰、安全）。

## 使用

- **控制面板**：在 AstrBot WebUI 的 Plugin Pages 查看开关、会话状态、冷却 / 配额与决策日志。
- **管理指令**（需管理员）：

| 指令 | 说明 |
|------|------|
| `/idle_status` | 查看当前会话状态 |
| `/idle_enable` / `/idle_disable` | 开关闲时主动 |
| `/idle_wl_add_private` / `/idle_wl_del_private` | 私聊白名单 |
| `/idle_wl_add_group` / `/idle_wl_del_group` | 群聊白名单 |
| `/idle_sleep_set <HH:MM> <HH:MM>` | 设置免打扰时段 |
| `/idle_test` | 立即发送一条测试主动消息 |

## 隐私与安全

- **记忆召回默认仅私聊**生效；群聊一律不注入记忆。如需群聊召回，须显式开启，且仅允许公共记忆进入群聊，私聊私有记忆不进入。
- 守灯提供的生活细节（天气 / 用餐 / 作息 / 小结）**仅在私聊注入**。
- 群聊只使用群活跃度与话题氛围（有界计数 + 短标签），不注入任何私聊关系 / 情绪 / 生活线 / 画像。
- 主动内容受安全项约束（屏蔽词、长度上限），不会原样复述记忆原文。

## 兼容性

- AstrBot `>=4.23,<5`；支持主流适配器（图片回复按 URL / 本地路径分流）。
- 记忆 / 陪伴插件缺失或超时时自动降级，不影响发送。

## 许可

GNU AGPL-3.0（见 [`LICENSE`](./LICENSE)）。版本与变更历史见 [`CHANGELOG.md`](./CHANGELOG.md)。
