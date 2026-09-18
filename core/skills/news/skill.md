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

```python
import httpx, json
r = httpx.get('https://60s.viki.moe/v2/weibo', timeout=20, follow_redirects=True)
d = json.loads(r.content.decode('utf-8'))     # response: {"code":200,"data":[...]}
data = d.get('data') or []                    # data may be list or dict, or null
```

**Flakiness (verified):** this is a free third-party service and **intermittently returns `data: null`/empty or HTTP 500 even on healthy endpoints**, especially under burst requests. Always retry up to 3 times with a short backoff; if still empty, fall through to the next channel. Verified-unstable endpoints: `rednote`, `bili`, `maoyan/realtime/movie`, `dongchedi` (empty), `zhihu-daily` (404 — does not exist), `baike?query=` (400). Do not hard-fail on any single source.

**Verified endpoints and real field names** (probe 2026-09-18; key names vary per endpoint — print `data[0].keys()` when unsure):

| Endpoint | Shape | Item fields |
|---|---|---|
| `/v2/60s` | `{date, news[15], tip, link}` | news item: `title`, `link` — 每日 15 条精选 + 每日一言 |
| `/v2/weibo` | list[50] | `title`, `hot_value`, `link` — 微博热搜 |
| `/v2/zhihu` | list[30] | `title`, `hot_value_desc`, `answer_cnt`, `link` — 知乎热榜 |
| `/v2/baidu/hot` | list[50] | `rank`, `title`, `desc`, `score`, `url` — 百度热搜 |
| `/v2/douyin` | list[49] | `title`, `hot_value`, `link` — 抖音热点 |
| `/v2/toutiao` | list[50] | `title`, `hot_value`, `link` — 今日头条 |
| `/v2/quark` | list[50] | `title`, `summary`, `source`, `link` — 夸克热点 |
| `/v2/hacker-news/top` | list[10] | `title`, `score`, `author`, `link` — Hacker News（另有 `/new`、`/best`） |
| `/v2/ai-news` | `{date, news[4]}` | `title`, `detail`, `link`, `source` — AI 资讯 |
| `/v2/today-in-history` | `{date, items[11]}` | `title`, `year`, `description`, `link` — 历史上的今天 |
| `/v2/gold-price` | `{date, metals, stores, banks, recycle}` | metals: `name`,`sell_price`; stores: `brand`,`price`; banks: `bank`,`price` — 金价 |
| `/v2/exchange-rate?currency=CNY` | `{base_code, updated, rates}` | `currency`, `rate` — 汇率 |
| `/v2/fuel-price` | `{region, trend, items, link}` | `name`, `price` — 油价 |
| `/v2/maoyan/all/movie` | `{list[20], tip}` | `rank`, `movie_name`, `box_office` — 票房 |
| `/v2/douban/weekly/movie` | list[10] | `rank`, `title`, `rating`, `url` — 口碑电影 |
| `/v2/ncm-rank/list` | list[63] | `id`, `name`, `update_frequency` — 音乐榜 |
| `/v2/weather/realtime?query=北京` | `{location, weather, air_quality, sunrise, life_indices, alerts}` | 天气 |
| `/v2/weather/forecast?query=北京&days=7` | — | 预报 |
| `/v2/moyu` | dict | 摸鱼日历（假期/周末/进度） |
| `/v2/epic` | list[4] | `title`, `is_free_now`, `free_end`, `link` — Epic 免费游戏 |
| `/v2/60s?encoding=markdown` | text/markdown | 直接可展示的文本，无需解析 |

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
