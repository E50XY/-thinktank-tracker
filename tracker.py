#!/usr/bin/env python3
"""
智库动态追踪器 — 每 6 小时抓取一批机构的最新发布,按领域分类输出。

用法:
    python tracker.py discover          # 自动探测各机构的 RSS/Atom 源,写入 feeds.yaml
    python tracker.py discover --only "Bruegel,OECD"   # 只探测指定机构
    python tracker.py fetch             # 抓取最近 6 小时的新条目,生成报告
    python tracker.py fetch --hours 24 --format md,html,json
    python tracker.py status            # 查看有几家机构已有可用订阅源
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

from site_builder import build_site
from translate import Translator
try:                       # 可选模块:没有 keypoints.py 也能正常抓取和翻译
    from keypoints import KeyPointer
except ImportError:
    KeyPointer = None

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports"
SITE = ROOT / "site"
ARCHIVE = ROOT / "data" / "items.json"
STATE_FILE = ROOT / "state.json"
FEEDS_FILE = ROOT / "feeds.yaml"
ARCHIVE_DAYS = 90

UA = ("Mozilla/5.0 (compatible; ThinkTankTracker/1.0; "
      "+research aggregation; contact: you@example.com)")
HEADERS = {"User-Agent": UA, "Accept": "*/*"}
TIMEOUT = 20
socket.setdefaulttimeout(25)   # feedparser 内部走 urllib,没有这行会卡死

# 探测不到 <link rel=alternate> 时依次尝试的常见路径
COMMON_PATHS = [
    "/feed", "/feed/", "/rss", "/rss/", "/rss.xml", "/feed.xml", "/atom.xml",
    "/index.xml", "/rss/feed", "/feeds/all.rss.xml", "/rss/all.xml",
    "/en/feed", "/en/feed/", "/en/rss", "/en/rss.xml", "/en/index.xml",
    "/news/feed", "/news/rss", "/news/rss.xml", "/news/feed/",
    "/publications/feed", "/publications/rss", "/research/feed",
    "/blog/feed", "/blog/feed/", "/blog/rss.xml", "/blog?format=rss",
    "/?feed=rss2", "/?format=rss", "/rss_en", "/rss/news", "/rss/news.xml",
    "/de/rss.xml", "/fr/rss.xml", "/es/feed/", "/ja/rss.xml",
]


# ──────────────────────────── 基础工具 ────────────────────────────

def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=200)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": {}, "last_run": None}


def save_state(state: dict) -> None:
    # 只保留 60 天内的去重记录,避免文件无限增长
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def load_archive() -> list[dict]:
    if ARCHIVE.exists():
        try:
            return json.loads(ARCHIVE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return []


def save_archive(items: list[dict]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_DAYS)
    kept, seen = [], set()
    for it in sorted(items, key=lambda x: (x.get("published_utc")
                                          or x.get("first_seen") or ""), reverse=True):
        if it.get("fp") in seen:
            continue
        seen.add(it.get("fp"))
        pub = it.get("published_utc")
        if pub and datetime.fromisoformat(pub) < cutoff:
            continue
        kept.append(it)
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")


def needs_translation(it: dict) -> bool:
    """判断这条是否还需要翻译。

    除了"字段为空",还要识别旧版本留下的脏数据:那时翻译失败会把英文原文
    直接写进 summary_zh / title_zh,看起来像翻过了,其实没有。
    """
    from translate import is_chinese

    title, title_zh = it.get("title", ""), it.get("title_zh", "")
    if not title_zh:
        return True
    if title_zh == title and not is_chinese(title):
        return True

    summary, summary_zh = it.get("summary", ""), it.get("summary_zh", "")
    if summary and not summary_zh:
        return True
    if summary and summary_zh == summary and not is_chinese(summary):
        return True
    return False


def fingerprint(link: str, title: str) -> str:
    return hashlib.sha1(f"{link}|{title}".encode("utf-8")).hexdigest()[:16]


def strip_html(raw: str, limit: int = 320) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def looks_like_feed(content: bytes) -> bool:
    head = content[:1500].lower()
    return b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head


# ──────────────────────────── 订阅源发现 ────────────────────────────

def _verify(url: str, log: list) -> bool:
    """实际请求并解析,能取到条目才算数。同时把结果记进诊断日志。"""
    try:
        parsed = feedparser.parse(url, request_headers=HEADERS)
    except Exception as e:
        log.append(f"{url} → 解析异常 {type(e).__name__}")
        return False
    status = getattr(parsed, "status", "?")
    n = len(parsed.entries)
    if n:
        log.append(f"{url} → 可用,{n} 条")
        return True
    log.append(f"{url} → HTTP {status},0 条")
    return False


def discover_one(src: dict, session: requests.Session,
                 use_news_fallback: bool = True,
                 candidates: dict | None = None) -> dict:
    name, home = src["name"], src["homepage"]
    log: list[str] = []
    verified: list[str] = []
    seen: set[str] = set()

    def take(urls):
        for u in urls:
            if u in seen or len(verified) >= 5:
                continue
            seen.add(u)
            if _verify(u, log):
                verified.append(u)

    # 1) 先试候选清单
    take((candidates or {}).get(name, []))

    # 2) 读官网首页的 RSS 声明
    base = home
    if not verified:
        try:
            r = session.get(home, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            base = str(r.url)
            log.append(f"首页 {base} → HTTP {r.status_code}")
            if r.ok and r.content:
                soup = BeautifulSoup(r.content, "html.parser")
                declared = []
                for tag in soup.find_all("link", rel=lambda v: v and "alternate" in str(v).lower()):
                    ty, href = (tag.get("type") or "").lower(), tag.get("href")
                    if href and ("rss" in ty or "atom" in ty or "xml" in ty):
                        declared.append(urljoin(base, href))
                for a in soup.find_all("a", href=True):
                    h = a["href"].lower()
                    if h.endswith((".rss", "/rss", "/feed", "rss.xml", "atom.xml", "feed.xml")):
                        declared.append(urljoin(base, a["href"]))
                log.append(f"首页声明了 {len(declared)} 个候选")
                take(declared)
        except requests.RequestException as e:
            log.append(f"首页 {home} → 请求失败 {type(e).__name__}")

    # 3) 挨个试常见路径
    if not verified:
        root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
        hits = []
        for path in COMMON_PATHS:
            url = root + path
            try:
                rr = session.get(url, headers=HEADERS, timeout=TIMEOUT)
                if rr.ok and looks_like_feed(rr.content):
                    hits.append(url)
            except requests.RequestException:
                continue
        log.append(f"常见路径命中 {len(hits)} 个")
        take(hits)

    # 4) 最后用 Google News 站内检索兜底
    via_news = False
    if not verified and use_news_fallback:
        domain = urlparse(base).netloc.replace("www.", "")
        gn = ("https://news.google.com/rss/search?q=site:"
              f"{domain}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
        if _verify(gn, log):
            verified, via_news = [gn], True

    return {
        "name": name,
        "homepage": home,
        "region": src.get("region", ""),
        "default_domain": src.get("default_domain", "geopolitics"),
        "feeds": verified,
        "via_news_fallback": via_news,
        "status": ("ok" if verified and not via_news
                   else "ok_via_news" if via_news else "no_feed_found"),
        "diagnostics": log[-12:],
        "note": src.get("note", ""),
    }


def cmd_discover(args) -> None:
    sources = load_yaml(ROOT / "sources.yaml")["sources"]
    if args.only:
        wanted = [s.strip().lower() for s in args.only.split(",")]
        sources = [s for s in sources
                   if any(w in s["name"].lower() for w in wanted)]

    kf = ROOT / "known_feeds.yaml"
    candidates = load_yaml(kf).get("candidates", {}) if kf.exists() else {}
    if candidates:
        print(f"已加载 {len(candidates)} 家机构的候选地址(会逐个验证,猜错的自动丢弃)\n")

    existing = load_yaml(FEEDS_FILE).get("feeds", {}) if FEEDS_FILE.exists() else {}
    results: dict[str, dict] = dict(existing)

    session = requests.Session()
    print(f"开始探测 {len(sources)} 家机构的订阅源…\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(discover_one, s, session,
                                   not args.no_news_fallback, candidates): s for s in sources}
        for i, fut in enumerate(as_completed(futures), 1):
            src = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"name": src["name"], "homepage": src["homepage"],
                       "region": src.get("region", ""),
                       "default_domain": src.get("default_domain", "geopolitics"),
                       "feeds": [], "status": f"error: {e}", "note": ""}
            # 已手工锁定的源不覆盖
            prev = existing.get(res["name"])
            if prev and prev.get("locked"):
                results[res["name"]] = prev
            else:
                results[res["name"]] = res
            mark = "✓" if res["feeds"] else "✗"
            print(f"[{i:3}/{len(sources)}] {mark} {res['name']}"
                  + (f"  →  {res['feeds'][0]}" if res["feeds"] else "  (未找到 RSS)"))
            if not res["feeds"]:
                for line in res.get("diagnostics", [])[-4:]:
                    print(f"          {line}")

    save_yaml(FEEDS_FILE, {"feeds": results})
    ok = sum(1 for v in results.values() if v.get("feeds"))
    print(f"\n完成。{ok}/{len(results)} 家机构有可用订阅源,已写入 {FEEDS_FILE.name}")
    print("未找到 RSS 的机构:请手工在 feeds.yaml 里补 feeds 字段并加 locked: true,"
          "或参考 README 里的 HTML 抓取 / Google News 兜底方案。")


# ──────────────────────────── 抓取 ────────────────────────────

def classify(title: str, summary: str, topics: dict, fallback: str) -> str:
    text = f"{title} {summary}".lower()
    best, best_score = fallback, 0
    for code, cfg in topics.items():
        score = sum(1 for kw in cfg["keywords"] if kw.lower() in text)
        if score > best_score:
            best, best_score = code, score
    return best


def entry_time(entry) -> tuple[datetime | None, bool]:
    """返回 (UTC 时间, 是否含具体时分)。源里没给时间就返回 (None, False)。"""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tm = entry.get(key)
        if tm:
            dt = datetime.fromtimestamp(time.mktime(tm), tz=timezone.utc)
            raw = entry.get(key.replace("_parsed", "")) or ""
            # 只有日期的字符串通常不含 ':' 
            has_clock = ":" in str(raw)
            return dt, has_clock
    return None, False


def fetch_feed(name: str, meta: dict, url: str) -> list[dict]:
    items = []
    try:
        parsed = feedparser.parse(url, request_headers=HEADERS)
    except Exception:
        return items
    for e in parsed.entries[:60]:
        link = e.get("link") or ""
        title = strip_html(e.get("title", ""), 300)
        if not link or not title:
            continue
        summary = strip_html(e.get("summary") or e.get("description") or
                             (e.get("content", [{}])[0].get("value") if e.get("content") else ""))
        dt, has_clock = entry_time(e)
        items.append({
            "institution": name,
            "region": meta.get("region", ""),
            "default_domain": meta.get("default_domain", "geopolitics"),
            "title": title,
            "link": link,
            "summary": summary,
            "published_utc": dt.isoformat() if dt else None,
            "has_clock_time": has_clock,
            "feed": url,
        })
    return items


def cmd_fetch(args) -> None:
    if not FEEDS_FILE.exists():
        sys.exit("找不到 feeds.yaml,请先运行:python tracker.py discover")

    feeds_cfg = load_yaml(FEEDS_FILE)["feeds"]
    topics = load_yaml(ROOT / "topics.yaml")["domains"]
    state = load_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.hours)

    jobs = [(name, meta, url) for name, meta in feeds_cfg.items()
            for url in meta.get("feeds", [])]
    print(f"抓取 {len(jobs)} 个订阅源(窗口:最近 {args.hours} 小时)…")

    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_feed, n, m, u) for n, m, u in jobs]
        for fut in as_completed(futures):
            try:
                raw.extend(fut.result())
            except Exception:
                continue

    fresh, undated = [], {}
    for it in raw:
        fp = fingerprint(it["link"], it["title"])
        if fp in state["seen"]:
            continue
        if it["published_utc"]:
            pub = datetime.fromisoformat(it["published_utc"])
            if pub < cutoff:
                continue          # 太旧,不算新消息
        else:
            # 源站没给发布时间。直接丢掉会漏掉大量条目,所以靠指纹去重收进来,
            # 只在首轮对每个源设上限,避免把整个历史列表一次性灌进来。
            if args.skip_undated:
                continue
            undated[it["feed"]] = undated.get(it["feed"], 0) + 1
            if undated[it["feed"]] > args.max_undated:
                continue
        it["fp"] = fp
        it["first_seen"] = now.isoformat()
        it["domain"] = classify(it["title"], it["summary"], topics, it["default_domain"])
        fresh.append(it)

    fresh.sort(key=lambda x: (x["published_utc"] or x.get("first_seen") or "",
                              x["institution"]), reverse=True)

    archive = load_archive() + fresh
    if args.translate != "off":
        tr = Translator(backend=args.translate, model=args.model)
        pending = [it for it in archive if needs_translation(it)]
        if len(pending) > args.max_translate:
            print(f"待翻 {len(pending)} 条,本轮先翻 {args.max_translate} 条,"
                  f"其余下一轮继续(防止一次性产生意外费用)。")
            pending = pending[:args.max_translate]
        if pending:
            if len(pending) > len(fresh):
                print(f"发现 {len(pending) - len(fresh)} 条历史条目还没有中文,一并补翻。")
            tr.apply(pending, titles=not args.keep_original_titles)
    if args.keypoints != "off" and KeyPointer is None:
        print("提示:没有找到 keypoints.py,跳过要点提炼(不影响抓取和翻译)。")
    elif args.keypoints != "off":
        kp = KeyPointer(mode=args.keypoints, model=args.model)
        if kp.mode == "on":
            need = [it for it in archive if not it.get("points_zh")]
            kp.apply(need, limit=args.max_keypoints)
    for it in archive:
        it.setdefault("points_zh", [])
    save_archive(archive)

    for it in fresh:
        state["seen"][it["fp"]] = now.timestamp()
    state["last_run"] = now.isoformat()
    save_state(state)

    OUT.mkdir(exist_ok=True)
    stamp = now.astimezone().strftime("%Y%m%d-%H%M")
    formats = [f.strip() for f in args.format.split(",")]
    written = []
    if "json" in formats:
        p = OUT / f"report-{stamp}.json"
        p.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)
    if "md" in formats:
        p = OUT / f"report-{stamp}.md"
        p.write_text(render_md(fresh, topics, now, args.hours), encoding="utf-8")
        written.append(p)
    if "html" in formats:
        p = OUT / f"report-{stamp}.html"
        p.write_text(render_html(fresh, topics, now, args.hours), encoding="utf-8")
        written.append(p)

    if not args.no_site:
        page = build_site(load_archive(), topics, SITE, state["last_run"])
        written.append(page)

    print(f"\n本轮新增 {len(fresh)} 条,库内累计 {len(load_archive())} 条。输出:")
    for p in written:
        print("  ", p)


def cmd_check_translate(args) -> None:
    from translate import self_test
    if not self_test(args.translate, args.model):
        print("::warning::翻译链路不通,这一轮会显示原文。任务本身继续。")
    # 诊断步骤,永远以成功退出,不让整个任务变红


def cmd_translate(args) -> None:
    """把库里所有还没有中文的条目补翻一遍,然后重建网页。"""
    topics = load_yaml(ROOT / "topics.yaml")["domains"]
    archive = load_archive()
    pending = [it for it in archive if needs_translation(it)]
    if args.all:
        pending = archive
    if not pending:
        print("库里所有条目都已有中文,无需补翻。")
    else:
        print(f"待补翻 {len(pending)} 条。")
        Translator(backend=args.translate, model=args.model).apply(
            pending, titles=not args.keep_original_titles)
        save_archive(archive)
    page = build_site(load_archive(), topics, SITE, load_state().get("last_run"))
    print("已生成", page)


def cmd_keypoints(args) -> None:
    if KeyPointer is None:
        sys.exit("缺少 keypoints.py,无法提炼要点。把该文件上传到仓库后再试。")
    """给库里还没有要点的条目补提炼,然后重建网页。"""
    topics = load_yaml(ROOT / "topics.yaml")["domains"]
    archive = load_archive()
    need = [it for it in archive if not it.get("points_zh")]
    if not need:
        print("库里所有条目都已有中文要点。")
    else:
        KeyPointer(mode="on", model=args.model).apply(need, limit=args.max_keypoints)
        save_archive(archive)
    print("已生成", build_site(load_archive(), topics, SITE,
                             load_state().get("last_run")))


def cmd_build(args) -> None:
    topics = load_yaml(ROOT / "topics.yaml")["domains"]
    state = load_state()
    page = build_site(load_archive(), topics, SITE, state.get("last_run"))
    print("已生成", page)


def fmt_time(it: dict) -> str:
    if not it["published_utc"]:
        if it.get("first_seen"):
            fs = datetime.fromisoformat(it["first_seen"]).astimezone()
            return fs.strftime("%Y-%m-%d %H:%M") + "(源站无发布时间,此为首次抓到的时间)"
        return "发布时间未提供"
    dt = datetime.fromisoformat(it["published_utc"]).astimezone()
    if it["has_clock_time"]:
        return dt.strftime("%Y-%m-%d %H:%M") + f" ({dt.tzname()})"
    return dt.strftime("%Y-%m-%d") + "(源站未提供时分)"


def render_md(items: list[dict], topics: dict, now: datetime, hours: int) -> str:
    local = now.astimezone()
    lines = [f"# 智库动态简报",
             f"",
             f"生成时间:{local.strftime('%Y-%m-%d %H:%M')} · "
             f"覆盖窗口:最近 {hours} 小时 · 新增条目:{len(items)}",
             f""]
    if not items:
        lines.append("_本轮窗口内没有抓到新发布。_")
        return "\n".join(lines)

    by_domain: dict[str, list[dict]] = {}
    for it in items:
        by_domain.setdefault(it["domain"], []).append(it)

    order = [c for c in topics if c in by_domain]
    lines.append("**目录**:" + " · ".join(
        f"{topics[c]['label']}({len(by_domain[c])})" for c in order))
    lines.append("")

    for code in order:
        lines.append(f"## {topics[code]['label']}")
        lines.append("")
        for it in by_domain[code]:
            zh_title = it.get("title_zh") or it["title"]
            lines.append(f"### {zh_title}")
            if zh_title != it["title"]:
                lines.append(f"原标题:{it['title']}")
                lines.append("")
            lines.append(f"- **发布机构**:{it['institution']}"
                         + (f"（{it['region']}）" if it["region"] else ""))
            lines.append(f"- **发布时间**:{fmt_time(it)}")
            lines.append(f"- **原文链接**:{it['link']}")
            lines.append(f"- **简介**:{it.get('summary_zh') or it['summary'] or '源站未提供摘要'}")
            if it.get("points_zh"):
                lines.append("- **主要观点**:")
                for pt in it["points_zh"]:
                    lines.append(f"  - {pt}")
            lines.append("")
    return "\n".join(lines)


def render_html(items: list[dict], topics: dict, now: datetime, hours: int) -> str:
    local = now.astimezone()
    by_domain: dict[str, list[dict]] = {}
    for it in items:
        by_domain.setdefault(it["domain"], []).append(it)
    order = [c for c in topics if c in by_domain]

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    body = []
    for code in order:
        body.append(f'<h2>{esc(topics[code]["label"])} '
                    f'<span class="count">{len(by_domain[code])}</span></h2>')
        for it in by_domain[code]:
            zh_title = it.get("title_zh") or it["title"]
            orig = (f'<p class="orig">{esc(it["title"])}</p>'
                    if zh_title != it["title"] else "")
            body.append(
                '<article>'
                f'<h3><a href="{esc(it["link"])}" target="_blank" rel="noopener">'
                f'{esc(zh_title)}</a></h3>'
                + orig
                + f'<p class="meta">{esc(it["institution"])}'
                + (f'，{esc(it["region"])}' if it["region"] else "")
                + f'　{esc(fmt_time(it))}</p>'
                f'<p class="sum">'
                f'{esc(it.get("summary_zh") or it["summary"]) or "源站未提供摘要"}</p>'
                + ("<ul class='pts'>" +
                   "".join(f"<li>{esc(pt)}</li>" for pt in it["points_zh"]) + "</ul>"
                   if it.get("points_zh") else "")
                + '</article>')
    inner = "\n".join(body) or "<p>本轮窗口内没有抓到新发布。</p>"
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>智库动态简报 {local.strftime('%Y-%m-%d %H:%M')}</title>
<style>
 body{{max-width:860px;margin:2rem auto;padding:0 1rem;
      font:16px/1.65 -apple-system,"Segoe UI","Noto Sans SC",sans-serif;color:#1a1a1a}}
 h1{{font-size:1.6rem;margin-bottom:.2rem}}
 .top{{color:#666;font-size:.9rem;margin-bottom:2rem}}
 h2{{margin-top:2.5rem;padding-bottom:.4rem;border-bottom:2px solid #e3e3e3;font-size:1.2rem}}
 .count{{color:#888;font-weight:400;font-size:.85rem}}
 article{{padding:.9rem 0;border-bottom:1px solid #f0f0f0}}
 h3{{font-size:1rem;margin:0 0 .35rem}}
 a{{color:#1a4fa0;text-decoration:none}} a:hover{{text-decoration:underline}}
 .orig{{color:#8a8f95;font-size:.82rem;margin:0 0 .25rem}}
 .meta{{color:#777;font-size:.82rem;margin:0 0 .4rem}}
 .sum{{margin:0;color:#333;font-size:.92rem}}
 .pts{{margin:.5rem 0 0;padding-left:1.15rem;color:#333;font-size:.9rem}}
 .pts li{{margin:.2rem 0}}
</style>
<h1>智库动态简报</h1>
<p class="top">生成时间 {local.strftime('%Y-%m-%d %H:%M')} · 覆盖窗口 最近 {hours} 小时 · 新增 {len(items)} 条</p>
{inner}
</html>"""


def cmd_status(args) -> None:
    if not FEEDS_FILE.exists():
        sys.exit("还没有 feeds.yaml,请先运行 discover。")
    feeds = load_yaml(FEEDS_FILE)["feeds"]
    direct = {k: v for k, v in feeds.items()
              if v.get("feeds") and not v.get("via_news_fallback")}
    news = [k for k, v in feeds.items() if v.get("via_news_fallback")]
    bad = [k for k, v in feeds.items() if not v.get("feeds")]
    print(f"官方 RSS:{len(direct)} 家  |  Google News 兜底:{len(news)} 家  "
          f"|  完全没有源:{len(bad)} 家  |  合计 {len(feeds)} 家")
    print(f"订阅源总数:{sum(len(v['feeds']) for v in feeds.values() if v.get('feeds'))}")

    archive = load_archive()
    if archive:
        covered = {it["institution"] for it in archive}
        silent = [k for k in feeds if k not in covered]
        print(f"\n库内条目 {len(archive)} 条,来自 {len(covered)} 家机构。")
        if silent:
            print(f"以下 {len(silent)} 家至今一条都没抓到:")
            for n in silent:
                print("  -", n, "(无订阅源)" if n in bad else "")
    if bad:
        print(f"\n以下 {len(bad)} 家连兜底源都没有。每家附最后几条探测记录,"
              f"用来判断是网站没有 RSS,还是访问被拦:")
        for n in bad:
            print(f"  - {n}")
            for line in feeds[n].get("diagnostics", [])[-3:]:
                print(f"      {line}")


def main() -> None:
    ap = argparse.ArgumentParser(description="智库动态追踪器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="自动探测各机构 RSS/Atom 源")
    d.add_argument("--only", help="只探测名称含该关键词的机构,逗号分隔")
    d.add_argument("--workers", type=int, default=10)
    d.add_argument("--no-news-fallback", action="store_true",
                   help="没有 RSS 的机构不使用 Google News 兜底")
    d.set_defaults(func=cmd_discover)

    f = sub.add_parser("fetch", help="抓取新条目并生成分类报告")
    f.add_argument("--hours", type=int, default=6, help="时间窗口,默认 6 小时")
    f.add_argument("--format", default="md,html,json")
    f.add_argument("--workers", type=int, default=16)
    f.add_argument("--skip-undated", action="store_true",
                   help="丢弃源站没给发布时间的条目(默认收录,靠指纹去重)")
    f.add_argument("--max-undated", type=int, default=15,
                   help="每个源每轮最多收录多少条无时间戳的条目,默认 15")
    f.add_argument("--translate", default="auto", choices=["auto", "claude", "google", "off"],
                   help="简介翻译后端,默认 auto(有 ANTHROPIC_API_KEY 用 claude,否则用 google)")
    f.add_argument("--model", default="claude-haiku-4-5-20251001", help="claude 后端使用的模型")
    f.add_argument("--keep-original-titles", action="store_true",
                   help="只翻简介,标题保留原文(能省掉约三分之一费用)")
    f.add_argument("--max-translate", type=int, default=400,
                   help="每轮最多翻译多少条,默认 400,积压的顺延到下一轮")
    f.add_argument("--keypoints", default="auto", choices=["auto", "on", "off"],
                   help="抓原文提炼中文要点,默认 auto(有 ANTHROPIC_API_KEY 才开)")
    f.add_argument("--max-keypoints", type=int, default=120,
                   help="每轮最多提炼多少条,默认 120,其余下轮继续")
    f.add_argument("--no-site", action="store_true", help="本轮不重建网页")
    f.set_defaults(func=cmd_fetch)

    ct = sub.add_parser("check-translate", help="翻一句样例,确认翻译链路是通的")
    ct.add_argument("--translate", default="auto", choices=["auto", "claude", "google"])
    ct.add_argument("--model", default="claude-haiku-4-5-20251001")
    ct.set_defaults(func=cmd_check_translate)

    tl = sub.add_parser("translate", help="给库里缺中文的条目补翻并重建网页")
    tl.add_argument("--translate", default="auto",
                    choices=["auto", "claude", "google", "off"])
    tl.add_argument("--model", default="claude-haiku-4-5-20251001")
    tl.add_argument("--keep-original-titles", action="store_true")
    tl.add_argument("--all", action="store_true", help="不管有没有中文,全部重翻")
    tl.set_defaults(func=cmd_translate)

    kp = sub.add_parser("keypoints", help="给库里缺要点的条目补提炼并重建网页")
    kp.add_argument("--model", default="claude-haiku-4-5-20251001")
    kp.add_argument("--max-keypoints", type=int, default=200)
    kp.set_defaults(func=cmd_keypoints)

    b = sub.add_parser("build", help="只用已有数据重建网页")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("status", help="查看订阅源覆盖情况")
    s.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
