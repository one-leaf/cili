# 系统环境初始化设计文档

## 概述

Cili Agent 自动管理 Python 运行环境，强制使用 `data/deps/python/` 目录下的 embeddable Python 3.11.9。不依赖系统环境中的 Python 或 Bash，所有运行时均自动下载到 `data/deps/` 目录。

## 目录结构

```
data/
├── cili/                # 系统数据
│   ├── setting.json     # 全局配置
│   └── cron.d/          # Cron 定时任务数据
├── agents/              # 每个 agent 的完整状态
├── tmp/                 # 统一临时目录（TEMP/TMP/TMPDIR/CILI_TMP）
└── deps/
    ├── python/          # Python 运行环境（embeddable 模式）
    │   ├── python.exe   # Python 解释器
    │   ├── Scripts/     # pip 和其他工具
    │   ├── python311._pth  # 路径配置
    │   └── Lib/         # 标准库和第三方包
    ├── git/             # Git Bash（自动下载）
    │   └── bin/bash.exe
    ├── tectonic/        # Tectonic LaTeX 编译器（自动下载）
    │   └── tectonic.exe
    └── browser/         # Chrome profile 数据
```

## 初始化流程

### 1. 启动脚本 (`start.ps1`)

**职责**：确保有可用的 Python、Git Bash 和 LaTeX 编译器（均从 deps 目录或系统），然后启动 main.py

**流程**：
```
1. 检查 data/deps/python/python.exe 是否存在
2. 如果不存在，下载 embeddable Python 3.11.9
3. 检查 data/deps/git/bin/bash.exe 是否存在
4. 如果不存在，下载 PortableGit
5. 检查系统是否已有 LaTeX 编译器（tectonic、pdflatex、xelatex、lualatex）
6. 如果不存在，下载 Tectonic v0.17.0 到 data/deps/tectonic/
7. 设置 GIT_BASH_PATH 环境变量
8. 将 Tectonic 添加到 PATH（如果在 deps 目录）
9. 使用 deps Python 运行 main.py
```

**关键函数**：
- `Test-Python`: 检查 deps 目录中的 Python
- `Install-Python`: 下载和配置 embeddable Python
- `Test-GitBash`: 检查 deps 目录中的 Git Bash
- `Install-GitBash`: 下载 Git for Windows
- `Test-Tectonic`: 检查系统中的 LaTeX 编译器（PATH、deps 目录、环境变量、常见安装路径）
- `Install-Tectonic`: 下载 Tectonic（多镜像源：GitHub → ghproxy → ghfast → gh-proxy）

### 2. 主程序 (`main.py`)

**职责**：初始化运行环境，安装依赖包，启动服务

**流程**：
```
1. 创建目录结构和配置文件（_setup_directories）
   - 创建 data/cili/、data/agents/、data/tmp/ 等目录
   - 创建 System workspace（data/agents/system/）
   - 设置临时目录环境变量：TEMP、TMP、TMPDIR、CILI_TMP → data/tmp/
   - 生成示例配置文件 setting.example.json
2. 初始化配置（_init_settings）
   - 若 setting.json 不存在，尝试从 ~/.claude/settings.json 迁移 API Key
   - 否则创建默认配置
3. 迁移旧会话格式（migrate_all_sessions）
4. 检查 Git Bash 是否存在于 deps 目录
5. 确保 deps Python 存在且健康（pip 可用）
6. 安装依赖包（_install_packages）
   - 若有新包安装，自动重启服务（os.execv）确保 import 生效
7. 自动检测浏览器（_auto_detect_browser）
   - 若 browser_path 为空或路径不存在，自动检测 Edge → Chrome 并写回配置
8. 启动 Cron 调度器
9. 启动 Web 服务（uvicorn）
10. 延迟 2 秒后自动打开浏览器（webbrowser.open）
```

**关键函数**：
- `_ensure_deps_python()`: 确保 deps Python 可用
- `_install_deps_python()`: 下载 embeddable Python
- `_install_packages()`: 安装依赖

## Embeddable Python 模式

**特点**：
- 下载 Python embeddable package（约 11MB）
- 解压到 data/deps/python/
- 配置 `python311._pth` 文件：
  - 启用 `import site`
  - 添加 `Lib\site-packages`
  - 添加项目根目录（用于 import core 等）
- 通过 get-pip.py 安装 pip
- pip 使用 `--only-binary=:all:` 避免编译

**优势**：
- 不依赖系统 Python 版本
- 无需创建 venv
- 完全便携部署

## Python 环境检测逻辑

```python
def _ensure_deps_python() -> bool:
    # 1. 检查 data/deps/python 是否已存在且可用
    if os.path.exists(_DEPS_PYTHON_DIR):
        if _check_deps_python_healthy():
            return True
        # 损坏的安装，删除重建
        shutil.rmtree(_DEPS_PYTHON_DIR)

    # 2. 下载 embeddable Python
    return _install_deps_python()
```

## 依赖包安装

**安装方式**：
```bash
pip install --only-binary=:all: <package>
```

**原因**：
- 避免需要 C++ 编译工具
- 使用预编译的 wheel 包
- 提高安装成功率

**依赖列表**：
- httpx, playwright, playwright-stealth
- fastapi, uvicorn[standard], python-multipart
- requests, beautifulsoup4, lxml
- numpy, pandas, scipy, matplotlib
- pyyaml, toml, Pillow
- openpyxl, python-docx, python-pptx
- pytest

**镜像源**：
- 默认：https://repo.huaweicloud.com/repository/pypi/simple/
- 可在 data/cili/setting.json 中配置

## 环境变量

**start.ps1 设置的变量**：
- `GIT_BASH_PATH`: 始终设置为 data/deps/git/bin/bash.exe
- `PATH`: 追加 Tectonic 目录（如果从 deps 安装）

**main.py 设置的变量**：
- `TEMP`、`TMP`、`TMPDIR`、`CILI_TMP`: 全部设置为 `data/tmp/`（统一临时目录）
  - 确保所有工具（bash、python、tempfile 模块）使用同一个临时目录
  - bash 中可用 `$TEMP` 或 `$TMPDIR`
  - Python 中 `tempfile` 模块自动配置到此目录

**main.py 使用的变量**：
- `GIT_BASH_PATH`: Git Bash 可执行文件路径（由 start.ps1 设置）
- `CILI_TMP`: 临时目录路径（由 main.py 自身设置）

## LaTeX / Tectonic 支持

start.ps1 自动检测并安装 Tectonic（现代 LaTeX 编译器），供 `latex` 工具使用。

**检测优先级**：
1. 系统 PATH 中的 LaTeX 编译器（tectonic、pdflatex、xelatex、lualatex）
2. `data/deps/tectonic/tectonic.exe`
3. `TECTONIC_PATH` 环境变量
4. 常见安装路径（MiKTeX、TeX Live）

**安装**：
- 版本：Tectonic v0.17.0
- 下载位置：`data/deps/tectonic/tectonic.exe`
- 多镜像源：GitHub → ghproxy.net → ghfast.top → gh-proxy.com
- 安装失败不阻塞启动（降级为无 LaTeX 支持）

**目录结构**：
```
data/deps/
├── tectonic/
│   └── tectonic.exe    # Tectonic LaTeX 编译器
├── python/
└── git/
```

## 常见问题

### 1. deps Python 损坏

**症状**：
```
[setup] Deps Python is broken, recreating: data/deps/python
```

**解决**：
- 自动删除并重新下载
- 无需手动干预

### 2. Git Bash 未找到

**症状**：
```
[setup] FATAL: Git Bash not found in deps directory!
```

**解决**：
- 确保使用 start.cmd 启动
- start.ps1 会自动下载 Git Bash 到 data/deps/git/

### 3. 包安装失败

**可能原因**：
- 网络问题
- 镜像源不可用
- 缺少预编译 wheel

**解决**：
- 更换 pip_mirror（data/cili/setting.json）
- 检查网络连接

### 4. Embeddable Python 导入错误

**症状**：
```
ModuleNotFoundError: No module named 'core'
```

**原因**：
- _pth 文件配置错误
- 缺少项目根目录路径

**解决**：
- 删除 data/deps/python 重新运行
- 检查 _pth 文件是否包含项目根目录

## 设计原则

1. **自动化**：无需手动配置，启动即用
2. **容错性**：多种降级策略，确保能启动
3. **隔离性**：使用独立目录，不污染系统环境
4. **便携性**：整个 data 目录可移动
5. **可维护性**：清晰的日志和错误提示

## 相关文件

- `main.py`: 主程序，环境初始化逻辑
- `start.ps1`: 启动脚本，Python/Git 检测和下载
- `start.cmd`: Windows 批处理启动器
- `core/tools/shared/base.py`: 工具基类，Python 路径定义
- `CLAUDE.md`: 项目说明，Python 环境描述

## 未来改进

1. **缓存优化**：检测已下载的 embeddable Python，避免重复下载
2. **版本管理**：支持升级/降级 Python 版本
3. **依赖检查**：启动前检查关键依赖是否安装
4. **健康检查**：定期验证 Python 环境完整性
5. **多版本支持**：同时维护多个 Python 版本
