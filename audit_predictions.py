"""只读审计：高分预测的已实现3日收益，不改历史数据。"""
import json
import config
import panel
import ranker


def main():
    config.setup_stdout()
    cache = panel._load()
    history = ranker._load_history()
    rows = []
    for rec in history:
        for code, score in (rec.get("scores") or {}).items():
            bars = (cache.get("data") or {}).get(code) or []
            pos = ranker._index_by_date(bars).get(str(rec.get("date")))
            if pos is None or pos + 3 >= len(bars) or not bars[pos]["close"]:
                continue
            ret = 100 * (bars[pos + 3]["close"] / bars[pos]["close"] - 1)
            if float(score.get("model_score") or 0) >= 80:
                rows.append({"date": rec["date"], "code": code,
                             "score": score["model_score"],
                             "p_up": score.get("p_up_3d"), "return_pct": round(ret, 2)})
    print(json.dumps({"high_score_matured": len(rows),
                      "win_rate": round(100 * sum(r["return_pct"] > 0 for r in rows) / len(rows), 1) if rows else None,
                      "recent": sorted(rows, key=lambda r: r["date"])[-20:]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
