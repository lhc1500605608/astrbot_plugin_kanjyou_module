# WarmWhisper · 暖语

<div align="center">
  <img src="./logo.png" alt="WarmWhisper · 暖语" width="180">
</div>

[![Version](https://img.shields.io/badge/version-v2.12.0-blue.svg)](https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.23%2C%3C5-green.svg)](https://github.com/AstrBotDevs/AstrBot)

一个面向 AstrBot 的闲时主动聊天插件。  
核心目标是：只在合适时机主动开场，减少打扰，提升对话自然度。

## 功能特性

- 私聊/群聊分会话独立计时
- 白名单控制（插件白名单仅控制“是否允许主动问候”）
- 夜间免打扰（支持跨天）
- 闲时自动发起问候（按 idle/cooldown 条件触发）
- 时间感知（按本地时段调整问候语气）
- 节假日感知（内置中国节假日信息，可选在线接口）
- 情绪值系统（按会话消耗与恢复）
- 管理员指令控制（自动继承 AstrBot 管理员权限）
- 低打扰 Debug 日志（默认不刷屏）

### 进阶能力

- **被动回复分段**：普通回复可自动拆成多条、按打字节奏逐条发送，更像真人聊天。
- **主动消息分段**：主动问候按语义拆成 1-3 条发送，分片间插入随机打字延迟。
- **情绪状态调制**：根据会话情绪切换语气、目标字数与主动程度，状态内容可在 WebUI 配置，换人格无需改源码。
- **长期记忆**：生成主动消息前从 tmemory 召回相关记忆并自然融入。
- **陪伴上下文**：可选对接 companion-core，使用其生活/关系/动机上下文、生活细节（天气/用餐/作息/当日小结）与其提供的记忆片段（与自有记忆去重后一起注入）。
- **群聊参与**：对接 companion-core 时，群内主动发言前会先看该群的活跃度与「最短间隔 / 每小时上限 / 群繁忙」节奏，按群氛围自然接话、不过度打扰；群聊只参考群活跃度与话题氛围，不注入任何私聊关系/情绪/生活线/画像。

- **情绪事件**：识别感谢、误解、突然亲近等互动，动态调整表达方式。
- **未完话题**：记住对方提过、还没继续的事，在合适的主动消息里自然提起一次。
- **兼容性**：图片回复按 URL/本地路径分流；支持 AstrBot 4.23 及以上版本。

## 安装方式

1. 下载或克隆仓库到 AstrBot 插件目录  
2. 在 AstrBot WebUI 启用插件  
3. 填写白名单与基础时间参数后开始使用

## 群聊隐私边界

- 记忆召回**默认仅私聊生效**：`memory_recall_private_only=true` 且 `memory_recall_group_enabled=false`，群聊一律不注入记忆。
- 如需群聊召回，必须同时关闭 `memory_recall_private_only` 并开启 `memory_recall_group_enabled`；此时仅允许 tmemory 返回的非私有（公共）记忆，私聊私有记忆不会进入群聊。
- companion-core 提供的记忆同样受上述开关约束，群聊默认不注入任何私聊记忆。
- companion-core 的生活细节（天气/用餐/作息/当日小结）仅在私聊注入，群聊一律不注入。
- 群聊只使用 companion-core 的群活跃度与话题氛围（有界计数 + 短标签），不注入任何私聊关系/情绪/生活线/画像；群内成员为局部匿名键，不与私聊身份合并、不跨群聚合。
- 默认预设中的「撒娇」态仅在私聊且久未互动时触发，群聊自动抑制。
- 主动消息生成受 `security_blocked_words`、`security_max_text_length` 等安全项约束，不会原样复述记忆原文。

## WebUI 核心配置

配置按 12 个分组折叠展示（基础/触发/时间/配额/生成/情绪/记忆/陪伴/情绪事件/节假日/安全/调试），以下为常用项：

- `enabled`：插件总开关
- `advanced_enabled`：高级配置开关
- `private_whitelist` / `group_whitelist`：主动问候会话白名单
- `sleep_start` / `sleep_end`：夜间免打扰
- `min_idle_min` / `max_idle_min` / `cooldown_min`：触发与冷却
- `persona_id` / `proactive_provider_id`：人格与主模型
- `enable_holiday_perception` / `holiday_api_enabled`：节假日感知开关
- `debug_log`：调试日志开关

## Pages 自定义页面

插件附带 3 个 AstrBot Pages 页面，可在 WebUI 侧边栏直接访问：

| 页面 | 路径 | 功能 |
|------|------|------|
| Control | `pages/control/` | 快速开关插件、查看当前会话白名单 |
| Status | `pages/status/` | 会话状态看板：情绪值、冷却、配额、最近主动记录 |
| Logs | `pages/logs/` | 决策轨迹日志：触发判定、概率计算、发送结果 |

> 截图待补充：首次使用可在 AstrBot WebUI → Pages 查看实际效果。

### 主动消息分段

- `proactive_segment_enabled`：是否按真人打字节奏分段发送（默认开启）
- `proactive_segment_max_parts`：最大分片数（默认 3）
- `proactive_segment_delay_min_ms` / `proactive_segment_delay_max_ms`：分片间随机延迟区间（默认 300-900ms）

### 情绪状态调制（高级配置）

插件只提供「mood → 语义状态 → 语气/长度/主动度调制」的机制，状态内容（名称/风格提示/说明/字数区间/映射规则）全部来自预设或配置。

- `persona_state_enabled`：是否启用状态调制（默认开启）；关闭后退回通用 mood 行为且不注入状态区块
- `persona_state_preset`：内置预设名，`chika`（默认）或 `generic`（中性通用）
- `persona_state_custom`：**表单化**自定义状态集，填写后**整体替换**预设；`states` 列表留空则使用上方所选预设
- `persona_state_overrides`：**表单化**部分覆盖，仅覆盖填写的状态/字段，其余沿用所选预设（同名状态走深合并）
- `persona_state_low_threshold`：低情绪阈值（默认 35）
- `persona_state_high_threshold`：高情绪阈值（默认 75）
- `persona_state_clingy_idle_sec`：久未互动类状态所需空闲秒数（默认 14400＝4 小时）
- `persona_state_low_persist_rounds`：低情绪状态需连续经过的决策轮数（默认 2）

#### WebUI 表单用法

在 AstrBot WebUI「高级·情绪」分组中展开 `persona_state_custom`（或 `persona_state_overrides`）：

- **默认状态名**：所有规则都不匹配时使用；留空自动取 `states` 列表第一个状态。
- **情绪状态列表**：点「添加」新增条目，每个条目只需填写：

| 表单字段 | 说明 | 示例 |
|----------|------|------|
| 状态名 | 唯一名称 | `低落` |
| 优先级 | 数值越大越优先匹配 | `80` |
| 触发条件（JSON） | 可选；dict=AND，数组=OR，留空=无条件兜底 | `{"mood_below":"low","low_persist":true}` |
| 语气提示 | 注入 prompt 的语气描述 | `简短克制、不追问` |
| 提示词补充 | 追加到人格 prompt 的一句话 | `精力较低，表达简短。` |
| 长度范围 | 形如 `20-60`（字） | `6-20` |
| 暂停主动 | 该状态跳过闲时主动问候 | `true` |

> `states` 列表留空时视为「未提供」，回退到上方 `persona_state_preset` 所选预设；不会用空值覆盖预设。旧的整段手写 JSON（`states` 为字典）仍然兼容。

#### 预设结构示例

```jsonc
{
  "default": "平静",              // 兜底状态名
  "states": {
    "专注": {
      "priority": 100,            // 数值越大越先匹配
      "when": [                   // 匹配条件：dict=AND，list of dict=OR；缺省=无条件命中
        { "holiday_qa": true },
        { "important_topic": true }
      ],
      "style_hint": "专注平和、条理清晰，少寒暄",
      "prompt_note": "话题较重要，表达有条理，减少玩笑。",
      "length_range": "40-80",
      "suppress_proactive": false // 可选：为 true 时该状态持续则不主动
    },
    "疲惫": {
      "priority": 80,
      "when": { "mood_below": "low", "low_persist": true },
      "style_hint": "简短克制、降低主动度，不追问",
      "prompt_note": "精力较低，表达简短，降低主动度，不追问。",
      "length_range": "6-20",
      "suppress_proactive": true
    },
    "平静": {
      "priority": 0,
      "style_hint": "平实中性、自然简洁",
      "prompt_note": "保持平实中性的表达，自然简洁。",
      "length_range": "20-60"
    }
  }
}
```

`when` 支持的通用条件键：`holiday_qa`、`important_topic`、`mood_below` / `mood_gte`（`low` | `mid` | `high` 或数值）、`low_persist`、`no_reply_streak_gte`、`private_only`、`idle_gte_clingy`、`default`。未知状态或缺失字段会安全回退到 `default` 状态。

#### 非默认预设示例：三状态自定义预设

无需改源码：在 WebUI 表单中按上表逐条添加状态即可。下面对应的等价旧版 JSON 写法仍然兼容（可直接粘贴进配置文件的历史 `persona_state_custom` 字段）：

```json
{
  "default": "平静",
  "states": {
    "专注": {
      "priority": 100,
      "when": [{ "important_topic": true }],
      "style_hint": "专注、条理清晰",
      "prompt_note": "话题重要，表达有条理。",
      "length_range": "40-80"
    },
    "疲惫": {
      "priority": 80,
      "when": { "mood_below": "low", "low_persist": true },
      "style_hint": "简短、少追问",
      "prompt_note": "精力偏低，表达简短。",
      "length_range": "6-20",
      "suppress_proactive": true
    },
    "平静": {
      "priority": 0,
      "style_hint": "平实中性",
      "prompt_note": "保持平实中性。",
      "length_range": "20-60"
    }
  }
}
```

### 长期记忆召回（高级配置）

- `memory_recall_enabled`：是否召回并注入记忆（默认开启）
- `memory_recall_limit`：每次召回条数上限（默认 3）
- `memory_recall_timeout_sec`：召回超时秒数（默认 2，超时按无记忆继续）
- `memory_recall_private_only`：仅私聊召回（默认开启）
- `memory_recall_group_enabled`：是否允许群聊召回（默认关闭，需同时关闭 private_only）
- `memory_recall_plugin_name`：tmemory 插件注册名（默认 `astrbot_plugin_tmemory`）

### 陪伴上下文（高级配置）

- `companion_enabled`：是否启用 companion-core 上下文消费（默认关闭）
- `companion_plugin_name`：companion-core 插件注册名（默认 `astrbot_plugin_tcompanion_core`）
- `companion_timeout_sec`：拉取/回执超时秒数（默认 1.5，超时按不注入继续发送）
- `companion_inject_life_state` / `companion_inject_life_detail` / `companion_inject_relationship` / `companion_inject_motivation`：分域注入开关（默认开启；生活细节仅在私聊注入，且与记忆去重）
- `companion_inject_life_content`：是否注入她读到的见闻话题（默认开启；仅私聊作为话题候选，与记忆/生活细节去重）
- 上游插件缺失、超时或返回异常时自动降级，不影响正常发送。

## 打包规范

发布/分发时使用**单一顶层目录**（目录名与插件同名 `astrbot_plugin_kanjyou_module`），并排除以下内容：

- `__MACOSX/`
- `.DS_Store`（含子目录）
- `.git/`
- `__pycache__/`（含子目录）
- `idle_state.json`（运行时状态）
- `.venv/`、`.pytest_cache/`、`.ruff_cache/`、`tests/`

规范目录结构：

```text
astrbot_plugin_kanjyou_module/
├── __init__.py
├── main.py
├── config.py
├── _conf_schema.json
├── metadata.yaml
├── README.md
├── LICENSE
└── units/
    ├── __init__.py
    ├── unit_advanced.py
    ├── unit_commands.py
    ├── unit_companion.py
    ├── unit_events.py
    ├── unit_generation.py
    ├── unit_memory_recall.py
    ├── persona_presets.py
    ├── unit_runtime.py
    └── unit_session.py
```

## 管理指令

以下指令仅管理员可用：

- `/idle_status`
- `/idle_enable` / `/idle_disable`
- `/idle_wl_add_private <user_id>` / `/idle_wl_del_private <user_id>`
- `/idle_wl_add_group <group_id>` / `/idle_wl_del_group <group_id>`
- `/idle_sleep_set <HH:MM> <HH:MM>`
- `/idle_test`

## 许可证

本项目采用 **GNU Affero General Public License v3.0 (AGPL-3.0)** 许可。

- 你可以在遵守 AGPL-3.0 的前提下使用、修改和分发本项目。
- 若你基于本项目提供网络服务（SaaS/机器人服务等），需按 AGPL 要求公开对应修改源码。
- 完整许可证文本见仓库根目录 [LICENSE](LICENSE) 文件。

---

作者：Tango  
仓库：https://github.com/lhc1500605608/astrbot_plugin_kanjyou_module
