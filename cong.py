"""旱地拔葱形态预筛。无筹码及逐笔买盘数据时，绝不输出完整通过。"""
import math
import engine


def ema(values, span):
    result = [values[0]]
    for value in values[1:]:
        result.append(result[-1] + 2 / (span + 1) * (value - result[-1]))
    return result


def suitability(age, spread, extension, volume, location, r20, r60, setup="breakout"):
    """可解释的形态匹配分；非回测胜率，不计入缺失的筹码/逐笔信息。"""
    parts = [
        {"name": "MACD时效", "score": max(12, 30 - max(0, age-(5 if setup == "pullback" else 1))*3), "max": 30},
        {"name": "均线粘合", "score": round(max(5, 25-max(0, spread-1)*5), 1), "max": 25},
        {"name": "突破距离", "score": round(max(3, 20-max(0, extension-1)*3), 1), "max": 20},
        {"name": "量价承接", "score": round((8 if 1.2 <= volume <= 3 else 4) + min(7, max(0, location)*7), 1), "max": 15},
        {"name": "低位程度", "score": round(max(0, 10-max(0, r20-5)*.5-max(0, r60-15)*.2), 1), "max": 10},
    ]
    return {"score": round(sum(p["score"] for p in parts), 1), "parts": parts,
             "version": "shape_v2", "meaning": "仅比较已入池股票的形态匹配度，不是上涨概率或买入许可；回踩型允许较早金叉，权重尚未样本外验证"}


def universe(rows):
    result = []
    for row in rows:
        s = engine.enrich(row)
        if (s["code"].startswith(("60", "00")) and not engine.is_st(s["name"])
                and 40e8 <= s["mktcap"] <= 100e8 and s["price"] > 0):
            result.append(s)
    return result


def resonance(rows, industries=None):
    """当日板块共振度：同一行业内涨停家数、涨超5%家数（均排除自身）。

    历史检验（1681 只 × 1.27 万涨停样本）：同行业涨超5%≤1家时 5 日 +1.52%/胜率 49.3%，
    5-8 家时 +2.74%/55.9%，≥9 家 +2.71%/56.8%。该值只用于排序与降级，不构成买入许可。
    """
    fallback = {}
    for it in (industries or []):
        for code in (it.get("codes") or []):
            fallback[str(code)] = it.get("name") or "未分类"
    buckets = {}
    for row in rows or []:
        # 快照原始行（f12/f100/f3）先转成统一结构，已是统一结构的直接用
        item = row if "code" in row else engine.enrich(row)
        code = str(item.get("code") or "")
        if not code:
            continue
        ind = str(item.get("industry") or fallback.get(code) or "未分类")
        pct = item.get("pct")
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        stat = buckets.setdefault(ind, {"lu": 0, "up5": 0, "n": 0, "codes": {}})
        stat["n"] += 1
        stat["codes"][code] = pct
        if pct >= 9.7:
            stat["lu"] += 1
        if pct >= 5.0:
            stat["up5"] += 1
    result = {}
    for ind, stat in buckets.items():
        if ind in ("", "未分类"):
            # 兜底桶把全市场混在一起，家数没有行业含义，标记为未知，不参与降级
            for code in stat["codes"]:
                result[code] = {"industry": "未分类", "ind_n": 0, "lu": 0, "up5": None}
            continue
        for code, pct in stat["codes"].items():
            result[code] = {
                "industry": ind,
                "ind_n": stat["n"],
                "lu": stat["lu"] - (1 if pct >= 9.7 else 0),
                "up5": stat["up5"] - (1 if pct >= 5.0 else 0),
            }
    return result


def inspect(s, bars, trade_date, watch=False):
    if len(bars or []) < 120 or str(bars[-1].get("date")) != trade_date:
        return None
    try:
        c = [float(b["close"]) for b in bars]
        o = [float(b["open"]) for b in bars]
        h = [float(b["high"]) for b in bars]
        low = [float(b["low"]) for b in bars]
        v = [float(b["volume"]) for b in bars]
        if any(not math.isfinite(x) or x <= 0 for x in c + o + h + low + v):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    fast, slow = ema(c, 12), ema(c, 26)
    diff = [a - b for a, b in zip(fast, slow)]
    dea = ema(diff, 9)
    hist = [2 * (a - b) for a, b in zip(diff, dea)]
    # 启动与回踩共用核心MACD过滤，回踩允许更早的金叉。
    crosses = [i for i in range(len(c) - 12, len(c))
               if diff[i - 1] <= dea[i - 1] and diff[i] > dea[i]]
    if not crosses or not (diff[-1] >= 0 and dea[-1] >= -c[-1] * .001
                            and hist[-1] > max(0, hist[-2])):
        return None
    before = [sum(c[-n-1:-1]) / n for n in (5, 10, 20)]
    now = [sum(c[-n:]) / n for n in (5, 10, 20)]
    spread = (max(before) / min(before) - 1) * 100
    ma20_old = sum(c[-25:-5]) / 20
    bullish = now[0] >= now[1] >= now[2]
    piercing = o[-1] <= min(now) and c[-1] > max(now) and c[-1] > o[-1]
    just_above = c[-2] <= max(before) * 1.01 and c[-1] / max(now) <= 1.05
    age = len(c)-1-crosses[-1]
    pullback = (bullish and low[-1] <= now[1]*1.015 and low[-1] >= now[2]*.985
                and c[-1] > max(now) and c[-1] >= o[-1]
                and c[-1]/max(now) <= 1.04)
    breakout = age <= 6 and just_above and (piercing or bullish)
    shape_ok = spread <= 3.5 and c[-1] > max(now) and (breakout or pullback)
    watch_shape = (spread <= 5 and c[-1] > max(now)
                   and c[-1] / max(now) <= 1.07 and now[0] >= now[1])
    if not ((watch_shape if watch else shape_ok) and now[2] >= ma20_old
            and not before[0] < before[1] < before[2]):
        return None
    r20, r60 = [(c[-2] / c[-n-2] - 1) * 100 for n in (20, 60)]
    amplitude = (max(h[-21:-1]) / min(low[-21:-1]) - 1) * 100
    limits = sum(c[i] / c[i-1] >= 1.095 for i in range(len(c)-10, len(c)))
    volume_ratio = v[-1] / (sum(v[-6:-1]) / 5)
    close_location = (c[-1] - low[-1]) / max(h[-1] - low[-1], .001)
    if not (-5 <= r20 <= 15 and -10 <= r60 <= 30 and amplitude <= 25
            and limits < 2 and (.3 if watch else .8) <= volume_ratio <= 4
            and close_location >= (.5 if watch else .6)):
        return None
    defects = []
    if watch and not shape_ok:
        if spread > 3.5:
            defects.append("均线间距3.5%–5%，等待进一步收敛")
        if not just_above and not pullback:
            defects.append("已离开首次突破位置，等待回踩均线确认，不追涨")
        if not (piercing or bullish):
            defects.append("尚未形成一阳穿线或完整多头排列")
    if age > 6 and not pullback:
        defects.append("金叉距今7–11根K线，尚未确认有效回踩")
    if 2 < age <= 6 and not pullback:
        defects.append("金叉距今3–6根K线，需确认启动持续性")
    if 2 < spread <= 3.5:
        defects.append("均线间距2%–3.5%，粘合略松")
    if volume_ratio < 1.2:
        defects.append("日K量比不足1.2，盘中与完整交易日不可直接比较，需收盘复核")
    if close_location < .7:
        defects.append("价格未进入日振幅上部30%，需要确认回落后承接")
    if len(defects) > (5 if watch else 3):
        return None
    setup = "pullback" if pullback and not breakout else "breakout"
    fit = suitability(age, spread, (c[-1]/max(now)-1)*100,
                      volume_ratio, close_location, r20, r60, setup)
    return {"code": s["code"], "name": s["name"], "price": s["price"],
            "fit": fit, "extension": round((c[-1]/max(now)-1)*100, 2),
            "setup": setup, "setup_label": "趋势回踩再启动" if setup == "pullback" else "低位突破跟踪",
            "close_location": round(close_location*100, 1), "bar_price": c[-1],
            "chart": [{"date": bars[i]["date"], "close": c[i],
                       "ma5": sum(c[i-4:i+1])/5, "ma10": sum(c[i-9:i+1])/10,
                       "ma20": sum(c[i-19:i+1])/20} for i in range(len(c)-30, len(c))],
            "next_step": ("跟踪回踩MA10后能否守住MA20、重新站稳短期均线；" if setup == "pullback" else "跟踪突破后能否站稳均线，避免远离支撑追涨；") + "盘中可重点跟踪，收盘复核量能；人工核查筹码和持续买盘",
            "cap": round(s["mktcap"] / 1e8, 1), "date": trade_date,
            "diff": round(diff[-1], 4), "dea": round(dea[-1], 4),
            "cross_age": len(c)-1-crosses[-1], "spread": round(spread, 2),
            "r20": round(r20, 2), "r60": round(r60, 2),
            "volume_ratio": round(volume_ratio, 2), "actionable": False,
            "defects": defects, "review": {"status": "pending", "decision": "待复核", "reason": "尚未完成模型复核"},
            "stage": "watch" if watch else "shape",
            "status": "接近形态 · 观察池（未通过形态）" if watch else "形态预筛通过 · 待模型复核",
            "checks": ["近12根K线金叉、零轴附近或上方、红柱增长",
                       "均线间距≤5%、站上均线、MA20不下行" if watch else "均线粘合≤3.5%、首次启动或有效回踩、MA20不下行",
                       "近20/60日未暴涨、无近期连续涨停特征",
                       "日K量比0.3–4倍、价格位于日振幅上半部" if watch else "日K量比0.8–4倍、价格位于当日振幅上部40%"],
            "pending": ["筹码上方是否真空、平均成本是否接近现价（缺少筹码数据）",
                        "分时是否有持续买盘跟进（缺少逐笔/盘口时间序列）"]}


def scan(stocks, klines, trade_date, reson=None):
    picks, missing, weak = [], 0, 0
    for s in stocks:
        bars = klines.get(s["code"]) or []
        if len(bars) < 120 or str(bars[-1].get("date")) != trade_date:
            missing += 1
            continue
        item = inspect(s, bars, trade_date)
        if item is None:
            item = inspect(s, bars, trade_date, watch=True)
        if not item:
            continue
        res = (reson or {}).get(s["code"]) or {}
        item["reson"] = res
        # 板块共振门槛：形态通过但同行业无跟随的，降级为观察，不占形态预筛名额
        up5 = res.get("up5")
        if item["stage"] == "shape" and up5 is not None:
            if up5 <= 1:
                weak += 1
                item["stage"] = "watch"
                item["reson_gate"] = "weak"
                item["status"] = "形态通过 · 板块无共振 · 观察池"
                item["defects"] = list(item.get("defects") or []) + [
                    "同行业当日涨超5%%仅%d家，缺少板块共振" % up5]
            else:
                item["reson_gate"] = "ok"
        elif item["stage"] == "shape" and res:
            item["reson_gate"] = "unknown"
        picks.append(item)
    picks.sort(key=lambda p: (p["stage"] != "shape", -p["fit"]["score"], p["code"]))
    return {"date": trade_date, "pool": len(stocks), "missing": missing,
            "items": picks, "confirmed": 0,
            "shape_count": sum(p["stage"] == "shape" for p in picks),
            "watch_count": sum(p["stage"] == "watch" for p in picks),
            "reson_weak_count": weak,
            "note": "均衡积极版：总市值40–100亿主板，分别跟踪低位突破和趋势回踩；形态预筛优先，同层按适合度排序；"
                    "形态通过但同行业当日涨超5%不足2家的降为观察（板块无共振）。"
                    "评分不是胜率，尚未样本外验证。模型复核前12只。支持盘中跟踪，量能收盘复核；"
                    "筹码和持续买盘缺失，均不自动授权买入。"}
