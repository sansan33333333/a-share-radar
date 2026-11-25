"""执行价位和近期模型表现的买入检查。"""
import math
import config


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def reasons(pick, context):
    result = []
    trend = pick.get("trend_guard") or {}
    if trend.get("ok") is not True:
        result.append(trend.get("reason") or "个股趋势尚未确认")
    live = (context or {}).get("live") or {}
    n = finite(live.get("n"))
    hit, ret, ic = (finite(live.get(k)) for k in ("hit", "avg_ret", "ic"))
    if n is None or n < config.LIVE_MIN_DAYS or any(v is None for v in (hit, ret, ic)):
        result.append("近期已验证样本不足，暂不买入")
    elif hit < config.LIVE_MIN_HIT or ret <= 0 or ic <= 0:
        result.append("近期模型表现不达标，暂停买入")
    model = pick.get("model") or {}
    if any(finite(model.get(k)) is None for k in ("p_tb", "p_up_3d", "rank_pct", "exp_excess_3d")):
        result.append("模型数值缺失或异常")
    plan = pick.get("plan") or {}
    price, low, high, stop, target = (finite(v) for v in (
        pick.get("price"), plan.get("buy_low"), plan.get("buy_high"),
        plan.get("stop"), plan.get("target1")))
    if any(v is None or v <= 0 for v in (price, low, high, stop, target)):
        result.append("交易价位缺失或异常")
    elif not stop < low <= high < target:
        result.append("买区、止损或目标顺序异常")
    else:
        if not low <= price <= high:
            result.append("当前价格不在买区，等待确认")
        if (high - stop) / high * 100 > config.MAX_STOP_PCT + 1e-8:
            result.append("买区上沿至止损距离超过风险上限")
        if (target - high) / (high - stop) < config.ABS_RR_MIN:
            result.append("按买区上沿计算的盈亏比不足")
    return result


def trend_check(bars, price):
    """只用截至当前的日K判断趋势，不用未来收益选股。"""
    closes = [finite(b.get("close")) for b in (bars or [])[-25:]]
    price = finite(price)
    if len(closes) < 25 or any(x is None or x <= 0 for x in closes) or price is None or price <= 0:
        return {"ok": False, "reason": "趋势数据不足或异常"}
    ma20 = sum(closes[-20:]) / 20
    previous = sum(closes[:20]) / 20
    ok = price >= ma20 and ma20 >= previous
    return {"ok": ok, "ma20": round(ma20, 4), "ma20_5bars_ago": round(previous, 4),
            "reason": "站上20日线且20日线不下行" if ok else "低于20日线或20日线下行，反抽暂不买入"}
