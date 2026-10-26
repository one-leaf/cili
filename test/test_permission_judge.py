# -*- coding: utf-8 -*-
"""PermissionJudge 输入上下文测试。"""

import json

from core.security.permission_judge import PermissionJudge
from core.tools.permission import PermissionStore


def _payload(judge, permission):
    messages = judge._build_input(permission)
    text = messages[0].content
    return json.loads(text.split("待判定动作：\n", 1)[1])


class TestJudgeInput:
    def test_recent_approvals_and_routing(self):
        store = PermissionStore()
        store.approve("d1", "ls", kind="command")
        store.approve("d2", "http://127.0.0.1:8000", kind="browser:navigate")
        store.approve("d3", "C:\\out", kind="path:write")
        judge = PermissionJudge(
            None, None, store,
            role="master", has_user_channel=True,
            workspace_root="E:\\ws", cwd="E:\\ws",
        )
        payload = _payload(judge, {
            "kind": "path:read",
            "command": "C:\\secret",
            "tool": "read",
            "decision_id": "d4",
            "mode": "ai",
            "reason": "outside read",
        })
        assert payload["routing"] == {"mode": "ai", "reason": "outside read"}
        assert "command: ls" in payload["recent_approvals"]
        assert "navigate: http://127.0.0.1:8000" in payload["recent_approvals"]
        assert "path:write: C:\\out" in payload["recent_approvals"]
        assert payload["path_metadata"]["path"] == "C:\\secret"
        assert "content" not in payload["path_metadata"]
