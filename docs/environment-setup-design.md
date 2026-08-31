# 系统环境初始化设计文档

## 概述

Cili Agent 自动管理 Python 运行环境，确保在 `data/deps/python/` 目录下有可用的 Python 3.10+ 环境。根据系统环境自动选择最佳方案：创建 venv 或下载 embeddable Python。

## 目录结构

```
data/
├── cili/                # 系统数据
│   ├── setting.json     # 全局配置
│   └── cron.d/          # Cron 定时任务数据
├── agents/              # 每个 agent 的完整状态
└── deps/
    ├── python/          # Python 运行环境（venv 或 embeddable）
    │   ├── python.exe   # Python 解释器
    │   ├── Scripts/     # pip 和其他工具（venv 模式）
    │   ├── python311._pth  # 路径配置（embeddable 模式）
    │   └── Lib/         # 标准库和第三方包
    ├── git/             # Git Bash（可选，自动下载）
    │   └── bin/bash.exe
    └── browser/         # Chrome profile 数据
```

## 初始化流程

### 1. 启动脚本 (`start.ps1`)

**职责**：确保有可用的 Python 3.10+ 和 Git Bash，然后启动 main.py

**流程**：
```
1. 检查系统 Python 版本（要求 3.10+）
2. 如果版本不够：
   - 检查 data/deps/python/ 是否已有合适的 Python
   - 如果没有，下载 embeddable Python 3.11.9
   - 配置 _pth 文件（添加项目根目录、启用 site-packages）
3. 检查 Git Bash（类似逻辑）
4. 使用找到的 Python 运行 main.py
```

**关键函数**：
- `Test-Python`: 检查系统 Python 版本
- `Install-Python`: 下载和配置 embeddable Python
- `Test-GitBash`: 检查 Git Bash
- `Install-GitBash`: 下载 Git for Windows

### 2. 主程序 (`main.py`)

**职责**：初始化完整运行环境，安装依赖包，启动服务

**流程**：
```
1. 检查是否在 venv 中运行（_in_venv）
2. 如果不在，调用 _setup_environment()
3. _setup_environment():
   a. 创建目录结构
   b. 初始化配置文件
   c. 查找/配置 Git Bash
   d. 创建/验证 Python 环境（_create_venv）
   e. 安装依赖包（_install_packages）
4. 在 venv 中重新启动 main.py
5. 启动 Web 服务
```

**关键函数**：
- `_in_venv()`: 检查是否在 data/deps/python 中运行
- `_create_venv()`: 创建 Python 环境
- `_download_embeddable_python()`: 下载 embeddable Python
- `_install_packages()`: 安装依赖

## 两种运行模式

### 模式 1: Venv 模式（优先）

**触发条件**：系统有 Python 3.10+ 且能成功创建 venv

**特点**：
- 使用 `python -m venv data/deps/python` 创建真正的虚拟环境
- 完整的 venv 结构（Scripts/, Lib/, pyvenv.cfg）
- pip 自动安装到 Scripts/
- 标准 Python 环境，兼容性最好

**适用场景**：
- 系统有 Python 3.10+
- 有足够权限创建 venv
- 网络可以访问 PyPI

### 模式 2: Embeddable 模式（降级）

**触发条件**：系统 Python 版本太低，或 venv 创建失败

**特点**：
- 下载 Python embeddable package（约 11MB）
- 解压到 data/deps/python/
- 配置 `python311._pth` 文件：
  - 启用 `import site`
  - 添加 `Lib\site-packages`
  - 添加项目根目录（用于 import core 等）
- 通过 get-pip.py 安装 pip
- pip 使用 `--only-binary=:all:` 避免编译

**适用场景**：
- 系统 Python 版本 < 3.10
- 没有权限创建 venv
- 需要便携部署

## Python 环境检测逻辑

```python
def _create_venv() -> bool:
    # 1. 检查 data/deps/python 是否已存在且可用
    if os.path.exists(_VENV_DIR) and os.path.exists(_VENV_PYTHON):
        # 检查是否是正常的 venv
        if _check_venv_healthy():
            return True
        # 检查是否是 embeddable Python（有 _pth 文件）
        pth_files = [f for f in os.listdir(_VENV_DIR) if f.endswith("._pth")]
        if pth_files:
            return True
        # 损坏的安装，删除重建
        shutil.rmtree(_VENV_DIR)

    # 2. 尝试使用系统 Python 创建 venv
    if sys.version_info >= (3, 10):
        result = subprocess.run(
            [sys.executable, "-m", "venv", _VENV_DIR]
        )
        if result.returncode == 0:
            return True

    # 3. 降级：下载 embeddable Python
    return _download_embeddable_python()
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
- numpy, pandas, pyyaml, toml, Pillow
- pytest

**镜像源**：
- 默认：https://mirrors.aliyun.com/pypi/simple/
- 可在 data/cili/setting.json 中配置

## 环境变量

**main.py 设置的变量**：
- `GIT_BASH_PATH`: Git Bash 可执行文件路径
- `PATH`: 自动添加 data/deps/python/Scripts 到开头

**start.ps1 设置的变量**：
- `GIT_BASH_PATH`: 如果使用下载的 Git
- 其他环境变量保持系统默认

## 常见问题

### 1. 系统 Python 版本太低

**症状**：
```
[cili] System Python version too low: 3.9 (need 3.10+)
[cili] Python not found or version too low, downloading...
```

**解决**：
- start.ps1 会自动下载 embeddable Python 3.11.9
- 无需手动干预

### 2. Venv 创建失败

**可能原因**：
- 权限不足
- 磁盘空间不足
- 杀毒软件拦截

**解决**：
- main.py 会自动降级到 embeddable Python 模式
- 检查磁盘空间和权限
- 临时关闭杀毒软件

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
