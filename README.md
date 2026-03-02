# 适用于 AstrBot 的群聊好感度系统

## V2 迁移指引（Breaking Change）

- `fav_profile` 与 `fav_assess` 已移除，不再作为 LLM 工具暴露。
- 主链路改为插件自动执行：
  - `on_llm_request`：自动注入 1 行短风格指令
  - `on_llm_response + after_message_sent`：自动规则评分并落库
- 若你仍在 persona/skill 里写了 `fav_profile` / `fav_assess` 调用，请移除。

## 项目目标

在保持“好感度驱动风格变化”的同时，降低 token 开销并减少对话链路复杂度：

- 常规对话零工具调用（不再有额外工具回合）
- 评分在插件本地规则引擎完成（无额外 LLM token）
- 仍保留完整限幅、防刷、日上限逻辑

## 安装

### 从 AstrBot 插件市场安装（推荐）

1. 打开 AstrBot 管理面板
2. 进入「插件市场」
3. 搜索 `astrbot_plugin_favorability_system`
4. 安装并重启 AstrBot

### 手动安装

```bash
cd <AstrBot数据目录>/data/plugins/
git clone https://github.com/SaltedDoubao/astrbot_plugin_favorability_system.git
```

## 使用方式

### 推荐配置

1. 将 `persona/system_prompt.md` 内容加入人格系统提示词
2. 不再加载 `persona/skill_favorability`（该目录在 V2 为 deprecated）

### 用户命令

| 命令 | 说明 |
|------|------|
| `fav-init` | 在当前会话中注册自己的好感度记录 |
| `好感度查询` | 查询自己在当前会话中的好感度和昵称 |
| `fav-rl [页码]` | 查看当前会话好感度排行榜（每页 10 条） |

### 管理工具

| 工具名 | 说明 |
|--------|------|
| `fav_query` | 在当前会话内通过用户 ID 或当前昵称查询 |
| `fav_update` | 直接设置当前会话用户好感度（绝对值） |
| `fav_add_user` | 注册用户并设置当前昵称 |
| `fav_remove_user` | 删除用户及昵称记录 |
| `fav_add_nickname` | 更新当前昵称（旧昵称沉淀为曾用名） |
| `fav_remove_nickname` | 删除当前昵称 |
| `fav_get_effect` | 查询指定数值对应层级效果 |

## 自动评分规则（V1）

### 分类优先级

`abuse > rude > celebration > thanks > deep_talk > helpful_dialogue > small_talk > none`

### 负向策略（默认 conservative）

- 仅命中明显辱骂/攻击词时触发 `rude`/`abuse`
- 冷淡、简短、未配合默认不自动扣分

### 强度

- `1`：轻度
- `2`：中度
- `3`：重度（强攻击/强辱骂）

## 评分算法（与 V1 保持一致）

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
| `initial_level` | int | 0 | 新用户注册时初始值（-100~100） |
| `decay_enabled` | bool | false | 是否启用长期不互动衰减 |
| `idle_days_threshold` | int | 14 | 衰减触发阈值（天） |
| `decay_per_day` | int | 1 | 超阈值后每天向 0 回归点数 |
| `auto_style_injection_enabled` | bool | true | 是否启用每轮短风格注入 |
| `auto_assess_enabled` | bool | true | 是否启用每轮自动评分 |
| `auto_assess_skip_commands` | bool | true | 自动评分时是否跳过插件命令消息 |
| `negative_policy` | string | conservative | 负向策略：conservative/balanced/aggressive |
| `style_prompt_mode` | string | short_tier | 风格注入模式（当前仅 short_tier） |
| `rule_version` | string | v1 | 自动评分规则版本（当前仅 v1） |
| `tiers` | str(JSON) | 见下方 | 层级定义，必须连续覆盖 -100~100 |

## 数据结构

- 数据库文件：`<AstrBot数据目录>/data/plugin_data/astrbot_plugin_favorability_system/favorability.db`
- 当前 schema：`v3`
- 继续兼容 `v2 -> v3` 自动迁移
- 本版本不自动迁移旧路径数据库（`<AstrBot数据目录>/favorability/favorability.db`），如需保留旧数据请手动处理

## 回滚建议

若需要回滚至旧流程，请使用 `v1.x` 分支。  
若仅担心误判，可先关闭 `auto_assess_enabled`，仅保留风格注入。

## 参考

- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
