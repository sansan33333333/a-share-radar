# -*- coding: utf-8 -*-
"""公告事件风险过滤：把"煌上煌式"的公告利空挡在买入名单之外。

背景：2026-11-10 煌上煌控股股东密集减持（两周4笔大宗+竞价），11-10 晚公告
权益变动触及1%整数倍，11-12/11-13 两连跌停。技术形态（旱地拔葱形态分93）
对此完全失明。本模块把公告标题纳入筛选层，而不是只做展示。

设计边界（诚实声明）：
- 只读东财公告标题做规则分类，不抓全文、不做语义判断，会有漏判和误判
- "block"级事件强制候选进入观察池（仓位0%），旱地拔葱沉底；不是删除，人工仍可复核
- 规则针对高频踩雷事件（控股股东减持、立案、退市风险、冻结、违约）；
  一般减持/质押/解禁/预亏只标 warn，不否决买入
- 缓存 data/events_cache.json，每代码 TTL 默认 6 小时；公告盘后集中披露，
  盘中不重复抓取，收盘后 15:05 以后的新公告下一轮生效
"""
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import config

CACHE_PATH = config.DATA_DIR / "events_cache.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 配置（可被 config.py 覆盖）
LOOKBACK_DAYS = getattr(config, "EVENT_LOOKBACK_DAYS", 30)
CACHE_TTL = getattr(config, "EVENT_CACHE_TTL", 6 * 3600)
ENABLED = getattr(config, "EVENT_FILTER_ENABLED", True)
PAGE_SIZE = 40

# 排除假阳性：含这些词的标题即便命中关键词也不算利空
NEGATE = ("不减持", "终止减持", "取消减持", "暂缓减持", "提前终止减持",
          "解除质押", "回购", "注销", "澄清", "更正")

# (关键词组, 级别, 事件名)。一条标题命中多个时取最严级别。
RULES = [
    (("立案",), "block", "被立案调查"),
    (("退市风险警示",), "block", "退市风险警示"),
    (("终止上市", "退市"), "block", "终止/退市风险"),
    (("冻结",), "block", "股份/资产被冻结"),
    (("违约",), "block", "债务违约"),
    (("警示函", "监管函"), "warn", "收到监管函件"),
    (("问询", "关注函"), "warn", "收到问询/关注函"),
    (("商誉减值",), "warn", "商誉减值风险"),
    (("质押",), "warn", "股份质押"),
    (("解禁",), "warn", "限售解禁"),
    (("预亏", "预减", "业绩下滑", "亏损"), "warn", "业绩利空"),
    (("担保",), "warn", "对外担保"),
    (("减持",), "warn", "股东减持"),
]


def _clean(text, limit=140):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://data.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def fetch_announcements(code, page_size=PAGE_SIZE):
    """拉取个股公告标题列表：[{date: 'YYYY-MM-DD HH:MM', title: ...}]。"""
    code = str(code or "")
    if not re.fullmatch(r"\d{6}", code):
        return []
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?"
           "sr=-1&page_size=%d&page_index=1&ann_type=A&client_source=web&stock_list=%s"
           % (page_size, code))
    data = _http_json(url)
    rows = ((data.get("data") or {}).get("list")) or []
    out = []
    for r in rows:
        title = _clean(r.get("title") or r.get("title_ch"))
        if not title:
            continue
        out.append({
            "date": str(r.get("notice_date") or r.get("display_time") or "")[:16],
            "title": title,
        })
    return out


def classify_titles(items, days=LOOKBACK_DAYS, as_of=None):
    """纯规则分类（可离线测试）。items: [{date,title}]。

    返回 {"level": none|warn|block, "reasons": [...], "hits": [{date,title,level,event}]}
    只看最近 days 天；命中多条取最严级别。
    """
    now = as_of or datetime.now()
    cutoff = now - timedelta(days=days)
    level_rank = {"none": 0, "warn": 1, "block": 2}
    level, reasons, hits = "none", [], []
    for it in items:
        title = str(it.get("title") or "")
        date_raw = str(it.get("date") or "")
        try:
            dt = datetime.strptime(date_raw[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if dt < cutoff:
            continue
        if any(w in title for w in NEGATE):
            continue
        # 控股股东/一致行动人 减持或权益变动 → block（煌上煌模式）
        holder = ("控股股东" in title or "实控人" in title or "实际控制人" in title
                  or "一致行动人" in title or "持股5%以上" in title)
        if holder and any(w in title for w in ("减持", "权益变动")):
            ev = "控股股东/一致行动人减持或权益变动"
            hits.append({"date": date_raw[:10], "title": title, "level": "block", "event": ev})
            if level_rank["block"] > level_rank[level]:
                level = "block"
            if ev not in reasons:
                reasons.append("%s（%s）" % (ev, dt.strftime("%m-%d")))
            continue
        for words, lv, ev in RULES:
            if any(w in title for w in words):
                hits.append({"date": date_raw[:10], "title": title, "level": lv, "event": ev})
                if level_rank[lv] > level_rank[level]:
                    level = lv
                label = ("%s（%s）" % (ev, dt.strftime("%m-%d")))
                if label not in reasons:
                    reasons.append(label)
                break
    hits.sort(key=lambda h: h["date"], reverse=True)
    return {
        "level": level,
        "reasons": reasons[:5],
        "hits": hits[:8],
    }


def _load_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache):
    now = time.time()
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


def assess(code, days=LOOKBACK_DAYS, force=False):
    """单个代码的事件风险评估（带缓存）。接口失败时 level=unknown，不阻断分析。"""
    code = str(code or "")
    now = time.time()
    if not ENABLED:
        return {"code": code, "level": "none", "reasons": [], "hits": [],
                "as_of": None, "note": "事件过滤已关闭"}
    cache = _load_cache()
    hit = cache.get(code)
    if hit and not force and now - float(hit.get("ts") or 0) < CACHE_TTL:
        return hit.get("data") or {"code": code, "level": "unknown", "reasons": []}
    try:
        items = fetch_announcements(code)
        result = classify_titles(items, days=days)
        result.update({"code": code, "ok": True,
                       "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "n_ann": len(items)})
    except Exception as e:  # noqa: BLE001
        result = {"code": code, "ok": False, "level": "unknown",
                  "reasons": [], "hits": [], "error": str(e)[:100]}
    cache[code] = {"ts": now, "data": result}
    _save_cache(cache)
    return result


def annotate_batch(codes, days=LOOKBACK_DAYS, workers=6, log=None):
    """批量评估，返回 {code: result}。失败/关闭时代码缺失或 unknown。"""
    codes = [str(c) for c in dict.fromkeys(codes) if c]
    if not ENABLED or not codes:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(assess, c, days): c for c in codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                out[c] = fut.result()
            except Exception as e:  # noqa: BLE001
                out[c] = {"code": c, "ok": False, "level": "unknown",
                          "reasons": [], "error": str(e)[:100]}
    n_block = sum(1 for v in out.values() if v.get("level") == "block")
    n_warn = sum(1 for v in out.values() if v.get("level") == "warn")
    if log:
        log("公告事件过滤：%d 只，block %d，warn %d" % (len(out), n_block, n_warn))
    return out


def short_reason(ev):
    """给前端/复核展示用的短句。"""
    if not ev or not isinstance(ev, dict):
        return ""
    reasons = ev.get("reasons") or []
    return reasons[0] if reasons else ""
