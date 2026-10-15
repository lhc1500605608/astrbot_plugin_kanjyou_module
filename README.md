# WarmWhisper · 暖语

<div align="center">
  <img src="./logo.png" alt="WarmWhisper · 暖语" width="180">
</div>

[![Version](https://img.shields.io/badge/version-v2.13.3-blue.svg)](https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module)
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

- `enabled`：仅控制闲时主动轮询；关闭后消息合并仍按 `merge_enabled` 运行。`advanced_enabled`：高级配置开关。
- `private_whitelist` / `group_whitelist`：允许主动问候的会话白名单。
- `sleep_start` / `sleep_end`：夜间免打扰（支持跨天）。
- `min_idle_min` / `max_idle_min` / `cooldown_min`：触发与冷却。
- `persona_id` / `proactive_provider_id`：人格与主模型。
- `enable_holiday_perception` / `holiday_api_enabled`：节假日感知。
- `merge_enabled`：独立控制消息合并；`enabled=false` 不会关闭合并，完整停用请在 AstrBot 插件管理中禁用插件。
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

## 会话隔离与旧数据迁移

会话状态以 `private:<完整 UMO>` / `group:<完整 UMO>` 保存，平台实例、线程及会话 ID 均不省略。消息只在完整会话、发送者和权限上下文相同时合并。

白名单绑定 **平台实例 ID + 原生用户 / 群 ID**，不使用适配器名称作为实例身份。管理命令自动绑定当前会话的平台实例；控制面板需填写实例 ID 和用户 / 群 ID。手动配置的每个列表项是一个 JSON 字符串，例如 `{"kind":"private","platform":"qq-bot-A","id":"10001"}`，群聊使用 `"kind":"group"`。

旧状态按已保存的完整 UMO 迁移；缺失 / 无效 origin、身份不一致或重复映射的行保留在 `__quarantine__` 命名空间，停止轮询和授权。旧裸 ID 白名单仅在已有持久状态能唯一确定平台实例时迁移；其余保存为 `{"legacy_id":"原 ID"}`，不授权，也不会在未来消息到达或重载时自动绑定。请通过管理命令或面板显式重新添加，再移除未绑定条目。修订前已经混入的历史内容无法自动识别或还原，可按需清理旧会话数据。

## 隐私与安全

- **记忆召回默认仅私聊**生效；群聊一律不注入记忆。如需群聊召回，须显式开启，且仅允许公共记忆进入群聊，私聊私有记忆不进入。
- 守灯提供的生活细节（天气 / 用餐 / 作息 / 小结）**仅在私聊注入**。
- 群聊只使用群活跃度与话题氛围（有界计数 + 短标签），不注入任何私聊关系 / 情绪 / 生活线 / 画像。
- 主动内容受屏蔽词、长度上限及提示词约束；提示词不能保证模型绝不复述记忆，使用时仍应考虑模型提供商的数据处理范围。

## 兼容性

- 核心插件 API 声明支持 AstrBot `>=4.23,<5`。修订前的隔离审查验证过 4.23.2 与 4.28.2；本次补丁使用单元与边界回归测试，尚未重新验证真实宿主，中间及后续 4.x 也未逐项测试。
- 上述两个 AstrBot 版本要求 Python `>=3.12`；本次回归使用 Python 3.12.14。插件运行依赖由 AstrBot 提供；Ruff 的 `py310` 是静态检查目标，不是宿主运行版本要求。
- Plugin Pages / Web API 依赖 `astrbot.api.web`。此前隔离审查已验证 WebUI 在 4.28.2 可用，需面板功能的部署请使用 4.28.2 或具备相同 API 的版本；4.23.2 缺少该 API 时插件会降级注册 0 条面板路由，不影响核心功能。旧版能力由插件运行时探测。
- 支持主流适配器（图片回复按 URL / 本地路径分流）。
- 记忆 / 陪伴插件缺失或超时时自动降级，不影响发送。

## WebUI 界面

新版面板由 Lovable 设计并接入插件，包含概览、会话详情、平台实例白名单、免打扰设置与决策日志，支持深浅色、中文 / English 与手机卡片布局。

AstrBot 中的状态、控制、日志三个入口均可访问完整面板；分别默认打开概览、设置、日志。操作通过现有宿主 SDK 完成，不新增云端数据库或登录服务。未检测到宿主时禁用修改；仅显式添加 `?demo=1` 时显示合成演示数据，演示不会发送真实消息。

免打扰时段会读取服务端已保存值并显示插件时区；未保存的输入在实时更新与刷新后仍保留，可一键恢复。保存期间防止重复提交；请求超时会提示结果未知，请刷新确认后再操作。断线时显示数据可能过期的提示。

界面源码为 `pages/common/panel.html`，共享宿主助手为 `pages/common/app.js`。修改后运行 `python scripts/sync_webui.py` 生成三个独立入口，`--check` 检查同步状态。页面自包含，不依赖 CDN、外部字体或前端构建工具。

[Lovable 设计项目](https://lovable.dev/projects/060b795c-dbd0-4247-ad25-60850b7782e8) 保留生成版；本地包含最终接入与交互修正。真实宿主全链路仍需在部署环境验证。

## 开发验证

使用 Python 3.12 及 Node.js，在仓库目录运行：

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python --group dev
TZ=Asia/Shanghai .venv/bin/python -m pytest -q -ra
.venv/bin/ruff check .
node tests/test_sse_lifecycle.js
node tests/test_webui_panel.js
node tests/test_webui_completion.js
.venv/bin/python scripts/sync_webui.py --check
```

`pyproject.toml` 提供开发依赖组，不将插件打包为独立 Python 项目。原三行 `uv.lock` 未包含依赖解析记录且无法通过 `uv lock --check`，已移除；宿主 Python 要求以 AstrBot 为准。

## 许可

GNU AGPL-3.0（见 [`LICENSE`](./LICENSE)）。版本与变更历史见 [`CHANGELOG.md`](./CHANGELOG.md)。


### 跨插件协作与主动发送修订

WarmWhisper 负责触发与投递，tmemory 负责权威 Person 绑定，Hearthlight 提供陪伴状态及幂等回执。未绑定的身份按完整 UMO 隔离；裸 ID 历史数据需确认归属后显式迁移。

手动测试和定时发送共用会话互斥、投递结果处理和 attempt ID。生成期间收到新消息、移出白名单、会话身份变化或插件停止时，取消待发送内容。定时发送还会复核 enabled、静默时段、冷却和配额；手动测试允许绕过 enabled、静默时段及调度配额，使用独立的 manual_proactive_count，不增加定时主动次数。关闭 enabled 只停止定时主动检查，回执补报、消息合并和被动回复处理仍可运行。

生成同时最多执行两项，等待及运行任务有上限；完整生成流程设有超时并在停止时取消。投递结果不明或已发送部分分段时不重发整条内容。外部陪伴回执失败会保存不含正文的待补报记录，以原 attempt ID 重试，不重新生成或投递消息。待补报队列满时停止新增发送，避免丢失回执。
