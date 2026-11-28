# 教材数据与工具设计

> 按当前实现整理（2026-11-28）。代码：`core/textbooks/`、`core/tools/textbook_*.py`、
> `web/routes_textbooks.py`、`web/static/textbook-settings.js`、`core/templates/textbooks/`。

## 一、两类数据

1. **精炼章节包**（模型阅读用）：来自独立数据仓库 `one-leaf/cili-data`，运行时分发到
   `data/cili/textbooks/`：

   ```
   chapters.zip      # <cid>/index.md、<cid>/chapters/NNN.md
   catalog.json      # 书目索引：学段/学科/版本/年级/册次/章数/字数
   catalog.db        # SQLite FTS 检索库（books + chapters + trigram）
   pack_meta.json    # 数据集版本与各包 SHA256
   originals.zip     # 可选：清洗版原文（仅本地，分发包不含）
   ```

   内置兜底：`core/templates/textbooks/dataset_index.json` 随 cili 发布，
   首次启动尚未同步时仍可返回书目与分类。

2. **官方源文件**（下载给用户）：国家中小学智慧教育平台（SmartEdu）的 PDF/图片教材，
   由 `core/textbooks/smartedu.py` 抓目录、`downloader.py` 下载到工作区 `教材/`。

## 二、数据包同步（`core/textbooks/pack_sync.py`）

- 配置：`setting.json → textbooks`，默认 `auto_sync=true`、`repo=one-leaf/cili-data`、
  `channel=release|raw`、`mirror=auto|direct`、`check_interval_hours=24`；
- 流程：取远端 `manifest.json` → 比对 `pack_meta.json` 的 `dataset_version` →
  下载 `chapters.zip + catalog.json`（镜像链 `gh-proxy.com / ghfast.top / ghproxy.net`
  失败转移，直连兜底，Range 断点续传）→ SHA256 校验 → 临时目录解压 →
  原子替换 `data/cili/textbooks/` → 构建 `catalog.db`（FTS trigram，约 85 秒）；
- 后台线程静默执行，失败不影响其他功能，只写【教材数据】日志。

## 三、数据包访问（`core/textbooks/pack_store.py`）

- `PackStore.available` → 检查 `chapters.zip`；缺失时抛 `PackUnavailable`；
- `resolve_book(book_ref)`：8 位 id 或标题关键词；歧义返回候选列表；
- `outline(cid)`：读 `<cid>/index.md`，列出章节与每章字数；
- `read_chapter(cid, chapter, max_chars)`：序号或标题关键词；正文去 frontmatter，
  超长截断并提示全章字数；
- `chapter_headings(cid, chapter, compact)`：章内小节标题（过滤“思考/练习”等活动标题）；
- `original(cid, offset, limit)`：读取 `originals.zip` 的清洗原文，按页偏移；
- `search(query, stage/subject/grade/edition/volume)`：优先 `catalog.db` FTS trigram
  （≥3 字），2 字查询回退 LIKE；返回书名、章节、命中片段。

## 四、模型工具

| 工具 | 作用 |
|---|---|
| `textbook_find` | 按学段/学科/年级/版本/册次或标题关键词找书，返回 8 位 id 与章数/字数；无参返回语料概览 |
| `textbook_outline` | 目录与每章字数；`chapter=N/关键词` 返回小节；`chapter="all"` 一次返回全部小节 |
| `textbook_read` | 读单章精炼正文，长章截断（可调 `max_chars`，上限 60000） |
| `textbook_search` | 全文检索（约 8800 章），支持元数据过滤，返回定位参数 |
| `textbook_original` | 读清洗原文（需本地 `originals.zip`）；无原文时明确降级提示 |

工具共用 `TextbookToolBase`（store 实例、书名解析、候选提示）。

## 五、官方教材下载（`downloader.py` + `smartedu*.py`）

- 目录：SmartEdu 官方静态 JSON（标签树 + 版本清单分片），内存 + 磁盘缓存
  `data/cili/cache/smartedu_catalog.json`，TTL 24 小时；过期先用旧缓存并后台刷新；
- 源文件在私有 CDN：多数教材只要求带 `Authorization` 头（匿名即可）；个别严格资源需要
  登录凭据（`access_token + mac_key`），按 URL 现算 `X-ND-AUTH` MAC；
- 需要凭据时打开浏览器让用户登录，从 `localStorage` 抓取并缓存到
  `data/cili/cache/smartedu_auth.json`，失效自动清除；
- 下载任务：FastAPI 路由创建，事件循环后台执行；目标 `{workspace}/教材/`（重名加版本前缀），
  临时文件 `.cili/tmp/textbooks/<content_id>/`；支持断点续传、重试、取消、进度查询；
- 下载前/向导创建时会把对应 Markdown 复制到工作区 `教材/<可读教材名>.md`，保证总有可读内容。

## 六、HTTP 接口（`web/routes_textbooks.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/textbooks/options` | 学段/学科/版本/年级等筛选选项 |
| POST | `/api/textbooks/download` | 创建下载任务（按 content_id 列表） |
| GET | `/api/textbooks/download/{task_id}` | 任务进度/结果 |
| POST | `/api/textbooks/download/{task_id}/retry` | 重试 |
| POST | `/api/textbooks/download/{task_id}/cancel` | 取消 |

设置页「教材」页签：数据包状态/同步、官方教材下载。

## 七、测试

`test/test_textbooks.py`、`test/test_textbook_pack.py`。

## 八、已知边界

- `originals.zip` 不在分发包内，`textbook_original` 在多数安装上只能降级提示；
- 数据包同步依赖外网（GitHub Release/镜像），失败时使用旧包或内置索引兜底；
- 首次构建 FTS 库约 85 秒，期间搜索可能不可用；
- 官方下载依赖 SmartEdu 接口与私有 CDN，接口变更需要更新 `smartedu.py`。
