"""教材平台匿名访问与 MAC 签名测试。"""
from core.textbooks import smartedu


def test_base_headers_carry_random_bearer():
    """匿名模式的关键：Authorization 头存在且不是全零占位。"""
    auth = smartedu.BASE_HEADERS["Authorization"]
    assert auth.startswith("Bearer ")
    assert auth != "Bearer 0"


def test_anonymous_nd_auth_is_not_placeholder():
    h = smartedu.anonymous_nd_auth()
    assert 'MAC id="0"' not in h
    assert 'nonce="0"' not in h
    assert 'mac="0"' not in h
    assert h.startswith('MAC id="')


def test_build_nd_auth_anonymous_without_mac_key(monkeypatch):
    creds = smartedu.SmartEduCredentials(access_token="", mac_key="")
    h = smartedu.build_nd_auth("https://r1-ndr-private.ykt.cbern.com.cn/a.pdf",
                               "GET", creds)
    assert 'nonce="0"' not in h


def test_build_nd_auth_signed_with_mac_key():
    creds = smartedu.SmartEduCredentials(access_token="tok123", mac_key="key456")
    h = smartedu.build_nd_auth(
        "https://r1-ndr-private.ykt.cbern.com.cn/a.pdf", "GET", creds)
    assert h.startswith('MAC id="tok123"')
    assert 'nonce="0"' not in h and 'mac="0"' not in h
