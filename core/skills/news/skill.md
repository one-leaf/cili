---
name: 每日热点
description: 热点新闻聚合与简报：热搜、早报、金价汇率、科技/开源资讯、天气预报。Use when user mentions 热点、热搜、热榜、新闻、早报、简报、金价、汇率、油价、微博、知乎、百度、抖音、B站、HN、GitHub 热门、36氪、IT之家、少数派，或问"今天有什么新鲜事 / 最近怎么样"。
roles: [master]
---

# Daily Hot News — 每日热点聚合

Fetch hot searches, daily briefings, gold/forex quotes and tech/open-source news from verified free APIs, then produce a de-duplicated Chinese digest for the user.

## Core principles

- **Fact-only, zero hallucination (highest priority)** — every title, figure and trend line must come from data actually fetched in this run. Never invent a news item or a heat value. If a source cannot be fetched, label it `[数据源] — 获取失败，已跳过` and continue.
- **No extra scripts** — fetch via cili's `python` tool with inline `httpx` (already a core dependency). Do NOT install npm packages, do NOT write helper scripts, do NOT shell out to node.
- **API first, browser as fallback** — always try a JSON/HTTP API before opening the `browser` tool.

## Data channels

### Channel A — 60s API (primary)

Base URL: `https://60s.viki.moe/v2`. Free, no auth, covers hot searches + finance + weather + movie/music.

**Encoding (verified 2026-09-18):** every endpoint returns **UTF-8 JSON** — always `r.content.decode('utf-8')`. On Windows, `print()`ing Chinese to the terminal looks garbled (console codepage cp936); that is purely a display artifact, not the data. Write output to a file and Read it, or `json.dumps(data, ensure_ascii=False)` into a file.

**⚠️ Single-script rule:** always fetch AND format in the **same** `python` tool call. Each python call is a fresh `stdin` with no variable persistence — `results` defined in call 1 is gone in call 2.

```python
import httpx, json

def fetch(url, retries=3):
    """Fetch a 60s API endpoint with retry. Returns data or None."""
    for i in range(retries):
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            d = json.loads(r.content.decode('utf-8'))
            if d.get('data') is not None:
                return d['data']
        except Exception:
            if i < retries - 1:
                import time; time.sleep(1)
    return None
```

**Flakiness (verified):** this is a free third-party service and **intermittently returns `data: null`/empty or HTTP 500 even on healthy endpoints**, especially under burst requests. Always retry up to 3 times with a short backoff; if still empty, fall through to the next channel. Verified-unstable endpoints: `rednote`, `bili`, `maoyan/realtime/movie`, `dongchedi` (empty), `zhihu-daily` (404 — does not exist), `baike?query=` (400). Do not hard-fail on any single source.

**Verified endpoints and real field names** (probe 2026-09-18; key names vary per endpoint — print `data[0].keys()` when unsure):

| Endpoint | Shape | Item fields |
|---|---|---|
| `/v2/60s` | `{date, news[15], tip, cover, image, link}` | ⚠️ **news 是 `list[str]`**，每条直接是标题字符串（无 `title`/`link` 字段）；tip 也是 str |
| `/v2/weibo` | list[50] | `title`, `hot_value`, `link` — 微博热搜 |
| `/v2/zhihu` | list[30] | `title`, `detail`, `hot_value_desc`, `answer_cnt`, `follower_cnt`, `comment_cnt`, `cover`, `created_at`, `link` — 知乎热榜 |
| `/v2/baidu/hot` | list[50] | `rank`, `title`, `desc`, `score`, `url` — 百度热搜 |
| `/v2/douyin` | list[49] | `title`, `hot_value`, `cover`, `link`, `event_time`, `active_time` — 抖音热点 |
| `/v2/toutiao` | list[50] | `title`, `hot_value`, `link` — 今日头条 |
| `/v2/quark` | list[50] | `title`, `summary`, `source`, `link` — 夸克热点 |
| `/v2/hacker-news/top` | list[10] | `id`, `title`, `score`, `author`, `link`, `created`, `created_at` — Hacker News（另有 `/new`、`/best`） |
| `/v2/ai-news` | `{date, news[4]}` | `title`, `detail`, `link`, `source` — AI 资讯 |
| `/v2/today-in-history` | `{date, items[11]}` | `title`, `year`, `description`, `link` — 历史上的今天 |
| `/v2/gold-price` | `{date, metals, stores, banks, recycle}` | metals: `name`,`sell_price`,`today_price`,`high_price`,`low_price`,`unit`；stores: `brand`,`product`,`price`,`unit`,`formatted`；banks: `bank`,`product`,`price`,`time`；recycle: `type`,`price`,`purity` |
| `/v2/exchange-rate?currency=CNY` | `{base_code, updated, updated_at, rates}` | rates item: `currency`, `rate`。注意：`updated` 是字符串时间（如 `2026/10/30 08:02:31`），`updated_at` 是毫秒时间戳 |
| `/v2/fuel-price` | `{region, trend, items, link}` | `name`, `price` — 油价 |
| `/v2/maoyan/all/movie` | `{list[20], tip}` | `rank`, `movie_name`, `box_office` — 票房 |
| `/v2/douban/weekly/movie` | list[10] | `rank`, `title`, `rating`, `url` — 口碑电影 |
| `/v2/ncm-rank/list` | list[63] | `id`, `name`, `update_frequency` — 音乐榜 |
| `/v2/weather/realtime?query=北京` | `{location, weather, air_quality, sunrise, life_indices, alerts}` | 天气（⚠️ 有频率限制，可能 429） |
| `/v2/weather/forecast?query=北京&days=7` | — | 预报 |
| `/v2/moyu` | dict | 摸鱼日历（假期/周末/进度） |
| `/v2/epic` | list[4] | `title`, `is_free_now`, `free_end`, `link` — Epic 免费游戏 |
| `/v2/60s?encoding=markdown` | text/markdown | 直接可展示的文本，无需解析 |

**Reference implementation** — 完整的 fetch + format 单脚本模板（直接可用，按需裁剪）：

```python
import httpx, json, time

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            d = json.loads(r.content.decode('utf-8'))
            if d.get('data') is not None:
                return d['data']
        except Exception:
            if i < retries - 1:
                time.sleep(1)
    return None

# Fetch all needed endpoints in one script
data_60s = fetch('https://60s.viki.moe/v2/60s') or {}
weibo = fetch('https://60s.viki.moe/v2/weibo') or []
douyin = fetch('https://60s.viki.moe/v2/douyin') or []
zhihu = fetch('https://60s.viki.moe/v2/zhihu') or []
hn = fetch('https://60s.viki.moe/v2/hacker-news/top') or []
gold = fetch('https://60s.viki.moe/v2/gold-price') or {}
exchange = fetch('https://60s.viki.moe/v2/exchange-rate?currency=CNY') or {}

output = []

# 1. 每日早报 — news 是 list[str]，直接遍历
output.append("# 今日热点速览")
output.append("> 数据来源: 60s API\n")
output.append("## 每日要闻")
for item in (data_60s.get('news') or [])[:12]:
    output.append(f"- {item}")  # item 直接是字符串
tip = data_60s.get('tip') or ''
if tip:
    output.append(f"\n💡 每日一言: {tip}")

# 2. 微博热搜
output.append("\n---\n## 🔥 微博热搜 TOP15")
for i, item in enumerate(weibo[:15], 1):
    if isinstance(item, dict):
        output.append(f"{i}. {item.get('title', '')} — 热度 {item.get('hot_value', '')}")

# 3. 抖音热点
output.append("\n---\n## 🎵 抖音热点 TOP10")
for i, item in enumerate(douyin[:10], 1):
    if isinstance(item, dict):
        output.append(f"{i}. {item.get('title', '')} — 热度 {item.get('hot_value', '')}")

# 4. 知乎热榜
output.append("\n---\n## 📘 知乎热榜 TOP10")
for i, item in enumerate(zhihu[:10], 1):
    if isinstance(item, dict):
        output.append(f"{i}. {item.get('title', '')} — {item.get('hot_value_desc', '')} · {item.get('answer_cnt', '')}条回答")

# 5. Hacker News
output.append("\n---\n## 💻 Hacker News TOP10")
for i, item in enumerate(hn[:10], 1):
    if isinstance(item, dict):
        output.append(f"{i}. {item.get('title', '')} — {item.get('score', '')} points")

# 6. 金价
output.append("\n---\n## 💰 金价与汇率")
for m in (gold.get('metals') or [])[:4]:
    if isinstance(m, dict):
        output.append(f"- {m.get('name', '')}: ¥{m.get('sell_price', '')}/克")
for s in (gold.get('stores') or [])[:6]:
    if isinstance(s, dict):
        output.append(f"- {s.get('brand', '')}: ¥{s.get('price', '')}/克")

# 7. 汇率 — 注意字段名是 updated 不是 updated_at
rates = exchange.get('rates') or []
base = exchange.get('base_code', 'CNY')
updated = exchange.get('updated', '')  # 字符串时间，如 "2026/10/30 08:02:31"
key_cur = ['USD', 'EUR', 'GBP', 'JPY', 'HKD']
output.append(f"\n汇率 (基准: {base})")
for r in rates:
    if isinstance(r, dict) and r.get('currency') in key_cur:
        output.append(f"- 1 {base} = {r.get('rate', '')} {r.get('currency', '')}")
if updated:
    output.append(f"更新时间: {updated}")

text = '\n'.join(output)
print(text[:3000])
```

### Channel B — direct free APIs (verified, no auth)

Use these for tech/open-source topics the 60s API does not cover:

- **GitHub Trending via Search API** — `GET https://api.github.com/search/repositories?q=created:>YYYY-MM-DD&sort=stars&order=desc&per_page=5` → `{items:[{full_name, description, stargazers_count, html_url}]}`. Use `created:>{7_days_ago}` for weekly trending. Add header `Accept: application/vnd.github+json` and a short `User-Agent`.
- **IT之家 RSS** — `GET https://www.ithome.com/rss/` → XML; parse with `xml.etree.ElementTree`, items under `channel/item/title`. For tech news beyond 60s coverage.

Verified NOT usable directly (use browser fallback): V2EX official API (timeout), 36氪 gateway (HTTP 500).

### Channel C — browser (fallback)

For sources with no working API (36氪、掘金、V2EX、虎扑、少数派…) or when Channels A/B fail: open the site with the `browser` tool, take a snapshot, and extract titles from the accessibility tree. Scrape sequentially, one source at a time.

## Workflow

1. **Route** — map the request to a topic and pick sources. Platform-named requests (微博/知乎/HN…) go straight to that source; finance words (金价/汇率/油价) → finance data endpoints; "分析/深度" → deep mode (6–8 sources); otherwise general (4–5 sources).
2. **Fetch** — Channel A via `python`/httpx (parallel OK), Channel B, then Channel C. Retry each API 3× before falling back.
3. **Aggregate** — merge duplicate titles across sources (same title = one entry, list all sources; sort by cross-source count then heat value). Apply user-preference filter if the user tracks specific topics/teams (`⭐ 你的关注`).
4. **Deliver** — pick an output format below, stay in the length band, append the source/status footer.

## Output formats

Standard brief (default, 500–800 字):

```markdown
# 今日热点速览
> 时间: {YYYY-MM-DD HH:MM}
> 数据来源: {源列表}
> 获取方式: {API: N个, 浏览器: N个, 失败: N个}

## 热点头条
（跨平台出现次数越多排名越靠前，最多 10 条）

1. **{标题}** — {一句话摘要}
   - 热度: {数值} | 来源: {平台1, 平台2}

## 分类浏览
### 科技互联网 / 财经商业 / 社会民生
- {标题} ({来源}) — {简述}

## 趋势洞察
{2-3句基于本次数据的跨平台规律总结}
```

Quick mode (单平台或"只看标题", 200–400 字):

```markdown
# {数据源名称} 热榜
> 时间: {YYYY-MM-DD HH:MM}
1. {标题} — 热度 {数值}
...（最多 20 条，不含分析）
```

Deep analysis (标准 + 追加，800–1200 字): `## 深度分析` with `### 市场情绪信号 / 值得关注的行业 / 跨平台趋势`.

Finance brief (金价/汇率/股市): lead with 金价、汇率、油价 numbers (from `/v2/gold-price`、`/v2/exchange-rate`、`/v2/fuel-price`), tag affected sectors `[半导体] [新能源]`, and always end with `以上分析仅供参考，不构成投资建议。`

## Output rules

- Every item must come from this run's fetched data; heat values must be real numbers from the payload.
- Trend insight must generalize only from data seen in this run — never import outside knowledge.
- If data is too thin to analyze, say `信息有限，仅供参考` instead of padding.
- End the brief with the actual sources fetched, method, and time.
- Default output is Chinese; keep technical terms in English where natural.
