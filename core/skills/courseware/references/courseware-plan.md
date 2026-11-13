# 课件规划 JSON 契约（schema_version 1.0）

模型只输出这个 JSON；渲染器负责版式、转义、资源内联。

## 顶层

```json
{
  "schema_version": "1.0",
  "template": "parchment-v1",
  "mode": "chapter",
  "meta": { "title": "分数的意义", "subject": "数学", "grade": "五年级下册", "textbook": "人教版 · 第四单元", "duration_min": 30 },
  "objectives": ["理解分数的意义", "认识分子和分母"],
  "scenes": []
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 固定 `"1.0"` |
| `template` | 是 | 固定 `"parchment-v1"` |
| `mode` | 是 | `chapter` 或 `error_type` |
| `focus` | 错题模式必填 | 见下文 |
| `meta` | 是 | `title` / `subject` / `grade` 必填；`textbook`、`duration_min` 可选 |
| `objectives` | 否 | 学习目标列表 |
| `scenes` | 是 | 页面列表，不能为空 |

固定值（不要写入）：受众 = 家长；语言 = 中文；风格 = 羊皮卷暖黄 × 卡通手绘；模板 = `parchment-v1`。

## 页面（scene）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 页内唯一，如 `s1` |
| `layout` | 是 | 9 种布局之一 |
| `title` | 是（cover 可省） | ≤ 20 字 |
| `subtitle` | 否 | 一句话导入 |
| `blocks` | 否 | 内容块 |
| `diagram` | `diagram` 布局必填 | `{kind, params, caption}` |
| `questions` | `quiz` 布局必填 | 测验题 |
| `notes` | 否 | 家长版讲稿，≤ 300 字 |
| `source` | 否 | 教材出处 |
| `error_type` | 否 | 错题模式下该页对应的错因 |

## 布局

`cover` 封面 · `objectives` 学习目标 · `section` 章节分隔 · `concept` 概念讲解 ·
`steps` 编号步骤 · `two-column` 左文右图 · `diagram` 大图讲解 ·
`quiz` 课堂练习 · `summary` 小结。

## 内容块（blocks）

| type | 字段 |
|---|---|
| `text` | `text`, `style`: `lead` / `body` / `note` |
| `bullets` | `items[]`, `ordered` |
| `formula` | `latex`, `display` |
| `callout` | `tone`: `tip` / `warn` / `key`, `text` |
| `table` | `headers[]`, `rows[][]`, `caption` |
| `image` | `path`（工作区内）, `alt`, `caption` |
| `diagram` | `kind`, `params`, `caption` |
| `illustration` | `kind`, `params`, `caption` |
| `mistake` | `question`, `my_answer`, `correct_answer`, `analysis`, `error_type`, `source` |
| `code` | `language`, `code` |

图示 `kind`：`number_line`、`fraction_bar`、`bar_chart`、`geometry`、`flow`。
插图 `kind`：`life_scene`（cake/rope/water/money/book）、`apparatus`（beaker/flask/circuit/scale）、`object`（cube/cylinder/cone/sphere/triangle）、`icon_row`。

## 测验题（questions）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 页内唯一，如 `q1` |
| `type` | 是 | `single` / `multiple` / `fill` |
| `stem` | 是 | 题干 |
| `options` | single/multiple | 选项数组 |
| `answer` | 是 | `"A"` / `["A","C"]` / 填空答案数组 |
| `explain` | 是 | 解析 |
| `hint` | 否 | 提示 |

## 错题模式 `focus`

```json
"focus": {
  "source": "错题本.md",
  "date_range": "2026-09-01~2026-11-13",
  "total": 10,
  "inferred_count": 3,
  "error_types": [
    {"type": "概念不清", "count": 5, "question_ids": ["fenshu-yiyi#q1"]},
    {"type": "计算失误", "count": 3, "question_ids": ["xiaoshu#q2"]}
  ]
}
```

错题模式页面序列：封面 → 错题分布（`diagram` + `bar_chart`）→ 每类（`concept` → `concept` + `mistake` → `steps` → `quiz`）→ `summary`。
错例必须来自 `错题本.md`，原文引用；变式题标注「变式」；总页数 ≤ 15。
