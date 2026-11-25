# -*- coding: utf-8 -*-
"""个股消息面：东财新闻搜索 + 东财公告，供量化结果做一致性校验。

只抓取公开标题/摘要，不做全文抓取；新闻情绪先用关键词做规则初筛，
最终是否支持量化价格与走势，交给本机大模型结合标题和摘要判断。
"""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

import config

CACHE_PATH = config.DATA_DIR / "news_cache.json"
CACHE_TTL = 30 * 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

POS = ("增持", "回购", "中标", "签约", "预增", "扭亏", "利好", "突破", "合作",
       "投产", "获批", "分红", "超预期", "新高", "订单", "扩产", "涨价")
NEG = ("减持", "亏损", "立案", "问询", "处罚", "诉讼", "质押", "解禁", "风险",
       "下调", "终止", "违约", "跌停", "警示", "调查", "商誉", "冻结", "退市")


def _clean(text, limit=180):
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    text = html.unescape(text).replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _http_json(url, referer, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "ignore").strip()
    if raw.startswith("(") or raw.startswith("cb("):
        raw = raw[raw.find("(") + 1:raw.rfind(")")]
    return json.loads(raw)


def _tone(text):
    p = sum(1 for w in POS if w in text)
    n = sum(1 for w in NEG if w in text)
    if p > n:
        return "偏多", p - n
    if n > p:
        return "偏空", p - n
    return "中性", 0


def _parse_dt(text):
    text = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(datetime.now().strftime(fmt))], fmt)
        except Exception:
            continue
    return None


def _news_search(name, limit=8):
    param = {
        "uid": "", "keyword": name, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": limit,
        }},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    data = _http_json(url, "https://so.eastmoney.com/")
    rows = (((data.get("result") or {}).get("cmsArticleWebOld")) or [])
    out = []
    for r in rows[:limit]:
        title = _clean(r.get("title"), 120)
        if not title:
            continue
        content = _clean(r.get("content"), 200)
        if content.startswith("数据来源") or len(content) < 8:
            content = ""
        tone, score = _tone(title + " " + content)
        out.append({
            "time": str(r.get("date") or "")[:16],
            "title": title,
            "source": _clean(r.get("mediaName"), 20) or "新闻",
            "url": str(r.get("url") or ""),
            "snippet": content,
            "tone": tone,
            "tone_score": score,
            "kind": "news",
        })
    return out


def _announcements(code, limit=6):
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?"
           "sr=-1&page_size=%d&page_index=1&ann_type=A&client_source=web&stock_list=%s"
           % (limit, code))
    data = _http_json(url, "https://data.eastmoney.com/")
    rows = ((data.get("data") or {}).get("list")) or []
    out = []
    for r in rows[:limit]:
        title = _clean(r.get("title") or r.get("title_ch"), 140)
        if not title:
            continue
        tone, score = _tone(title)
        out.append({
            "time": str(r.get("notice_date") or r.get("display_time") or "")[:16],
            "title": title,
            "source": "公告",
            "url": "https://data.eastmoney.com/notices/stock/%s.html" % code,
            "snippet": "",
            "tone": tone,
            "tone_score": score,
            "kind": "announcement",
        })
    return out


def _summarize(items):
    pos = sum(1 for x in items if x["tone"] == "偏多")
    neg = sum(1 for x in items if x["tone"] == "偏空")
    neutral = len(items) - pos - neg
    if pos >= neg + 2:
        bias = "偏多"
    elif neg >= pos + 2:
        bias = "偏空"
    else:
        bias = "中性"
    reasons = []
    for x in items:
        if x["tone"] == "偏多":
            reasons.append("偏多:" + x["title"][:28])
        elif x["tone"] == "偏空":
            reasons.append("偏空:" + x["title"][:28])
        if len(reasons) >= 4:
            break
    return {
        "bias": bias,
        "positive": pos,
        "negative": neg,
        "neutral": neutral,
        "summary": "近30天消息 %d 条，规则初筛%s（偏多%d/偏空%d/中性%d）"
                   % (len(items), bias, pos, neg, neutral),
        "reasons": reasons,
    }


def fetch_news(code, name, limit=8, force=False):
    """返回新闻/公告、规则情绪和缓存时间。接口失败时返回空列表，不阻断分析。"""
    code = str(code or "")
    now = time.time()
    cache = {}
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(code)
    if hit and not force and now - float(hit.get("ts") or 0) < CACHE_TTL:
        return hit.get("data") or {"ok": False, "items": [], "summary": "消息面缓存为空"}

    items = []
    errors = []
    try:
        items.extend(_news_search(name, limit))
    except Exception as e:  # noqa: BLE001
        errors.append("新闻:" + str(e)[:80])
    try:
        items.extend(_announcements(code, 5))
    except Exception as e:  # noqa: BLE001
        errors.append("公告:" + str(e)[:80])

    # 去重 + 只保留近30天；若没有近期消息，保留最新几条供参考
    seen = {}
    for x in items:
        key = re.sub(r"\s+", "", x["title"])[:60]
        if key and key not in seen:
            seen[key] = x
    items = list(seen.values())
    cutoff = now - 30 * 86400
    recent = []
    for x in items:
        dt = _parse_dt(x["time"])
        if dt and dt.timestamp() >= cutoff:
            recent.append(x)
    if recent:
        items = recent
    items.sort(key=lambda x: x["time"], reverse=True)
    items = items[:12]

    summary = _summarize(items) if items else {
        "bias": "未知", "positive": 0, "negative": 0, "neutral": 0,
        "summary": "消息面接口暂不可用或近30天无相关新闻", "reasons": [],
    }
    data = {
        "ok": bool(items),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "items": items,
        **summary,
        "errors": errors,
        "note": "情绪为关键词规则初筛，最终需由模型结合标题/摘要判断，不能替代公告全文。",
    }
    cache[code] = {"ts": now, "data": data}
    # 清理 7 天前的缓存
    for k in list(cache.keys()):
        try:
            if now - float(cache[k].get("ts") or 0) > 7 * 86400:
                cache.pop(k, None)
        except Exception:
            cache.pop(k, None)
    try:
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(CACHE_PATH))
    except OSError:
        pass
    return data
