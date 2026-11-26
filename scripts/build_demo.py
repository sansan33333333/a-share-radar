# -*- coding: utf-8 -*-
"""从本地 data/ 生成 demo_data/：让看板在完全离线的机器上也能跑起来。

用法（项目根目录）：
    python scripts/build_demo.py

只导出看板真正会读的东西：
    latest.json      最近一次分析结果（全部候选、评分拆解、K线小图都在里面）
    astock.db        最近 N 次历史报告（侧栏"历史报告"用）
    symbols.json     搜索索引
    industries.json  行业排名
    industry_map.json 行业归属
日K库（klines.db）不导出：看板的 K 线小图已经内嵌在 latest.json 里，
实时个股诊断在演示模式下是关闭的。
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data"
DST = ROOT / "demo_data"
KEEP_RUNS = 12

FILES = ["latest.json", "symbols.json", "industries.json", "industry_map.json"]


def main():
    if not (SRC / "latest.json").exists():
        print("缺少 data/latest.json，请先跑一次 python pipeline.py --force")
        return 1
    DST.mkdir(parents=True, exist_ok=True)

    for name in FILES:
        src = SRC / name
        if src.exists():
            shutil.copy2(src, DST / name)
            print("  copied", name, "%.0f KB" % (src.stat().st_size / 1024))
        else:
            print("  skip  ", name, "(不存在)")

    # 历史报告：只留最近 KEEP_RUNS 条，避免把整库带进来
    src_db = SRC / "astock.db"
    dst_db = DST / "astock.db"
    if src_db.exists():
        if dst_db.exists():
            dst_db.unlink()
        con = sqlite3.connect(str(dst_db))
        con.execute("ATTACH DATABASE ? AS src", (str(src_db),))
        row = con.execute(
            "select sql from src.sqlite_master where type='table' and name='runs'").fetchone()
        if row:
            con.execute(row[0])
            cols = [c[1] for c in con.execute("PRAGMA src.table_info(runs)")]
            col_sql = ", ".join(cols)
            rows = con.execute(
                "select %s from src.runs order by ts desc limit ?" % col_sql,
                (KEEP_RUNS,)).fetchall()
            con.executemany("insert into runs (%s) values (%s)"
                            % (col_sql, ", ".join("?" * len(cols))), rows)
            con.commit()
            print("  built  astock.db  %d 条历史报告  %.0f KB"
                  % (len(rows), dst_db.stat().st_size / 1024))
        con.close()

    total = sum(f.stat().st_size for f in DST.rglob("*") if f.is_file())
    print("demo_data 合计 %.1f MB" % (total / 1024 / 1024))
    payload = json.loads((DST / "latest.json").read_text(encoding="utf-8"))
    print("快照日期 %s，候选 %d 只" % (payload.get("generated_at"),
                                     len(payload.get("picks") or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
