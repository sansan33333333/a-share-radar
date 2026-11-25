"""实测已有证券的增量更新及紧接着的数据库复用。"""
import time
import config
import kline_store as ks

config.setup_stdout()
with ks.connect() as conn:
    rows = conn.execute("SELECT secid,MAX(date) FROM bars GROUP BY secid ORDER BY secid LIMIT 12").fetchall()
items = [(s.split(".")[1], int(s.split(".")[0])) for s, _ in rows]
day = max(d for _, d in rows)
for name in ("首次增量", "立即复用"):
    start = time.monotonic()
    result = ks.load(items, day, log=print)
    print(name, "有效", sum(bool(v) for v in result.values()), "耗时", round(time.monotonic()-start, 2))
