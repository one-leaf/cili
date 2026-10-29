"""智慧教育平台登录捕获：浏览器交互 → 缓存凭据。

不提供任何配置项。需要平台凭据时，直接打开浏览器让用户用手机号注册/登录，
再从页面 ``localStorage`` 抓取 ``access_token`` / ``mac_key`` / ``diff``，
写入 ``data/cili/cache/smartedu_auth.json`` 长期生效；凭据失效时清除，下次
下载前再次引导登录。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from core.browser_service import get_service
from core.textbooks.smartedu import (
    SmartEduCredentials,
    load_credentials,
    save_credentials,
)

logger = logging.getLogger(__name__)

LOGIN_PAGE = "https://basic.smartedu.cn/tchMaterial"

# 精确查找 ND_UC_AUTH-*&token 键，解析双层 JSON：
# 外层 {"value": "{...json string...}", "expire": ...}
# 内层包含 access_token, mac_key, diff
CAPTURE_JS = r"""(() => {
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && key.startsWith("ND_UC_AUTH") && key.endsWith("&token")) {
      try {
        const outer = JSON.parse(localStorage.getItem(key));
        if (outer && outer.value) {
          const inner = JSON.parse(outer.value);
          if (inner.access_token && inner.mac_key) {
            return {
              access_token: String(inner.access_token),
              mac_key: String(inner.mac_key),
              diff: Number(inner.diff) || 0
            };
          }
        }
      } catch (e) {
        // 继续查找
      }
    }
  }
  return null;
})()"""

_state_lock = threading.Lock()
_state: dict[str, object] = {
    "running": False,
    "ok": False,
    "error": "",
    "message": "未开始",
    "started_at": "",
    "finished_at": "",
}


def login_status() -> dict:
    with _state_lock:
        return dict(_state)


def credentials_ready() -> bool:
    return load_credentials().complete


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _capture_from_output(output: str) -> dict | None:
    """从 execute_script 的文本输出中解析抓取结果。"""
    marker = "JavaScript result:\n"
    if marker not in output:
        return None
    payload = output.split(marker, 1)[1].split("\n... (truncated)")[0]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _login_worker(timeout_s: int, poll_s: float) -> None:
    global _state
    try:
        service = get_service()
        result = service.navigate(LOGIN_PAGE)
        if result.error:
            raise RuntimeError(result.output)
        tab = (result.meta or {}).get("tab_index")
        with _state_lock:
            _state.update(message="请在浏览器中完成手机号注册/登录")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            res = service.execute_script(CAPTURE_JS, tab_index=tab)
            if not res.error:
                found = _capture_from_output(res.output)
                if found:
                    save_credentials(SmartEduCredentials(
                        access_token=str(found["access_token"]),
                        mac_key=str(found["mac_key"]),
                        diff=int(found.get("diff") or 0),
                    ))
                    with _state_lock:
                        _state.update(running=False, ok=True, error="",
                                      message="登录成功，凭据已缓存",
                                      finished_at=_now())
                    return
            time.sleep(poll_s)
        raise TimeoutError(f"等待登录超时（{timeout_s}s）")
    except Exception as e:  # noqa: BLE001 —— 登录流程失败只记录状态
        logger.warning(f"[smartedu-login] 登录失败: {e}")
        with _state_lock:
            _state.update(running=False, ok=False, error=str(e),
                          message=f"登录失败: {e}", finished_at=_now())


def start_login(timeout_s: int = 600, poll_s: float = 2.0) -> dict:
    """打开浏览器进入平台并等待用户登录，抓取凭据后写入缓存。"""
    with _state_lock:
        if _state["running"]:
            return dict(_state)
        _state.update(running=True, ok=False, error="",
                      message="正在打开浏览器...", started_at=_now(),
                      finished_at="")
    threading.Thread(
        target=_login_worker, args=(timeout_s, poll_s),
        daemon=True, name="smartedu-login",
    ).start()
    return login_status()


def ensure_credentials() -> dict:
    """下载前调用：已有凭据直接返回；缺失时打开浏览器引导登录。"""
    if credentials_ready():
        return {"ok": True, "message": "已有登录凭据", "running": False}
    return start_login()
