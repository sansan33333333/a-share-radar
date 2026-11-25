# -*- coding: utf-8 -*-
"""多因子评分、主力意图识别与交易计划生成。"""
import math
from datetime import datetime

import config
import ranker
import signal_guard


def num(v, default=0.0):
    try:
        if v in (None, "-", ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def enrich(r):
    price = num(r.get("f2"))
    return {
        "code": str(r.get("f12") or ""),
        "market": int(num(r.get("f13"))),
        "name": str(r.get("f14") or ""),
        "price": price,
        "pct": num(r.get("f3")),
        "amount": num(r.get("f6")),
        "turnover": num(r.get("f8")),
        "volratio": num(r.get("f10")),
        "high": num(r.get("f15")),
        "low": num(r.get("f16")),
        "open": num(r.get("f17")),
        "prev": num(r.get("f18")),
        "mktcap": num(r.get("f20")),
        "floatcap": num(r.get("f21")),
        "pct60": num(r.get("f24")),
        "pct_ytd": num(r.get("f25")),
        "listdate": str(r.get("f26") or ""),
        "main": num(r.get("f62")),
        "main_pct": num(r.get("f184")),
        "super": num(r.get("f66")),
        "super_pct": num(r.get("f69")),
        "big": num(r.get("f72")),
        "big_pct": num(r.get("f75")),
        "mid": num(r.get("f78")),
        "mid_pct": num(r.get("f81")),
        "small": num(r.get("f84")),
        "small_pct": num(r.get("f87")),
        "industry": str(r.get("f100") or "未分类"),
        "concepts": str(r.get("f103") or ""),
    }


def is_st(name):
    n = name.upper()
    return "ST" in n or "退" in n or n.startswith("N") or n.startswith("C")


def limit_pct(code, name):
    if is_st(name):
        return 4.8
    if code.startswith(("30", "68")):
        return 19.7
    return 9.7


def list_days(listdate):
    try:
        d = datetime.strptime(listdate, "%Y%m%d")
        return (datetime.now() - d).days
    except Exception:
        return 9999


def board_allowed(code):
    code = str(code or "")
    if code.startswith(("4", "8", "9")):
        return False
    if not config.ALLOW_CHINEXT and code.startswith(("300", "301", "302")):
        return False
    if not config.ALLOW_STAR and code.startswith(("688", "689")):
        return False
    return True


def eligible(s):
    if not s["code"] or not s["price"]:
        return False
    if is_st(s["name"]):
        return False
    if not board_allowed(s["code"]):
        return False
    if not (config.MIN_PRICE <= s["price"] <= config.MAX_PRICE):
        return False
    if not (config.MIN_FLOAT_CAP <= s["floatcap"] <= config.MAX_FLOAT_CAP):
        return False
    if s["amount"] < config.MIN_AMOUNT:
        return False
    if not (config.MIN_TURNOVER <= s["turnover"] <= config.MAX_TURNOVER):
        return False
    if s["volratio"] and s["volratio"] < config.MIN_VOL_RATIO:
        return False
    if not (config.MIN_PCT <= s["pct"] <= config.MAX_PCT):
        return False
    if s["high"] and s["low"] and abs(s["high"] - s["low"]) < 1e-9:
        return False
    if list_days(s["listdate"]) < config.MIN_LIST_DAYS:
        return False
    return True


def prelim_score(s, sector_pct):
    """资金 + 量价 + 板块 + 趋势代理，用于筛出进入日K精算的候选。"""
    w = config.SCORE_WEIGHTS
    fund = 0.0
    fund += clamp(s["main_pct"] * 0.9, -6, 16)
    fund += clamp(s["super_pct"] * 0.35, -3, 6)
    flow_ratio = (s["main"] / s["floatcap"] * 100) if s["floatcap"] else 0
    fund += clamp(flow_ratio * 40, 0, 8)
    fund = clamp(fund, 0, 30) * (w["fund"] / 30.0)

    vol = 0.0
    vr = s["volratio"]
    if 1.2 <= vr <= 3.0:
        vol += 8
    elif 0.9 <= vr < 1.2:
        vol += 5
    elif 3.0 < vr <= 5.0:
        vol += 3
    to = s["turnover"]
    if 3 <= to <= 12:
        vol += 8
    elif 1.5 <= to < 3 or 12 < to <= 18:
        vol += 5
    else:
        vol += 2
    p = s["pct"]
    if 1 <= p <= 5:
        vol += 7
    elif 0 <= p < 1:
        vol += 5
    elif 5 < p <= 7.5:
        vol += 3
    elif -2 <= p < 0:
        vol += 1
    vol += clamp(s["amount"] / 1e8 / 10, 0, 2)
    vol = clamp(vol, 0, 25) * (w["vol"] / 25.0)

    # 无日K时用 60 日涨幅 + 当日强弱做趋势代理
    p60 = s["pct60"]
    if p60 < -10:
        tp = 0.0
    elif p60 < 0:
        tp = 2.0
    elif p60 <= 50:
        tp = 4.0 + min(p60 / 50 * 4, 4)
    elif p60 <= 90:
        tp = 5.0
    else:
        tp = 1.0
    if s["price"] > s["open"] > 0:
        tp += 2
    tp = clamp(tp, 0, w["trend"] / 3.0)

    sector = sector_pct * w["sector"]
    return fund + vol + sector + tp, {
        "fund": round(fund, 1), "vol": round(vol, 1),
        "sector": round(sector, 1), "trend": round(tp, 1),
    }


def industry_sort(industries):
    """有资金数据按主力净额排，否则按板块涨幅排。"""
    flow = sum(abs(num(x.get("f62"))) for x in industries)
    key = (lambda x: num(x.get("f62"))) if flow > 0 else (lambda x: num(x.get("f3")))
    return sorted(industries, key=key, reverse=True)


def trend_metrics(kl):
    """指数/个股趋势快照。"""
    if not kl or len(kl) < 20:
        return None
    closes = [k["close"] for k in kl]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    pct5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0
    return {
        "price": round(closes[-1], 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "above20": closes[-1] >= ma20,
        "pct5": round(pct5, 2),
    }


def preselect(snapshot_rows, industries, limit=None):
    """粗筛出进入日K精算的候选，返回 [(code, market)]。limit 用于扩大候选池。"""
    rows = [enrich(r) for r in snapshot_rows]
    rows = [s for s in rows if s["price"] > 0]
    ind_sorted = industry_sort(industries)
    n = max(len(ind_sorted), 1)
    ind_rank = {str(it.get("f14")): 1 - i / n for i, it in enumerate(ind_sorted)}
    cands = []
    for s in rows:
        if eligible(s):
            sc, _ = prelim_score(s, ind_rank.get(s["industry"], 0.5))
            s["prelim"] = sc
            cands.append(s)
    cands.sort(key=lambda x: x["prelim"], reverse=True)
    keep = limit if limit else config.DEEP_COUNT
    return [(s["code"], s["market"]) for s in cands[:keep]], len(cands)


def apply_quotes(snapshot_rows, quotes):
    """用腾讯批量行情补齐新浪快照缺失的量比/换手/成交额。"""
    n = 0
    for r in snapshot_rows:
        q = quotes.get(str(r.get("f12") or ""))
        if not q:
            continue
        if not num(r.get("f10")) and q.get("volratio"):
            r["f10"] = q["volratio"]
            n += 1
        if not num(r.get("f8")) and q.get("turnover"):
            r["f8"] = q["turnover"]
        if not num(r.get("f6")) and q.get("amount"):
            r["f6"] = q["amount"]
    return n


def kline_metrics(kl, price):
    closes = [k["close"] for k in kl]
    highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]

    def ma(n):
        return sum(closes[-n:]) / n if len(closes) >= n else None

    ma5, ma10, ma20 = ma(5), ma(10), ma(20)
    trs = []
    for i in range(1, len(kl)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[-14:]) / min(14, len(trs)) if trs else price * 0.02
    high20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    low20 = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    mom20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0
    vol_ratio5 = 0.0
    if len(kl) >= 6:
        v5 = sum(k["volume"] for k in kl[-6:-1]) / 5
        if v5:
            vol_ratio5 = kl[-1]["volume"] / v5
    return {
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "atr": atr, "high20": high20, "low20": low20,
        "mom20": mom20, "vol_ratio5": vol_ratio5,
        "n": len(kl),
    }


def final_score(s, km, sector_pct):
    w = config.SCORE_WEIGHTS
    fund = 0.0
    fund += clamp(s["main_pct"] * 0.9, -6, 16)
    fund += clamp(s["super_pct"] * 0.35, -3, 6)
    flow_ratio = (s["main"] / s["floatcap"] * 100) if s["floatcap"] else 0
    fund += clamp(flow_ratio * 40, 0, 8)
    fund = clamp(fund, 0, 30) * (w["fund"] / 30.0)

    vol = 0.0
    vr = s["volratio"]
    if 1.2 <= vr <= 3.0:
        vol += 8
    elif 0.9 <= vr < 1.2:
        vol += 5
    elif 3.0 < vr <= 5.0:
        vol += 3
    to = s["turnover"]
    if 3 <= to <= 12:
        vol += 8
    elif 1.5 <= to < 3 or 12 < to <= 18:
        vol += 5
    else:
        vol += 2
    p = s["pct"]
    if 1 <= p <= 5:
        vol += 7
    elif 0 <= p < 1:
        vol += 5
    elif 5 < p <= 7.5:
        vol += 3
    elif -2 <= p < 0:
        vol += 1
    vol = clamp(vol, 0, 25) * (w["vol"] / 25.0)

    trend = 0.0
    price = s["price"]
    if km["ma5"] and price > km["ma5"]:
        trend += 4
    if km["ma5"] and km["ma10"] and km["ma5"] > km["ma10"]:
        trend += 3
    if km["ma10"] and km["ma20"] and km["ma10"] > km["ma20"]:
        trend += 3
    trend += clamp(km["mom20"] * 0.25, -3, 5)
    dist = (price / km["high20"] - 1) if km["high20"] else 0
    if -0.03 <= dist <= 0:
        trend += 5
    elif -0.08 <= dist < -0.03:
        trend += 2
    elif dist < -0.15:
        trend -= 2
    trend = clamp(trend, 0, 20) * (w["trend"] / 20.0)

    form = 0.0
    rng = s["high"] - s["low"]
    close_pos = ((s["price"] - s["low"]) / rng) if rng > 0 else 0.5
    if close_pos > 0.75:
        form += 4
    elif close_pos > 0.5:
        form += 2
    upper = ((s["high"] - s["price"]) / rng) if rng > 0 else 0
    if upper > 0.5:
        form -= 3
    amp = (rng / s["prev"] * 100) if s["prev"] else 0
    if amp < 5:
        form += 3
    if 0 <= s["pct60"] <= 40:
        form += 3
    elif s["pct60"] > 70:
        form -= 4
    if s["volratio"] and s["volratio"] > 5:
        form -= 2
    form = clamp(form, 0, 15) * (w["form"] / 15.0)

    sector = sector_pct * w["sector"]
    total = fund + vol + trend + sector + form
    return round(total, 1), {
        "fund": round(fund, 1), "vol": round(vol, 1), "trend": round(trend, 1),
        "sector": round(sector, 1), "form": round(form, 1),
    }


def classify_intent(s, fflow):
    p, mp = s["pct"], s["main_pct"]
    if p >= 1 and mp >= 3:
        base = "主动进攻"
    elif -2 <= p < 1 and mp >= 5:
        base = "逆势吸筹"
    elif p >= 2 and mp <= -3:
        base = "拉高出货"
    elif p <= -1 and mp <= -3:
        base = "主动减仓"
    elif mp > 0:
        base = "温和流入"
    else:
        base = "资金观望"

    flow5 = sum(x["main"] for x in fflow[-5:]) if fflow else 0
    if flow5 > 0 and s["main"] > 0:
        detail = "5日持续净流入" if all(x["main"] > 0 for x in fflow[-5:]) else "5日累计净流入"
    elif flow5 <= 0 < s["main"]:
        detail = "今日回补、5日仍净流出"
    elif flow5 < 0 and s["main"] < 0:
        detail = "5日持续净流出"
    else:
        detail = "资金分歧"
    return base, detail, flow5


def make_plan(s, km):
    price = s["price"]
    atr = max(km["atr"], price * 0.012)
    ma5, ma10 = km["ma5"], km["ma10"]

    low = max(price - atr * 0.5, (ma5 or 0) * 0.995)
    low = min(low, price * 0.995)
    high = min(price * 1.006, low + atr * 0.6)
    if high <= low:
        high = low * 1.008

    stop = low * 0.97
    if ma10 and ma10 < low:
        stop = max(stop, ma10 * 0.985)
    stop = max(stop, low * 0.92)
    # 按买入区上沿算，最大亏损不超过 MAX_STOP_PCT
    stop = max(stop, high * (1 - config.MAX_STOP_PCT / 100.0))
    stop = min(stop, low * 0.995)

    t1 = max(price * 1.045, km["high20"] * 1.005)
    t1 = min(t1, price * 1.13)
    t2 = min(max(t1 * 1.04, price * 1.12), price * 1.2)
    rr = (t1 - price) / max(price - stop, 0.01)

    pos = 6
    for th, pct in config.POSITION_TIERS:
        if (s.get("score") or 0) >= th:
            pos = pct
            break
    return {
        "buy_low": round(low, 2),
        "buy_high": round(high, 2),
        "stop": round(stop, 2),
        "target1": round(t1, 2),
        "target2": round(t2, 2),
        "rr": round(rr, 2),
        "position": "%d%%" % pos,
        "valid_days": 2,
    }


def market_stats(rows, indices):
    up = down = flat = 0
    lu = ld = 0
    pcts = []
    amount = 0.0
    for s in rows:
        p = s["pct"]
        if not s["price"]:
            continue
        pcts.append(p)
        amount += s["amount"]
        if p > 0.01:
            up += 1
        elif p < -0.01:
            down += 1
        else:
            flat += 1
        lim = limit_pct(s["code"], s["name"])
        if p >= lim - 0.2:
            lu += 1
        if p <= -(lim - 0.2):
            ld += 1
    pcts.sort()
    median = pcts[len(pcts) // 2] if pcts else 0
    up_ratio = up / max(up + down + flat, 1)
    idx = [{
        "code": str(i.get("f12") or ""),
        "name": str(i.get("f14") or ""),
        "price": num(i.get("f2")),
        "pct": num(i.get("f3")),
        "change": num(i.get("f4")),
        "amount": num(i.get("f6")),
    } for i in indices]
    if up_ratio >= 0.6 and median > 0.4:
        regime = "强势"
    elif up_ratio >= 0.45:
        regime = "偏强震荡"
    elif up_ratio >= 0.3:
        regime = "弱势震荡"
    else:
        regime = "普跌"
    return {
        "up": up, "down": down, "flat": flat, "limit_up": lu, "limit_down": ld,
        "median_pct": round(median, 2), "up_ratio": round(up_ratio, 3),
        "amount": amount, "regime": regime, "indices": idx,
    }


def rule_market_view(st):
    return (
        "%s：上涨 %d 家 / 下跌 %d 家，中位涨幅 %.2f%%，涨停 %d 家、跌停 %d 家，"
        "两市成交 %.0f 亿元。%s"
        % (
            st["regime"], st["up"], st["down"], st["median_pct"], st["limit_up"],
            st["limit_down"], st["amount"] / 1e8,
            "赚钱效应尚可，可在主线板块内低吸。"
            if st["up_ratio"] >= 0.5 else
            "分化明显，控制仓位、只做强势股回踩。"
            if st["up_ratio"] >= 0.35 else
            "普跌环境，优先防守，等待情绪修复。",
        )
    )


def analyze_stock(s, kl, ff, sector_pct):
    """对单只股票做完整打分、意图识别和交易计划。"""
    if ff and not s["main_pct"]:
        last = ff[-1]
        s["main"] = last.get("main", 0)
        s["main_pct"] = last.get("main_pct", 0)
        s["super"] = last.get("super", 0)
        s["super_pct"] = last.get("super_pct", 0)
    km = kline_metrics(kl, s["price"])
    sc, parts = final_score(s, km, sector_pct)
    base, detail, flow5 = classify_intent(s, ff)
    s["score"] = sc
    s["factors"] = parts
    s["intent"] = base
    s["intent_detail"] = detail
    s["flow5"] = flow5
    s["plan"] = make_plan(s, km)
    s["trend_guard"] = signal_guard.trend_check(kl, s["price"])
    s["confidence"] = int(clamp(sc * 0.95 + (6 if base in ("主动进攻", "逆势吸筹") else 0), 20, 95))
    s["flags"] = []
    if s["pct60"] > 60:
        s["flags"].append("60日涨幅偏大")
    if s["turnover"] > 15:
        s["flags"].append("换手过热")
    if s["volratio"] > 5:
        s["flags"].append("量比过热")
    if s["main_pct"] < 0:
        s["flags"].append("资金净流出")
    if km["ma20"] and s["price"] < km["ma20"]:
        s["flags"].append("处于20日线下方")
    s["km"] = {
        "ma5": round(km["ma5"], 2) if km["ma5"] else None,
        "ma10": round(km["ma10"], 2) if km["ma10"] else None,
        "ma20": round(km["ma20"], 2) if km["ma20"] else None,
        "atr": round(km["atr"], 2),
        "high20": round(km["high20"], 2),
        "mom20": round(km["mom20"], 2),
    }
    s["spark"] = [round(k["close"], 2) for k in kl[-30:]]
    return s


def build_payload(snapshot_rows, raw_indices, industries, concepts,
                  klines, fflows, trade_date, started_at, ranker_ctx=None,
                  extra_codes=None, event_ctx=None):
    rows = [enrich(r) for r in snapshot_rows]
    valid = [s for s in rows if s["price"] > 0]

    ind_sorted = industry_sort(industries)
    ind_rank = {}
    n = max(len(ind_sorted), 1)
    for i, it in enumerate(ind_sorted):
        ind_rank[str(it.get("f14"))] = 1 - i / n
    ind_flow = {str(it.get("f14")): num(it.get("f62")) for it in ind_sorted}

    cands = []
    for s in valid:
        if eligible(s):
            sp = ind_rank.get(s["industry"], 0.5)
            sc, parts = prelim_score(s, sp)
            s["prelim"] = round(sc, 1)
            cands.append(s)
    cands.sort(key=lambda x: x["prelim"], reverse=True)
    deep = cands[:config.DEEP_COUNT]
    if extra_codes:
        by_code = {s["code"]: s for s in cands}
        have = {s["code"] for s in deep}
        for code in extra_codes:
            s = by_code.get(str(code))
            if s and s["code"] not in have:
                deep.append(s)
                have.add(s["code"])

    picks = []
    for s in deep:
        kl = klines.get(s["code"])
        if not kl or len(kl) < 30:
            continue
        ff = fflows.get(s["code"]) or []
        sp = ind_rank.get(s["industry"], 0.5)
        s = analyze_stock(s, kl, ff, sp)
        picks.append(s)

    picks = ranker.apply_scores(picks, ranker_ctx)
    picks.sort(key=lambda x: x["score"], reverse=True)

    risk_off = bool((ranker_ctx or {}).get("risk_off"))
    # 弱势环境：只保留模型排名不差、3日超额不为负、三重障碍胜率不过低的标的
    if risk_off and picks:
        filtered = []
        for p in picks:
            m = p.get("model") or {}
            if m.get("ok"):
                if (m.get("rank_pct", 0.5) < 0.52
                        or m.get("exp_excess_3d", 0) <= -0.2
                        or m.get("p_tb", 50) < 36
                        or float(p.get("heuristic_score") or 0) < 52):
                    continue
            filtered.append(p)
        if len(filtered) >= 3:
            picks = filtered

    primary, overflow, counts = [], [], {}
    for p in picks:
        ind = p.get("industry") or ""
        if ind and ind != "未分类":
            if counts.get(ind, 0) >= 2:
                overflow.append(p)
                continue
            counts[ind] = counts.get(ind, 0) + 1
        primary.append(p)
    pick_limit = config.PICK_COUNT if not risk_off else max(4, config.PICK_COUNT - 3)
    selected = primary[:pick_limit]
    if len(selected) < pick_limit:
        selected += overflow[:pick_limit - len(selected)]
    selected.sort(key=lambda x: x["score"], reverse=True)
    picks = selected

    # 绝对买入门槛：横截面排名只说明相对强弱，不代表绝对胜率。
    # 未通过者保留在观察池，但仓位强制 0%，避免把“相对最强”误读成“可以买”。
    for p in picks:
        m = p.get("model") or {}
        plan = p.get("plan") or {}
        reasons = []
        if not m.get("ok"):
            reasons.append("3日模型未就绪")
        else:
            p_tb = float(m.get("p_tb") or 0)
            p_up = float(m.get("p_up_3d") or 0)
            rank = float(m.get("rank_pct") or 0)
            exp_ex = float(m.get("exp_excess_3d") or 0)
            if p_tb < config.ABS_PTB_MIN:
                reasons.append("三重障碍胜率%.0f%%<%.0f%%" % (p_tb, config.ABS_PTB_MIN))
            if p_up < config.ABS_PUP_MIN:
                reasons.append("3日上涨概率%.0f%%<%.0f%%" % (p_up, config.ABS_PUP_MIN))
            if rank < config.ABS_RANK_MIN:
                reasons.append("横截面排名前%.1f%%<前%.0f%%" % ((1 - rank) * 100, (1 - config.ABS_RANK_MIN) * 100))
            if exp_ex <= config.ABS_EXP_MIN:
                reasons.append("3日超额%.2f%%<=%.2f%%" % (exp_ex, config.ABS_EXP_MIN))
        try:
            rr = float(plan.get("rr") or 0)
        except (TypeError, ValueError):
            rr = 0.0
        if rr < config.ABS_RR_MIN:
            reasons.append("盈亏比%.2f<%.2f" % (rr, config.ABS_RR_MIN))
        if risk_off and getattr(config, "ABS_BLOCK_RISK_OFF", False):
            reasons.append("大盘偏弱，暂停买入")
        reasons.extend(signal_guard.reasons(p, ranker_ctx))
        # 公告事件过滤：block 级利空（控股股东减持/立案/退市风险等）直接否决买入。
        # 这层在技术形态之外，专治"形态分很高但大股东在撤退"的煌上煌式踩雷。
        ev = (event_ctx or {}).get(p["code"]) or {}
        p["event_risk"] = ev if ev else None
        if ev.get("level") == "block":
            for r in (ev.get("reasons") or [])[:2]:
                reasons.append("公告利空：" + r)
            if not ev.get("reasons"):
                reasons.append("公告利空（详见事件明细）")
        elif ev.get("level") == "warn":
            if "公告风险" not in (p.get("flags") or []):
                p.setdefault("flags", []).append("公告风险：" + (ev.get("reasons") or ["未知"])[0])
        p["actionable"] = not reasons
        p["watch_reasons"] = reasons
        p["trial"] = False
        if reasons:
            plan["position"] = "0%"

    # 试探仓：没过绝对门槛，但各项只差一点，允许小仓位试错（止损同样按计划执行）
    trial_n = 0
    if config.TRIAL_ENABLED:
        for p in picks:
            if p.get("actionable") or trial_n >= config.TRIAL_MAX_COUNT:
                continue
            if ((event_ctx or {}).get(p["code"]) or {}).get("level") == "block":
                continue  # 公告硬利空不给试探仓
            m = p.get("model") or {}
            if not m.get("ok"):
                continue
            try:
                rr = float((p.get("plan") or {}).get("rr") or 0)
            except (TypeError, ValueError):
                rr = 0.0
            if (float(m.get("p_tb") or 0) >= config.TRIAL_PTB_MIN
                    and float(m.get("p_up_3d") or 0) >= config.TRIAL_PUP_MIN
                    and float(m.get("rank_pct") or 0) >= config.TRIAL_RANK_MIN
                    and float(m.get("exp_excess_3d") or 0) > config.TRIAL_EXP_MIN
                    and rr >= config.TRIAL_RR_MIN):
                p["trial"] = True
                p["plan"]["position"] = "%d%%" % config.TRIAL_POSITION
                trial_n += 1

    approved = [p for p in picks if p.get("actionable")]
    picks.sort(key=lambda p: (bool(p.get("actionable")),
                              bool((p.get("trend_guard") or {}).get("ok")),
                              float((p.get("model") or {}).get("p_up_3d") or 0),
                              p.get("score", 0)), reverse=True)
    trade_gate = {
        "ok": bool(approved),
        "count": len(approved),
        "trial": trial_n,
        "total": len(picks),
        "thresholds": {
            "p_tb": config.ABS_PTB_MIN,
            "p_up": config.ABS_PUP_MIN,
            "rr": config.ABS_RR_MIN,
            "rank_pct": config.ABS_RANK_MIN,
            "exp_excess": config.ABS_EXP_MIN,
        },
        "reason": "" if approved else "没有标的同时满足绝对买入门槛，当前只适合观察/空仓等待",
    }

    def pos_pct(p):
        try:
            return int(str(p["plan"]["position"]).rstrip("%"))
        except (ValueError, KeyError):
            return 0

    max_total = config.MAX_TOTAL_POS * (0.65 if risk_off else 1.0)
    total_pos = sum(pos_pct(p) for p in picks)
    if total_pos > max_total:
        scale = max_total / total_pos
        for p in picks:
            p["plan"]["position"] = "%d%%" % int(pos_pct(p) * scale)

    stats = market_stats(valid, raw_indices)
    sectors = [{
        "name": str(it.get("f14")),
        "pct": num(it.get("f3")),
        "main": num(it.get("f62")),
        "main_pct": num(it.get("f184")),
    } for it in ind_sorted]
    concepts_top = [{
        "name": str(it.get("f14")),
        "pct": num(it.get("f3")),
        "main": num(it.get("f62")),
    } for it in sorted(concepts, key=lambda x: num(x.get("f62")), reverse=True)[:6]]

    for p in picks:
        p.pop("concepts", None)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": started_at,
        "trade_date": trade_date,
        "session": None,
        "model": config.MODEL,
        "market": stats,
        "sectors": {
            "top": sectors[:8],
            "bottom": sectors[-4:][::-1],
        },
        "concepts": concepts_top,
        "picks": picks,
        "ai": {
            "status": "pending",
            "market_view": rule_market_view(stats),
            "risk": "中" if stats["up_ratio"] < 0.6 else "低",
            "notes": "模型研判生成中…",
            "avoid": [],
        },
        "alerts": [],
        "trade_gate": trade_gate,
        "ranker": {
            "ok": bool((ranker_ctx or {}).get("ok")),
            "error": (ranker_ctx or {}).get("error"),
            "blend_weight": (ranker_ctx or {}).get("blend_weight"),
            "risk_off": risk_off,
            "metrics": (ranker_ctx or {}).get("metrics"),
            "live": (ranker_ctx or {}).get("live"),
            "market": (ranker_ctx or {}).get("market"),
            "latest_date": (ranker_ctx or {}).get("latest_date"),
        },
        "stats": {
            "scanned": len(valid),
            "filtered": len(cands),
            "deep": len(deep),
            "priced": len(picks),
            "actionable": len(approved),
            "modeled": sum(1 for p in picks if (p.get("model") or {}).get("ok")),
        },
        "disclaimer": config.DISCLAIMER,
    }
