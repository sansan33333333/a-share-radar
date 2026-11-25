"""历史预测诊断：次日开盘至第3日收盘，扣0.2%成本；非真实成交回测。"""
import json
from collections import defaultdict
import config
import panel
import ranker


def main():
    config.setup_stdout()
    data = panel._load().get("data") or {}
    groups = defaultdict(list)
    dates = set()
    for rec in ranker._load_history():
        date = str(rec["date"])
        for code, score in (rec.get("scores") or {}).items():
            if float(score.get("model_score") or 0) < 80:
                continue
            bars = data.get(code) or []
            i = ranker._index_by_date(bars).get(date)
            if i is None or i < 25 or i + 3 >= len(bars):
                continue
            closes = [b["close"] for b in bars]
            entry = bars[i+1]["open"]
            if entry <= 0:
                continue
            ret = (closes[i+3] / entry - 1) * 100 - .2
            ma20 = sum(closes[i-19:i+1]) / 20
            old20 = sum(closes[i-24:i-4]) / 20
            trend = closes[i] >= ma20 and ma20 >= old20
            r5 = (closes[i] / closes[i-5] - 1) * 100
            groups["全部高分"].append(ret)
            groups["站上MA20且MA20不下行" if trend else "低于MA20或MA20下行"].append(ret)
            groups["5日涨幅超过10%" if r5 > 10 else "5日涨幅不超过10%"].append(ret)
            dates.add(date)
    result = {"dates": sorted(dates), "method": "分组有重叠；历史股票池有选择偏差；未模拟涨跌停成交和止损", "groups": {
        k: {"n": len(v), "positive_pct": round(100*sum(x>0 for x in v)/len(v), 1),
            "mean_net_pct": round(sum(v)/len(v), 2)} for k, v in groups.items()}}
    (config.DATA_DIR / "regime_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
