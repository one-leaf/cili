---
name: 课件生成
description: 为家长生成单文件 HTML 课件（羊皮卷暖黄 × 卡通手绘），支持按教材章节/内容做课件，也支持按错题类型做复习课件。Use when the user mentions 做课件、课件、教学演示、讲解页、复习课件、按错题做课件、错题复习。
roles: [master, worker]
---

# 课件生成

Base directory for this skill: `$SKILL_DIR`

给家长陪孩子学习用的单文件 HTML 课件：16:9 幻灯片、翻页、讲稿、测验、成绩与错题记录。**模型只写课件规划 JSON，版式、样式、渲染由脚本负责。**

## 固定契约

- 模板：`parchment-v1`（唯一；羊皮卷暖黄 × 卡通手绘），不可改。
- 语言：中文；受众：家长。
- 布局只能从：`cover`、`objectives`、`section`、`concept`、`steps`、`two-column`、`diagram`、`quiz`、`summary` 中选。
- **不写 HTML/CSS/JS/SVG**；不提供颜色、坐标、字体。
- 页面正文 ≤ 180 字，讲稿 ≤ 300 字；每页至少一个视觉元素。
- 不使用外部图片/链接；图片只能引用工作区内文件。
- 不把学生姓名、学校等个人信息写进课件。

## 开始前

1. 读当前工作区 `AGENTS.md`：确认类别（学习项目/办公/科研/空白）、学习档案与输出目录。
2. 确认入口：
   - **章节/内容** → 走「章节课件流程」。
   - **错题类型** → 走「错题复习流程」。
3. 受众固定为家长，不询问；缺章节信息时才用 `ask_user`。

## 章节课件流程

1. 用教材工具取内容与出处：
   `textbook_find` → `textbook_outline` → `textbook_read`。
2. 写 `<输出目录>/<章节简称>/课件.plan.json`，严格按 `references/courseware-plan.md`。
   建议页面序列：封面 → 学习目标 → 概念 → 图示/插图 → 例题 → 步骤 → 课堂练习 → 小结。
3. 渲染：
   ```text
   python(action="execute_file",
          script_file="$SKILL_DIR/scripts/render_html.py",
          args="<plan> <out.html> --workspace-root <workspace> --workspace-uuid <uuid>")
   ```
4. 质检：
   ```text
   python(action="execute_file",
          script_file="$SKILL_DIR/scripts/qa_courseware.py",
          args="--plan <plan> --html <out.html> --out .cili/tmp/courseware/<slug>")
   ```
   读 `report.json`；有 blocker 就修 plan 重新渲染，最多 3 轮；
   用 `read_image` 抽查 `.cili/tmp/courseware/<slug>/page-*.png`。
5. 交付：`send_user_file(paths=["<out.html>"], note="…")`；回复路径与预览方式。

## 错题复习流程（P1）

1. 解析错题本：
   ```text
   python(action="execute_file",
          script_file="$SKILL_DIR/scripts/parse_mistakes.py",
          args="<错题本.md> <workspace>/.cili/courseware/quiz-results.jsonl --out .cili/tmp/courseware/<slug>/mistakes.json")
   ```
2. 按 `references/courseware-plan.md` 的错题模式生成 plan：`mode: "error_type"`，`focus` 记录类型与题目 ID。
3. 页面序列：封面 → 错题分布 → 每类（讲解 → 典型错例 → 正确方法 → 变式练习）→ 小结。
4. 渲染、质检、交付同上。错例必须原文引用，变式题必须标注「变式」。

## 输出目录（按工作区类别）

| 类别 | 课件目录 | 成绩页 |
|---|---|---|
| 学习项目 | `学习资料/课件/<章节简称>/`；错题复习 `学习资料/课件/错题复习-YYYYMMDD/` | `学习资料/成绩记录.html` |
| 日常办公 | `演示/YYYY-MM-DD-主题/` | `演示/成绩记录.html` |
| 科研项目 | `输出/YYYY-MM-DD-主题/` | `输出/成绩记录.html` |
| 空白/默认 | `课件/YYYY-MM-DD-主题/` | `课件/成绩记录.html` |

- 学习项目目录名不带年级/学科（工作区已按科目拆分；年级只记录在 `AGENTS.md`）。
- 练习/答案写 `练习/`；临时文件写 `.cili/tmp/courseware/<slug>/`。
- 不把课件写进 `教材/`、`错题本/`、`文献/`。

## 常用参考

- 内容契约与示例：`references/courseware-plan.md`
- 布局与页面规格：`references/layout-rules.md`
- 学科适配与错因分类：`references/subject-adaptation.md`
- 质检清单：`references/qa-checklist.md`

## 记录说明

课件里的测验提交后会调用 `/api/courseware/quiz-result`：
全部问答写入 `.cili/courseware/quiz-results.jsonl`，错题每题一条写入 `错题本.md`，并刷新 `成绩记录.html`。
不要在前端或讲稿里重复解释这个机制；交付时一句话说明即可。

## 失败处理

- 校验失败：按 `render_html.py` 返回的错误路径修 plan。
- 质检 blocker：修内容或拆页；最多 3 轮，仍失败如实报告。
- 教材缺失：询问用户，或按通用课标生成并在 `source_note` 标注「非教材内容」。
- 图片缺失：移除或换成 `illustration`/`diagram`；不要留外链。
