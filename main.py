#!/usr/bin/env python3
"""Cili Agent - A self-hosted Python coding agent with a web-based UI.

Usage:
    python main.py                     # Start web UI server
    python main.py --port 8080         # Start web UI on custom port
"""

from __future__ import annotations

import sys
import argparse
import json
import logging
import os
import subprocess
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 兼容的按日期轮转日志处理器。

    Python 默认的 TimedRotatingFileHandler 在轮转时使用 os.rename，
    但 Windows 会在文件被打开时锁定文件，导致午夜轮转失败并抛出
    PermissionError。本类重写 rotate() 方法，改用 "复制 + 原地截断"
    策略：先把日志复制到带日期后缀的备份文件，再把原文件清空。
    原文件句柄在 rotate 调用前已由父类关闭，复制与截断都使用独立
    的临时句柄，完成后父类会重新 _open() 继续写入，整个过程无需
    重命名已打开的文件，因此不受 Windows 文件锁影响。
    """

    def doRollover(self):
        """重写 doRollover 以处理 "备份文件已存在" 的边界情况。

        父类在发现目标备份文件已存在时会直接 return，但此时原 stream 已
        被关闭，若不调用 _open() 后续日志将无处可写。本实现确保无论是否
        真正轮转，stream 都会被重新打开。
        """
        currentTime = int(time.time())
        t = self.rolloverAt - self.interval
        if self.utc:
            timeTuple = time.gmtime(t)
        else:
            timeTuple = time.localtime(t)
            dstNow = time.localtime(currentTime)[-1]
            dstThen = timeTuple[-1]
            if dstNow != dstThen:
                timeTuple = time.localtime(t + (3600 if dstNow else -3600))
        dfn = self.rotation_filename(
            self.baseFilename + "." + time.strftime(self.suffix, timeTuple)
        )

        # 关闭当前 stream
        if self.stream:
            self.stream.close()
            self.stream = None

        if os.path.exists(dfn):
            # 父类在此处直接 return 导致 stream 未重开；这里改为跳过 rotate
            # 但仍继续执行清理与重新打开逻辑
            pass
        else:
            self.rotate(self.baseFilename, dfn)

        if self.backupCount > 0:
            for s in self.getFilesToDelete():
                try:
                    os.remove(s)
                except OSError:
                    pass
        if not self.delay:
            self.stream = self._open()
        self.rolloverAt = self.computeRollover(currentTime)

    def rotate(self, source, dest):
        """用 "复制 + 截断" 替代 os.rename，绕过 Windows 文件锁。

        在调用此方法前，父类（或 doRollover）已关闭原 stream，因此 source
        当前无句柄持有。
        """
        try:
            if os.path.exists(source):
                # 复制当前日志内容到带日期的备份文件
                with open(source, "rb") as fsrc, open(dest, "wb") as fdst:
                    while True:
                        buf = fsrc.read(64 * 1024)
                        if not buf:
                            break
                        fdst.write(buf)
                # 原地清空原文件：以 "w" 模式打开即截断为 0 字节
                # 随后立即关闭，让父类的 _open() 能以相同方式重新打开
                with open(source, "w"):
                    pass
        except OSError:
            # 任何文件 I/O 异常都降级处理：放弃轮转，继续向原文件追加
            # 这样既不会丢失日志，也不会阻塞业务线程
            pass


# Project paths
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_CILI_DIR = os.path.join(_PROJECT_ROOT, "data", "cili")
_DEPS_DIR = os.path.join(_PROJECT_ROOT, "data", "deps")
_TMP_DIR = os.path.join(_PROJECT_ROOT, "data", "tmp")
_DEPS_GIT_BASH = os.path.join(_DEPS_DIR, "git", "bin", "bash.exe")

# Runtime Python directory (always use data/deps/python, embeddable mode)
_DEPS_PYTHON_DIR = os.path.join(_DEPS_DIR, "python")
_DEPS_PYTHON_SCRIPTS = os.path.join(_DEPS_PYTHON_DIR, "Scripts")


def _get_deps_python() -> str:
    """Get the deps Python executable path."""
    return os.path.join(_DEPS_PYTHON_DIR, "python.exe")

_SETTING_FILE = os.path.join(_CILI_DIR, "setting.json")

# Preset pip mirror sources: {name: url}
_PIP_MIRRORS = {
    "pypi": "",  # Official PyPI (default, no -i flag)
    "huaweicloud": "https://repo.huaweicloud.com/repository/pypi/simple/",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple/",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple/",
    "douban": "https://pypi.doubanio.com/simple/",
}

# Default model config values (shared across example, migration, and initial creation)
_DEFAULT_MODEL = {
    "name": "claude-sonnet-4-6",
    "interface_type": "anthropic",
    "base_url": "https://api.anthropic.com",
    "max_tokens": 16384,
    "max_context_tokens": 256000,
    "multimodal": True,
    "temperature": 0.2,
}



def _setup_logging() -> None:
    """配置日志系统，仅输出到终端（stdout）。"""
    # 创建根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 清除已有的处理器（避免重复添加）
    root_logger.handlers.clear()

    # 日志格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台处理器（输出到 stdout）
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    # 降低第三方库的日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _create_example_config() -> None:
    """Create an example config file for reference with explanations."""
    example_file = os.path.join(os.path.dirname(_SETTING_FILE), "setting.example.json")
    if os.path.exists(example_file):
        return

    example_config = {
        "_comment": "Cili Agent 配置示例 - 复制为 setting.json 并修改",
        "model": {
            "_comment": "主模型配置（用于多轮对话，RootAgent 和 SubAgent 均使用此模型）",
            "name": "claude-sonnet-4-6",
            "interface_type": "anthropic",  # "anthropic" 或 "openai"
            "api_key": "sk-xxx",
            "base_url": "https://api.anthropic.com",
            "max_tokens": 36000,  # 单次响应最大 token 数
            "max_context_tokens": 256000,  # 上下文窗口大小
            "multimodal": True,  # 是否支持图片输入
            "temperature": 0.2,  # 0.1(稳定) ~ 1.0(创意)
        },
        "llm_model": {
            "_comment": "LLM 工具模型配置（用于翻译、摘要等单次调用任务，可选）",
            "name": "claude-haiku-4-5",
            "interface_type": "anthropic",
            "api_key": "sk-xxx",  # 不填则使用主模型的 api_key
            "base_url": "https://api.anthropic.com",
            "max_tokens": 8192,
            "max_context_tokens": 200000,
            "multimodal": False,
            "temperature": 0.1,
        },
        "system": {
            "_comment": "系统参数配置",
            "pip_mirror": "https://repo.huaweicloud.com/repository/pypi/simple/",  # Python 包镜像源，留空使用官方源
            "browser_path": "",  # 浏览器可执行文件路径，留空自动检测（Edge→Chrome）
            "allowed_ips": [],  # IP 白名单，留空仅允许本机访问。示例: ["192.168.1.100", "10.0.0.5"]
        }
    }

    try:
        with open(example_file, "w", encoding="utf-8") as f:
            json.dump(example_config, f, indent=2, ensure_ascii=False)
        print(f"[setup] Created example config: {example_file}")
    except Exception as e:
        print(f"[setup] Warning: failed to create example config: {e}")


def _setup_directories() -> None:
    """Create basic directories."""
    print("[setup] Creating directories...")
    os.makedirs(_CILI_DIR, exist_ok=True)
    os.makedirs(os.path.join(_PROJECT_ROOT, "data", "agents"), exist_ok=True)

    # System workspace: UUID "system", cwd = data/
    system_ws_dir = os.path.join(_PROJECT_ROOT, "data", "agents", "system")
    os.makedirs(os.path.join(system_ws_dir, "sessions"), exist_ok=True)
    system_config = os.path.join(system_ws_dir, "setting.json")
    if not os.path.exists(system_config):
        import json
        from datetime import datetime
        with open(system_config, "w", encoding="utf-8") as f:
            json.dump({
                "workspace_name": "System",
                "directory": os.path.join(_PROJECT_ROOT, "data"),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "system": True,  # Mark as system workspace
            }, f, indent=2, ensure_ascii=False)

    os.makedirs(os.path.join(_PROJECT_ROOT, "workspace"), exist_ok=True)

    # 统一临时目录：创建 data/tmp 并注入环境变量
    os.makedirs(_TMP_DIR, exist_ok=True)
    os.environ["TEMP"] = _TMP_DIR
    os.environ["TMP"] = _TMP_DIR
    os.environ["TMPDIR"] = _TMP_DIR
    os.environ["CILI_TMP"] = _TMP_DIR

    _create_example_config()


def _init_settings() -> None:
    """Initialize settings file."""
    if os.path.exists(_SETTING_FILE):
        print(f"[setup] Config exists: {_SETTING_FILE}")
        return

    # Check for existing config
    # Check multiple possible locations
    claude_config_paths = [
        os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
        os.path.join(os.path.expanduser("~"), ".claude.json"),
    ]

    claude_env = {}
    config_found = None

    for path in claude_config_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # settings.json has env field, .claude.json may have it at root
                if "env" in data:
                    claude_env = data["env"]
                else:
                    claude_env = data
                config_found = path
                break
            except Exception as e:
                print(f"[setup] Warning: failed to read {path}: {e}")

    # Also check environment variables directly
    # Support both ANTHROPIC_AUTH_TOKEN and ANTHROPIC_API_KEY
    api_key = (claude_env.get("ANTHROPIC_AUTH_TOKEN") or
               claude_env.get("ANTHROPIC_API_KEY") or
               os.environ.get("ANTHROPIC_AUTH_TOKEN") or
               os.environ.get("ANTHROPIC_API_KEY") or "")
    base_url = claude_env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", "")
    model = claude_env.get("ANTHROPIC_MODEL") or os.environ.get("ANTHROPIC_MODEL", "")

    if config_found:
        print(f"[setup] Found existing config: {config_found}")
        if api_key:
            print(f"[setup] Migrating account:")
            if base_url:
                print(f"[setup]   - Base URL: {base_url}")
            if model:
                print(f"[setup]   - Model: {model}")
            print(f"[setup] Copying config to Cili Agent...")

    if api_key:
        settings = {
            "model": {
                **_DEFAULT_MODEL,
                "name": model or _DEFAULT_MODEL["name"],
                "api_key": api_key,
                "base_url": base_url or _DEFAULT_MODEL["base_url"],
            }
        }
        print("[setup] OK: Account migrated")
    else:
        print("[setup] No API key found, creating default config...")
        settings = {
            "model": {**_DEFAULT_MODEL, "api_key": "your-api-key-here"}
        }

    try:
        with open(_SETTING_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        print(f"[setup] Config saved: {_SETTING_FILE}")
        if not api_key:
            print("[setup] Please edit the config file and add your API Key.")
    except Exception as e:
        print(f"[setup] Warning: failed to create settings: {e}")


def _check_deps_python_healthy() -> bool:
    """Check if existing deps Python is functional (pip works)."""
    python_exe = _get_deps_python()
    if not os.path.exists(python_exe):
        return False
    pip_exe = os.path.join(_DEPS_PYTHON_SCRIPTS, "pip.exe")
    if not os.path.exists(pip_exe):
        return False
    try:
        result = subprocess.run(
            [pip_exe, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _ensure_deps_python() -> bool:
    """Ensure deps Python exists and is functional."""
    if os.path.exists(_DEPS_PYTHON_DIR):
        if _check_deps_python_healthy():
            print(f"[setup] Using existing deps Python: {_DEPS_PYTHON_DIR}")
            return True
        # Broken installation, recreate
        print(f"[setup] Deps Python is broken, recreating: {_DEPS_PYTHON_DIR}")
        import shutil
        shutil.rmtree(_DEPS_PYTHON_DIR, ignore_errors=True)

    return _install_deps_python()


def _install_deps_python() -> bool:
    """Download and configure embeddable Python to data/deps/python."""
    import shutil
    import zipfile

    # Embeddable Python has python.exe at root
    embed_python = os.path.join(_DEPS_PYTHON_DIR, "python.exe")

    print("[setup] Downloading embeddable Python 3.11.9...")

    # Clean up existing directory
    if os.path.exists(_DEPS_PYTHON_DIR):
        shutil.rmtree(_DEPS_PYTHON_DIR, ignore_errors=True)
    os.makedirs(_DEPS_PYTHON_DIR, exist_ok=True)

    # Download embeddable Python
    python_version = "3.11.9"
    url = f"https://mirrors.huaweicloud.com/python/{python_version}/python-{python_version}-embed-amd64.zip"
    zip_path = os.path.join(_DEPS_DIR, "python-embed.zip")

    try:
        result = subprocess.run(
            ["certutil", "-urlcache", "-split", "-f", url, zip_path],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0 or not os.path.exists(zip_path):
            print(f"[setup] Error: download failed: {result.stderr.strip()[:200]}")
            return False
    except Exception as e:
        print(f"[setup] Error: download failed: {e}")
        return False

    # Extract
    print("[setup] Extracting Python...")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(_DEPS_PYTHON_DIR)
    except Exception as e:
        print(f"[setup] Error: extraction failed: {e}")
        return False
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

    # Configure _pth file
    pth_files = [f for f in os.listdir(_DEPS_PYTHON_DIR) if f.endswith("._pth")]
    if not pth_files:
        print("[setup] Error: _pth file not found")
        return False

    pth_path = os.path.join(_DEPS_PYTHON_DIR, pth_files[0])
    print(f"[setup] Configuring _pth file: {pth_path}")

    try:
        with open(pth_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        has_lib_site = False
        for line in lines:
            line = line.strip()
            if line == '#import site':
                new_lines.append('import site\n')
            elif line == 'Lib\\site-packages':
                new_lines.append(line + '\n')
                has_lib_site = True
            else:
                new_lines.append(line + '\n')

        if not has_lib_site:
            new_lines.append('Lib\\site-packages\n')

        # Add project root for imports
        new_lines.append(_PROJECT_ROOT + '\n')

        with open(pth_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"[setup] Error: _pth configuration failed: {e}")
        return False

    # Install pip
    print("[setup] Installing pip...")
    get_pip_path = os.path.join(_DEPS_PYTHON_DIR, "get-pip.py")
    try:
        # 使用阿里云镜像下载 get-pip.py
        result = subprocess.run(
            ["certutil", "-urlcache", "-split", "-f",
             "https://mirrors.aliyun.com/pypi/get-pip.py", get_pip_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0 or not os.path.exists(get_pip_path):
            print(f"[setup] Error: pip download failed")
            return False

        # 使用华为云源安装 pip
        result = subprocess.run(
            [embed_python, get_pip_path, "-i", "https://repo.huaweicloud.com/repository/pypi/simple/", "--no-warn-script-location"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            print(f"[setup] Error: pip installation failed: {result.stderr.strip()[:200]}")
            return False
    except Exception as e:
        print(f"[setup] Error: pip installation failed: {e}")
        return False
    finally:
        if os.path.exists(get_pip_path):
            os.remove(get_pip_path)

    print("[setup] Embeddable Python configured successfully.")
    return True


def _check_installed_packages() -> dict[str, str]:
    """Check which packages are installed in deps Python and return {name: version}."""
    import subprocess
    try:
        # Use deps Python to check installed packages
        python_exe = _get_deps_python()
        result = subprocess.run(
            [python_exe, "-c", """
import importlib.metadata
for dist in importlib.metadata.distributions():
    print(f"{dist.metadata['Name']}=={dist.version}")
"""],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}
        packages = {}
        for line in result.stdout.strip().split('\n'):
            if '==' in line:
                name, version = line.split('==', 1)
                packages[name.lower()] = version
        return packages
    except Exception:
        return {}


def _install_packages(pip_mirrors: list[str] | None = None) -> tuple[bool, bool]:
    """Install only missing dependencies in the venv, with mirror failover.

    Returns:
        (success, installed_new): whether installation succeeded, and whether new packages were installed
    """
    if pip_mirrors is None:
        pip_mirrors = [""]
    # All dependencies - no version constraints for flexibility
    required = [
        # Web framework dependencies
        "httpx",
        "playwright",
        "playwright-stealth",
        "fastapi",
        "uvicorn[standard]",
        "python-multipart",
        # Agent packages
        "requests",
        "beautifulsoup4",
        "lxml",
        "numpy",
        "pandas",
        "scipy",
        "matplotlib",
        "pyyaml",
        "toml",
        "Pillow",
        "openpyxl",
        "python-docx",
        "python-pptx",
        "pdfplumber",
        "pytest",
    ]

    pip_exe = os.path.join(_DEPS_PYTHON_SCRIPTS, "pip.exe")
    installed = _check_installed_packages()

    # Find missing packages
    missing = []
    for pkg in required:
        # Handle extras like uvicorn[standard]
        base_name = pkg.split('[')[0]
        # Normalize: PEP 503 says - and _ are equivalent
        normalized = base_name.lower().replace('-', '_')

        # Check if any installed package matches (normalized comparison)
        found = False
        for installed_name in installed.keys():
            inst_normalized = installed_name.lower().replace('-', '_')
            if inst_normalized == normalized:
                found = True
                break

        if not found:
            missing.append(pkg)

    if not missing:
        print("[setup] All packages OK.")
        return True, False

    print(f"[setup] Missing {len(missing)} package(s): {', '.join(missing)}")
    print("[setup] Installing...")

    # Install each package with mirror failover
    current_mirror_idx = 0
    for i, pkg in enumerate(missing, 1):
        pkg_installed = False
        # Try each mirror in sequence
        while current_mirror_idx < len(pip_mirrors):
            mirror = pip_mirrors[current_mirror_idx]
            mirror_name = mirror or "PyPI official"
            print(f"[setup] ({i}/{len(missing)}) Installing {pkg} from {mirror_name}...")
            cmd = [pip_exe, "install", "--disable-pip-version-check", "--only-binary=:all:"]
            if mirror:
                cmd += ["-i", mirror]
            cmd.append(pkg)

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    pkg_installed = True
                    break  # Package installed successfully
                err = result.stderr.strip() or result.stdout.strip()
                print(f"[setup] Failed with {mirror_name}: {err[:200]}")
            except Exception as e:
                print(f"[setup] Failed with {mirror_name}: {e}")

            # Try next mirror
            current_mirror_idx += 1
            if current_mirror_idx < len(pip_mirrors):
                print(f"[setup] Trying next mirror...")

        if not pkg_installed:
            print(f"[setup] Error: all mirrors failed for {pkg}")
            return False, False

    # Save the working mirror to config
    working_mirror = pip_mirrors[current_mirror_idx]
    _save_pip_mirror(working_mirror)
    print("[setup] All packages installed.")
    return True, True  # 安装了新包


# Cached settings dict (read once during startup)
_settings_cache: dict | None = None


def _load_settings_cached() -> dict:
    """Load setting.json once and cache for subsequent calls."""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    try:
        if os.path.exists(_SETTING_FILE):
            with open(_SETTING_FILE, "r", encoding="utf-8") as f:
                _settings_cache = json.load(f)
                return _settings_cache
    except Exception:
        pass
    _settings_cache = {}
    return _settings_cache


def _get_pip_mirrors_ordered() -> list[str]:
    """Get ordered list of pip mirrors: configured one first, then presets for failover."""
    system_cfg = _load_settings_cached().get("system", {})
    configured = system_cfg.get("pip_mirror", None)
    # All preset mirrors (excluding empty 'pypi' which is official source)
    presets = [v for _, v in _PIP_MIRRORS.items() if v]
    if configured is not None and configured != "":
        # User configured a specific URL: use it first, others as fallback
        if configured in presets:
            # Move configured to front, keep order of others
            result = [configured] + [m for m in presets if m != configured]
        else:
            # Custom URL: use it first
            result = [configured] + presets
        return result
    elif configured == "":
        # User explicitly chose official PyPI
        return [""] + presets
    else:
        # No config: start from first preset (huaweicloud)
        return presets


def _save_pip_mirror(mirror: str) -> None:
    """Save the working pip mirror to config."""
    try:
        config = _load_settings_cached()
        if "system" not in config:
            config["system"] = {}
        config["system"]["pip_mirror"] = mirror
        with open(_SETTING_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"[setup] Saved working pip mirror: {mirror or 'PyPI official'}")
    except Exception as e:
        print(f"[setup] Warning: failed to save pip mirror: {e}")


def _init_git_bash() -> bool:
    """Check if Git Bash exists in deps directory.

    Returns True if bash found, False otherwise (triggers exit).
    """
    # Check deps directory
    if os.path.isfile(_DEPS_GIT_BASH):
        os.environ["GIT_BASH_PATH"] = _DEPS_GIT_BASH
        print(f"[setup] Git Bash from deps: {_DEPS_GIT_BASH}")
        return True

    # Not found - fatal error (should have been downloaded by start.ps1)
    print("[setup] FATAL: Git Bash not found in deps directory!")
    print("[setup] Please use start.cmd to start Cili Agent, which will auto-download Git Bash.")
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cili Agent - Python coding agent",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for web server (default: 8000)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host for web server (default: 0.0.0.0)",
    )
    return parser.parse_args()


def _auto_detect_browser() -> None:
    """检查 setting.json 中 browser_path，若为空或路径不存在则自动检测并写回。"""
    if not os.path.exists(_SETTING_FILE):
        return

    try:
        with open(_SETTING_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[setup] Warning: failed to read config for browser detection: {e}")
        return

    system = config.get("system", {})
    browser_path = system.get("browser_path", "")

    # 如果已配置且路径存在，跳过检测
    if browser_path and os.path.isfile(browser_path):
        return

    # 自动检测
    from core.config import _detect_browser_path
    detected = _detect_browser_path()
    if detected:
        system["browser_path"] = detected
        config["system"] = system
        try:
            with open(_SETTING_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            print(f"[setup] Browser auto-detected: {detected}")
        except OSError as e:
            print(f"[setup] Warning: failed to save browser config: {e}")
    else:
        print("[setup] No browser (Edge/Chrome) found on system")


def main() -> None:
    args = parse_args()

    # Ensure UTF-8 encoding on Windows
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 配置日志系统
    _setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("日志系统已初始化")

    # Setup directories and settings
    _setup_directories()
    _init_settings()

    # Migrate old session format to new format (optional, skip if missing)
    try:
        from core.migration import migrate_all_sessions
        agents_dir = os.path.join(_PROJECT_ROOT, "data", "agents")
        migrated = migrate_all_sessions(Path(agents_dir))
        if migrated > 0:
            print(f"[migration] Migrated {migrated} session(s) to new format")
    except ImportError:
        print("[migration] core/migration.py not found, skipping session migration")

    # Check Git Bash in deps
    if not _init_git_bash():
        print("[setup] Error: Git Bash not found", file=sys.stderr)
        sys.exit(1)

    # Ensure deps Python exists and is healthy
    if not _ensure_deps_python():
        print("[setup] Error: deps Python setup failed", file=sys.stderr)
        sys.exit(1)

    # Ensure all required packages are installed
    pip_mirrors = _get_pip_mirrors_ordered()
    pkg_success, pkg_installed = _install_packages(pip_mirrors)
    if not pkg_success:
        print("[setup] Error: failed to install required packages", file=sys.stderr)
        sys.exit(1)

    # If packages were newly installed, need to restart for imports to work
    if pkg_installed:
        print("[setup] New packages installed, restarting service...")
        # Re-execute the same script with same arguments
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # Auto-detect browser if not configured
    _auto_detect_browser()

    # Start cron scheduler
    from core.cron import start_scheduler
    start_scheduler()

    print(f"Starting Cili Agent web server on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop")

    # Auto-open browser after server starts
    import webbrowser
    import threading

    def _open_browser():
        """等待 2 秒后自动打开浏览器（让服务器有时间就绪）"""
        time.sleep(2)
        webbrowser.open(f"http://localhost:{args.port}")

    threading.Thread(target=_open_browser, daemon=True).start()

    import uvicorn
    try:
        uvicorn.run(
            "web.web_api:app",
            host=args.host,
            port=args.port,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass  # Ctrl+C: exit silently


if __name__ == "__main__":
    main()
