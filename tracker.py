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

# 探测不到 <link rel=alternate> 时依次尝试的常见路径
COMMON_PATHS = [
    "/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml",
    "/rss/feed", "/en/rss.xml", "/en/feed", "/news/rss", "/news/feed",
    "/?feed=rss2", "/publications/feed", "/blog/feed", "/rss/all.xml",
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
    for it in sorted(items, key=lambda x: (x.get("published_utc") or ""), reverse=True):
        if it.get("fp") in seen:
            continue
        seen.add(it.get("fp"))
        pub = it.get("published_utc")
        if pub and datetime.fromisoformat(pub) < cutoff:
            continue
        kept.append(it)
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")


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

def discover_one(src: dict, session: requests.Session) -> dict:
    """返回 {name, homepage, feeds: [...], status}"""
    name, home = src["name"], src["homepage"]
    found: list[str] = []

    try:
        r = session.get(home, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        base = str(r.url)
        if r.ok and r.content:
            soup = BeautifulSoup(r.content, "html.parser")
            for tag in soup.find_all("link", rel=lambda v: v and "alternate" in str(v).lower()):
                t = (tag.get("type") or "").lower()
                href = tag.get("href")
                if href and ("rss" in t or "atom" in t or "xml" in t):
                    found.append(urljoin(base, href))
            # 有些站点只在 <a> 上挂 feed 链接
            if not found:
                for a in soup.find_all("a", href=True):
                    h = a["href"].lower()
                    if h.endswith((".rss", "/rss", "/feed", "rss.xml", "atom.xml", "feed.xml")):
                        found.append(urljoin(base, a["href"]))
    except requests.RequestException:
        base = home

    if not found:
        root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
        for path in COMMON_PATHS:
            url = root + path
            try:
                rr = session.get(url, headers=HEADERS, timeout=TIMEOUT)
                if rr.ok and looks_like_feed(rr.content):
                    found.append(url)
                    break
            except requests.RequestException:
                continue

    # 去重 + 校验能否解析出条目
    verified, seen = [], set()
    for url in found:
        if url in seen:
            continue
        seen.add(url)
        try:
            parsed = feedparser.parse(url, request_headers=HEADERS)
            if parsed.entries:
                verified.append(url)
        except Exception:
            continue
        if len(verified) >= 3:
            break

    return {
        "name": name,
        "homepage": home,
        "region": src.get("region", ""),
        "default_domain": src.get("default_domain", "geopolitics"),
        "feeds": verified,
        "status": "ok" if verified else "no_feed_found",
        "note": src.get("note", ""),
    }


def cmd_discover(args) -> None:
    sources = load_yaml(ROOT / "sources.yaml")["sources"]
    if args.only:
        wanted = [s.strip().lower() for s in args.only.split(",")]
        sources = [s for s in sources
                   if any(w in s["name"].lower() for w in wanted)]

    existing = load_yaml(FEEDS_FILE).get("feeds", {}) if FEEDS_FILE.exists() else {}
    results: dict[str, dict] = dict(existing)

    session = requests.Session()
    print(f"开始探测 {len(sources)} 家机构的订阅源…\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(discover_one, s, session): s for s in sources}
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

    fresh = []
    for it in raw:
        fp = fingerprint(it["link"], it["title"])
        if fp in state["seen"]:
            continue
        if it["published_utc"]:
            pub = datetime.fromisoformat(it["published_utc"])
            if pub < cutoff:
                continue          # 太旧,不算新消息
        elif not args.include_undated:
            continue              # 无时间戳的条目默认跳过(可用 --include-undated 放行)
        it["fp"] = fp
        it["domain"] = classify(it["title"], it["summary"], topics, it["default_domain"])
        fresh.append(it)

    fresh.sort(key=lambda x: (x["published_utc"] or "", x["institution"]), reverse=True)
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

    archive = load_archive() + fresh
    save_archive(archive)
    if not args.no_site:
        page = build_site(load_archive(), topics, SITE, state["last_run"])
        written.append(page)

    print(f"\n本轮新增 {len(fresh)} 条,库内累计 {len(load_archive())} 条。输出:")
    for p in written:
        print("  ", p)


def cmd_build(args) -> None:
    topics = load_yaml(ROOT / "topics.yaml")["domains"]
    state = load_state()
    page = build_site(load_archive(), topics, SITE, state.get("last_run"))
    print("已生成", page)


def fmt_time(it: dict) -> str:
    if not it["published_utc"]:
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
            lines.append(f"### {it['title']}")
            lines.append(f"- **发布机构**:{it['institution']}"
                         + (f"（{it['region']}）" if it["region"] else ""))
            lines.append(f"- **发布时间**:{fmt_time(it)}")
            lines.append(f"- **原文链接**:{it['link']}")
            lines.append(f"- **简介**:{it['summary'] or '(源站未提供摘要)'}")
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
            body.append(
                '<article>'
                f'<h3><a href="{esc(it["link"])}" target="_blank" rel="noopener">'
                f'{esc(it["title"])}</a></h3>'
                f'<p class="meta">{esc(it["institution"])}'
                + (f' · {esc(it["region"])}' if it["region"] else "")
                + f' · {esc(fmt_time(it))}</p>'
                f'<p class="sum">{esc(it["summary"]) or "(源站未提供摘要)"}</p>'
                '</article>')
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
 .meta{{color:#777;font-size:.82rem;margin:0 0 .4rem}}
 .sum{{margin:0;color:#333;font-size:.92rem}}
</style>
<h1>智库动态简报</h1>
<p class="top">生成时间 {local.strftime('%Y-%m-%d %H:%M')} · 覆盖窗口 最近 {hours} 小时 · 新增 {len(items)} 条</p>
{inner}
</html>"""


def cmd_status(args) -> None:
    if not FEEDS_FILE.exists():
        sys.exit("还没有 feeds.yaml,请先运行 discover。")
    feeds = load_yaml(FEEDS_FILE)["feeds"]
    ok = {k: v for k, v in feeds.items() if v.get("feeds")}
    bad = [k for k, v in feeds.items() if not v.get("feeds")]
    print(f"有订阅源:{len(ok)} 家 / 共 {len(feeds)} 家")
    print(f"订阅源总数:{sum(len(v['feeds']) for v in ok.values())}")
    if bad:
        print(f"\n以下 {len(bad)} 家需手工补源:")
        for n in bad:
            print("  -", n)


def main() -> None:
    ap = argparse.ArgumentParser(description="智库动态追踪器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="自动探测各机构 RSS/Atom 源")
    d.add_argument("--only", help="只探测名称含该关键词的机构,逗号分隔")
    d.add_argument("--workers", type=int, default=10)
    d.set_defaults(func=cmd_discover)

    f = sub.add_parser("fetch", help="抓取新条目并生成分类报告")
    f.add_argument("--hours", type=int, default=6, help="时间窗口,默认 6 小时")
    f.add_argument("--format", default="md,html,json")
    f.add_argument("--workers", type=int, default=16)
    f.add_argument("--include-undated", action="store_true",
                   help="把没有时间戳的条目也算作新条目(靠去重判断)")
    f.add_argument("--no-site", action="store_true", help="本轮不重建网页")
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("build", help="只用已有数据重建网页")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("status", help="查看订阅源覆盖情况")
    s.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
