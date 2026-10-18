"""QQ 入站侧测试：状态（两级键）/ owner / 指令 / SSRF / sink transcript。

全部不依赖网络与 QQ 凭据。配置面（账号列表 / 兼容读 / 固定默认值）的测试在
test_qq_settings_api.py。
"""

from __future__ import annotations

import asyncio

import pytest

# ─── 状态：账号 → 聊天 两级（设计 §3.3） ─────────────────────────────


class TestQQAccountState:
    def test_roundtrip(self):
        from interfaces.qq.state import QQAccountState, QQChatState

        acct = QQAccountState(
            owner="o1", chats={"o1": QQChatState(workspace_uuid="w1", session_id="s1")}
        )
        assert QQAccountState.from_dict(acct.to_dict()) == acct

    def test_from_dict_tolerates_junk(self):
        from interfaces.qq.state import QQAccountState

        assert QQAccountState.from_dict(None) == QQAccountState()
        acct = QQAccountState.from_dict(
            {"owner": 123, "chats": {"c": {"workspace_uuid": 1, "bogus": 2}}}
        )
        assert acct.owner == "123"
        assert acct.chats["c"].workspace_uuid == "1"

    def test_chat_get_or_create(self):
        from interfaces.qq.state import QQAccountState, QQChatState

        acct = QQAccountState()
        chat = acct.chat("c1")
        assert isinstance(chat, QQChatState)
        assert acct.chats["c1"] is chat
        chat.workspace_uuid = "w1"
        assert acct.chat("c1") is chat          # 已存在的不重建

    def test_is_empty(self):
        from interfaces.qq.state import QQAccountState, QQChatState

        assert QQAccountState().is_empty()
        assert not QQAccountState(owner="o").is_empty()
        assert not QQAccountState(chats={"c": QQChatState()}).is_empty()


class TestQQStatePersistence:
    def test_save_and_load_per_account(self, tmp_path, monkeypatch):
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.state import (
            QQAccountState,
            QQChatState,
            load_account_state,
            save_account_state,
        )

        acct = QQAccountState(
            owner="o1", chats={"o1": QQChatState(workspace_uuid="w1", session_id="s1")}
        )
        assert save_account_state("acc1", acct)
        assert load_account_state("acc1") == acct
        # 其它账号互不可见
        assert load_account_state("acc2").is_empty()

    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.state import load_account_state

        assert load_account_state("acc1").is_empty()

    def test_multi_account_save_preserves_others(self, tmp_path, monkeypatch):
        """两个账号先后保存，互不覆盖（_STATE_LOCK 的读改写，设计 §10.1）。"""
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.state import (
            QQAccountState,
            QQChatState,
            load_account_state,
            save_account_state,
        )

        save_account_state(
            "acc1", QQAccountState(owner="o1", chats={"o1": QQChatState(workspace_uuid="w1")})
        )
        save_account_state(
            "acc2", QQAccountState(owner="o2", chats={"o2": QQChatState(workspace_uuid="w2")})
        )
        assert load_account_state("acc1").chats["o1"].workspace_uuid == "w1"
        assert load_account_state("acc2").chats["o2"].workspace_uuid == "w2"

    def test_legacy_flat_file_compat(self, tmp_path, monkeypatch):
        """旧扁平文件（单账号）在迁移前按兼容读解析：owner 即聊天键（设计 §3.3）。"""
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from core.fs_utils import atomic_write_json
        from interfaces.qq.state import load_account_state, state_path

        atomic_write_json(
            state_path(), {"owner_openid": "o1", "workspace_uuid": "w1", "session_id": "s1"}
        )
        loaded = load_account_state("whatever")
        assert loaded.owner == "o1"
        assert loaded.chats["o1"].workspace_uuid == "w1"
        assert loaded.chats["o1"].session_id == "s1"

    def test_legacy_file_converts_on_first_save(self, tmp_path, monkeypatch):
        """兼容读拿到的切片保存时写回新形态，旧键消失（写函数只写新形态）。"""
        import json

        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from core.fs_utils import atomic_write_json
        from interfaces.qq.state import load_account_state, save_account_state, state_path

        atomic_write_json(
            state_path(), {"owner_openid": "o1", "workspace_uuid": "w1", "session_id": "s1"}
        )
        acct = load_account_state("acc1")
        acct.chat("o1").session_id = "s2"
        assert save_account_state("acc1", acct)
        data = json.loads(state_path().read_text(encoding="utf-8"))
        assert "accounts" in data and "owner_openid" not in data
        assert data["accounts"]["acc1"]["chats"]["o1"]["session_id"] == "s2"


# ─── owner 记录（退化为「本账号最近对话的对方」，不落盘） ─────────────


class TestOwnerTracker:
    def _tracker(self, account):
        from interfaces.qq.access import OwnerTracker

        return OwnerTracker(account)

    def test_first_message_binds(self):
        from interfaces.qq.state import QQAccountState

        account = QQAccountState()
        assert self._tracker(account).track("first") is True
        assert account.owner == "first"

    def test_updates_on_every_conversation(self):
        """每次对话把 owner 刷新为当前发送者（openid 会因换设备/换号而变）。"""
        from interfaces.qq.state import QQAccountState

        account = QQAccountState()
        tr = self._tracker(account)
        tr.track("first")
        assert tr.track("second") is True
        assert account.owner == "second"

    def test_empty_sender_rejected(self):
        from interfaces.qq.state import QQAccountState

        assert self._tracker(QQAccountState()).track("") is False

    def test_owner_property_exposes_current(self):
        from interfaces.qq.state import QQAccountState

        account = QQAccountState()
        tr = self._tracker(account)
        tr.track("who")
        assert tr.owner_openid == "who"

    def test_track_does_not_touch_disk(self, tmp_path, monkeypatch):
        """owner 不再独立落盘——绑定由命令（/w /s /new）或默认解析决定（设计 §3.3）。"""
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.state import QQAccountState

        account = QQAccountState()
        self._tracker(account).track("first")
        assert not (tmp_path / "tools" / "qq" / "state.json").exists()


# ─── SSRF / 下载 URL 校验 ─────────────────────────────────────────────


class TestDownloadUrlCheck:
    def test_https_public_literal_ip_ok(self):
        from interfaces.qq.attachment import check_download_url
        assert check_download_url("https://8.8.8.8/a.png") is None

    def test_rejects_http(self):
        from interfaces.qq.attachment import check_download_url
        assert check_download_url("http://example.com/a.png") is not None

    def test_rejects_private_and_loopback_literal_ips(self):
        from interfaces.qq.attachment import check_download_url
        for url in (
            "https://127.0.0.1/a",
            "https://10.0.0.1/a",
            "https://192.168.1.1/a",
            "https://169.254.169.254/a",       # 云元数据
            "https://[::1]/a",
        ):
            assert check_download_url(url) is not None, url

    def test_rejects_metadata_hostname(self):
        from interfaces.qq.attachment import check_download_url
        assert check_download_url("https://metadata.tencentyun.com/a") is not None

    def test_rejects_empty_and_missing_host(self):
        from interfaces.qq.attachment import check_download_url
        assert check_download_url("") is not None
        assert check_download_url("https:///nohost") is not None

    def test_rejects_domain_resolving_to_private(self, monkeypatch):
        """关键：域名指向内网也必须拦（DNS 解析后判定）。

        实现已提到 ``core/media_fetch.py``，所以补丁要打在那边的 ``socket``；
        ``check_download_url`` 仍是同一个函数对象，QQ 侧的重导出照常生效。
        """
        import socket

        import core.media_fetch as mf
        from interfaces.qq.attachment import check_download_url

        def fake_getaddrinfo(host, port, *a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

        monkeypatch.setattr(mf.socket, "getaddrinfo", fake_getaddrinfo)
        assert check_download_url("https://evil.example.com/a") is not None

    def test_allows_domain_resolving_to_public(self, monkeypatch):
        import socket

        import core.media_fetch as mf
        from interfaces.qq.attachment import check_download_url

        def fake_getaddrinfo(host, port, *a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(mf.socket, "getaddrinfo", fake_getaddrinfo)
        assert check_download_url("https://example.com/a") is None

    def test_classify(self):
        from interfaces.qq.attachment import classify
        assert classify("image/png") == "image"
        assert classify("image/jpeg") == "image"
        assert classify("voice") == "voice"
        assert classify("audio/wav") == "voice"
        assert classify("file") == "file"
        assert classify("") == "file"


# ─── 指令 ─────────────────────────────────────────────────────────────


class _FakeRuntime:
    def __init__(self, *, cfg=None):
        from interfaces.qq.config import QQConfig
        from interfaces.qq.limiter import ReplyLimiter
        from interfaces.qq.state import QQAccountState

        self.cfg = cfg or QQConfig()
        self.account_id = self.cfg.id or "acc1"
        self.limiter = ReplyLimiter()
        self.state = QQAccountState()
        self.api = None
        self.loop = None


class TestCommands:
    def test_parse(self):
        from interfaces.qq.commands import parse
        assert parse("/h") == ("h", "")
        assert parse("/w 2") == ("w", "2")
        assert parse("  /status  ") == ("status", "")
        assert parse("hello") is None
        assert parse("") is None

    def test_build_commands_registry(self):
        """键 = 短名 + 别名；去重后恰好 6 条指令。"""
        from interfaces.qq.commands import build_commands, unique_commands
        cmds = build_commands()
        assert set(cmds) == {
            "h", "help", "w", "workspace", "s", "session",
            "new", "status", "stop",
        }
        assert [c.name for c in unique_commands()] == ["h", "w", "s", "new", "status", "stop"]
        for c in cmds.values():
            assert c.usage.startswith("/")
            assert callable(c.handler)

    def test_dispatch_unknown(self):
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        out = asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", "/nope"))
        assert "未知指令" in out

    def test_dispatch_non_command_returns_none(self):
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        assert asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", "你好")) is None

    def test_help_lists_all(self):
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        out = asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", "/h"))
        for name in ("/h", "/w", "/s", "/new", "/status", "/stop"):
            assert name in out

    def test_help_does_not_duplicate_aliases(self):
        """别名指向同一个 Command，/h 里不该把同一条指令列两遍。"""
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        out = asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", "/h"))
        assert out.count("/w ") <= 1
        assert out.count("/s ") <= 1
        assert out.count("/h") == 1

    def test_long_form_aliases_still_work(self):
        """`/help` `/workspace` `/session` 保留为等价别名——省得改了短名后旧习惯直接报未知指令。"""
        from interfaces.qq.commands import build_commands
        table = build_commands()
        assert table["help"] is table["h"]
        assert table["workspace"] is table["w"]
        assert table["session"] is table["s"]

    def test_aliases_dispatch_identically(self, tmp_path, monkeypatch):
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        for short, long in (("/h", "/help"), ("/w", "/workspace"), ("/s", "/session")):
            a = asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", short))
            b = asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", long))
            assert a == b, f"{short} 与 {long} 的输出应完全一致"

    def test_short_names_do_not_collide_with_prefixes(self):
        """/s 不能和 /status /stop 混（parse 是精确匹配，不是前缀匹配）。"""
        from interfaces.qq.commands import build_commands, parse
        table = build_commands()
        assert table["s"] is not table["status"]
        assert table["s"] is not table["stop"]
        assert parse("/status")[0] == "status"
        assert parse("/stop")[0] == "stop"
        assert parse("/s")[0] == "s"

    def test_status_shows_current(self, tmp_path, monkeypatch):
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        out = asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", "/status"))
        assert "当前状态" in out
        assert "被动回复配额" in out

    def test_status_shows_owner(self, tmp_path, monkeypatch):
        """owner 没有配置项了，/status 是查看自己 openid 的唯一途径。"""
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        state = QQAccountState(owner="MY_OPENID")
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/status"))
        assert "MY_OPENID" in out

    def test_status_shows_unbound_owner(self, tmp_path, monkeypatch):
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import QQAccountState
        out = asyncio.run(dispatch(_FakeRuntime(), QQAccountState(), "c1", "/status"))
        assert "未绑定" in out

    def test_status_shows_lock(self, tmp_path, monkeypatch):
        """锁定工作空间的账号在 /status 里可见（设计 §3.5）。"""
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.commands import dispatch
        from interfaces.qq.config import QQConfig
        from interfaces.qq.state import QQAccountState
        cfg = QQConfig(workspace_locked=True)
        out = asyncio.run(dispatch(_FakeRuntime(cfg=cfg), QQAccountState(), "c1", "/status"))
        assert "已锁定" in out

    def test_workspace_locked_rejects_w(self, tmp_path, monkeypatch):
        """锁定工作空间的账号：/w 拒绝执行且不改绑定（设计 §3.5）。"""
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.commands import dispatch
        from interfaces.qq.config import QQConfig
        from interfaces.qq.state import QQAccountState, QQChatState
        cfg = QQConfig(workspace_locked=True, default_workspace_uuid="test")
        state = QQAccountState(chats={"c1": QQChatState(workspace_uuid="test")})
        out = asyncio.run(dispatch(_FakeRuntime(cfg=cfg), state, "c1", "/w"))
        assert "已锁定" in out
        assert state.chats["c1"].workspace_uuid == "test"      # 未变

    def test_help_annotates_locked_w(self, tmp_path, monkeypatch):
        """锁定时 /h 的 /w 条目标注「本账号已锁定」（设计 §3.5）。"""
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        from interfaces.qq.commands import dispatch
        from interfaces.qq.config import QQConfig
        from interfaces.qq.state import QQAccountState
        cfg = QQConfig(workspace_locked=True)
        out = asyncio.run(dispatch(_FakeRuntime(cfg=cfg), QQAccountState(), "c1", "/h"))
        assert "本账号已锁定" in out


class TestCommandsWithWorkspaces:
    """用隔离的工作区索引测 /w 与 /s 的列表与切换（作用于当前聊天切片）。"""

    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        import core.config as config_mod
        idx = tmp_path / "workspaces.json"
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", idx)
        # 目录名刻意不含 uuid（用 wsA/wsB 命名的话，「输出里不该出现 uuid」的断言
        # 会被目录路径里的同名子串误触发，测不出真问题）
        ws_root = tmp_path / "alpha-repo"
        (ws_root / ".cili" / "sessions").mkdir(parents=True)
        config_mod.upsert_workspace_entry({
            "uuid": "wsA", "workspace_name": "Alpha", "directory": str(ws_root),
        })
        config_mod.upsert_workspace_entry({
            "uuid": "wsB", "workspace_name": "Beta", "directory": str(tmp_path / "beta-repo"),
        })
        import interfaces.qq.state as st
        monkeypatch.setattr(st, "DATA_DIR", tmp_path)
        return config_mod

    @staticmethod
    def _state(ws: str = "wsA"):
        """预绑定 wsA 的账号状态（聊天键固定 c1）。"""
        from interfaces.qq.state import QQAccountState, QQChatState
        return QQAccountState(chats={"c1": QQChatState(workspace_uuid=ws)})

    @staticmethod
    def _chat(state):
        return state.chats["c1"]

    def test_ws_list_shows_name_and_directory(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/w"))
        assert "Alpha" in out and "Beta" in out
        assert "✅" in out
        # 要目录
        assert str(isolated.find_workspace_entry("wsA")["directory"]) in out
        assert str(isolated.find_workspace_entry("wsB")["directory"]) in out

    def test_ws_list_hides_uuid(self, isolated):
        """列表不显示 id —— 用户认的是名字和目录。"""
        from interfaces.qq.commands import dispatch
        out = asyncio.run(dispatch(_FakeRuntime(), self._state(), "c1", "/w"))
        assert "wsA" not in out
        assert "wsB" not in out

    def test_ws_switch_by_index(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/w 2"))
        assert self._chat(state).workspace_uuid == "wsB"
        assert "Beta" in out

    def test_ws_switch_by_name(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state("wsB")
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/w alpha"))
        assert self._chat(state).workspace_uuid == "wsA"

    def test_ws_switch_not_found(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/w 99"))
        assert "找不到" in out
        assert self._chat(state).workspace_uuid == "wsA"      # 未变

    def test_new_creates_and_switches_session(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 测试会话"))
        assert "已新建" in out
        assert self._chat(state).session_id

    def test_session_list_after_new(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 甲"))
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/s"))
        assert "甲" in out

    def test_session_list_hides_session_id(self, isolated):
        """列表不显示 session id —— 切换只按序号，露出 id 反而诱导用户去敲它。"""
        from interfaces.qq.commands import dispatch
        state = self._state()
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 甲"))
        sid = self._chat(state).session_id
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/s"))
        assert sid not in out
        assert sid[:8] not in out

    def test_session_switch_by_index(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 甲"))
        first = self._chat(state).session_id
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 乙"))
        assert self._chat(state).session_id != first

        # 从列表里读出「甲」的序号再切（不假设排序：同一秒创建的会话
        # updated_at 相同，顺序由 tiebreaker 决定，测里不该硬编码）
        listing = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/s"))
        idx = next(
            int(ln.split(".", 1)[0])
            for ln in listing.splitlines()
            if ln[:2] in ("1.", "2.") and "甲" in ln
        )
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", f"/s {idx}"))
        assert self._chat(state).session_id == first
        assert "甲" in out                      # 确认文案用展示名，不是 id

    def test_session_index_stable_across_calls(self, isolated):
        """同一秒建的两个会话，序号在两次列表之间必须稳定（tiebreaker 的作用）。"""
        from interfaces.qq.commands import dispatch
        state = self._state()
        for name in ("甲", "乙", "丙"):
            asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new " + name))
        first = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/s"))
        second = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/s"))
        assert first == second

    def test_session_rejects_non_numeric(self, isolated):
        """不再支持按 id 切换 —— 给出明确指引而不是静默失败。"""
        from interfaces.qq.commands import dispatch
        state = self._state()
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 甲"))
        before = self._chat(state).session_id
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", f"/s {before}"))
        assert "序号" in out
        assert self._chat(state).session_id == before       # 未变

    def test_session_index_out_of_range(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 甲"))
        before = self._chat(state).session_id
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/s 99"))
        assert "超出范围" in out
        assert self._chat(state).session_id == before

    def test_switch_confirmation_uses_display_name(self, isolated):
        """切换确认里不该出现 session id。"""
        from interfaces.qq.commands import dispatch
        state = self._state()
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 甲"))
        sid = self._chat(state).session_id
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 乙"))
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/s 2"))
        assert sid not in out

    def test_status_hides_session_id(self, isolated):
        from interfaces.qq.commands import dispatch
        state = self._state()
        asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/new 甲"))
        sid = self._chat(state).session_id
        out = asyncio.run(dispatch(_FakeRuntime(), state, "c1", "/status"))
        assert sid not in out
        assert "甲" in out

    def test_w_and_s_persist_to_state_file(self, isolated):
        """/w 的切换要落盘（按账号键），重启后兼容读能拿回来（设计 §3.3）。"""
        import interfaces.qq.state as st
        from interfaces.qq.commands import dispatch
        from interfaces.qq.state import load_account_state

        rt = _FakeRuntime()
        state = self._state()
        asyncio.run(dispatch(rt, state, "c1", "/w 2"))
        loaded = load_account_state(rt.account_id)
        assert loaded.chats["c1"].workspace_uuid == "wsB"


# ─── sink：单流 transcript ────────────────────────────────────────────


class _FakeApi:
    def next_msg_seq(self):
        return 7


class _SinkRuntime:
    def __init__(self):
        from interfaces.qq.config import QQConfig
        from interfaces.qq.limiter import ReplyLimiter
        from interfaces.qq.state import QQAccountState
        self.account_id = "acc1"
        self.cfg = QQConfig()
        self.limiter = ReplyLimiter()
        self.api = _FakeApi()
        self.loop = None
        self.state = QQAccountState()


class TestQQSinkTranscript:
    def _sink(self, **kw):
        from interfaces.qq.sink import QQSink
        rt = _SinkRuntime()
        sink = QQSink(rt, "openid", "msgid", **kw)
        # 用假 target 接管流式下发，避免依赖 SDK/loop
        pushed: list[tuple[str, bool]] = []

        class _T:
            sent_ok = 0
            failed = False

            def push(self, text, *, final):
                pushed.append((text, final))
                _T.sent_ok += 1

            def restart(self):
                pushed.append(("<<restart>>", False))

            def close(self):
                pass

        sink._target = _T()
        from interfaces.qq.stream import StreamingController
        sink._controller = StreamingController(
            sink._target, flush_interval=0.0, max_wait=0.0
        )
        return sink, pushed

    def test_tool_line_format_and_dedup(self):
        sink, pushed = self._sink()
        sink.on_tool_call("read", {"file_path": "a.py"}, "t1")
        sink.on_tool_call("read", {"file_path": "b.py"}, "t2")   # 连续同名 → 去重
        sink.on_tool_call("grep", {"pattern": "x"}, "t3")
        text = pushed[-1][0]
        assert text.count("🔧 正在调用 read 工具…") == 1
        assert "🔧 正在调用 grep 工具…" in text
        # 不含入参
        assert "a.py" not in text and "x" not in text

    def test_tool_line_repeats_after_text(self):
        sink, pushed = self._sink()
        sink.on_tool_call("read", {}, "t1")
        sink.on_text("分析结果如下。")
        sink.on_tool_call("read", {}, "t2")   # 中间夹了正文 → 重新记一行
        assert pushed[-1][0].count("🔧 正在调用 read 工具…") == 2

    def test_ask_user_rendered_into_stream(self):
        sink, pushed = self._sink()
        sink.on_tool_call("ask_user", {"questions": [{
            "question": "选哪个?", "header": "选择",
            "options": [{"label": "A", "description": "方案甲"},
                        {"label": "B", "description": "方案乙"}],
        }]}, "t1")
        text = pushed[-1][0]
        assert "选哪个?" in text
        assert "1. A — 方案甲" in text
        assert "2. B — 方案乙" in text
        assert "回复序号" in text

    def test_retry_clear_sentinel_resets_stream(self):
        from interfaces.qq.sink import RETRY_CLEAR_SENTINEL
        sink, pushed = self._sink()
        sink.on_text("旧内容")
        sink.on_text(RETRY_CLEAR_SENTINEL)
        assert "<<restart>>" in [p[0] for p in pushed]
        sink.on_text("新内容")
        assert pushed[-1][0] == "新内容"

    def test_buffer_is_monotonic_prefix(self):
        """replace 模式要求新内容以已下发前缀开头（防 40007）。"""
        sink, pushed = self._sink()
        sink.on_text("第一段")
        sink.on_text("，第二段")
        sink.on_tool_call("read", {}, "t1")
        texts = [t for t, _ in pushed if t != "<<restart>>"]
        for prev, cur in zip(texts, texts[1:]):
            assert cur.startswith(prev), f"{cur!r} 不以 {prev!r} 开头"

    def test_thinking_not_pushed_to_qq(self):
        sink, pushed = self._sink()
        base_calls = []
        sink._base = type("B", (), {
            "on_thinking": staticmethod(lambda t: base_calls.append(t)),
        })()
        sink.on_thinking("内部推理")
        assert pushed == []                 # 不推给 QQ
        assert base_calls == ["内部推理"]     # 但走全局流

    def test_tool_result_not_pushed_to_qq(self):
        sink, pushed = self._sink()
        sink._base = type("B", (), {"on_tool_result": staticmethod(lambda *a: None)})()
        sink.on_tool_result("read", "文件内容", False, "t1")
        assert pushed == []

    def test_turn_complete_closes_stream(self):
        sink, pushed = self._sink()
        sink.on_text("答案")
        sink.on_turn_complete()
        assert pushed[-1][1] is True        # final 标记

    def test_pure_scaffolding_text_not_pushed(self):
        """整段都是 thinking 标签 → 不推给 QQ。"""
        sink, pushed = self._sink()
        sink.on_text("<thinking>只有推理</thinking>")
        assert pushed == []

    def test_build_returns_output_sink(self):
        from core.output_sink import OutputSink
        sink, _ = self._sink()
        assert isinstance(sink.build(), OutputSink)

    def test_fanout_to_base_on_text(self):
        """QQ 驱动的回合必须仍发布到全局事件流（Web 才看得见）。"""
        seen = []
        from core.output_sink import OutputSink
        sink, _ = self._sink(base=OutputSink(on_text=seen.append))
        sink.on_text("你好")
        assert seen == ["你好"]

    def test_multi_question_render_asks_for_qualified_answer(self):
        """多问题时编号在每个问题内重开，单个数字有歧义 → 提示「问题序号: 选项序号」。"""
        sink, pushed = self._sink()
        sink.on_tool_call("ask_user", {"questions": [
            {"question": "甲?", "header": "A", "options": [{"label": "A1", "description": ""}]},
            {"question": "乙?", "header": "B", "options": [{"label": "B1", "description": ""}]},
        ]}, "t1")
        text = pushed[-1][0]
        assert "1: 2" in text
        assert "回复序号即可" not in text


class TestResolveAnswer:
    """ask_user 的编号 → label 映射。"""

    class _Session:
        def __init__(self, questions):
            self.messages = [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "ask_user",
                     "input": {"questions": questions}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": "Waiting for user input...",
                     "_meta": {"completed": False, "tool_name": "ask_user"}},
                ]},
            ]

    class _Runner:
        def __init__(self, questions):
            self.session = TestResolveAnswer._Session(questions)

    def _resolve(self, questions, content):
        from interfaces.qq.inbound import InboundHandler
        return InboundHandler._resolve_answer(self._Runner(questions), content)

    def test_single_question_maps_number_to_label(self):
        qs = [{"question": "选?", "options": [{"label": "允许", "description": ""},
                                             {"label": "拒绝", "description": ""}]}]
        assert self._resolve(qs, "1") == "允许"
        assert self._resolve(qs, "2") == "拒绝"

    def test_out_of_range_number_passes_through(self):
        qs = [{"question": "选?", "options": [{"label": "A", "description": ""}]}]
        assert self._resolve(qs, "9") == "9"

    def test_multi_question_does_not_guess(self):
        """两个问题时 "1" 是歧义的 → 原样透传，不猜。"""
        qs = [
            {"question": "甲?", "options": [{"label": "甲1", "description": ""}]},
            {"question": "乙?", "options": [{"label": "乙1", "description": ""}]},
        ]
        assert self._resolve(qs, "1") == "1"

    def test_non_numeric_passes_through(self):
        qs = [{"question": "选?", "options": [{"label": "A", "description": ""}]}]
        assert self._resolve(qs, "我自己输入") == "我自己输入"

    def test_no_pending_ask_user_passes_through(self):
        class _Empty:
            class session:  # noqa: N801
                messages = []
        from interfaces.qq.inbound import InboundHandler
        assert InboundHandler._resolve_answer(_Empty(), "1") == "1"
