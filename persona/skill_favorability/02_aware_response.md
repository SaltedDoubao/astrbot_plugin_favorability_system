---
description: 每轮生成回复前，拉取用户好感度画像并按软权重风格调整输出。
allowed-tools:
  - fav_profile
---

# Skill 02 · 好感度感知回复

> **触发：** 每轮生成回复前  
> **前置：** 已从事件上下文获取 `sender_id` 和 `display_name`

## 步骤

1. 调用 `fav_profile`，参数必须为 `{"user_id":"<sender_id>","nickname":"<display_name>"}`（首次自动注册）
2. 解析返回 JSON，提取以下字段：
   - `effect_brief`
   - `style_weight`
   - `style_axes.warmth / initiative / boundary / playfulness`
3. 将上述字段注入当前轮风格约束，生成回复：
   - `style_weight` 低：保持克制、简洁、边界更清晰
   - `style_weight` 高：语气更温和、主动、可适度延展
   - `boundary` 高时，即使用户热情也保持分寸

## 注意

- 软约束不是固定模板，不要机械复制句式。
- 不向用户透露好感度数值、层级名或风格轴数值（Debug 模式除外）。
- 禁止添加 schema 外参数，尤其是 `_`。
- 禁止位置参数写法（如 `fav_profile(sender_id, display_name)`），仅使用命名参数对象。
