"""共享SQLite日K；按证券和日期存储，重叠校验后增量更新。"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import datasource as ds

_locks = {}
_guard = threading.Lock()


@contextmanager
def connect():
    conn = sqlite3.connect(str(config.DATA_DIR / "klines.db"), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS bars(secid TEXT, date TEXT, body TEXT, PRIMARY KEY(secid,date))")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(secid TEXT PRIMARY KEY, fetched REAL, checked TEXT)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def read(secid):
    with connect() as conn:
        rows = conn.execute("SELECT body FROM bars WHERE secid=? ORDER BY date", (secid,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def save(secid, bars, fetched=0, checked=""):
    # 一次事务替换一个证券，防止复权历史混接。
    with connect() as conn:
        conn.execute("DELETE FROM bars WHERE secid=?", (secid,))
        conn.executemany("INSERT INTO bars VALUES(?,?,?)", [(secid, b["date"], json.dumps(b)) for b in bars])
        conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (secid, fetched, checked))


def fetch(secid, trade_date, force=False):
    with _guard:
        lock = _locks.setdefault(secid, threading.Lock())
    with lock:
        old = read(secid)
        with connect() as conn:
            meta = conn.execute("SELECT fetched,checked FROM meta WHERE secid=?", (secid,)).fetchone()
        now = datetime.now()
        cutoff = now.replace(hour=15, minute=5, second=0, microsecond=0).timestamp()
        stamp, checked = meta or (0, "")
        fresh = checked == trade_date and (0 <= time.time()-stamp < 900)
        # 已收盘的数据跨天保留；盘中版本在15:05后必须再确认。
        fetched_time = datetime.fromtimestamp(stamp)
        closed = (checked == trade_date and old and old[-1]["date"] == trade_date
                  and fetched_time.strftime("%Y-%m-%d") == trade_date
                  and (fetched_time.hour, fetched_time.minute) >= (15, 5))
        if not force and old and (closed or (fresh and (now.timestamp() < cutoff or stamp >= cutoff or trade_date != now.strftime("%Y-%m-%d")))):
            return old, "cached"
        full = force or len(old) < 120
        if old and not full:
            gap = (datetime.strptime(trade_date, "%Y-%m-%d") - datetime.strptime(old[-1]["date"], "%Y-%m-%d")).days
            full = gap > 20
        try:
            new = ds.fetch_kline(secid, lmt=500 if full else 40)
            if not new:
                return [], "fail"
            if not full:
                by_date = {b["date"]: b for b in old}
                overlap = [b for b in new if b["date"] in by_date and b["date"] < old[-1]["date"]]
                changed = any(abs(float(b[k])-float(by_date[b["date"]][k])) > .011
                              for b in overlap for k in ("open", "close", "high", "low"))
                if not overlap or changed:
                    new = ds.fetch_kline(secid, lmt=500)
                    full = True
                    if not new or len(new) < min(120, len(old)):
                        return [], "fail"
            merged = {} if full else {b["date"]: b for b in old}
            merged.update({b["date"]: b for b in new})
            bars = [merged[d] for d in sorted(merged)][-500:]
            save(secid, bars, time.time(), trade_date)
            return bars, "full" if full else "incremental"
        except Exception:
            # 数据库保留历史，但本轮失败不能把旧数据当作已刷新。
            return [], "fail"


def load(items, trade_date, force=False, log=None):
    items = list(dict.fromkeys(items))
    result, counts = {}, {}
    start = time.monotonic()
    def one(item):
        code, market = item
        bars, how = fetch("%d.%s" % (market, code), trade_date, force)
        return code, bars, how
    with ThreadPoolExecutor(max_workers=12) as pool:
        for n, future in enumerate(as_completed([pool.submit(one, it) for it in items]), 1):
            code, bars, how = future.result()
            result[code] = bars
            counts[how] = counts.get(how, 0) + 1
            if log and (n % 100 == 0 or n == len(items)):
                log("共享日K %d/%d，%s，耗时%.1f秒" % (n, len(items), counts, time.monotonic()-start))
    return result


def migrate():
    """仅首次导入旧JSON，保留文件；导入数据下一轮需重新确认。"""
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)")
        if conn.execute("SELECT 1 FROM migrations WHERE name='json_v1'").fetchone():
            return 0
    combined = {}
    for name in ("panel_cache.json", "kline_cache.json"):
        path = config.DATA_DIR / name
        if not path.exists():
            continue
        obj = json.loads(path.read_text(encoding="utf-8"))
        for code, bars in (obj.get("data") or {}).items():
            if not bars:
                continue
            secid = "%d.%s" % (1 if code.startswith("6") else 0, code)
            previous = combined.get(secid) or []
            if not previous or (bars[-1]["date"], len(bars)) >= (previous[-1]["date"], len(previous)):
                combined[secid] = bars
    for secid, bars in combined.items():
        if not read(secid):
            save(secid, bars)
    with connect() as conn:
        conn.execute("INSERT INTO migrations VALUES('json_v1')")
    return len(combined)


if __name__ == "__main__":
    print("Imported securities:", migrate())
