# -*- coding: utf-8 -*-
"""单只股票搜索与按买入价分析。"""
import json
import os
import time
from datetime import datetime

import config
import datasource as ds
import engine
import forecast as forecast_mod
import news as news_mod

SYMBOLS_PATH = config.DATA_DIR / "symbols.json"
INDUSTRIES_PATH = config.DATA_DIR / "industries.json"
INDUSTRY_MAP_PATH = config.DATA_DIR / "industry_map.json"

_cache = {"mtime": 0, "items": []}
_fc_cache = {}


def save_symbols(rows):
    """把可交易股票的代码/名称/行业写入缓存，供页面搜索。"""
    items = []
    for r in rows:
        code = str(r.get("f12") or "")
        name = str(r.get("f14") or "").strip()
        if not code or not name or not engine.board_allowed(code):
            continue
        items.append([code, name, int(engine.num(r.get("f13"))), str(r.get("f100") or "未分类")])
    _write(SYMBOLS_PATH, {"date": datetime.now().strftime("%Y-%m-%d"), "items": items})
    _cache["mtime"] = 0
    return len(items)


def save_industries(industries):
    items = []
    for it in engine.industry_sort(industries):
        items.append({
            "name": str(it.get("f14") or ""),
            "pct": round(engine.num(it.get("f3")), 2),
            "main": engine.num(it.get("f62")),
        })
    _write(INDUSTRIES_PATH, {"date": datetime.now().strftime("%Y-%m-%d"), "items": items})
    return len(items)


def _write(path, obj):
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def symbols():
    try:
        mtime = SYMBOLS_PATH.stat().st_mtime
    except OSError:
        mtime = 0
    if mtime and mtime == _cache["mtime"]:
        return _cache["items"]
    items = []
    try:
        obj = json.loads(SYMBOLS_PATH.read_text(encoding="utf-8"))
        items = obj.get("items") or []
    except Exception:
        items = []
    if not items:
        try:
            rows, _total, _source = ds.fetch_snapshot()
            save_symbols(rows)
            obj = json.loads(SYMBOLS_PATH.read_text(encoding="utf-8"))
            items = obj.get("items") or []
        except Exception:
            items = []
    _cache["items"] = items
    _cache["mtime"] = mtime
    return items


def lookup(code):
    code = str(code or "").strip()
    for item in symbols():
        if item[0] == code:
            return {"code": item[0], "name": item[1], "market": item[2], "industry": item[3]}
    return None


def search(q, limit=15):
    q = str(q or "").strip().lower()
    if not q:
        return []
    try:
        industry_map = json.loads(INDUSTRY_MAP_PATH.read_text(encoding="utf-8")).get("data") or {}
    except Exception:
        industry_map = {}
    exact, prefix, name_start, name_has = [], [], [], []
    for code, name, market, industry in symbols():
        plain = name.replace(" ", "").lower()
        if not industry or industry == "未分类":
            industry = industry_map.get(code) or industry
        row = {"code": code, "name": name, "market": market, "industry": industry}
        if code == q:
            exact.append(row)
        elif code.startswith(q):
            prefix.append(row)
        elif plain.startswith(q):
            name_start.append(row)
        elif q in plain:
            name_has.append(row)
    return (exact + prefix + name_start + name_has)[:limit]


def _industry_of(code, fallback):
    if fallback and fallback != "未分类":
        return fallback
    try:
        obj = json.loads(INDUSTRY_MAP_PATH.read_text(encoding="utf-8"))
        return (obj.get("data") or {}).get(code) or "未分类"
    except Exception:
        return "未分类"


def _sector_score(name):
    """返回 (0-1 板块强度, 板块涨幅%)，优先用上一轮缓存的行业排名。"""
    try:
        obj = json.loads(INDUSTRIES_PATH.read_text(encoding="utf-8"))
        items = obj.get("items") or []
    except Exception:
        items = []
    if items:
        n = max(len(items), 1)
        for i, it in enumerate(items):
            if it.get("name") == name:
                return 1 - i / n, it.get("pct", 0)
        return 0.5, 0
    try:
        payload = json.loads(config.LATEST_PATH.read_text(encoding="utf-8"))
        for key, val in (("top", 0.85), ("bottom", 0.15)):
            for s in (payload.get("sectors") or {}).get(key) or []:
                if s.get("name") == name:
                    return val, s.get("pct", 0)
    except Exception:
        pass
    return 0.5, 0


def _market_context():
    try:
        payload = json.loads(config.LATEST_PATH.read_text(encoding="utf-8"))
        m = payload.get("market") or {}
        return {
            "regime": m.get("regime"),
            "up": m.get("up"), "down": m.get("down"),
            "up_ratio": m.get("up_ratio"),
            "median_pct": m.get("median_pct"),
            "amount_yi": round((m.get("amount") or 0) / 1e8, 0),
            "generated_at": payload.get("generated_at"),
        }
    except Exception:
        return {}


def _forecast_for(code, kl, index_kl, price):
    """2 分钟缓存预测结果；只缓存收益率，现价变化时重新换算价位。"""
    now = time.time()
    hit = _fc_cache.get(code)
    if hit and now - hit[0] < 120:
        data = dict(hit[1])
        med = data.get("median_return", 0) / 100
        lo = data.get("low_return", 0) / 100
        hi = data.get("high_return", 0) / 100
        data.update({
            "expected_price": round(price * (1 + med), 2),
            "low_price": round(price * (1 + lo), 2),
            "high_price": round(price * (1 + hi), 2),
        })
        return data
    data = forecast_mod.forecast_2d(kl, index_kl, price)
    _fc_cache[code] = (now, data)
    return data


def _verdict(price, buy, plan):
    if buy > plan["target1"]:
        level, text = "追高", "买入价已高于第一目标位，短线空间有限，不建议在此位置新开仓。"
    elif buy > price * 1.05:
        level, text = "追高", "买入价明显高于现价，短线追高，盈亏比大幅下降。"
    elif buy > price * 1.02:
        level, text = "偏高", "买入价高于现价，接近追高；建议等回踩买区再介入。"
    elif buy > plan["buy_high"]:
        level, text = "偏高", "买入价略高于量化买区，可轻仓或等回踩确认。"
    elif buy >= plan["buy_low"]:
        level, text = "合理", "买入价位于量化买区，可按计划分批，跌破止损离场。"
    elif buy >= plan["stop"]:
        level, text = "偏低", "低于量化买区但仍在止损上方，等待企稳信号再介入。"
    else:
        level, text = "风险", "买入价已低于止损位，属于下跌途中接刀，不建议。"
    rr = (plan["target1"] - buy) / max(buy - plan["stop"], 0.01)
    upside = (plan["target1"] / buy - 1) * 100 if buy else 0
    downside = (plan["stop"] / buy - 1) * 100 if buy else 0
    return {
        "level": level,
        "text": text,
        "rr": round(rr, 2),
        "upside": round(upside, 2),
        "downside": round(downside, 2),
    }


def analyze(code, buy_price=None):
    code = str(code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise ValueError("请输入 6 位股票代码")
    if not engine.board_allowed(code):
        raise ValueError("该股票属于创业板/科创板/北交所，已按当前设置过滤")
    market = 1 if code.startswith(("6", "9")) else 0
    meta = lookup(code) or {"code": code, "name": code, "market": market, "industry": "未分类"}

    quotes = ds.fetch_tencent_quotes([(code, market)])
    q = quotes.get(code)
    if not q or not q.get("price"):
        raise ValueError("未获取到实时行情（可能停牌或代码不存在）")
    kl = ds.fetch_kline("%d.%s" % (market, code), lmt=500)
    if not kl or len(kl) < 30:
        raise ValueError("日K数据不足，暂无法分析")
    try:
        ff = ds.fetch_fflow_sina(code, market, 6)
    except Exception:
        ff = []

    industry = _industry_of(code, meta.get("industry"))
    sp, sector_pct = _sector_score(industry)
    price = q["price"]
    closes = [k["close"] for k in kl]
    base_idx = -61 if len(closes) >= 61 else 0
    pct60 = (price / closes[base_idx] - 1) * 100 if closes[base_idx] else 0

    raw = {
        "f2": price, "f3": q["pct"], "f6": q["amount"], "f8": q["turnover"],
        "f10": q["volratio"], "f12": code, "f13": market, "f14": meta.get("name") or q.get("name"),
        "f15": q["high"], "f16": q["low"], "f17": q["open"], "f18": q["prev"],
        "f20": q.get("totalcap") or q["floatcap"], "f21": q["floatcap"], "f24": pct60,
        "f100": industry,
    }
    s = engine.analyze_stock(engine.enrich(raw), kl, ff, sp)
    plan = s["plan"]
    buy = engine.num(buy_price, 0)
    if buy <= 0:
        buy = price
    verdict = _verdict(price, buy, plan)

    try:
        index_kl = ds.fetch_kline("1.000001", lmt=500)
    except Exception:
        index_kl = []
    try:
        fc = _forecast_for(code, kl, index_kl, price)
    except Exception as e:  # noqa: BLE001
        fc = {"error": "预测模型暂不可用：%s" % str(e)[:100]}
    try:
        sell = forecast_mod.build_sell_plan(s, s["km"], plan, fc, buy, kl)
    except Exception as e:  # noqa: BLE001
        sell = {"error": "卖出模型暂不可用：%s" % str(e)[:100]}
    try:
        news_data = news_mod.fetch_news(code, meta.get("name") or q.get("name"))
    except Exception as e:  # noqa: BLE001
        news_data = {"ok": False, "items": [], "summary": "消息面暂不可用：%s" % str(e)[:80]}

    return {
        "ok": True,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "腾讯实时行情 · 腾讯日K · 新浪资金流 · 东财新闻/公告",
        "stock": {
            "code": code, "name": meta.get("name") or q.get("name"), "industry": industry,
            "price": round(price, 2), "pct": round(q["pct"], 2),
            "amount": round(q["amount"] / 1e8, 2), "turnover": round(q["turnover"], 2),
            "volratio": round(q["volratio"], 2), "floatcap": round(q["floatcap"] / 1e8, 1),
            "sector_pct": sector_pct,
        },
        "buy_price": round(buy, 2),
        "score": s["score"],
        "factors": s["factors"],
        "intent": s["intent"],
        "intent_detail": s["intent_detail"],
        "flow5": round(s["flow5"] / 1e8, 2),
        "main_pct": round(s["main_pct"], 2),
        "super_pct": round(s["super_pct"], 2),
        "km": s["km"],
        "plan": plan,
        "flags": s["flags"],
        "confidence": s["confidence"],
        "spark": s["spark"],
        "verdict": verdict,
        "forecast": fc,
        "sell_plan": sell,
        "news": news_data,
        "market": _market_context(),
        "disclaimer": config.DISCLAIMER,
    }
