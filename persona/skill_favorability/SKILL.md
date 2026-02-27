---
name: skill_favorability
description: 好感度系统技能包。每轮对话自动查询/注册用户好感度，根据层级效果调整回复风格，并在回复后评估情感变化。
---

# 好感度技能

## 执行流程

```
每轮对话
  ├─ 回复前 ──▶ 02_aware_response（自动注册 + 查询层级效果）
  └─ 回复后 ──▶ 03_evaluate（评估情感变化）
```

## Skill 列表

| 文件 | 触发时机 |
|------|---------|
| [02_aware_response.md](02_aware_response.md) | 每轮生成回复前 |
| [03_evaluate.md](03_evaluate.md) | 每轮回复生成后 |
