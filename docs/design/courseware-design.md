# 课件答题记录设计

> 按当前实现整理（2026-11-28）。代码：`core/courseware_records.py`、
> `web/routes_courseware.py`、`core/skills/courseware/`（生成与模板脚本）。

## 一、定位

「课件」是给家长陪孩子学习用的**单文件 HTML**（16:9 幻灯片、翻页、讲稿、测验），
由 `courseware` 技能生成：模型只产出课件规划 JSON，版式/样式/渲染交给技能脚本。

本模块负责**答题结果回传**：课件页面在本地打开时把每题结果 POST 回 cili，
cili 按工作区落 JSONL、维护错题本、重生成成绩页。实现放在 `core`，Web 路由与技能脚本共用。

## 二、数据布局（按工作区 + 节点）

| 产物 | 路径 |
|---|---|
| 事件流 | `{workspace}/courseware/quiz-results/{node_id}.jsonl`（追加写） |
| 成绩页 | `{workspace}/成绩记录-{node_id}.html`（每次答题后重生成） |
| 错题本 | `{workspace}/错题本-{node_id}.md`（或课件同目录；保留 `学习资料/` 旧位置兼容） |

节点 id 来自 `core/node_identity.py`：同一工作区在多台机器上各自维护自己的记录，
避免共享工作区 Git 冲突。

## 三、事件模型

`record_quiz_result(workspace_root, payload, node_id?)` 校验并追加一条事件：

- 必填 `question_id`；`is_correct` 接受 bool 或 `"true"/"false"`；
- 字段（截断上限）：`courseware(120)`、`courseware_path(500)`、`scene_id`、`question(2000)`、
  `options(≤12×300)`、`selected/answer`、`explain(2000)`、`error_type`、`source`、
  `attempt_id`、`ts`、`node_id`；
- `entry_id` 由 `courseware_path#question_id` 的 sha1 前缀 + 课件名 slug 生成，用于错题本去重/更新；
- 答错时更新错题本；无论对错都重新生成成绩页；返回 `{recorded, entry_id, scores_path}`。

## 四、错题本格式

`错题本-{node}.md` 是普通 Markdown，其中的课件错题放在标记块内：

```
<!-- courseware:mistakes-start -->
<!-- cw-q:begin {"entry_id":"...","courseware":"...","question":"..."} -->
### 题目 …
**选项** / **我的答案** / **正确答案** / **解析** / **错因** / **来源**
<!-- cw-q:end -->
<!-- courseware:mistakes-end -->
```

- 只更新标记块，不碰用户手写的其他内容；
- `parse_mistake_book(path)` 解析 `cw-q` 条目；`merge_quiz_events(entries, events_path)`
  可把 JSONL 中历史答错记录合并进已有条目（技能复习时使用）；
- 错题条目字段兼容旧中文标签（`题目/选项/我的答案/正确答案/解析/错因/来源` 等）。

## 五、成绩页

`regenerate_scores()` 用事件流生成单文件 HTML：统计卡片（累计题数/正确率/错题数）、
按课件聚合的表格（时间、课件、题数、正确、错题、正确率），并注明错题本文件名。
每次答题后原子写覆盖。

## 六、HTTP 接口（`web/routes_courseware.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| OPTIONS | `/api/courseware/quiz-result` | CORS 预检 |
| POST | `/api/courseware/quiz-result` | 课件页面回传单题结果 |

- Origin 校验：无 Origin、`null`（file://）或 localhost/127.0.0.1 允许，其他跨站拒绝；
- Body 上限保护；JSON 必须为对象；
- 工作区解析：优先 `workspace_uuid`，否则按 `courseware_path` 落在哪个已登记工作区内；
- `courseware_path` 必须位于该工作区内；青少年模式 `guard_workspace` 拒绝锁定工作区；
- 响应带 CORS 头，返回 `{success, recorded, entry_id, scores_path}`。

## 七、与技能的关系

`core/skills/courseware/skill.md` 定义生成流程：读教材章节/错题 → 产出课件规划 JSON →
模板脚本渲染单文件 HTML（含测验与回传脚本）。答题页把结果 POST 到上面的端点；
cili 端不需要额外配置，工作区路径与 `workspace_uuid` 由技能注入。

## 八、测试

`test/test_courseware.py`。

## 九、已知边界

- 记录按节点分文件，不跨机器合并；共享工作区里各节点只看到自己的成绩页；
- 事件流只追加、不自动清理；历史题目内容存在 JSONL 中（有长度上限）；
- 回传端点只接受本机/同源，远程打开课件无法回传；
- 课件 HTML 由技能脚本生成，模板变更需同步回传字段。
