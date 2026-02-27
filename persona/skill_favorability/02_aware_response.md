---
description: 每轮生成回复前，查询用户好感度并按层级效果调整回复风格。
allowed-tools:
  - fav_ensure
---

# Skill 02 · 好感度感知回复

> **触发：** 每轮生成回复前
> **前置：** 已从事件上下文获取 `sender_id` 和 `display_name`

## 步骤

1. 调用 `fav_ensure`，参数必须为 `{"user_id":"<sender_id>","nickname":"<display_name>"}`（首次自动注册）
2. 从返回结果中提取 `effect` 描述（格式：`【层级名】效果描述`）
3. 将 `effect` 作为人格约束注入当前上下文，生成符合层级的回复

## 注意

- `effect` 是行为直接指令，优先级高于默认风格。
- 不向用户透露好感度数值或层级名称（Debug 模式除外）。
- 禁止添加 schema 外参数，尤其是 `_`。
- 禁止位置参数写法（如 `fav_ensure(sender_id, display_name)`），仅使用命名参数对象。
