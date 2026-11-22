"""邮件账号配置：白名单解析、类型兜底、读写往返。"""

from interfaces.email import config as config_mod
from interfaces.email.config import (
    EmailConfig,
    load_email_accounts,
    save_email_accounts,
)


class TestFromDict:
    def test_allowed_senders_list(self):
        cfg = EmailConfig.from_dict({"allowed_senders": ["Me@Example.com", " me@example.com "]})
        # 小写归一化 + 去重
        assert cfg.allowed_senders == ["me@example.com"]

    def test_allowed_senders_string(self):
        cfg = EmailConfig.from_dict({"allowed_senders": "a@x.com\nb@x.com, a@x.com"})
        assert cfg.allowed_senders == ["a@x.com", "b@x.com"]

    def test_allowed_senders_invalid_type(self):
        assert EmailConfig.from_dict({"allowed_senders": 123}).allowed_senders == []

    def test_port_type_fallback(self):
        cfg = EmailConfig.from_dict({"imap_port": "abc"})
        assert cfg.imap_port == 993

    def test_smtp_security_fallback(self):
        assert EmailConfig.from_dict({"smtp_security": "weird"}).smtp_security == "ssl"
        assert EmailConfig.from_dict({"smtp_security": "starttls"}).smtp_security == "starttls"

    def test_unknown_keys_ignored(self):
        cfg = EmailConfig.from_dict({"poll_interval_seconds": 5, "unknown": "x"})
        # 调参字段不读配置，保留默认值
        assert cfg.poll_interval_seconds == config_mod.DEFAULT_POLL_INTERVAL_SECONDS

    def test_session_mode_normalized(self):
        assert EmailConfig.from_dict({"session_mode": "LATEST"}).session_mode == "latest"
        assert EmailConfig.from_dict({"session_mode": "bogus"}).session_mode == "dedicated"


class TestIsReady:
    def test_ready(self):
        cfg = EmailConfig(
            email_address="a@x.com", imap_host="imap.x.com", smtp_host="smtp.x.com",
            username="a@x.com", password="p",
        )
        assert cfg.is_ready()

    def test_missing_password(self):
        cfg = EmailConfig(
            email_address="a@x.com", imap_host="imap.x.com", smtp_host="smtp.x.com",
            username="a@x.com",
        )
        assert not cfg.is_ready()


class TestLoadSave:
    def test_roundtrip(self, monkeypatch):
        store = {"system": {"x": 1}}

        monkeypatch.setattr(config_mod, "load_global_config", lambda: dict(store))
        monkeypatch.setattr(config_mod, "save_global_config",
                            lambda data: store.update(data) or True)

        cfg = EmailConfig(
            id="a@x.com", email_address="a@x.com", enabled=True,
            imap_host="imap.x.com", smtp_host="smtp.x.com",
            username="a@x.com", password="secret",
            allowed_senders=["me@x.com"],
        )
        assert save_email_accounts([cfg])

        # 其它段保留
        assert store["system"] == {"x": 1}
        accounts = load_email_accounts()
        assert len(accounts) == 1
        assert accounts[0].password == "secret"
        assert accounts[0].allowed_senders == ["me@x.com"]

    def test_empty_section(self, monkeypatch):
        store = {}
        monkeypatch.setattr(config_mod, "load_global_config", lambda: dict(store))
        assert load_email_accounts() == []

    def test_id_defaults_to_address(self, monkeypatch):
        store = {"email": {"accounts": [{"email_address": "a@x.com"}]}}
        monkeypatch.setattr(config_mod, "load_global_config", lambda: dict(store))
        accounts = load_email_accounts()
        assert accounts[0].id == "a@x.com"
