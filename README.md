# astrbot_plugin_favorability_system

AstrBot 角色扮演好感度记录系统插件

## 功能

- 通过 LLM 工具函数管理用户好感度，模型可在对话中自主调用
- SQLite 持久化存储，按会话窗口隔离数据
- 支持“当前昵称 + 曾用名”记录，改名后自动沉淀历史昵称
- 配置文件统一管理好感度范围与层级（当前版本固定为 `-100~100`）

## 会话隔离规则

- 群聊：`session_type=group`，`session_id=群号`
- 私聊：`session_type=private`，`session_id=发送者QQ号`
- 同一 `user_id` 在不同群是独立数据
- 昵称查询仅在“当前会话”内生效

## 昵称策略

- 用户每个会话仅有 1 个“当前昵称”
- 调用 `fav_add_nickname` 会更新当前昵称，并将旧昵称转为“曾用名”
- 曾用名仅用于展示，不参与昵称查询
- 若同会话中多个用户当前昵称相同，`fav_query` 会提示歧义并要求使用用户 ID 查询

## LLM 工具函数

| 工具名 | 说明 |
|--------|------|
| `fav_query` | 在当前会话内通过用户 ID 或当前昵称查询好感度和层级效果 |
| `fav_update` | 设置当前会话内用户好感度等级（绝对值） |
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

## 启动校验与升级说明

- 配置缺失或格式非法时，插件会拒绝启动
- `tiers` 不连续、越界或重叠时，插件会拒绝启动
- 本版本数据库 `schema_version=2`

## 数据存储

数据库文件位于 AstrBot 数据目录下的 `favorability/favorability.db`。

## 参考

- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
