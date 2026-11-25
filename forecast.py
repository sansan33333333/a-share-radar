# -*- coding: utf-8 -*-
"""未来 2 个交易日概率预测与量化卖出计划。

预测部分没有使用简单的均线外推，而是组合了三类方法：
1. 分位数梯度提升树（HistGradientBoostingRegressor, quantile loss），
   直接学习条件分位数，而不是只给一个点预测；
2. 共形化分位回归 CQR（Romano et al., 2019）在时间序列留出集上校准
   80% 区间，降低小样本分位模型的覆盖率偏差；
3. 波动率锥（volatility cone）融合，约束极端预测，避免单只股票
   样本有限时模型过度自信。

卖出部分采用趋势交易中常用的 ATR 吊灯止损（Chandelier Exit）
与短线移动止损，并叠加分批止盈、时间止损和衰竭评分。
"""
import math
import statistics

try:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.linear_model import LogisticRegression

    _ML = True
except Exception:  # pragma: no cover - 依赖缺失时自动降级
    _ML = False

FEATURES = [
    "r1", "r5", "r20", "r60", "rsi14", "atr_pct", "vol_ratio5",
    "dist_ma5", "dist_ma20", "dist_ma60", "close_pos", "gap", "amp",
    "atr_regime", "mkt_r1", "mkt_r5", "mkt_dist_ma20",
]


def _sma(vals, i, n):
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def _rsi_series(closes, n=14):
    out = [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    for i in range(n, len(closes)):
        g = sum(gains[i - n:i]) / n
        l = sum(losses[i - n:i]) / n
        if l <= 1e-12:
            out[i] = 100.0 if g > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + g / l)
    return out


def _atr_series(kl, n=14):
    trs = [0.0]
    for i in range(1, len(kl)):
        tr = max(
            kl[i]["high"] - kl[i]["low"],
            abs(kl[i]["high"] - kl[i - 1]["close"]),
            abs(kl[i]["low"] - kl[i - 1]["close"]),
        )
        trs.append(tr)
    out = [0.0] * len(kl)
    for i in range(1, len(kl)):
        lo = max(1, i - n + 1)
        out[i] = sum(trs[lo:i + 1]) / (i - lo + 1)
    return out


def _market_features(index_kl):
    """按日期对齐的指数特征，避免用到未来数据。"""
    out = {}
    if not index_kl:
        return out
    closes = [k["close"] for k in index_kl]
    for i in range(20, len(index_kl)):
        ma20 = sum(closes[i - 19:i + 1]) / 20
        r1 = closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0.0
        r5 = closes[i] / closes[i - 5] - 1 if closes[i - 5] else 0.0
        out[index_kl[i]["date"]] = (
            r1, r5, closes[i] / ma20 - 1 if ma20 else 0.0,
        )
    return out


def _feature_row(kl, i, atr, rsi, mkt):
    c = kl[i]["close"]
    o = kl[i]["open"]
    h = kl[i]["high"]
    low = kl[i]["low"]
    v = kl[i]["volume"]

    def ret(n):
        return c / kl[i - n]["close"] - 1 if i >= n and kl[i - n]["close"] else 0.0

    def dist(n):
        ma = _sma([x["close"] for x in kl[:i + 1]], i, n)
        return c / ma - 1 if ma else 0.0

    rng = h - low
    close_pos = (c - low) / rng if rng > 0 else 0.5
    gap = o / kl[i - 1]["close"] - 1 if i >= 1 and kl[i - 1]["close"] else 0.0
    amp = rng / kl[i - 1]["close"] if i >= 1 and kl[i - 1]["close"] else 0.0
    v5 = sum(x["volume"] for x in kl[i - 5:i]) / 5 if i >= 5 else 0.0
    vol_ratio5 = v / v5 if v5 > 0 else 1.0
    atr_pct = atr[i] / c if c else 0.0
    base_atr = statistics.median(atr[max(0, i - 59):i + 1]) if i >= 1 else atr[i]
    atr_regime = atr[i] / base_atr if base_atr > 0 else 1.0
    mf = mkt.get(kl[i]["date"], (0.0, 0.0, 0.0))
    return [
        ret(1), ret(5), ret(20), ret(60),
        rsi[i] / 100.0, atr_pct, vol_ratio5,
        dist(5), dist(20), dist(60), close_pos, gap, amp, atr_regime,
        mf[0], mf[1], mf[2],
    ]


def _fallback(kl, price):
    closes = [k["close"] for k in kl]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 60), len(closes))
            if closes[i - 1]]
    sigma = statistics.pstdev(rets) * math.sqrt(2) if len(rets) >= 10 else 0.03
    sigma = min(max(sigma, 0.008), 0.09)
    lo, hi = -1.2816 * sigma, 1.2816 * sigma
    return {
        "horizon": 2,
        "method": "波动率锥（历史样本不足，已降级）",
        "median_return": 0.0,
        "low_return": round(lo * 100, 2),
        "high_return": round(hi * 100, 2),
        "expected_price": round(price, 2),
        "low_price": round(price * (1 + lo), 2),
        "high_price": round(price * (1 + hi), 2),
        "p_up": 50.0,
        "direction": "震荡",
        "confidence": "低",
        "validation": None,
        "note": "样本不足，仅给出基于波动率的 80% 区间，不代表方向判断。",
    }


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def forecast_2d(kl, index_kl=None, price=None):
    """返回未来 2 个交易日收盘价的概率预测。"""
    if not kl or len(kl) < 30:
        return _fallback(kl or [], price or 0)
    price = price or kl[-1]["close"]
    if not _ML or len(kl) < 150:
        return _fallback(kl, price)

    closes = [k["close"] for k in kl]
    atr = _atr_series(kl)
    rsi = _rsi_series(closes)
    mkt = _market_features(index_kl or [])

    X, y = [], []
    for i in range(60, len(kl) - 2):
        base = closes[i]
        fwd = closes[i + 2]
        if not base or not fwd:
            continue
        X.append(_feature_row(kl, i, atr, rsi, mkt))
        y.append(_clip(fwd / base - 1, -0.22, 0.22))

    if len(y) < 120:
        return _fallback(kl, price)
    X = np.nan_to_num(np.asarray(X, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    y = np.asarray(y, dtype=float)
    n = len(y)
    cal_n = max(30, int(n * 0.2))
    train_n = n - cal_n
    if train_n < 80:
        return _fallback(kl, price)

    # 留2个交易日间隔，避免2日标签跨入校准段。
    x_train, y_train = X[:train_n - 2], y[:train_n - 2]
    x_cal, y_cal = X[train_n:], y[train_n:]

    def qmodel(q):
        return HistGradientBoostingRegressor(
            loss="quantile", quantile=q, max_iter=220, learning_rate=0.045,
            max_depth=3, min_samples_leaf=18, l2_regularization=1.0,
            early_stopping=False, random_state=42,
        )

    q10, q50, q90 = qmodel(0.1), qmodel(0.5), qmodel(0.9)
    q10.fit(x_train, y_train)
    q50.fit(x_train, y_train)
    q90.fit(x_train, y_train)
    lo_cal = q10.predict(x_cal)
    med_cal = q50.predict(x_cal)
    hi_cal = q90.predict(x_cal)

    # CQR：80% 区间的有限样本共形校正
    scores = np.maximum(lo_cal - y_cal, y_cal - hi_cal)
    offset = float(np.quantile(scores, 0.80))
    offset = _clip(offset, 0.0, 0.12)
    med_bias = _clip(float(np.median(y_cal - med_cal)), -0.02, 0.02)

    # 分类器 + 校准集 sigmoid 校准，再向历史基础概率收缩
    p_up = float(np.mean(y > 0))
    try:
        clf = HistGradientBoostingClassifier(
            max_iter=180, learning_rate=0.05, max_depth=3,
            min_samples_leaf=20, l2_regularization=1.0,
            early_stopping=False, random_state=42,
        )
        clf.fit(x_train, (y_train > 0).astype(int))
        raw_cal = clf.predict_proba(x_cal)[:, 1]
        if len(set((y_cal > 0).astype(int))) == 2:
            z = np.log(np.clip(raw_cal, 1e-4, 1 - 1e-4) / (1 - np.clip(raw_cal, 1e-4, 1 - 1e-4)))
            cal = LogisticRegression(C=0.5).fit(z.reshape(-1, 1), (y_cal > 0).astype(int))
            raw_now = float(clf.predict_proba(np.asarray([_feature_row(
                kl, len(kl) - 1, atr, rsi, mkt)], dtype=float))[0, 1])
            z_now = math.log(max(1e-4, min(1 - 1e-4, raw_now)) /
                             max(1e-4, min(1 - 1e-4, 1 - raw_now)))
            p_up = float(cal.predict_proba([[z_now]])[0, 1])
    except Exception:
        pass
    p_up = 0.75 * p_up + 0.25 * float(np.mean(y > 0))

    # 用训练段模型预测当前；最近 20% 只用于共形校准，避免“校准后又偷看”
    x_now = np.asarray([_feature_row(kl, len(kl) - 1, atr, rsi, mkt)], dtype=float)
    lo_raw = float(q10.predict(x_now)[0])
    med_raw = float(q50.predict(x_now)[0]) + med_bias
    hi_raw = float(q90.predict(x_now)[0])
    lo = lo_raw - offset
    hi = hi_raw + offset

    # 波动率锥融合，限制小样本模型的极端区间
    rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 60), len(closes))
            if closes[i - 1]]
    sigma = statistics.pstdev(rets) * math.sqrt(2) if len(rets) >= 10 else 0.03
    sigma = _clip(sigma, 0.008, 0.09)
    vol_lo, vol_hi = -1.2816 * sigma, 1.2816 * sigma
    lo = 0.7 * lo + 0.3 * vol_lo
    hi = 0.7 * hi + 0.3 * vol_hi
    # 中位数向 0 收缩 20%，降低小样本模型对单日噪声的过拟合
    med = 0.8 * med_raw
    lo = _clip(lo, -max(0.08, 4 * sigma), 0.08)
    hi = _clip(hi, -0.08, max(0.08, 4 * sigma))
    if hi <= lo:
        lo, hi = -sigma, sigma
    med = _clip(med, lo + 0.001, hi - 0.001)

    # 校准集上的最终区间覆盖率、中位误差与方向准确率
    coverage = mae = base_mae = direction_acc = None
    try:
        lo_cal_final = lo_cal - offset
        hi_cal_final = hi_cal + offset
        coverage = float(np.mean((y_cal >= lo_cal_final) & (y_cal <= hi_cal_final)))
        mae = float(np.mean(np.abs((med_cal + med_bias) - y_cal)) * 100)
        base_mae = float(np.mean(np.abs(y_cal)) * 100)
        direction_acc = float(np.mean((med_cal > 0) == (y_cal > 0)))
    except Exception:
        pass

    if p_up >= 0.56 and med > 0:
        direction = "偏多"
    elif p_up <= 0.44 and med < 0:
        direction = "偏空"
    elif abs(p_up - 0.5) < 0.06:
        direction = "震荡"
    else:
        direction = "分歧"
    strength = abs(p_up - 0.5) * 2
    width = hi - lo
    confidence = "高" if strength >= 0.35 and width <= 0.08 else (
        "中" if strength >= 0.15 or width <= 0.06 else "低")

    return {
        "horizon": 2,
        "method": "分位梯度提升 + CQR共形校准 + 波动率锥",
        "median_return": round(med * 100, 2),
        "low_return": round(lo * 100, 2),
        "high_return": round(hi * 100, 2),
        "expected_price": round(price * (1 + med), 2),
        "low_price": round(price * (1 + lo), 2),
        "high_price": round(price * (1 + hi), 2),
        "p_up": round(p_up * 100, 1),
        "direction": direction,
        "confidence": confidence,
        "validation": {
            "coverage": round(coverage * 100, 1) if coverage is not None else None,
            "target_coverage": 80.0,
            "mae_pct": round(mae, 2) if mae is not None else None,
            "baseline_mae_pct": round(base_mae, 2) if base_mae is not None else None,
            "direction_acc": round(direction_acc * 100, 1) if direction_acc is not None else None,
            "n_train": int(train_n),
            "n_cal": int(cal_n),
        },
        "note": "预测对象为2个交易日后的收盘价；区间为80%概率区间，不是确定性目标。",
    }


def _pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


def build_sell_plan(s, km, plan, forecast, buy, kl):
    """ATR 吊灯止损 + 短线移动止损 + 分批止盈 + 时间止损。"""
    price = s["price"]
    atr = max(km.get("atr") or price * 0.02, price * 0.008)
    highs = [k["high"] for k in kl]
    high10 = max(highs[-10:]) if len(highs) >= 10 else max(highs)
    high22 = max(highs[-22:]) if len(highs) >= 22 else max(highs)
    trail = high10 - 2.0 * atr
    chandelier = high22 - 3.0 * atr
    t1, t2 = plan["target1"], plan["target2"]
    ma5, ma10, ma20 = km.get("ma5"), km.get("ma10"), km.get("ma20")
    rsi = _rsi_series([k["close"] for k in kl])[-1]
    fast_t1 = None
    if forecast and forecast.get("high_price"):
        fp = forecast["high_price"]
        if price * 1.005 < fp < t1 * 0.995:
            fast_t1 = round(fp, 2)

    score = 0
    reasons = []
    if fast_t1 and price >= fast_t1 * 0.995:
        score += 15
        reasons.append("到达2日预测上沿")
    if price >= t1 * 0.995:
        score += 25
        reasons.append("已到/超过第一目标")
    if price >= t2 * 0.995:
        score += 20
        reasons.append("已到/超过第二目标")
    if km.get("high20") and price >= km["high20"] * 0.99:
        score += 10
        reasons.append("逼近20日高点压力")
    if rsi >= 75:
        score += 15
        reasons.append("RSI超买")
    elif rsi >= 68:
        score += 8
    if s.get("main_pct", 0) <= -3:
        score += 15
        reasons.append("主力明显流出")
    elif s.get("main_pct", 0) < 0:
        score += 5
    if ma5 and price < ma5:
        score += 8
        reasons.append("跌破MA5")
    if ma10 and price < ma10:
        score += 10
        reasons.append("跌破MA10")
    if ma20 and price < ma20:
        score += 15
        reasons.append("跌破MA20")
    if s.get("pct60", 0) >= 60:
        score += 10
        reasons.append("60日涨幅偏大")
    if forecast and forecast.get("p_up", 50) < 45:
        score += 10
        reasons.append("模型看跌概率偏高")
    if forecast and forecast.get("median_return", 0) < 0:
        score += 5
    if s.get("volratio", 0) > 3 and s.get("pct", 0) < 1:
        score += 5
        reasons.append("放量滞涨")
    if buy and buy > 0:
        if buy > t1:
            score = max(score, 68)
            reasons.append("成本高于T1，反弹优先减仓")
        if price < buy * 0.92:
            score = max(score, 72)
            reasons.append("浮亏超过8%")
    score = min(score, 100)

    if trail >= price:
        action = "移动止损已触发"
    elif score >= 70:
        action = "减仓/卖出"
    elif score >= 50:
        action = "分批止盈"
    elif score >= 30:
        action = "持有观察"
    else:
        action = "持有"
    signal = "强" if score >= 70 else ("中" if score >= 45 else "弱")
    if not reasons:
        reasons.append("未出现明显衰竭信号")

    levels = []
    if fast_t1:
        levels.append({
            "key": "fc_high", "name": "2日预测上沿", "price": fast_t1,
            "portion": "短线减仓", "basis": "80%概率区间上沿",
        })
    levels.extend([
        {"key": "t1", "name": "第一止盈 T1", "price": t1,
         "portion": "减仓50%", "basis": "20日高点/ATR结构目标（非2日目标）"},
        {"key": "t2", "name": "第二止盈 T2", "price": t2,
         "portion": "剩余仓位", "basis": "趋势延伸目标"},
        {"key": "trail", "name": "短线移动止损", "price": round(trail, 2),
         "portion": "全部离场", "basis": "10日最高 - 2×ATR"},
        {"key": "chandelier", "name": "吊灯止损", "price": round(chandelier, 2),
         "portion": "全部离场", "basis": "22日最高 - 3×ATR"},
    ])
    if buy and buy > 0:
        for x in levels:
            x["ret"] = round(_pct(x["price"], buy), 2)
    return {
        "action": action,
        "signal": signal,
        "score": score,
        "reasons": reasons[:6],
        "levels": levels,
        "atr": round(atr, 2),
        "rsi": round(rsi, 1),
        "time_stop": "2个交易日内未站上MA5，减仓；跌破移动止损清仓；先到2日预测上沿可先减仓。",
        "note": "T1/T2是结构目标，不等于2日必达；2日预测上沿更适合作为短线减仓参考。",
    }
