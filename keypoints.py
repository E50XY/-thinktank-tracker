"""抓原文正文,让 Claude 提炼中文要点。

RSS 里很多条目只有标题、没有摘要,光靠翻译什么都得不到。这个模块补这一环:
  1. 打开条目的原文链接,抽出正文
  2. 交给 Claude 生成一句话中文概述 + 3~5 条中文要点
  3. 按链接缓存,同一篇永远只做一次

只支持 Claude 后端 —— 提炼观点是理解任务,机器翻译做不了。
没有配置 ANTHROPIC_API_KEY 时整个功能自动跳过,不影响其他环节。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CACHE_PATH = Path(__file__).resolve().parent / "data" / "keypoints.json"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
UA = "Mozilla/5.0 (compatible; ThinkTankTracker/1.0; research aggregation)"
MAX_CHARS = 6000          # 送给模型的正文上限
MIN_CHARS = 350           # 正文太短就别浪费调用

SYSTEM = (
    "你是一名国际政策研究分析师。用户会给你一篇智库或国际组织出版物的正文。\n"
    "请输出一个 JSON 对象,只含两个字段:\n"
    '  "summary":一句话中文概述,不超过 60 字,说明这篇东西讲了什么。\n'
    '  "points":3 到 5 条中文要点组成的数组,每条一句话,不超过 45 字。\n'
    "要求:\n"
    "1. 只写原文实际给出的判断、数据和结论,不补充背景知识,不做评价,不推测。\n"
    "2. 优先保留具体结论、关键数字、政策建议,不要写「本文讨论了……」这种空话。\n"
    "3. 专业术语用中文政策研究界的通行译法,机构名用通用中译名。\n"
    "4. 如果正文是导航页、订阅页或内容过少无法提炼,返回 "
    '{"summary":"","points":[]}。\n'
    "只输出 JSON,不要代码块标记,不要任何说明文字。"
)

DROP_TAGS = ["script", "style", "nav", "header", "footer", "aside", "form",
             "noscript", "iframe", "svg"]


def _key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


# ──────────────────────────── 正文抽取 ────────────────────────────

def extract_text(html: bytes | str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    # 优先找语义化的正文容器,找不到再退回全页
    node = (soup.find("article") or soup.find("main")
            or soup.find(attrs={"role": "main"}) or soup.body or soup)
    paras = [p.get_text(" ", strip=True) for p in node.find_all(["p", "li", "h2", "h3"])]
    paras = [p for p in paras if len(p) > 40]          # 滤掉导航碎片
    text = "\n".join(paras) or node.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:MAX_CHARS]


def fetch_article(url: str, timeout: int = 25) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                     allow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()
    if "html" not in ctype:                            # PDF 等暂不处理
        return ""
    return extract_text(r.content)


# ──────────────────────────── 调模型 ────────────────────────────

def _post(url: str, **kw):
    return requests.post(url, timeout=90, **kw)


def summarize(text: str, title: str, api_key: str, model: str) -> dict:
    payload = {
        "model": model,
        "max_tokens": 800,
        "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": f"标题:{title}\n\n正文:\n{text}"}],
    }
    r = _post(ANTHROPIC_URL, json=payload, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    r.raise_for_status()
    raw = "".join(b.get("text", "") for b in r.json().get("content", [])
                  if b.get("type") == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    out = json.loads(raw)
    return {"summary": str(out.get("summary", "")).strip(),
            "points": [str(x).strip() for x in out.get("points", []) if str(x).strip()]}


# ──────────────────────────── 对外接口 ────────────────────────────

class KeyPointer:
    def __init__(self, mode: str = "auto", model: str = DEFAULT_MODEL,
                 workers: int = 6, verbose: bool = True):
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if mode == "auto":
            mode = "on" if self.api_key else "off"
        if mode == "on" and not self.api_key:
            raise SystemExit("要提炼中文要点需要 ANTHROPIC_API_KEY,没有找到。")
        self.mode = mode
        self.model = model
        self.workers = workers
        self.verbose = verbose
        self.cache = load_cache()
        self.done = self.skipped = self.failed = 0

    def _one(self, it: dict) -> tuple[str, dict | None]:
        url = it["link"]
        try:
            text = fetch_article(url)
            if len(text) < MIN_CHARS:
                return url, {"summary": "", "points": [], "skip": "正文太短或非网页"}
            res = summarize(text, it.get("title", ""), self.api_key, self.model)
            return url, res
        except Exception as e:
            return url, {"error": str(e)[:120]}

    def apply(self, items: list[dict], limit: int = 120) -> None:
        """给条目加 points_zh;原摘要为空时顺便补上 summary_zh。"""
        if self.mode == "off":
            for it in items:
                it.setdefault("points_zh", [])
            return

        todo = []
        for it in items:
            it.setdefault("points_zh", [])
            hit = self.cache.get(_key(it["link"]))
            if hit:
                it["points_zh"] = hit.get("points", [])
                if hit.get("summary") and not it.get("summary_zh"):
                    it["summary_zh"] = hit["summary"]
            else:
                todo.append(it)

        if len(todo) > limit:
            if self.verbose:
                print(f"待提炼 {len(todo)} 条,本轮只做 {limit} 条,其余下轮继续。")
            todo = todo[:limit]
        if not todo:
            return
        if self.verbose:
            print(f"抓取原文并提炼中文要点:{len(todo)} 条…")

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._one, it): it for it in todo}
            for fut in as_completed(futures):
                it = futures[fut]
                try:
                    url, res = fut.result()
                except Exception:
                    self.failed += 1
                    continue
                if res is None or "error" in res:
                    self.failed += 1
                    continue
                if res.get("skip"):
                    self.skipped += 1
                    self.cache[_key(url)] = {"summary": "", "points": []}
                    continue
                it["points_zh"] = res["points"]
                if res["summary"] and not it.get("summary_zh"):
                    it["summary_zh"] = res["summary"]
                self.cache[_key(url)] = res
                self.done += 1

        save_cache(self.cache)
        if self.verbose:
            print(f"要点提炼完成:成功 {self.done} 条,"
                  f"跳过 {self.skipped} 条(正文抓不到),失败 {self.failed} 条。")
