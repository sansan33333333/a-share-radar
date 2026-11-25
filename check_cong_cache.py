"""缓存形态诊断：覆盖缓存日K，未重新应用实时市值池。"""
import json
import config
import cong
import sys
from collections import Counter

config.setup_stdout()
cache = json.loads((config.DATA_DIR / "kline_cache.json").read_text(encoding="utf-8"))
date = cache["date"]
items = []
rejected = Counter()
details = Counter()
def trace(frame, event, arg):
    if frame.f_code is cong.inspect.__code__ and event == "return" and arg is None:
        rejected[frame.f_lineno] += 1
        x = frame.f_locals
        if "crosses" in x:
            details["MACD检查数量"] += 1
            for name, failed in {
                "无近5根金叉": not x["crosses"],
                "DIFF水下": x["diff"][-1] < 0,
                "DEA低于零轴容差": x["dea"][-1] < -x["c"][-1] * .001,
                "红柱未正向增长": x["hist"][-1] <= max(0, x["hist"][-2]),
            }.items():
                if failed:
                    details[name] += 1
        if "just_above" in x:
            details["通过MACD后均线检查数量"] += 1
            for name, failed in {
                "间距超过3%": x["spread"] > 3,
                "未站上三线": x["c"][-1] <= max(x["now"]),
                "非刚突破或偏离过大": not x["just_above"],
                "非穿线且非多头": not (x["piercing"] or x["bullish"]),
                "MA20下行": x["now"][2] < x["ma20_old"],
                "此前空头排列": x["before"][0] < x["before"][1] < x["before"][2],
            }.items():
                if failed:
                    details[name] += 1
    return trace
sys.settrace(trace)
for code, bars in cache["data"].items():
    if not code.startswith(("60", "00")):
        continue
    item = cong.inspect({"code": code, "name": code, "price": bars[-1]["close"], "mktcap": 0}, bars, date)
    if item:
        items.append(item)
sys.settrace(None)
watch_items = []
for code, bars in cache["data"].items():
    if not code.startswith(("60", "00")) or not bars:
        continue
    stock = {"code": code, "name": code, "price": bars[-1]["close"], "mktcap": 0}
    if not cong.inspect(stock, bars, date):
        candidate = cong.inspect(stock, bars, date, watch=True)
        if candidate:
            watch_items.append({"code": code, "defects": candidate["defects"]})
print(json.dumps({"overlapping_failures": dict(details), "rejected_at_source_line": dict(rejected), "date": date, "cached_bars": len(cache["data"]),
                  "watch_candidates": watch_items,
                  "strict_shapes": sum(not p["defects"] for p in items),
                  "with_minor_defects": sum(bool(p["defects"]) for p in items),
                  "items": [{"code": p["code"], "defects": p["defects"]} for p in items]}, ensure_ascii=False, indent=2))
