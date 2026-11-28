# Office → PDF 设计

> 按当前实现整理（2026-11-28）。代码：`core/office_pdf/`（cache / convert / sync）、
> `core/tools/office2pdf.py`、`web/routes_files.py`（预览）、`core/cron_seeds/office-pdf-sync.json`。

## 一、定位

用本机已安装的 **Microsoft Office 或 WPS** 的 COM 接口把 Word/Excel/PowerPoint 转成 PDF，
供「文件预览」与 `office2pdf` 工具使用。转换在**短命子进程**里执行，避免 COM 状态污染主进程；
PDF 落盘缓存，源文件变更才重转。

支持扩展名：`doc/docx/rtf/odt`、`xls/xlsx/csv/ods`、`ppt/pptx/odp`（以 `SUPPORTED_EXTENSIONS` 为准）。

## 二、COM worker（`core/office_pdf/convert.py`）

- 引擎候选按顺序探测：Microsoft Office Word/Excel/PowerPoint → WPS Writer/Spreadsheets/Presentation
  （ProgID 列表：`Word.Application`、`KWPS.Application`、`Excel.Application`、`KET.Application`、
  `PowerPoint.Application` 等）；
- `diagnose`/`action="diagnose"` 输出注册表探测结果（ProgID、CLSID、可执行文件），
  用于排查 `CO_E_CLASSSTRING` / “class not registered”；
- worker 由 `cache._run_worker` 以子进程 `python -m core.office_pdf.convert --worker ...` 调用；
- 把 Office 进程 PID 写入 `.officepid` 文件；超时/失败时按 PID 清理残留 Office 进程；
- 导出 PDF 后原子 `os.replace` 到临时目标，主进程再复制/改名。

## 三、缓存（`core/office_pdf/cache.py`）

- 全局缓存：`data/cili/cache/office2pdf/`；工作区预览缓存：`{workspace}/.cili/cache/pdf/`；
- 缓存键：`md5(持久化源路径 + mtime_ns + size)`；源路径用 `core/paths` 的存储编码，
  整个 cili 目录搬家后仍可命中；
- 每个 PDF 配一个 `.json` meta：`source/mtime_ns/size/engine/created_at`；
- `ensure_pdf(source, cache_dir?, timeout=120)` → `(pdf_path, engine, cached)`：
  命中直接返回；未命中拉起 worker 转换，写 meta，清理同源旧版本（换 key），
  再按上限裁剪缓存（`_CACHE_MAX_BYTES=500MB`，全局）；
- `copy_pdf` 原子复制到目标路径；
- 同一缓存键加进程内锁，避免并发生成同一 PDF。

## 四、工作区增量同步（`core/office_pdf/sync.py`）

`sync_workspace_pdf_cache(workspace_uuid, limit=100, timeout=120)`：

- 遍历工作区 Office 文档（跳过 `.cili/.git/node_modules/__pycache__/.svn/.idea/.vscode`）；
- 未命中缓存的文档转换（每轮最多 `limit` 个，剩余留到下一轮）；
- 清理过期缓存：源已删除或 meta 损坏的条目删除；
- 写入只落在 `{workspace}/.cili/cache/pdf/`，不触碰用户文件；
- 由全局种子任务 `office-pdf-sync` 周期执行（cron），也可手动 `office2pdf(action="sync")`。

## 五、工具与预览

`office2pdf` 工具：

| action | 行为 |
|---|---|
| `convert`（默认） | 单个文档转 PDF；输出路径缺省为源同目录 `.pdf`；支持 `timeout` |
| `sync` | 批量同步一个或全部工作区的预览缓存，清理过期项 |
| `diagnose` | 报告已注册的 Office/WPS COM ProgID，用于排查转换失败 |

Web 预览：`GET /api/office-preview/...`（`routes_files.py`）调用
`ensure_pdf(..., cache_dir=workspace_cache_dir(uuid))`，返回 PDF 的 URL；
超时 504、找不到 404、权限 403、类型/参数 400、其他 500。

## 六、测试

`test/test_office2pdf.py`、`test/test_office_preview.py`。

## 七、已知边界

- 仅 Windows，且必须安装 Office 或 WPS；两者都没有时只能 `diagnose` 报错；
- COM 转换受本机 Office/WPS 版本与注册表视图影响，失败时优先 `diagnose`；
- 全局缓存 500MB 上限、工作区缓存随工作区 `.cili/cache` 走（可丢弃）；
- 加密/损坏文档、超大文件可能超时；超时会清理残留 Office 进程。
