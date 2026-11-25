# -*- coding: utf-8 -*-
"""东方财富公开行情接口封装（curl 传输，规避 TLS 风控）。"""
import json
import math
import shutil
import subprocess
import threading
import time

import config
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

CURL = shutil.which("curl") or "curl.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REF = "https://quote.eastmoney.com/"

# 东财有 IP 级限流：全局节流 + 有限并发
_em_lock = threading.Lock()
_em_sem = threading.Semaphore(2)
_em_last = [0.0]
EM_MIN_INTERVAL = 0.20
EM_COOLDOWN = 20 * 60
_EM_FLAG = config.DATA_DIR / "em_down.json"
_em_until = 0.0


def _em_ok():
    global _em_until
    if _em_until > time.time():
        return False
    try:
        obj = json.loads(_EM_FLAG.read_text(encoding="utf-8"))
        _em_until = float(obj.get("until") or 0)
    except Exception:
        _em_until = 0
    return _em_until <= time.time()


def _em_open():
    global _em_until
    _em_until = time.time() + EM_COOLDOWN
    try:
        _EM_FLAG.write_text(json.dumps({"until": _em_until}), encoding="utf-8")
    except OSError:
        pass


def _em_reset():
    global _em_until
    _em_until = 0.0
    try:
        _EM_FLAG.unlink()
    except OSError:
        pass

SNAPSHOT_FIELDS = (
    "f2,f3,f5,f6,f8,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,"
    "f24,f25,f26,f62,f66,f69,f72,f75,f78,f81,f84,f87,f100,f103,f184"
)
UNIVERSE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


def _throttle_em():
    with _em_lock:
        gap = time.time() - _em_last[0]
        if gap < EM_MIN_INTERVAL:
            time.sleep(EM_MIN_INTERVAL - gap)
        _em_last[0] = time.time()


def _curl_json(url, timeout=20, tries=4, referer=None):
    last = None
    em = "eastmoney.com" in url
    ref = referer or REF
    if em and not _em_ok():
        raise RuntimeError("东财熔断中")
    for i in range(tries):
        try:
            if em:
                with _em_sem:
                    _throttle_em()
                    p = subprocess.run(
                        [CURL, "-s", "--http1.1", "--max-time", str(timeout), "-A", UA, "-e", ref, url],
                        capture_output=True,
                        stdin=subprocess.DEVNULL,
                        creationflags=config.NO_WINDOW,
                    )
            else:
                p = subprocess.run(
                    [CURL, "-s", "--http1.1", "--max-time", str(timeout), "-A", UA, "-e", ref, url],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    creationflags=config.NO_WINDOW,
                )
            raw = p.stdout.decode("utf-8", "ignore").strip()
            if raw.startswith("{") or raw.startswith("["):
                if em:
                    _em_reset()
                return json.loads(raw)
            last = RuntimeError("bad response len=%d" % len(raw))
        except Exception as e:  # noqa: BLE001
            last = e
        if i + 1 < tries:
            time.sleep(1.2 * (i + 1))
    if em:
        _em_open()
    raise last


def _get(url):
    j = _curl_json(url)
    if j.get("rc") not in (0, None):
        raise RuntimeError("eastmoney rc=%s" % j.get("rc"))
    return j.get("data") or {}


def _sina_page(pn):
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a"
           "&symbol=&_s_r_a=page" % pn)
    arr = _curl_json(url, timeout=20)
    return arr if isinstance(arr, list) else []


def _curl_text_gbk(url, timeout=15, referer="https://finance.sina.com.cn/"):
    p = subprocess.run(
        [CURL, "-s", "--http1.1", "--max-time", str(timeout), "-A", UA, "-e", referer, url],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        creationflags=config.NO_WINDOW,
    )
    return p.stdout.decode("gbk", "ignore")


def fetch_indices_sina():
    """新浪指数快照，东财不可用时降级。"""
    syms = "s_sh000001,s_sz399001,s_sz399006,s_sh000688,s_sh000300,s_sh000905"
    txt = _curl_text_gbk("https://hq.sinajs.cn/list=" + syms)
    out = []
    for line in txt.split(";"):
        if "=" not in line:
            continue
        head, _, val = line.partition("=")
        val = val.strip().strip('"')
        p = val.split(",")
        if len(p) < 6:
            continue
        code = head.strip().split("_")[-1][2:]
        out.append({
            "f12": code, "f14": p[0], "f2": p[1], "f4": p[2],
            "f3": p[3], "f6": float(p[6] or 0) * 1e4 if len(p) > 6 else 0.0,
        })
    return out


def fetch_tencent_quotes(items):
    """腾讯批量行情，补全新浪快照缺失的量比等字段。items=[(code, market)]"""
    out = {}

    def one(batch):
        syms = ",".join(("sh" if int(m) == 1 else "sz") + c for c, m in batch)
        txt = _curl_text_gbk("https://qt.gtimg.cn/q=" + syms, referer="https://gu.qq.com/")
        res = {}
        for line in txt.split(";"):
            if "=" not in line:
                continue
            val = line.split("=", 1)[1].strip().strip('"')
            p = val.split("~")
            if len(p) < 50:
                continue
            try:
                res[p[2]] = {
                    "name": p[1],
                    "volratio": float(p[49] or 0),
                    "turnover": float(p[38] or 0),
                    "amount": float(p[37] or 0) * 1e4,
                    "price": float(p[3] or 0),
                    "pct": float(p[32] or 0),
                    "open": float(p[5] or 0),
                    "high": float(p[33] or 0),
                    "low": float(p[34] or 0),
                    "prev": float(p[4] or 0),
                    "volume": float(p[36] or 0),
                    "floatcap": float(p[44] or 0) * 1e8,
                    "totalcap": float(p[45] or 0) * 1e8,
                }
            except ValueError:
                continue
        return res

    batches = [items[i:i + 60] for i in range(0, len(items), 60)]
    if not batches:
        return out
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(one, batches):
            out.update(part)
    return out


def fetch_snapshot_sina():
    """降级方案：新浪全A快照（无主力资金/量比/行业）。"""
    first = _sina_page(1)
    total = 0
    try:
        c = _curl_json("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                       "Market_Center.getHQNodeStockCount?node=hs_a", timeout=15)
        txt = (c[0].get("stock_count") if isinstance(c, list) and c else "") or ""
        total = int(txt)
    except Exception:
        total = 0
    if total < 100:
        total = 0
    pages = int(math.ceil(total / 100.0)) if total else 60
    raw = list(first)
    if pages > 1:
        failed = []
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(_sina_page, pn): pn for pn in range(2, pages + 1)}
            for f in as_completed(futs):
                try:
                    raw.extend(f.result() or [])
                except Exception:
                    failed.append(futs[f])
        for pn in failed:
            try:
                raw.extend(_sina_page(pn) or [])
            except Exception:
                pass
    out = []
    for r in raw:
        code = str(r.get("code") or "")
        sym = str(r.get("symbol") or "")
        if not code or code.startswith(("4", "8", "9")):
            continue
        out.append({
            "f2": r.get("trade"), "f3": r.get("changepercent"), "f5": r.get("volume"),
            "f6": r.get("amount"), "f8": r.get("turnoverratio"), "f10": 0,
            "f12": code, "f13": 1 if sym.startswith("sh") else 0, "f14": r.get("name"),
            "f15": r.get("high"), "f16": r.get("low"), "f17": r.get("open"),
            "f18": r.get("settlement"), "f20": num_scale(r.get("mktcap")),
            "f21": num_scale(r.get("nmc")), "f24": 0, "f25": 0, "f26": "",
            "f62": 0, "f66": 0, "f69": 0, "f72": 0, "f75": 0, "f78": 0, "f81": 0,
            "f84": 0, "f87": 0, "f100": "未分类", "f103": "", "f184": 0,
        })
    return out, total or len(out)


def num_scale(v):
    try:
        return float(v) * 1e4  # 新浪 mktcap/nmc 单位为万元
    except (TypeError, ValueError):
        return 0.0


def _page(pn, pz=100):
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1"
        "&fltt=2&invt=2&fid=f3&fs=%s&fields=%s"
        % (pn, pz, UNIVERSE, SNAPSHOT_FIELDS)
    )
    try:
        return _get(url).get("diff") or []
    except Exception:
        return _get(url.replace("push2.eastmoney", "82.push2.eastmoney")).get("diff") or []


def _fetch_snapshot_em():
    """全A快照，按页并发拉取。"""
    first = _get(
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1"
        "&fltt=2&invt=2&fid=f3&fs=%s&fields=%s" % (UNIVERSE, SNAPSHOT_FIELDS)
    )
    total = int(first.get("total") or 0)
    rows = list(first.get("diff") or [])
    pages = int(math.ceil(total / 100.0))
    if pages > 1:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_page, pn): pn for pn in range(2, pages + 1)}
            for f in as_completed(futs):
                try:
                    rows.extend(f.result() or [])
                except Exception:
                    pn = futs[f]
                    try:
                        rows.extend(_page(pn) or [])
                    except Exception:
                        pass
    seen, out = set(), []
    for r in rows:
        key = (r.get("f13"), r.get("f12"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out, total


def fetch_snapshot():
    """东财优先，失败自动降级新浪。返回 (rows, total, source)。"""
    try:
        rows, total = _fetch_snapshot_em()
        if len(rows) > 1000:
            return rows, total, "eastmoney"
    except Exception:
        pass
    rows, total = fetch_snapshot_sina()
    if total and len(rows) < total * 0.85:
        try:
            rows2, total2 = fetch_snapshot_sina()
            if len(rows2) > len(rows):
                rows, total = rows2, total2
        except Exception:
            pass
    return rows, total, "sina"


def fetch_indices():
    secids = "1.000001,0.399001,0.399006,1.000688,1.000300,0.399905"
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=%s"
        "&fields=f2,f3,f4,f6,f12,f14" % secids
    )
    return _get(url).get("diff") or []


def fetch_industries():
    try:
        rows = _get(
            "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1"
            "&fltt=2&invt=2&fid=f62&fs=m:90+t:2"
            "&fields=f12,f14,f3,f62,f66,f69,f72,f75,f78,f81,f84,f87,f184"
        ).get("diff") or []
        if rows:
            return rows
    except Exception:
        pass
    return fetch_industries_sina()


def fetch_industries_sina():
    """新浪行业板块（含涨跌幅，无资金流）。"""
    import re
    txt = _curl_text_gbk("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php")
    m = re.search(r"=\s*(\{.*\})", txt, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for _, v in obj.items():
        p = str(v).split(",")
        if len(p) < 6:
            continue
        out.append({
            "f12": p[0], "f14": p[1], "f3": p[5],
            "f62": 0, "f184": 0,
        })
    return out


def _sina_board_page(node, page):
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=%s"
           "&symbol=&_s_r_a=page" % (page, node))
    arr = _curl_json(url, timeout=20)
    return arr if isinstance(arr, list) else []


def fetch_industry_map():
    """构建 股票代码 -> 新浪行业 映射（每日一次，用于板块打分）。"""
    boards = fetch_industries_sina()
    if not boards:
        return {}

    def one(b):
        node, name = str(b.get("f12")), str(b.get("f14"))
        out = {}
        for page in range(1, 4):
            try:
                arr = _sina_board_page(node, page)
            except Exception:
                break
            if not arr:
                break
            for r in arr:
                code = str(r.get("code") or "")
                if code:
                    out[code] = name
            if len(arr) < 100:
                break
        return out

    merged = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(one, boards):
            merged.update(part)
    return merged


def fetch_concepts():
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=40&po=1&np=1"
        "&fltt=2&invt=2&fid=f62&fs=m:90+t:3"
        "&fields=f12,f14,f3,f62,f184"
    )
    try:
        return _get(url).get("diff") or []
    except Exception:
        return []


def fetch_kline_tencent(code, market, lmt=70):
    """腾讯日K：优先前复权，WAF拦截时回退不复权接口（指数用后者）。"""
    sym = ("sh" if int(market) == 1 else "sz") + code
    urls = [
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (sym, lmt),
        "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=%s,day,,,%d" % (sym, lmt),
    ]
    arr = []
    for url in urls:
        try:
            j = _curl_json(url, timeout=15, tries=2, referer="https://gu.qq.com/")
        except Exception:
            continue
        node = (j.get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        if arr:
            break
    out = []
    prev = None
    for row in arr:
        if len(row) < 6:
            continue
        o, c, h, l, v = (float(row[1]), float(row[2]), float(row[3]),
                         float(row[4]), float(row[5]))
        pct = ((c / prev - 1) * 100) if prev else 0.0
        out.append({
            "date": row[0], "open": o, "close": c, "high": h, "low": l,
            "volume": v, "amount": 0.0,
            "amp": ((h - l) / prev * 100) if prev else 0.0,
            "pct": pct, "turnover": 0.0,
        })
        prev = c
    return out


def fetch_kline(secid, lmt=70, klt=101):
    """secid 形如 1.600519；优先腾讯，失败回退东财。"""
    market, code = secid.split(".", 1)
    try:
        kl = fetch_kline_tencent(code, market, lmt)
        if len(kl) >= 30:
            return kl
    except Exception:
        pass
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&klt=%d"
        "&fqt=1&lmt=%d&end=20500101&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61" % (secid, klt, lmt)
    )
    kl = _get(url).get("klines") or []
    out = []
    for line in kl:
        p = line.split(",")
        if len(p) < 11:
            continue
        out.append({
            "date": p[0],
            "open": float(p[1]),
            "close": float(p[2]),
            "high": float(p[3]),
            "low": float(p[4]),
            "volume": float(p[5]),
            "amount": float(p[6]),
            "amp": float(p[7]),
            "pct": float(p[8]),
            "turnover": float(p[10]),
        })
    return out


def fetch_fflow_sina(code, market, lmt=6):
    """新浪个股资金流（主力=netamount，超大单=r0_net）。"""
    sym = ("sh" if int(market) == 1 else "sz") + code
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=%d&sort=opendate&asc=0&daima=%s"
           % (lmt, sym))
    arr = _curl_json(url, timeout=15)
    if not isinstance(arr, list):
        return []
    out = []
    for r in reversed(arr):
        try:
            main = float(r.get("netamount") or 0)
            sup = float(r.get("r0_net") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "date": str(r.get("opendate") or ""),
            "main": main,
            "super": sup,
            "big": main - sup,
            "mid": 0.0,
            "small": 0.0,
            "main_pct": float(r.get("ratioamount") or 0) * 100,
            "super_pct": float(r.get("r0_ratio") or 0) * 100,
        })
    return out


def fetch_fflow(secid, lmt=6):
    market, code = secid.split(".", 1)
    return fetch_fflow_sina(code, market, lmt)


def fetch_fflow_em(secid, lmt=6):
    url = (
        "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=%d&klt=101"
        "&secid=%s&fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        % (lmt, secid)
    )
    kl = _get(url).get("klines") or []
    out = []
    for line in kl:
        p = line.split(",")
        if len(p) < 6:
            continue
        out.append({
            "date": p[0],
            "main": float(p[1]),
            "small": float(p[2]),
            "mid": float(p[3]),
            "big": float(p[4]),
            "super": float(p[5]),
        })
    return out


def fetch_trade_date():
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001"
           "&fields=f43,f57,f58,f86,f169,f170")
    d = _get(url)
    ts = d.get("f86")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")


def session_state(now=None):
    now = now or datetime.now()
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "weekend"
    if 9 * 60 + 15 <= hm < 9 * 60 + 30:
        return "auction"
    if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
        return "open"
    if 11 * 60 + 30 < hm < 13 * 60:
        return "lunch"
    if 13 * 60 <= hm <= 15 * 60:
        return "open"
    return "closed"
