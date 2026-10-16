# Network speed test (win-ops, on demand)

Run when the user explicitly asks (测速/网速/宽带) or complains about 网速慢/下载慢, or when the fast report flags a network problem. Cost: ~20-30s and up to ~60MB traffic (~25MB per download node) — say so before running. Zero dependencies, pure stdlib: write the script with the write tool to `$CILI_TMP/winops_speedtest.py`, then run `python(action='execute_file', script_file='<the path>')` — do NOT run it from pwsh (python invocation there needs approval; the python tool is the dedicated path).

```python
# -*- coding: utf-8 -*-
"""win-ops 网络测速（零依赖，纯标准库）。输出 KEY=value 行协议。
双节点下载定位问题：CN 慢 + INTL 相当 → 本地宽带/网卡/Wi-Fi；
CN 正常 + INTL 明显慢 → 国际出口（大陆正常现象，不算故障）。"""
import json, random, socket, time
from urllib.request import Request, urlopen, getproxies

BYTES_25M = 25 * 1024 * 1024
CN_URL = "https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe"  # 腾讯 CDN 大文件，Range 截取 25MB
INTL_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
UP_URL = "https://speed.cloudflare.com/__up"
CN_PING_HOST = "dldir1.qq.com"

def fmt(bps):
    for u, d in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if bps >= d:
            return f"{bps / d:.2f} {u}"
    return f"{bps:.2f} bps"

def get_proxy():
    p = getproxies()
    if p:
        return "PROXY_INUSE yes kinds=" + ",".join(sorted(p))
    return "PROXY_INUSE none"

def ip_info():
    apis = [
        ("http://ip-api.com/json/?lang=zh-CN&fields=query,city,regionName,isp",
         lambda d: (d.get("query", "?"), d.get("city", ""), d.get("regionName", ""), d.get("isp", "?"))),
        ("https://ipinfo.io/json",
         lambda d: (d.get("ip", "?"), d.get("city", ""), d.get("region", ""), d.get("org", "?"))),
    ]
    for url, pick in apis:
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=8) as f:
                ip, city, region, isp = pick(json.loads(f.read().decode()))
            return f"IP ip={ip} city={city} region={region} isp={isp}"
        except Exception:
            continue
    return "IP ip=unavailable"

def ping_cn(times=4):
    try:  # 预热连接：排除 DNS/冷启动抖动，不计数
        s = socket.create_connection((CN_PING_HOST, 443), timeout=4)
        s.close()
    except OSError:
        pass
    lat = []
    for _ in range(times):
        t0 = time.time()
        try:
            s = socket.create_connection((CN_PING_HOST, 443), timeout=4)
            s.close()
            lat.append(round((time.time() - t0) * 1000))
        except OSError:
            lat.append(None)
    ok = [x for x in lat if x is not None]
    if ok:
        return (f"PING_CN avg={sum(ok) // len(ok)}ms min={min(ok)}ms "
                f"max={max(ok)}ms lost={times - len(ok)}/{times}")
    return f"PING_CN avg=unavailable lost={times}/{times}"

def download(name, url, use_range=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    if use_range:
        headers["Range"] = f"bytes=0-{BYTES_25M - 1}"
    total, t0, err = 0, time.time(), ""
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as r:
            while time.time() - t0 < 8:
                c = r.read(65536)
                if not c:
                    break
                total += len(c)
    except Exception as e:
        err = f" error={type(e).__name__}"  # 超时等异常仍上报已测字节
    if total > 0:
        cost = max(time.time() - t0, 0.001)
        tag = " partial" if err else ""
        return (f"DOWNLOAD_{name} mbps={total * 8 / cost / 1e6:.2f} "
                f"bytes={total} seconds={cost:.1f}{tag}{err}")
    return f"DOWNLOAD_{name} error=no-data{err}"

def upload():
    data = random.randbytes(8 * 1024 * 1024)
    t0 = time.time()
    try:
        urlopen(Request(UP_URL, data=data, headers={"User-Agent": "Mozilla/5.0"}), timeout=30)
        cost = max(time.time() - t0, 0.001)
        return f"UPLOAD mbps={len(data) * 8 / cost / 1e6:.2f} seconds={cost:.1f}"
    except Exception as e:
        return f"UPLOAD error={type(e).__name__}: {e}"

print("SPEEDTEST", time.strftime("%Y-%m-%d %H:%M:%S"))
print(get_proxy())
print(ip_info())
print(ping_cn())
print(download("CN", CN_URL, use_range=True))
print(download("INTL", INTL_URL))
print(upload())
```

Output protocol: `PROXY_INUSE yes|none`, `IP ip=... city=... region=... isp=...`, `PING_CN avg/min/max/lost`, `DOWNLOAD_CN mbps=...`, `DOWNLOAD_INTL mbps=...`, `UPLOAD mbps=...`.

Interpretation:

- CN slow + INTL comparable → local broadband/NIC/Wi-Fi problem — retest once before judging; if confirmed, evaluate against the threshold table (references/evaluation.md)
- CN fine + INTL much slower → international egress, normal from mainland China — note only, never grade it as a defect
- UPLOAD is a single POST to Cloudflare (no reliable keyless CN upload endpoint) — international path, indicative only, never grade it alone
- `PROXY_INUSE yes` → the numbers reflect the proxy path, not the raw broadband; cross-read batch-4 `PROXY Enable=` and say so in the report
