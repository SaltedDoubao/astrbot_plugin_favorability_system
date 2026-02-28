# 适用于 AstrBot 的简单好感度系统

AstrBot 角色扮演好感度记录系统插件。通过 LLM 工具函数自动管理用户好感度，让 AI 角色根据好感度层级动态调整回复风格。

- LLM 在对话中自主调用工具函数，管理好感度增删查改
- SQLite 持久化存储，群聊/私聊会话隔离
- 支持"当前昵称 + 曾用名"记录，改名后自动沉淀历史昵称

## 安装

### 从 AstrBot 插件市场安装（推荐）

1. 打开 AstrBot 管理面板
2. 进入「插件市场」页面
3. 搜索 `astrbot_plugin_favorability_system`
4. 点击安装并重启 AstrBot

### 手动安装

将本仓库克隆到 AstrBot 的插件目录下：

```bash
cd <AstrBot数据目录>/data/plugins/
git clone https://github.com/SaltedDoubao/astrbot_plugin_favorability_system.git
```

## 使用方式

### 配合人格提示词使用（推荐）

本插件提供了 `persona/` 目录下的人格提示词和技能文件，可配合 AstrBot 的人格系统使用：

1. 将 `persona/system_prompt.md` 的内容添加到你的人格系统提示词中
2. 将 `persona/skill_favorability/` 目录复制到skill目录下，或将 `skill_favorability` 压缩成zip文件，从AstrBot WebUI上传

配置完成后，LLM 会在每轮对话中自动执行以下流程：

```
每轮对话
  ├─ 回复前 → 查询/注册用户好感度，按层级效果调整回复风格
  └─ 回复后 → 评估用户情感倾向，动态调整好感度
```

### 用户命令

| 命令 | 说明 |
|------|------|
| `fav-init` | 在当前会话中注册自己的好感度记录 |
| `好感度查询` | 查询自己在当前会话中的好感度和昵称 |

## 数据隔离与昵称

- 群聊按群号隔离，私聊按发送者 ID 隔离，同一用户在不同群是独立数据
- 每个用户每个会话仅有 1 个”当前昵称”，更新昵称时旧昵称自动转为”曾用名”
- 若同会话中多个用户当前昵称相同，查询时会提示歧义并要求使用用户 ID

## LLM 工具函数

| 工具名 | 说明 |
|--------|------|
| `fav_ensure` | 查询好感度与层级效果，用户不存在时自动注册（推荐每轮对话开始时调用） |
| `fav_query` | 在当前会话内通过用户 ID 或当前昵称查询好感度和层级效果 |
| `fav_update` | 设置当前会话内用户好感度等级（绝对值） |
| `fav_delta` | 对用户好感度施加相对变化量（正数增加，负数减少） |
| `fav_add_user` | 在当前会话注册新用户并设置当前昵称 |
| `fav_remove_user` | 删除当前会话内用户及其昵称记录 |
| `fav_add_nickname` | 更新当前会话内用户的当前昵称（旧昵称自动入曾用名） |
| `fav_remove_nickname` | 删除当前会话内用户的当前昵称 |
| `fav_get_effect` | 查询指定等级对应的层级效果 |

## 配置项

在 AstrBot 管理面板中可配置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `min_level` | int | -100 | 好感度下限（本版本要求固定为 -100） |
| `max_level` | int | 100 | 好感度上限（本版本要求固定为 100） |
| `tiers` | str (JSON) | 见下方 | 层级定义数组，必须连续覆盖 -100~100 |

`tiers` 默认值：

```json
[
  {"name": "敌对", "min": -100, "max": -61, "effect": "强烈反感，尽量回避互动"},
  {"name": "冷淡", "min": -60, "max": -21, "effect": "态度疏离，交流意愿较低"},
  {"name": "中立", "min": -20, "max": 20, "effect": "保持客观，正常沟通"},
  {"name": "友好", "min": 21, "max": 60, "effect": "态度积极，愿意配合"},
  {"name": "亲密", "min": 61, "max": 100, "effect": "高度信任，互动自然亲近"}
]
```

## 其他说明

- 配置缺失、格式非法或 `tiers` 不连续/越界/重叠时，插件会拒绝启动
- 数据库文件位于 AstrBot 数据目录下的 `favorability/favorability.db`
- 本版本数据库 `schema_version=2`

## 注意事项

- 本插件可能导致api费用少量增加
- 插件效果取决于提示词和模型自身能力

## 参考

- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
