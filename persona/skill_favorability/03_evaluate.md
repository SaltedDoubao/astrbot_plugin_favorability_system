---
description: 每轮回复生成后，评估用户互动类型并更新好感度。
allowed-tools:
  - fav_assess
  - fav_add_nickname
---

# Skill 03 · 好感度动态评估

> **触发：** 每轮回复生成后  
> **前置：** `fav_profile` 已在回复前执行，用户已存在  
> **工具：** `fav_add_nickname`，参数 `{"user_id":"<sender_id>","nickname":"<new_nickname>"}`（偶发）

## 互动类型映射（必须从中选一并给出强度）

| interaction_type | 含义 | 基础倾向 |
|---|---|---|
| `small_talk` | 普通寒暄、轻量闲聊 | 轻微正向 |
| `thanks` | 感谢、赞美、积极反馈 | 明显正向 |
| `helpful_dialogue` | 有建设性的交流与配合 | 明显正向 |
| `deep_talk` | 深度沟通、持续投入讨论 | 较强正向 |
| `celebration` | 生日、里程碑、重要喜讯互动 | 强正向 |
| `cold` | 明显敷衍、长期低投入 | 轻微负向 |
| `rude` | 无礼、挑衅、攻击性表达 | 明显负向 |
| `abuse` | 辱骂、恶意羞辱、持续恶意 | 强负向 |

`intensity` 取值规则：
- `1`：轻度
- `2`：中度（默认）
- `3`：重度

## 步骤

1. 评估本轮用户输入，选择最匹配的 `interaction_type` 与 `intensity`。
2. 调用 `fav_assess`：  
   `{"user_id":"<sender_id>","interaction_type":"<enum>","intensity":<1|2|3>,"evidence":"<不超过20字的理由>"}`  
3. 若用户在本轮提及新称呼，可额外调用 `fav_add_nickname` 更新当前昵称。

## 注意

- 不直接手算 delta，统一交给 `fav_assess` 的算法管线处理。
- `evidence` 保持简短客观，避免情绪化描述。
- 禁止添加 schema 外参数，尤其是 `_`。
- 禁止位置参数写法（如 `fav_assess(sender_id, ...)`），仅使用命名参数对象。
