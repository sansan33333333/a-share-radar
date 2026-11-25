# -*- coding: utf-8 -*-
"""横截面训练面板：维护一批流动性较好的沪深主板股票日K。

为什么需要它：
- 候选池原来只有 60 只、约 70 日 K 线，只能做单票时间序列，无法学习
  “同一天哪些股票未来 3 日更强”的横截面规律；
- 短线量化更接近排序问题，而不是给单只股票猜一个绝对涨幅。这里维护
  约 260 只主板股票、最多 500 个交易日的前复权日K，供 ranker.py 做
  走步训练和横截面排序。

面板只做数据缓存，不产生交易信号。数据源仍是腾讯日K，按交易日增量更新。
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import config
import datasource as ds
import kline_store

PANEL_PATH = config.DATA_DIR / "panel_cache.json"
PANEL_SIZE = 260
PANEL_DAYS = 500
MAX_WORKERS = 6


def _num(v, default=0.0):
    try:
        if v in (None, "", "-"):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_st(name):
    n = str(name or "").upper()
    return "ST" in n or "退" in n or n.startswith(("N", "C"))


def board_allowed(code):
    code = str(code or "")
    if code.startswith(("4", "8", "9", "30", "68")):
        return False
    return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def _list_days(listdate):
    try:
        d = datetime.strptime(str(listdate), "%Y%m%d")
        return (datetime.now() - d).days
    except Exception:
        return 9999


def select_universe(rows, size=PANEL_SIZE):
    """从当日全A快照中选流动性较好的主板股票。

    注意：这会产生“当前成分股”幸存者偏差。面板用途是学习横截面因子，
    不是回测某个历史指数；后续可改为历史成分股快照。页面和文档会说明。
    """
    mapping = {}
    try:
        obj = json.loads((config.DATA_DIR / "industry_map.json").read_text(encoding="utf-8"))
        mapping = obj.get("data") or {}
    except Exception:
        mapping = {}

    out = []
    for r in rows or []:
        code = str(r.get("f12") or "")
        name = str(r.get("f14") or "")
        price = _num(r.get("f2"))
        amount = _num(r.get("f6"))
        floatcap = _num(r.get("f21"))
        turnover = _num(r.get("f8"))
        listdate = str(r.get("f26") or "")
        if not board_allowed(code) or not price or _is_st(name):
            continue
        if not (3.0 <= price <= 120.0):
            continue
        if amount < 1.2e8:
            continue
        if floatcap and not (20e8 <= floatcap <= 900e8):
            continue
        if turnover and not (1.0 <= turnover <= 25.0):
            continue
        if listdate and _list_days(listdate) < 90:
            continue
        out.append({
            "code": code,
            "name": name,
            "market": int(_num(r.get("f13"))),
            "industry": (str(r.get("f100") or "").strip()
                         if str(r.get("f100") or "").strip() not in ("", "未分类")
                         else (mapping.get(code) or "未分类")),
            "amount": amount,
        })
    out.sort(key=lambda x: x["amount"], reverse=True)
    return out[:size]


def _load():
    try:
        obj = json.loads(PANEL_PATH.read_text(encoding="utf-8"))
        if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
            return obj
        if isinstance(obj, dict) and obj.get("storage") == "sqlite":
            obj["data"] = {x["code"]: kline_store.read("%d.%s" % (int(x["market"]), x["code"]))
                           for x in obj.get("universe", [])}
            return obj
    except Exception:
        pass
    return {"updated": "", "universe": [], "data": {}}


def _save(cache):
    if PANEL_PATH.exists():
        backup = PANEL_PATH.with_suffix(".legacy.json")
        if not backup.exists():
            backup.write_bytes(PANEL_PATH.read_bytes())
    tmp = PANEL_PATH.with_suffix(".tmp")
    metadata = {k: v for k, v in cache.items() if k != "data"}
    metadata["storage"] = "sqlite"
    tmp.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(PANEL_PATH))


def _merge(old, new, limit=PANEL_DAYS):
    by_date = {str(k.get("date")): k for k in (old or []) if k.get("date")}
    for k in new or []:
        if k.get("date"):
            by_date[str(k["date"])] = k
    return [by_date[d] for d in sorted(by_date)][-limit:]


def ensure_panel(rows, trade_date, size=PANEL_SIZE, force=False, log=None):
    """确保面板覆盖当日并返回 cache。只在新交易日或股票不足时发请求。"""
    trade_date = str(trade_date or "")
    kline_store.migrate()
    cache = _load()
    universe = select_universe(rows, size=size)
    # 保留旧成分，避免每天换样本导致训练面板断裂
    old = {str(x.get("code")): x for x in cache.get("universe") or []}
    merged = dict(old)
    for x in universe:
        merged[x["code"]] = x
    universe = list(merged.values())
    cache["universe"] = universe
    data = cache.get("data") or {}

    data = kline_store.load([(x["code"], int(x["market"])) for x in universe], trade_date, force=force, log=log)
    cache.update(data=data, updated=trade_date, universe=universe)
    _save(cache)
    return cache

def bars_for(cache, code):
    return (cache.get("data") or {}).get(str(code)) or []
