# -*- coding: utf-8 -*-
"""3 日横截面排序模型 + 在线自适应层。

设计思路（不是把原来的五因子权重拍脑袋调一调）：

1. 横截面而不是单票时间序列：同一天把面板内 200+ 只主板股票放在一起，
   预测未来 3 个交易日相对全市场中位数的超额收益。短线选股本质是排序，
   不是猜某只股票绝对会涨多少。
2. 三重障碍标签（Triple Barrier）：除了 3 日收益，还给每只股票打一个
   “未来 3 日内先触及止盈障碍还是止损障碍”的标签，作为第二层过滤。
   障碍宽度随个股 ATR 变化，避免所有股票用同一个固定百分比。
3. 走步验证 + 净化间隔：训练集和验证集之间留 3 个交易日，防止 3 日标签
   重叠造成数据泄漏；只用验证集评估 RankIC、Top 十分位收益和命中率。
4. 在线自适应：每天用最近数据重训；根据已实现预测的滚动 RankIC 和
   Top 组命中率调整“模型分 / 规则分”的融合权重；市场宽度转弱时自动
   降低仓位和候选数量。
5. 可解释性：用 XGBoost 的 pred_contribs 输出每只候选的主要驱动因子，
   页面和模型研判都能看到“为什么排在这里”。

参考：
- 中国证券报（2025）基于排序学习的 LightGBM 选股增强策略
- Microsoft Qlib Alpha158 / LightGBM rolling retraining
- López de Prado, Advances in Financial Machine Learning（三重障碍、净化交叉验证）
- arXiv:2504.02249 三重障碍标签在日线 OHLCV 上的实证
- arXiv:2412.08435 概念漂移在线自适应集成

边界：这是统计排序模型，不是收益承诺；面板使用当前流动性股票，存在
幸存者偏差；A 股 T+1、涨跌停和交易成本没有完全建模。
"""
import json
import math
import os
import statistics
import time
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import xgboost as xgb

    _ML = True
except Exception:  # pragma: no cover
    _ML = False

import config
import panel

CACHE_PATH = config.DATA_DIR / "ranker_cache.json"
HISTORY_PATH = config.DATA_DIR / "ranker_history.json"
MODEL_VERSION = 3
HORIZON = 3
WARMUP = 65
VALID_DAYS = 60
MAX_HISTORY = 80

# 这些特征在每一天的横截面上做百分位排名，消掉市场整体涨跌和量纲变化
RANK_FEATURES = [
    "r1", "r3", "r5", "r10", "r20", "r60",
    "rsi14", "atr_pct", "vol_ratio5", "vol_ratio20", "vol_trend",
    "dist_ma5", "dist_ma10", "dist_ma20", "dist_ma60",
    "dist_high20", "dist_low20", "close_pos", "upper_shadow", "gap", "amp",
    "lu5", "ind_r5", "ind_r20", "rel_ind_r5", "rel_ind_r20",
]
# 市场状态特征保持原值，让树模型学习“什么环境下什么因子有效”
MARKET_FEATURES = [
    "mkt_r1", "mkt_r5", "mkt_r20", "mkt_breadth", "mkt_vol", "mkt_above20",
]
# 板块共振特征保持原值：绝对家数本身携带信息（强势板块日 vs 全市场冷清），
# 做横截面百分位会把“今天有几个板块在共振”这一层信息抹掉。已排除自身。
SECTOR_FEATURES = [
    "ind_lu_n", "ind_up5_n",
]
ALL_FEATURES = RANK_FEATURES + MARKET_FEATURES + SECTOR_FEATURES
FEATURE_LABEL = {
    "r1": "1日动量", "r3": "3日动量", "r5": "5日动量", "r10": "10日动量",
    "r20": "20日动量", "r60": "60日动量", "rsi14": "RSI14",
    "atr_pct": "ATR波动", "vol_ratio5": "5日量比", "vol_ratio20": "20日量比",
    "vol_trend": "量能趋势", "dist_ma5": "距MA5", "dist_ma10": "距MA10",
    "dist_ma20": "距MA20", "dist_ma60": "距MA60",
    "dist_high20": "距20日高点", "dist_low20": "距20日低点",
    "close_pos": "收盘位置", "upper_shadow": "上影线", "gap": "跳空",
    "amp": "振幅", "lu5": "近5日涨停", "ind_r5": "行业5日动量",
    "ind_r20": "行业20日动量", "rel_ind_r5": "行业相对5日",
    "rel_ind_r20": "行业相对20日", "mkt_r1": "市场1日", "mkt_r5": "市场5日",
    "mkt_r20": "市场20日", "mkt_breadth": "市场宽度", "mkt_vol": "市场波动",
    "mkt_above20": "MA20上方占比", "ind_lu_n": "同行业涨停家数",
    "ind_up5_n": "同行业涨超5%家数",
}

# 行业映射只在构建特征时使用；不参与未来信息
_INDUSTRY = {}


def _load_industry_map():
    global _INDUSTRY
    if _INDUSTRY:
        return _INDUSTRY
    try:
        obj = json.loads((config.DATA_DIR / "industry_map.json").read_text(encoding="utf-8"))
        _INDUSTRY = obj.get("data") or {}
    except Exception:
        _INDUSTRY = {}
    return _INDUSTRY


def _industry(code, fallback="未分类"):
    if fallback and fallback != "未分类":
        return fallback
    return _load_industry_map().get(str(code)) or "未分类"


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _rolling_mean(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _rolling_std(vals, n):
    out = [None] * len(vals)
    s = s2 = 0.0
    for i, v in enumerate(vals):
        s += v
        s2 += v * v
        if i >= n:
            s -= vals[i - n]
            s2 -= vals[i - n] * vals[i - n]
        if i >= n - 1:
            var = max(0.0, s2 / n - (s / n) ** 2)
            out[i] = math.sqrt(var)
    return out


def _limits(code, name=""):
    n = str(name or "").upper()
    if "ST" in n:
        return 4.8
    return 19.7 if str(code).startswith(("30", "68")) else 9.7


def _stock_rows(code, market, name, industry, kl):
    """把一只股票的日K转成逐日特征行。只使用当日及之前数据。"""
    if not kl or len(kl) < WARMUP + HORIZON + 2:
        return []
    dates = [str(k.get("date") or "") for k in kl]
    o = [float(k.get("open") or 0) for k in kl]
    c = [float(k.get("close") or 0) for k in kl]
    h = [float(k.get("high") or 0) for k in kl]
    lo = [float(k.get("low") or 0) for k in kl]
    v = [float(k.get("volume") or 0) for k in kl]
    L = len(c)
    if min(c[-WARMUP:]) <= 0:
        return []

    r1 = [0.0] * L
    for i in range(1, L):
        if c[i - 1]:
            r1[i] = c[i] / c[i - 1] - 1
    rets = {}
    for n in (3, 5, 10, 20, 60):
        arr = [0.0] * L
        for i in range(n, L):
            if c[i - n]:
                arr[i] = c[i] / c[i - n] - 1
        rets[n] = arr
    ma = {n: _rolling_mean(c, n) for n in (5, 10, 20, 60)}
    std20 = _rolling_std(r1, 20)
    tr = [0.0] * L
    for i in range(1, L):
        tr[i] = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
    atr14 = _rolling_mean(tr, 14)
    gains = [0.0] * L
    losses = [0.0] * L
    for i in range(1, L):
        d = c[i] - c[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    ag = _rolling_mean(gains, 14)
    al = _rolling_mean(losses, 14)
    rsi = [50.0] * L
    for i in range(L):
        if ag[i] is not None and al[i] is not None:
            rsi[i] = 100.0 if al[i] <= 1e-12 else 100.0 - 100.0 / (1.0 + ag[i] / al[i])
    vma5 = _rolling_mean(v, 5)
    vma20 = _rolling_mean(v, 20)
    limit = _limits(code, name)

    rows = []
    for i in range(WARMUP, L):
        # 只要当日特征；标签仅用于训练，预测时允许最后 3 天没有标签
        fwd3 = c[i + HORIZON] / c[i] - 1 if i + HORIZON < L and c[i] else None
        atr_pct = (atr14[i] / c[i]) if atr14[i] is not None and c[i] else 0.02
        high20 = max(h[max(0, i - 19):i + 1])
        low20 = min(lo[max(0, i - 19):i + 1])
        rng = h[i] - lo[i]
        prev = c[i - 1] if i >= 1 else c[i]
        v5 = vma5[i - 1] if i >= 1 and vma5[i - 1] else None
        v20 = vma20[i - 1] if i >= 1 and vma20[i - 1] else None
        lu5 = sum(1 for j in range(max(0, i - 4), i + 1)
                  if c[j - 1] and (c[j] / c[j - 1] - 1) * 100 >= limit - 0.2)
        row = {
            "date": dates[i], "code": code, "name": name, "industry": industry,
            "close": c[i], "fwd3": fwd3,
            "r1": r1[i], "r3": rets[3][i], "r5": rets[5][i],
            "r10": rets[10][i], "r20": rets[20][i], "r60": rets[60][i],
            "rsi14": rsi[i], "atr_pct": atr_pct,
            "vol_ratio5": (v[i] / v5) if v5 else 1.0,
            "vol_ratio20": (v[i] / v20) if v20 else 1.0,
            "vol_trend": (v5 / v20) if v5 and v20 else 1.0,
            "dist_ma5": (c[i] / ma[5][i] - 1) if ma[5][i] else 0.0,
            "dist_ma10": (c[i] / ma[10][i] - 1) if ma[10][i] else 0.0,
            "dist_ma20": (c[i] / ma[20][i] - 1) if ma[20][i] else 0.0,
            "dist_ma60": (c[i] / ma[60][i] - 1) if ma[60][i] else 0.0,
            "dist_high20": (c[i] / high20 - 1) if high20 else 0.0,
            "dist_low20": (c[i] / low20 - 1) if low20 else 0.0,
            "close_pos": ((c[i] - lo[i]) / rng) if rng > 0 else 0.5,
            "upper_shadow": ((h[i] - c[i]) / rng) if rng > 0 else 0.0,
            "gap": (o[i] / prev - 1) if prev else 0.0,
            "amp": (rng / prev) if prev else 0.0,
            "lu5": lu5,
            "ind_r5": 0.0, "ind_r20": 0.0,
            "rel_ind_r5": 0.0, "rel_ind_r20": 0.0,
            "ind_lu_n": 0.0, "ind_up5_n": 0.0,
            "vol20": std20[i] or 0.0,
        }
        # 三重障碍：未来 3 日先到止盈还是止损；两个都到则用收盘价判断先后
        if fwd3 is not None:
            up_bar = max(0.015, 1.2 * atr_pct)
            dn_bar = max(0.012, 0.9 * atr_pct)
            entry = c[i]
            tb = 1 if c[i + HORIZON] > entry else 0
            for j in range(i + 1, i + HORIZON + 1):
                hit_up = h[j] >= entry * (1 + up_bar)
                hit_dn = lo[j] <= entry * (1 - dn_bar)
                if hit_up and hit_dn:
                    tb = 1 if c[j] >= entry else 0
                    break
                if hit_up:
                    tb = 1
                    break
                if hit_dn:
                    tb = 0
                    break
            row["tb"] = tb
        else:
            row["tb"] = None
        rows.append(row)
    return rows


def _build_frame(panel_cache):
    """构建面板原始特征 + 市场/行业特征 + 标签。"""
    data = panel_cache.get("data") or {}
    universe = {str(x.get("code")): x for x in (panel_cache.get("universe") or [])}
    rows = []
    for code, kl in data.items():
        meta = universe.get(code) or {}
        rows.extend(_stock_rows(code, int(meta.get("market") or (1 if str(code).startswith("6") else 0)),
                                meta.get("name") or code,
                                _industry(code, meta.get("industry") or "未分类"), kl))
    if len(rows) < 2000:
        return None, None
    df = pd.DataFrame(rows)
    df = df[df["date"].astype(str) != ""].copy()
    # 市场状态：同一交易日面板的横截面统计
    g = df.groupby("date")
    mkt = g.agg(
        mkt_r1=("r1", "median"), mkt_r5=("r5", "median"),
        mkt_r20=("r20", "median"), mkt_breadth=("r1", lambda x: float((x > 0).mean())),
        mkt_vol=("atr_pct", "median"),
        mkt_above20=("dist_ma20", lambda x: float((x > 0).mean())),
    ).reset_index()
    df = df.merge(mkt, on="date", how="left")
    # 行业动量：同一天同行业的横截面中位数
    ind = df.groupby(["date", "industry"]).agg(
        ind_r5=("r5", "median"), ind_r20=("r20", "median"), ind_n=("code", "size")
    ).reset_index()
    df = df.drop(columns=["ind_r5", "ind_r20"]).merge(ind, on=["date", "industry"], how="left")
    small = df["ind_n"].fillna(0) < 3
    df.loc[small, "ind_r5"] = df.loc[small, "mkt_r5"]
    df.loc[small, "ind_r20"] = df.loc[small, "mkt_r20"]
    df["rel_ind_r5"] = df["r5"] - df["ind_r5"]
    df["rel_ind_r20"] = df["r20"] - df["ind_r20"]
    # 板块共振：同一交易日、同行业内涨停家数与涨超5%家数（排除自身）
    # 阈值按主板 10% 涨停近似；创业板/科创板 20% 上限只会少算，不会误算
    df["_lu"] = (df["r1"] >= 0.095).astype(float)
    df["_up5"] = (df["r1"] >= 0.05).astype(float)
    cnt = df.groupby(["date", "industry"])[["_lu", "_up5"]].sum().reset_index()
    cnt = cnt.rename(columns={"_lu": "ind_lu_n", "_up5": "ind_up5_n"})
    # 占位列来自 _stock_rows，先丢掉再合并，避免列名冲突变成 _x/_y
    df = df.drop(columns=["ind_lu_n", "ind_up5_n"]).merge(
        cnt, on=["date", "industry"], how="left")
    df["ind_lu_n"] = df["ind_lu_n"] - df["_lu"]
    df["ind_up5_n"] = df["ind_up5_n"] - df["_up5"]
    df = df.drop(columns=["_lu", "_up5"])
    # 未分类是兜底桶，把全市场混在一起，家数没有行业含义，置 0 不参与判断
    unknown = df["industry"].isin(["", "未分类"])
    df.loc[unknown, "ind_lu_n"] = 0.0
    df.loc[unknown, "ind_up5_n"] = 0.0
    df["fwd3"] = df["fwd3"].astype(float).clip(-0.20, 0.20)
    df["excess3"] = df["fwd3"] - df.groupby("date")["fwd3"].transform("median")
    df["y_up"] = (df["fwd3"] > 0).astype(float)
    df["y_rank"] = df.groupby("date")["fwd3"].rank(pct=True, method="average")
    # 保留最新一天原始特征，供实时候选股做同一横截面排名
    latest_date = str(df["date"].max())
    latest_raw = df[df["date"] == latest_date].copy()
    # 训练特征：横截面百分位
    for f in RANK_FEATURES:
        df[f] = df.groupby("date")[f].rank(pct=True, method="average")
    return df, latest_raw


def _fit_models(df, latest_raw, cand_raw, log=None):
    labeled = df[df["fwd3"].notna()].copy()
    dates = sorted(labeled["date"].astype(str).unique())
    if len(dates) < 160:
        raise RuntimeError("有效交易日不足（%d）" % len(dates))
    valid_n = min(VALID_DAYS, max(20, len(dates) // 6))
    k = len(dates) - valid_n
    train_dates = dates[:max(0, k - HORIZON)]
    valid_dates = dates[k:]
    tr = labeled[labeled["date"].isin(train_dates)].copy()
    va = labeled[labeled["date"].isin(valid_dates)].copy()
    if len(tr) < 5000 or len(va) < 500:
        raise RuntimeError("训练/验证样本不足（%d/%d）" % (len(tr), len(va)))

    def mat(frame):
        x = frame[ALL_FEATURES].to_numpy(dtype=float)
        return np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)

    X, y = mat(tr), tr["excess3"].to_numpy(dtype=float)
    Xv, yv = mat(va), va["excess3"].to_numpy(dtype=float)
    y_rank = tr["y_rank"].to_numpy(dtype=float)
    yv_rank = va["y_rank"].to_numpy(dtype=float)
    # 近期样本权重更高；半衰期约 180 个交易日
    idx = {d: i for i, d in enumerate(dates)}
    w = np.asarray([0.5 ** ((len(dates) - 1 - idx[d]) / 180.0) for d in tr["date"]], dtype=float)
    w = w / max(w.mean(), 1e-9)

    xgb_common = dict(
        max_depth=3, learning_rate=0.035, subsample=0.85, colsample_bytree=0.85,
        min_child_weight=30, reg_lambda=1.6, reg_alpha=0.0, gamma=0.0,
        tree_method="hist", n_jobs=4, random_state=42,
    )
    if log:
        log("横截面模型：训练 %d 行 / 验证 %d 行，特征 %d 个…" % (len(tr), len(va), len(ALL_FEATURES)))
    t0 = time.time()
    # 主排序模型：预测横截面收益分位（0-1），直接优化“谁排在前面”
    reg_rank = xgb.XGBRegressor(
        objective="reg:squarederror", n_estimators=420,
        early_stopping_rounds=35, eval_metric="rmse", **xgb_common,
    )
    reg_rank.fit(X, y_rank, sample_weight=w, eval_set=[(Xv, yv_rank)], verbose=False)
    # 辅助模型：预测 3 日超额收益幅度，用于页面展示，不参与排序
    reg_excess = xgb.XGBRegressor(
        objective="reg:squarederror", n_estimators=360,
        early_stopping_rounds=30, eval_metric="rmse", **xgb_common,
    )
    reg_excess.fit(X, y, sample_weight=w, eval_set=[(Xv, yv)], verbose=False)
    clf_tb = xgb.XGBClassifier(
        objective="binary:logistic", n_estimators=320,
        early_stopping_rounds=30, eval_metric="logloss", **xgb_common,
    )
    clf_tb.fit(X, tr["tb"].fillna(0).astype(int), sample_weight=w,
               eval_set=[(Xv, va["tb"].fillna(0).astype(int))], verbose=False)
    clf_up = xgb.XGBClassifier(
        objective="binary:logistic", n_estimators=320,
        early_stopping_rounds=30, eval_metric="logloss", **xgb_common,
    )
    clf_up.fit(X, tr["y_up"].astype(int), sample_weight=w,
               eval_set=[(Xv, va["y_up"].astype(int))], verbose=False)

    pred_rank = reg_rank.predict(Xv)
    pred_excess = reg_excess.predict(Xv)
    p_tb = clf_tb.predict_proba(Xv)[:, 1]
    p_up = clf_up.predict_proba(Xv)[:, 1]
    v = va.copy()
    v["pred"] = pred_rank
    v["pred_excess"] = pred_excess
    v["p_tb"] = p_tb
    v["p_up"] = p_up

    ics = []
    excess_ics = []
    top_ret, top_ex, top_hit = [], [], []
    top8_ret, top8_ex, top8_hit = [], [], []
    top3_ret, top3_ex, top3_hit = [], [], []
    for _, g in v.groupby("date"):
        if len(g) < 8 or g["pred"].nunique() < 3 or g["fwd3"].nunique() < 3:
            continue
        try:
            ic = g["pred"].corr(g["fwd3"], method="spearman")
            if not math.isnan(ic):
                ics.append(float(ic))
            ic2 = g["pred_excess"].corr(g["fwd3"], method="spearman")
            if not math.isnan(ic2):
                excess_ics.append(float(ic2))
        except Exception:
            pass
        q = max(3, int(round(len(g) * 0.10)))
        top = g.nlargest(q, "pred")
        top_ret.append(float(top["fwd3"].mean()))
        top_ex.append(float(top["excess3"].mean()))
        top_hit.append(float((top["fwd3"] > 0).mean()))
        t8 = g.nlargest(min(8, len(g)), "pred")
        top8_ret.append(float(t8["fwd3"].mean()))
        top8_ex.append(float(t8["excess3"].mean()))
        top8_hit.append(float((t8["fwd3"] > 0).mean()))
        t3 = g.nlargest(min(3, len(g)), "pred")
        top3_ret.append(float(t3["fwd3"].mean()))
        top3_ex.append(float(t3["excess3"].mean()))
        top3_hit.append(float((t3["fwd3"] > 0).mean()))
    rank_ic = float(np.mean(ics)) if ics else 0.0
    rank_icir = float(rank_ic / (np.std(ics) + 1e-9)) if len(ics) > 2 else 0.0
    metrics = {
        "horizon": HORIZON,
        "method": "XGBoost横截面收益分位 + 三重障碍过滤 + 走步验证",
        "rank_ic": round(rank_ic, 4),
        "rank_icir": round(rank_icir, 3),
        "excess_ic": round(float(np.mean(excess_ics)), 4) if excess_ics else None,
        "ic_days": len(ics),
        "top_decile_ret": round(float(np.mean(top_ret)) * 100, 2) if top_ret else None,
        "top_decile_excess": round(float(np.mean(top_ex)) * 100, 2) if top_ex else None,
        "top_decile_hit": round(float(np.mean(top_hit)) * 100, 1) if top_hit else None,
        "top8_ret": round(float(np.mean(top8_ret)) * 100, 2) if top8_ret else None,
        "top8_excess": round(float(np.mean(top8_ex)) * 100, 2) if top8_ex else None,
        "top8_hit": round(float(np.mean(top8_hit)) * 100, 1) if top8_hit else None,
        "top3_ret": round(float(np.mean(top3_ret)) * 100, 2) if top3_ret else None,
        "top3_excess": round(float(np.mean(top3_ex)) * 100, 2) if top3_ex else None,
        "top3_hit": round(float(np.mean(top3_hit)) * 100, 1) if top3_hit else None,
        "baseline_ret": round(float(va["fwd3"].mean()) * 100, 2),
        "baseline_excess": round(float(va["excess3"].mean()) * 100, 2),
        "up_acc": round(float(((p_up > 0.5) == (va["y_up"] > 0.5)).mean()) * 100, 1),
        "tb_acc": round(float(((p_tb > 0.5) == (va["tb"] > 0.5)).mean()) * 100, 1),
        "n_train": int(len(tr)), "n_valid": int(len(va)),
        "train_start": train_dates[0] if train_dates else None,
        "train_end": train_dates[-1] if train_dates else None,
        "valid_start": valid_dates[0], "valid_end": valid_dates[-1],
        "valid_days": len(valid_dates), "features": len(ALL_FEATURES),
        "elapsed": round(time.time() - t0, 1),
        "base_up": round(float(tr["y_up"].mean()) * 100, 1),
        "base_tb": round(float(tr["tb"].mean()) * 100, 1),
    }

    # 最终模型：用截至 T-HORIZON 的所有有标签样本重训
    def n_est(model, default):
        try:
            return max(80, int(getattr(model, "best_iteration", default - 1)) + 1)
        except Exception:
            return default

    reg_rank_final = xgb.XGBRegressor(
        objective="reg:squarederror", n_estimators=n_est(reg_rank, 420), **xgb_common)
    reg_rank_final.fit(X, y_rank, sample_weight=w, verbose=False)
    reg_excess_final = xgb.XGBRegressor(
        objective="reg:squarederror", n_estimators=n_est(reg_excess, 360), **xgb_common)
    reg_excess_final.fit(X, y, sample_weight=w, verbose=False)
    tb_final = xgb.XGBClassifier(
        objective="binary:logistic", n_estimators=n_est(clf_tb, 320), **xgb_common)
    tb_final.fit(X, tr["tb"].fillna(0).astype(int), sample_weight=w, verbose=False)
    up_final = xgb.XGBClassifier(
        objective="binary:logistic", n_estimators=n_est(clf_up, 320), **xgb_common)
    up_final.fit(X, tr["y_up"].astype(int), sample_weight=w, verbose=False)

    latest_date = str(df["date"].max())
    latest_rank = df[df["date"] == latest_date].drop_duplicates("code").set_index("code")
    panel_codes = list(latest_rank.index)
    panel_X = mat(latest_rank.reset_index())
    panel_pred = reg_rank_final.predict(panel_X)
    panel_excess = reg_excess_final.predict(panel_X)
    panel_tb = tb_final.predict_proba(panel_X)[:, 1]
    panel_up = up_final.predict_proba(panel_X)[:, 1]

    # 候选股如果不在面板里，用其日K单独算特征，再放进同一横截面排名
    cand = cand_raw.drop_duplicates("code").set_index("code") if cand_raw is not None else pd.DataFrame()
    base_raw = latest_raw.drop_duplicates("code").set_index("code")
    missing = [c for c in cand.index if c not in base_raw.index]
    cand_X = pd.DataFrame(index=list(cand.index), columns=ALL_FEATURES, dtype=float)
    all_raw = pd.concat([base_raw[RANK_FEATURES], cand.loc[missing, RANK_FEATURES]], axis=0)
    all_rank = all_raw.rank(pct=True, method="average")
    # 面板外候选拿不到自身行业家数（家数需全市场同日统计），用面板里该行业的中位数近似
    sector_by_ind = latest_raw.groupby("industry")[SECTOR_FEATURES].median()
    for code in cand.index:
        if code in base_raw.index:
            feats = latest_rank.loc[code, ALL_FEATURES].to_dict()
        else:
            feats = all_rank.loc[code].to_dict()
            ind = str(cand.loc[code, "industry"]) if "industry" in cand.columns else ""
            for f in SECTOR_FEATURES:
                feats[f] = float(sector_by_ind.loc[ind, f]) if ind in sector_by_ind.index else 0.0
        for f in MARKET_FEATURES:
            feats[f] = float(latest_raw[f].median())
        cand_X.loc[code] = [feats[f] for f in ALL_FEATURES]
    cand_X = cand_X.apply(pd.to_numeric, errors="coerce").fillna(0.0)
    cand_pred = reg_rank_final.predict(cand_X.to_numpy(dtype=float))
    cand_excess = reg_excess_final.predict(cand_X.to_numpy(dtype=float))
    cand_tb = tb_final.predict_proba(cand_X.to_numpy(dtype=float))[:, 1]
    cand_up = up_final.predict_proba(cand_X.to_numpy(dtype=float))[:, 1]

    # 候选与面板最新横截面一起排序，得到 0-1 的排名分位
    all_codes = list(panel_codes) + missing
    all_pred = np.concatenate([panel_pred, cand_pred[[cand.index.get_loc(c) for c in missing]]]) \
        if missing else panel_pred
    order = np.argsort(all_pred)
    rank_of = {all_codes[idx]: (i / max(len(all_pred) - 1, 1))
               for i, idx in enumerate(order)}
    panel_map = {code: i for i, code in enumerate(panel_codes)}
    base_up = float(tr["y_up"].mean())
    base_tb = float(tr["tb"].mean())
    scores = {}
    for code in cand.index:
        if code in panel_map:
            i = panel_map[code]
            pred_v, excess_v = float(panel_pred[i]), float(panel_excess[i])
            tb_v, up_v = float(panel_tb[i]), float(panel_up[i])
        else:
            i = missing.index(code)
            pred_v, excess_v = float(cand_pred[i]), float(cand_excess[i])
            tb_v, up_v = float(cand_tb[i]), float(cand_up[i])
        rank_pct = float(rank_of.get(code, 0.5))
        p_tb = 0.80 * tb_v + 0.20 * base_tb
        p_up = 0.80 * up_v + 0.20 * base_up
        # 0.65 排序分位 + 0.35 三重障碍胜率，避免把高动量但胜率低的票排在最前
        model_score = 50.0 + 50.0 * (0.65 * rank_pct + 0.35 * p_tb)
        conf = 0.45 * model_score + 0.35 * p_tb * 100 + 0.20 * p_up * 100
        scores[code] = {
            "model_score": round(model_score, 1),
            "rank_pct": round(rank_pct, 3),
            "exp_excess_3d": round(excess_v * 100, 2),
            "p_up_3d": round(p_up * 100, 1),
            "p_tb": round(p_tb * 100, 1),
            "confidence": int(_clip(conf, 20, 95)),
            "drivers": [],
        }
    # 局部贡献：只给候选股，避免对 260 只面板股都算
    try:
        dm = xgb.DMatrix(cand_X.to_numpy(dtype=float), feature_names=ALL_FEATURES)
        contrib = reg_rank_final.get_booster().predict(dm, pred_contribs=True)
        for j, code in enumerate(cand.index):
            vals = contrib[j][:-1]
            pairs = sorted(zip(ALL_FEATURES, vals), key=lambda x: abs(float(x[1])), reverse=True)[:3]
            scores[code]["drivers"] = [
                {"name": FEATURE_LABEL.get(f, f), "value": round(float(v), 3),
                 "dir": "+" if v >= 0 else "-"}
                for f, v in pairs
            ]
    except Exception:
        pass

    # 面板里其余股票也给出模型分：这些票可能因为规则初筛分低而没进入 deep 60，
    # 但模型可能认为它们更值得关注。后续可把它们补进精算/观察池。
    for code in panel_codes:
        if code in scores:
            continue
        i = panel_map[code]
        pred_v = float(panel_pred[i])
        excess_v = float(panel_excess[i])
        tb_v = float(panel_tb[i])
        up_v = float(panel_up[i])
        rank_pct = float(rank_of.get(code, 0.5))
        p_tb = 0.80 * tb_v + 0.20 * base_tb
        p_up = 0.80 * up_v + 0.20 * base_up
        model_score = 50.0 + 50.0 * (0.65 * rank_pct + 0.35 * p_tb)
        conf = 0.45 * model_score + 0.35 * p_tb * 100 + 0.20 * p_up * 100
        scores[code] = {
            "model_score": round(model_score, 1),
            "rank_pct": round(rank_pct, 3),
            "exp_excess_3d": round(excess_v * 100, 2),
            "p_up_3d": round(p_up * 100, 1),
            "p_tb": round(p_tb * 100, 1),
            "confidence": int(_clip(conf, 20, 95)),
            "drivers": [],
        }

    latest_meta = latest_raw.drop_duplicates("code").set_index("code")
    panel_top = []
    for code in panel_codes:
        v = scores.get(code)
        if not v:
            continue
        meta = latest_meta.loc[code] if code in latest_meta.index else {}
        panel_top.append({
            "code": code,
            "name": str(meta.get("name") or code),
            "industry": str(meta.get("industry") or ""),
            "model_score": v.get("model_score"),
            "rank_pct": v.get("rank_pct"),
            "p_tb": v.get("p_tb"),
            "p_up_3d": v.get("p_up_3d"),
            "exp_excess_3d": v.get("exp_excess_3d"),
        })
    panel_top.sort(key=lambda x: x.get("model_score") or 0, reverse=True)
    panel_top = panel_top[:40]

    market = {
        "latest_date": latest_date,
        "breadth": round(float(latest_raw["mkt_breadth"].median()) * 100, 1),
        "r5": round(float(latest_raw["mkt_r5"].median()) * 100, 2),
        "above20": round(float(latest_raw["mkt_above20"].median()) * 100, 1),
    }
    risk_off = (market["breadth"] < 40.0 or market["above20"] < 40.0 or
                (market["r5"] < -1.0 and market["above20"] < 45.0))
    return {
        "ok": True,
        "scores": scores,
        "panel_top": panel_top,
        "metrics": metrics,
        "market": market,
        "risk_off": bool(risk_off),
        "latest_date": latest_date,
        "model_kind": "xgboost_cross_sectional_rank_tb",
    }


def _load_json(path, default):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else default
    except Exception:
        return default


def _save_json(path, obj):
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        pass


def _load_history():
    obj = _load_json(HISTORY_PATH, {})
    return obj.get("items") if isinstance(obj.get("items"), list) else []


def _save_history(items):
    _save_json(HISTORY_PATH, {"updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              "items": items[-MAX_HISTORY:]})


def record_history(trade_date, scores):
    """记录当日全部深度候选的预测，供 3 日后评估真实 RankIC。"""
    if not scores:
        return
    items = _load_history()
    items = [x for x in items if str(x.get("date")) != str(trade_date)]
    items.append({
        "date": str(trade_date),
        "scores": {str(k): {
            "model_score": v.get("model_score"),
            "rank_pct": v.get("rank_pct"),
            "exp_excess_3d": v.get("exp_excess_3d"),
            "p_tb": v.get("p_tb"),
            "p_up_3d": v.get("p_up_3d"),
        } for k, v in scores.items()},
    })
    _save_history(items)


def _index_by_date(kl):
    return {str(k.get("date")): i for i, k in enumerate(kl or [])}


def evaluate_history(panel_cache, latest_date=None):
    """用已实现 3 日收益评估历史预测，得到在线 RankIC / Top3 命中率。"""
    items = _load_history()
    data = panel_cache.get("data") or {}
    idx = {code: _index_by_date(kl) for code, kl in data.items()}
    rows = []
    for rec in items:
        d = str(rec.get("date"))
        sc = rec.get("scores") or {}
        xs, ys = [], []
        for code, v in sc.items():
            kl = data.get(code) or []
            pos = (idx.get(code) or {}).get(d)
            if pos is None or pos + HORIZON >= len(kl):
                continue
            ret = kl[pos + HORIZON]["close"] / kl[pos]["close"] - 1 if kl[pos]["close"] else 0
            xs.append(float(v.get("model_score") or 0))
            ys.append(ret)
        if len(xs) < 8:
            continue
        try:
            ic = pd.Series(xs).corr(pd.Series(ys), method="spearman")
        except Exception:
            ic = None
        order = np.argsort(xs)[::-1]
        top = [ys[i] for i in order[:3]]
        med = float(np.median(ys))
        rows.append({
            "date": d, "n": len(xs), "ic": None if ic is None or math.isnan(ic) else float(ic),
            "top3_ret": float(np.mean(top)) if top else None,
            "top3_excess": float(np.mean(top)) - med if top else None,
            "hit": float(np.mean([1.0 if x > 0 else 0.0 for x in top])) if top else None,
        })
    recent = [r for r in rows if r.get("ic") is not None][-20:]
    if not recent:
        return {"n": 0, "ic": None, "hit": None, "avg_ret": None, "avg_excess": None}
    return {
        "n": len(recent),
        "ic": round(float(np.mean([r["ic"] for r in recent])), 4),
        "hit": round(float(np.mean([r["hit"] for r in recent if r["hit"] is not None])) * 100, 1),
        "avg_ret": round(float(np.mean([r["top3_ret"] for r in recent if r["top3_ret"] is not None])) * 100, 2),
        "avg_excess": round(float(np.mean([r["top3_excess"] for r in recent if r["top3_excess"] is not None])) * 100, 2),
        "last_date": recent[-1]["date"],
    }


def _blend_weight(metrics, live):
    base = 0.55
    if (metrics.get("rank_ic") or 0) <= 0:
        base = 0.38
    elif (metrics.get("rank_ic") or 0) < 0.02:
        base = 0.48
    if (metrics.get("top_decile_excess") or 0) <= 0:
        base -= 0.06
    if live and live.get("ic") is not None:
        base += 0.18 * _clip(float(live["ic"]) / 0.08, -1.0, 1.0)
        if live.get("hit") is not None:
            base += 0.05 if float(live["hit"]) >= 50 else -0.05
    return round(_clip(base, 0.30, 0.75), 2)


def _candidate_raw(deep, klines, panel_cache):
    universe = {str(x.get("code")): x for x in (panel_cache.get("universe") or [])}
    rows = []
    for item in deep:
        if isinstance(item, dict):
            code, market = str(item.get("code")), int(item.get("market") or 0)
        else:
            code, market = str(item[0]), int(item[1])
        kl = (klines or {}).get(code) or panel.bars_for(panel_cache, code)
        meta = universe.get(code) or {}
        name = meta.get("name") or code
        industry = _industry(code, meta.get("industry") or "未分类")
        rows.extend(_stock_rows(code, market, name, industry, kl))
    if not rows:
        return None
    df = pd.DataFrame(rows)
    # 只保留每只股票最新一根，用最新横截面参与排名
    return df.sort_values("date").groupby("code").tail(1).reset_index(drop=True)


def prepare(rows, deep, klines, trade_date, log=None):
    """确保面板、训练模型、给深度候选打分。失败时返回 ok=False，由规则分兜底。"""
    if not _ML:
        return {"ok": False, "error": "xgboost 未安装", "scores": {}, "blend_weight": 0.0}
    trade_date = str(trade_date or datetime.now().strftime("%Y-%m-%d"))
    cache = _load_json(CACHE_PATH, {})
    deep_codes = [str(x.get("code") if isinstance(x, dict) else x[0]) for x in deep]
    if (cache.get("date") == trade_date and cache.get("version") == MODEL_VERSION
            and all(c in (cache.get("scores") or {}) for c in deep_codes)
            and cache.get("scores")):
        if log:
            m = cache.get("metrics") or {}
            log("横截面模型：复用当日缓存（RankIC %s，权重 %s）"
                % (m.get("rank_ic"), cache.get("blend_weight")))
        cache["live"] = evaluate_history(panel._load(), trade_date)
        cache["blend_weight"] = _blend_weight(cache.get("metrics") or {}, cache["live"])
        return cache

    panel_cache = panel.ensure_panel(rows, trade_date, log=log)
    if log:
        log("横截面模型：构建特征面板…")
    df, latest_raw = _build_frame(panel_cache)
    if df is None:
        return {"ok": False, "error": "面板样本不足", "scores": {}, "blend_weight": 0.0}
    cand_raw = _candidate_raw(deep, klines, panel_cache)
    if cand_raw is None:
        return {"ok": False, "error": "候选特征不足", "scores": {}, "blend_weight": 0.0}
    try:
        ctx = _fit_models(df, latest_raw, cand_raw, log=log)
    except Exception as e:  # noqa: BLE001
        if log:
            log("横截面模型失败：%s" % str(e)[:160])
        return {"ok": False, "error": str(e)[:180], "scores": {}, "blend_weight": 0.0}
    live = evaluate_history(panel_cache, ctx.get("latest_date"))
    ctx["live"] = live
    ctx["blend_weight"] = _blend_weight(ctx.get("metrics") or {}, live)
    ctx["date"] = trade_date
    ctx["version"] = MODEL_VERSION
    ctx["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record_history(trade_date, ctx.get("scores") or {})
    _save_json(CACHE_PATH, ctx)
    if log:
        m = ctx.get("metrics") or {}
        log("横截面模型：RankIC %.3f，Top10%%超额 %s%%，3日命中 %s%%，融合权重 %.2f"
            % (m.get("rank_ic") or 0, m.get("top_decile_excess"),
               m.get("top_decile_hit"), ctx.get("blend_weight") or 0))
    return ctx


def apply_scores(picks, ctx):
    """把模型分与规则分融合，并写入每只候选的解释字段。"""
    if not picks:
        return picks
    if not ctx or not ctx.get("ok"):
        for p in picks:
            p["heuristic_score"] = p.get("score")
            p["model"] = {"ok": False, "note": (ctx or {}).get("error") or "模型未就绪"}
        return picks
    scores = ctx.get("scores") or {}
    w = float(ctx.get("blend_weight") or 0.0)
    for p in picks:
        code = p.get("code")
        p["heuristic_score"] = p.get("score")
        m = scores.get(code)
        if not m:
            p["model"] = {"ok": False, "note": "该股不在模型横截面内"}
            continue
        final = w * float(m["model_score"]) + (1 - w) * float(p.get("score") or 0)
        p["score"] = round(final, 1)
        p["model"] = dict(m, ok=True, weight=w)
        p["confidence"] = int(_clip(float(m.get("p_up_3d") or 0), 0, 100))
        flags = p.setdefault("flags", [])
        if m["rank_pct"] < 0.30:
            flags.append("横截面排名靠后")
        if m["exp_excess_3d"] <= 0:
            flags.append("3日超额为负")
        if m["p_tb"] < 36:
            flags.append("三重障碍胜率偏低")
        if m["model_score"] >= 85 and m["p_tb"] < 35:
            flags.append("模型排名与胜率分歧")
    return picks


def summary_text(ctx):
    if not ctx or not ctx.get("ok"):
        return "横截面模型暂不可用：%s" % ((ctx or {}).get("error") or "未知")
    m = ctx.get("metrics") or {}
    live = ctx.get("live") or {}
    ic_txt = "待验证" if live.get("ic") is None else ("%.3f" % float(live["ic"]))
    hit_txt = "待验证" if live.get("hit") is None else ("%.1f%%" % float(live["hit"]))
    return ("3日横截面模型：验证RankIC %.3f，Top8超额 %s%%，命中 %s%%；"
            "在线RankIC %s，Top3命中 %s，融合权重 %.2f"
            % (m.get("rank_ic") or 0, m.get("top8_excess"),
               m.get("top8_hit"), ic_txt, hit_txt,
               ctx.get("blend_weight") or 0))
