"""把抓到的标题和摘要翻成中文。

两个后端:
  claude  —— 用 Anthropic API(需要 API key)。质量最好,术语准,批量调用很便宜。
  google  —— 用 Google 翻译的免费接口,不需要 key。质量一般,偶尔限流。
  off     —— 不翻译。

默认 auto:检测到环境变量 ANTHROPIC_API_KEY 就用 claude,否则退回 google。
翻译结果永久缓存在 data/translations.json,同一段原文只翻一次,不会重复花钱。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

CACHE_PATH = Path(__file__).resolve().parent / "data" / "translations.json"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
GOOGLE_URL = ("https://translate.googleapis.com/translate_a/single"
              "?client=gtx&sl=auto&tl=zh-CN&dt=t&q={q}")
BATCH = 20          # 每次 API 调用翻译多少段
CJK = re.compile(r"[\u4e00-\u9fff]")

SYSTEM = (
    "你是一名国际政策研究领域的译者。把用户给出的每一条智库出版物标题或摘要翻译成简体中文。\n"
    "要求:\n"
    "1. 专业术语按中文政策研究界的通行译法,机构名和专有名词用通用中译名,没有通用译名的保留原文。\n"
    "2. 忠实直译,不增补原文没有的信息,不做评论,不加引号。\n"
    "3. 保持原文的详略程度,不要扩写或压缩。\n"
    "4. 原文若已是中文,原样返回。\n"
    "输出格式:只输出一个 JSON 数组,元素顺序与输入严格对应,每个元素是对应译文的字符串。"
    "不要输出 Markdown 代码块标记,不要输出任何说明文字。"
)


# ──────────────────────────── 缓存 ────────────────────────────

def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


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


def is_chinese(text: str) -> bool:
    """中文字符占比超过三成就认为不用翻。"""
    if not text:
        return True
    letters = [c for c in text if c.isalpha() or CJK.match(c)]
    if not letters:
        return True
    return sum(1 for c in letters if CJK.match(c)) / len(letters) > 0.3


# ──────────────────────────── 后端 ────────────────────────────

def _post(url: str, **kw):
    """单独抽出来,方便测试时替换。"""
    return requests.post(url, timeout=90, **kw)


def _get(url: str, **kw):
    return requests.get(url, timeout=30, **kw)


def translate_claude(texts: list[str], api_key: str, model: str) -> list[str]:
    payload = {
        "model": model,
        "max_tokens": 4000,
        "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": json.dumps(texts, ensure_ascii=False)}],
    }
    headers = {"x-api-key": api_key,
               "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    r = _post(ANTHROPIC_URL, headers=headers, json=payload)
    r.raise_for_status()
    body = r.json()
    raw = "".join(b.get("text", "") for b in body.get("content", [])
                  if b.get("type") == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    out = json.loads(raw)
    if not isinstance(out, list) or len(out) != len(texts):
        raise ValueError(f"译文条数对不上:期望 {len(texts)},实得 {len(out)}")
    return [str(x) for x in out]


def translate_google(texts: list[str]) -> list[str]:
    out = []
    for t in texts:
        try:
            r = _get(GOOGLE_URL.format(q=quote(t[:4500])))
            r.raise_for_status()
            out.append("".join(seg[0] for seg in r.json()[0] if seg and seg[0]))
        except Exception:
            out.append(t)          # 失败就保留原文,不中断整轮抓取
        time.sleep(0.25)           # 轻微节流,避免触发限流
    return out


# ──────────────────────────── 对外接口 ────────────────────────────

class Translator:
    def __init__(self, backend: str = "auto", model: str = DEFAULT_MODEL,
                 verbose: bool = True):
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if backend == "auto":
            backend = "claude" if self.api_key else "google"
        if backend == "claude" and not self.api_key:
            raise SystemExit("选了 claude 后端但没有找到 ANTHROPIC_API_KEY 环境变量。")
        self.backend = backend
        self.model = model
        self.verbose = verbose
        self.cache = load_cache()
        self.calls = 0
        self.failed = 0

    def _run(self, texts: list[str]) -> list[str]:
        if self.backend == "claude":
            return translate_claude(texts, self.api_key, self.model)
        return translate_google(texts)

    def translate_many(self, texts: list[str]) -> dict[str, str]:
        """返回 {原文: 译文}。已缓存或已是中文的不再调用接口。"""
        if self.backend == "off":
            return {t: t for t in texts}

        result, todo = {}, []
        for t in dict.fromkeys(texts):      # 去重并保序
            if not t:
                continue
            if is_chinese(t):
                result[t] = t
            elif (k := _key(t)) in self.cache:
                result[t] = self.cache[k]
            else:
                todo.append(t)

        if todo and self.verbose:
            print(f"翻译 {len(todo)} 段新文本(后端:{self.backend},"
                  f"缓存命中 {len(result)} 段)…")

        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            for attempt in range(3):
                try:
                    zh = self._run(chunk)
                    for src, dst in zip(chunk, zh):
                        result[src] = dst
                        self.cache[_key(src)] = dst
                    break
                except Exception as e:
                    if attempt == 2:
                        self.failed += len(chunk)
                        if self.verbose:
                            print(f"  这一批翻译失败,保留原文({e})")
                        for src in chunk:
                            result[src] = src
                    else:
                        time.sleep(2 * (attempt + 1))
            self.calls += 1

        save_cache(self.cache)
        return result

    def apply(self, items: list[dict], titles: bool = True) -> None:
        """给每条加上 summary_zh / title_zh 字段(原地修改)。"""
        pool = [it["summary"] for it in items if it.get("summary")]
        if titles:
            pool += [it["title"] for it in items if it.get("title")]
        mapping = self.translate_many(pool)
        for it in items:
            it["summary_zh"] = mapping.get(it.get("summary", ""), it.get("summary", ""))
            it["title_zh"] = (mapping.get(it.get("title", ""), it["title"])
                              if titles else it["title"])
        if self.verbose and self.backend != "off":
            note = f",{self.failed} 段失败保留原文" if self.failed else ""
            print(f"翻译完成:接口调用 {self.calls} 次{note}。")
