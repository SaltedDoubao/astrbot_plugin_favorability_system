# 适用于 AstrBot 的群聊好感度系统

AstrBot 角色扮演好感度插件。面向群聊高频日常场景，提供更激进但可控的增减分策略，并将好感度影响以结构化“软权重风格”注入回复。

- 回复前拉取用户画像（`fav_profile`），按 `style_weight + style_axes` 调整回复
- 回复后评估互动类型（`fav_assess`），自动应用反刷、限幅、日内上限
- SQLite 持久化，群聊/私聊隔离

## 安装

### 从 AstrBot 插件市场安装（推荐）

1. 打开 AstrBot 管理面板
2. 进入「插件市场」页面
3. 搜索 `astrbot_plugin_favorability_system`
4. 点击安装并重启 AstrBot

### 手动安装

```bash
cd <AstrBot数据目录>/data/plugins/
git clone https://github.com/SaltedDoubao/astrbot_plugin_favorability_system.git
```

## 使用方式

### 配合人格提示词使用（推荐）

1. 将 `persona/system_prompt.md` 内容添加到你的人格系统提示词中
2. 将 `persona/skill_favorability/` 目录复制到 skill 目录，或打包后从 AstrBot WebUI 上传

配置完成后，LLM 每轮自动执行：

```text
每轮对话
  ├─ 回复前 → fav_profile（查询/注册 + 返回风格画像）
  └─ 回复后 → fav_assess（互动评估 + 动态更新）
```

### 用户命令

| 命令 | 说明 |
|------|------|
| `fav-init` | 在当前会话中注册自己的好感度记录 |
| `好感度查询` | 查询自己在当前会话中的好感度和昵称 |
| `fav-rl [页码]` | 查看当前会话的好感度排行榜，每页 10 条 |

## 新核心工具（V3）

| 工具名 | 说明 |
|--------|------|
| `fav_profile(user_id, nickname)` | 获取用户画像（自动注册），返回 `level/tier/style_weight/style_axes/effect_brief` |
| `fav_assess(user_id, interaction_type, intensity, evidence)` | 对本轮互动评分并更新好感度，内置反刷、限幅、上限和日志 |

### 管理工具（保留）

| 工具名 | 说明 |
|--------|------|
| `fav_query` | 在当前会话内通过用户 ID 或当前昵称查询 |
| `fav_update` | 直接设置当前会话用户好感度（绝对值） |
| `fav_add_user` | 注册用户并设置当前昵称 |
| `fav_remove_user` | 删除用户及昵称记录 |
| `fav_add_nickname` | 更新当前昵称（旧昵称沉淀为曾用名） |
| `fav_remove_nickname` | 删除当前昵称 |
| `fav_get_effect` | 查询指定数值对应层级效果 |

## 评分算法（内置）

### interaction_type 基础分

- `small_talk:+2`
- `thanks:+4`
- `helpful_dialogue:+5`
- `deep_talk:+6`
- `celebration:+9`
- `cold:-2`
- `rude:-6`
- `abuse:-10`

### 强度与偏置

- `intensity`：`1/2/3 -> 0.8/1.0/1.25`
- 正向额外偏置：`+15%`

### 防刷与限幅

- 同用户同类型正向事件，120 秒内收益递减：
  - 第 1 次 `1.0`
  - 第 2 次 `0.75`
  - 第 3 次 `0.5`
  - 第 4 次及以上 `0.3`
- 单轮限幅：`[-12, +12]`
- 10 分钟正向累计上限：`+20`
- 自然日正向累计上限：`+50`

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `min_level` | int | -100 | 下限（固定要求 -100） |
| `max_level` | int | 100 | 上限（固定要求 100） |
| `decay_enabled` | bool | false | 是否启用长期不互动衰减 |
| `idle_days_threshold` | int | 14 | 衰减触发阈值（天） |
| `decay_per_day` | int | 1 | 超阈值后每天向 0 回归点数 |
| `tiers` | str(JSON) | 见下方 | 层级定义，必须连续覆盖 -100~100 |

`tiers` 默认值：

```json
[
  {"name":"敌对","min":-100,"max":-51,"effect":"强烈反感，回复克制并保持距离"},
  {"name":"冷淡","min":-50,"max":-11,"effect":"态度保守，偏简短回应"},
  {"name":"中立","min":-10,"max":9,"effect":"正常沟通，理性且不过分亲近"},
  {"name":"友好","min":10,"max":39,"effect":"积极配合，语气更温和并适度主动"},
  {"name":"亲密","min":40,"max":100,"effect":"信任度高，互动自然亲近并更愿意延展话题"}
]
```

## 数据结构与迁移

- 数据库文件：`<AstrBot数据目录>/favorability/favorability.db`
- 当前 schema：`v3`
- 支持 `v2 -> v3` 自动迁移：
  - `users` 新增：`last_interaction_at/daily_pos_gain/daily_neg_gain/daily_bucket`
  - 新增 `score_events` 评分事件表
  - 启动时自动迁移并保留原有用户与昵称数据

## 注意事项

- 插件效果受模型能力与提示词执行稳定性影响。
- 该插件会增加少量 token 与数据库写入开销。

## 参考

- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
