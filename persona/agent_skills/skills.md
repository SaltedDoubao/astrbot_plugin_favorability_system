# Skills 索引

## 执行流程

```
每轮对话
  ├─ 回复前 ──▶ Skill 02 · fav_ensure（自动注册 + 查询层级效果）
  └─ 回复后 ──▶ Skill 03 · fav_delta（评估情感变化）
```

## Skill 列表

| 编号 | 文件 | 触发时机 |
|------|------|---------|
| 02 | [skills/02_aware_response.md](skills/02_aware_response.md) | 每轮生成回复前 |
| 03 | [skills/03_evaluate.md](skills/03_evaluate.md) | 每轮回复生成后 |
