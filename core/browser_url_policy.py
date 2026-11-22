"""浏览器导航 URL 策略：scheme 白名单、云元数据硬拒绝、SSRF 私网拦截。"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# navigate 仅允许 http/https scheme（拒绝 file://、data:、javascript: 等）
_NAVIGATE_SAFE_SCHEMES = frozenset({"http", "https"})

# 云元数据端点：即使用户批准了非公网导航也硬拒绝 —— 批准的语义是「访问内网
# 服务」，不应连带解锁云凭证获取端点（169.254.169.254 等可换取实例凭证）
_NAVIGATE_METADATA_HOSTS = frozenset({
    "169.254.169.254",           # AWS / GCP / Azure / OpenStack
    "fd00:ec2::254",             # AWS IMDSv2（IPv6）
    "metadata.google.internal",  # GCP
    "metadata.goog",
    "100.100.100.200",           # 阿里云
})


def hard_deny_reason(url: str) -> str | None:
    """任何情况下都不放行的拒绝项（含用户已批准）：scheme 白名单 + 云元数据端点。

    与 check_navigate_url 的私网判定分开，是为了让「用户批准非公网导航」
    只解锁私网/环回地址，而不会连带绕过 scheme 白名单（否则批准一个
    file:/// 就能读本地文件）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _NAVIGATE_SAFE_SCHEMES:
        scheme = parsed.scheme or "(空)"
        return f"URL scheme '{scheme}' 不允许，仅支持 http/https"
    host = parsed.hostname
    if not host:
        return "URL 缺少主机名"
    if host.lower() in _NAVIGATE_METADATA_HOSTS:
        return f"{host} 是云元数据端点，不允许导航（SSRF 硬拒绝，用户批准亦不放行）"
    return None


def check_navigate_url(url: str, *, allow_non_public: bool = False) -> str | None:
    """校验浏览器导航 URL（A30/SEC-17）。

    Args:
        allow_non_public: 用户已批准该非公网导航时为 True —— 只跳过
            「私网/环回/链路本地」判定；scheme 白名单与云元数据端点属硬拒绝，
            不受此参数影响。

    Returns:
        None 表示可导航；否则返回拒绝原因（供 ToolResult error 展示）。

    策略：
    - scheme 白名单：仅 http/https，硬拒绝 file://、data:、javascript: 等
    - 云元数据端点硬拒绝（见 hard_deny_reason）
    - 私网/环回/链路本地/保留地址拦截（SSRF 防护）：字面 IP 判定 + localhost 特判。
      配合网页内容提示注入，防止恶意网页诱导浏览器读取本地文件或探测内网。
    """
    reason = hard_deny_reason(url)
    if reason:
        return reason
    if allow_non_public:
        return None
    host = urlparse(url).hostname
    if host.lower() == "localhost":
        return "localhost（环回地址）不允许导航（SSRF 防护）"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None  # 域名，交由浏览器 DNS 解析（无法静态预判，不拦截）
    if not addr.is_global:
        return (
            f"{host} 不是公网地址（环回/私网/链路本地等），不允许导航（SSRF 防护）"
        )
    return None
