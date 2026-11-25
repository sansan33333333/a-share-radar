# -*- coding: utf-8 -*-
"""一次完整分析：抓数 -> 评分 -> 模型研判 -> 入库 -> 输出 latest.json"""
import argparse
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import ai
import config
import cong
import datasource as ds
import engine
import events
import panel
import ranker
import single
import store
import kline_store

LOCK = config.DATA_DIR / ".run.lock"
KLINE_CACHE_VERSION = 2


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def acquire_lock():
    if LOCK.exists():
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            age = 0
        if age < 900:
            return False
        try:
            LOCK.unlink()
        except OSError:
            return False
    try:
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def release_lock():
    try:
        LOCK.unlink()
    except OSError:
        pass


def safe(fut, default):
    try:
        return fut.result()
    except Exception:
        return default


def load_klines(items, trade_date, refresh=False):
    """两套策略共用数据库；refresh兼容旧调用，统一按时效增量更新。"""
    kline_store.migrate()
    return kline_store.load(items, trade_date, log=log)


def load_flows(items):
    def one(item):
        code, market = item
        try:
            return code, ds.fetch_fflow_sina(code, market, lmt=6)
        except Exception:
            return code, []

    with ThreadPoolExecutor(max_workers=4) as ex:
        return {code: ff for code, ff in ex.map(one, items)}


def load_industry_map(trade_date):
    """行业映射按交易日缓存，补全降级模式下缺失的行业字段。"""
    cache_path = config.DATA_DIR / "industry_map.json"
    try:
        obj = json.loads(cache_path.read_text(encoding="utf-8"))
        if obj.get("date") == trade_date and obj.get("data"):
            return obj["data"]
    except Exception:
        pass
    try:
        mapping = ds.fetch_industry_map()
    except Exception:
        mapping = {}
    if mapping:
        try:
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"date": trade_date, "data": mapping}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(str(tmp), str(cache_path))
        except Exception:
            pass
    return mapping


def apply_industry(rows, mapping):
    if not mapping:
        return 0
    n = 0
    for r in rows:
        code = str(r.get("f12") or "")
        name = mapping.get(code)
        if name and (not r.get("f100") or r.get("f100") == "未分类"):
            r["f100"] = name
            n += 1
    return n


def load_index_trend():
    def one(item):
        name, secid = item
        try:
            return name, engine.trend_metrics(ds.fetch_kline(secid, lmt=30))
        except Exception:
            return name, None

    with ThreadPoolExecutor(max_workers=2) as ex:
        return {name: v for name, v in ex.map(one, [("上证", "1.000001"), ("创业板", "0.399006")]) if v}


def track_performance(price_map):
    """跟踪历史候选自首次推荐以来的收益。"""
    runs = store.recent_payloads(12)
    seen = {}
    for run in reversed(runs):
        ts = run.get("generated_at") or ""
        for p in run.get("picks") or []:
            code = p.get("code")
            rec = p.get("price")
            if not code or not rec or code in seen or not engine.board_allowed(code):
                continue
            seen[code] = {
                "code": code, "name": p.get("name"), "rec": rec, "ts": ts,
                "intent": p.get("intent"), "score": p.get("score"),
            }
    items = []
    for code, x in seen.items():
        now = price_map.get(code)
        if not now:
            continue
        items.append(dict(x, now=now, ret=round((now / x["rec"] - 1) * 100, 2),
                          days=max(0, (datetime.now() - datetime.strptime(x["ts"][:10], "%Y-%m-%d")).days)
                          if x["ts"] else 0))
    if not items:
        return {"n": 0, "win_rate": None, "avg_ret": 0, "tested": 0, "items": []}
    moved = [i for i in items if abs(i["ret"]) >= 0.05]
    avg = sum(i["ret"] for i in items) / len(items)
    win_rate = (round(sum(1 for i in moved if i["ret"] > 0) / len(moved) * 100, 1)
                if moved else None)
    items.sort(key=lambda i: i["ret"], reverse=True)
    return {
        "n": len(items),
        "win_rate": win_rate,
        "avg_ret": round(avg, 2),
        "tested": len(moved),
        "items": items[:8],
    }


def build_alerts(prev, payload, price_map):
    alerts = []
    if not prev:
        return alerts
    curr = {p["code"]: p for p in payload["picks"]}
    for p in prev.get("picks") or []:
        code = p.get("code")
        plan = p.get("plan") or {}
        px = price_map.get(code) or p.get("price") or 0
        name = p.get("name") or code
        if code in curr:
            if px and plan.get("buy_low") and pl(plan, "buy_low") <= px <= pl(plan, "buy_high"):
                alerts.append({"level": "info", "text": "%s(%s) 回到买区 %.2f-%.2f" %
                               (name, code, plan["buy_low"], plan["buy_high"])})
            elif px and plan.get("stop") and px <= plan["stop"]:
                alerts.append({"level": "danger", "text": "%s(%s) 触发止损 %.2f，建议离场" %
                               (name, code, plan["stop"])})
            elif px and plan.get("buy_high") and px > plan["buy_high"] * 1.02:
                alerts.append({"level": "warn", "text": "%s(%s) 已高于买区上沿，不追高" %
                               (name, code)})
        else:
            alerts.append({"level": "warn", "text": "%s(%s) 资金/评分转弱，移出候选" % (name, code)})
    for code, p in curr.items():
        if code not in {x.get("code") for x in prev.get("picks") or []}:
            alerts.append({"level": "new", "text": "新入选：%s(%s) 评分 %.1f，意图 %s" %
                           (p["name"], code, p["score"], p["intent"])})
    return alerts[:10]


def pl(plan, key):
    try:
        return float(plan.get(key))
    except (TypeError, ValueError):
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略交易时段限制")
    ap.add_argument("--no-ai", action="store_true", help="跳过模型研判")
    args = ap.parse_args()

    config.setup_stdout()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    session = ds.session_state(now)

    if not args.force:
        if session in ("weekend", "lunch"):
            log("非交易时段(%s)，跳过。" % session)
            return 0

    if not acquire_lock():
        log("已有分析任务在运行，跳过。")
        return 0

    try:
        log("开始抓取数据…")
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_snap = ex.submit(ds.fetch_snapshot)
            f_idx = ex.submit(ds.fetch_indices)
            f_ind = ex.submit(ds.fetch_industries)
            f_con = ex.submit(ds.fetch_concepts)
            rows, total, source = f_snap.result()
            indices = safe(f_idx, [])
            industries = safe(f_ind, [])
            concepts = safe(f_con, [])
            if not indices:
                try:
                    indices = ds.fetch_indices_sina()
                except Exception:
                    indices = []
        trade_date = None
        try:
            trade_date = ds.fetch_trade_date()
        except Exception:
            pass
        today = now.strftime("%Y-%m-%d")
        if not args.force and trade_date and trade_date != today:
            log("最近交易日为 %s，今日休市，跳过。" % trade_date)
            return 0
        log("快照 %d 只（数据源 %s，接口总数 %d），行业 %d 个，最新交易日 %s"
            % (len(rows), source, total, len(industries), trade_date or today))

        ind_map = load_industry_map(trade_date or today)
        applied = apply_industry(rows, ind_map)
        log("行业映射 %d 只（补全 %d 只）" % (len(ind_map), applied))
        log("搜索索引 %d 只，行业排名 %d 个"
            % (single.save_symbols(rows), single.save_industries(industries)))

        pool, _ = engine.preselect(rows, industries, limit=600)
        try:
            quotes = ds.fetch_tencent_quotes(pool)
        except Exception:
            quotes = {}
        filled = engine.apply_quotes(rows, quotes)
        log("腾讯量比补全 %d 只（批量 %d 只）" % (filled, len(quotes)))

        deep, filtered = engine.preselect(rows, industries)
        log("筛选后 %d 只，进入日K精算 %d 只…" % (filtered, len(deep)))
        klines = load_klines(deep, trade_date or today)
        log("日K就绪 %d 只" % len(klines))
        fflows = load_flows(deep)
        log("资金流就绪 %d 只" % sum(1 for v in fflows.values() if v))

        # 公告事件风险过滤：候选池 + 旱地拔葱池统一评估（带缓存，公告盘后披露）
        ev_codes = [c for c, _ in deep]
        event_ctx = events.annotate_batch(ev_codes, log=log)

        try:
            ranker_ctx = ranker.prepare(rows, deep, klines, trade_date or today, log=log)
        except Exception as e:  # noqa: BLE001
            log("横截面模型初始化失败：%s" % str(e)[:160])
            ranker_ctx = {"ok": False, "error": str(e)[:180], "scores": {}, "blend_weight": 0.0}
        log(ranker.summary_text(ranker_ctx))

        # 规则初筛只取前60，可能漏掉“规则分一般但模型排名很高”的股票。
        # 把面板模型 Top 候选补进精算池；日K直接复用面板缓存，不重复抓取。
        extra_codes = []
        try:
            deep_set = {str(c) for c, _ in deep}
            pc = panel.ensure_panel(rows, trade_date or today)
            for item in (ranker_ctx.get("panel_top") or []):
                code = str(item.get("code") or "")
                if not code or code in deep_set or code in extra_codes:
                    continue
                bars = panel.bars_for(pc, code)
                if bars:
                    klines[code] = bars
                    extra_codes.append(code)
                if len(extra_codes) >= config.MODEL_EXTRA_COUNT:
                    break
            if extra_codes:
                market_by_code = {s["code"]: s["market"] for s in map(engine.enrich, rows)}
                need_flow = [c for c in extra_codes if not fflows.get(c)]
                if need_flow:
                    fflows.update(load_flows([(c, market_by_code.get(c, 0)) for c in need_flow]))
                log("模型补充候选 %d 只（规则初筛外）" % len(extra_codes))
        except Exception as e:  # noqa: BLE001
            log("模型补充候选失败：%s" % str(e)[:120])

        payload = engine.build_payload(
            rows, indices, industries, concepts, klines, fflows,
            trade_date or today, started_at, ranker_ctx=ranker_ctx,
            extra_codes=extra_codes, event_ctx=event_ctx,
        )
        payload["session"] = session
        # 独立股票池，不受原策略前60名或模型Top候选限制。
        cong_stocks = cong.universe(rows)
        cong_klines = load_klines([(s["code"], s["market"]) for s in cong_stocks], trade_date or today, refresh=True)
        # 板块共振：同一快照内按行业统计涨停/涨超5%家数，用于排序与降级
        cong_reson = cong.resonance(rows, industries)
        cong_result = cong.scan(cong_stocks, cong_klines, trade_date or today, reson=cong_reson)
        # 旱地拔葱同样过公告事件过滤：block 级沉底并标注，避免"形态分高、大股东在卖"
        cong_items = cong_result.get("items") or []
        if cong_items:
            cong_events = events.annotate_batch([p["code"] for p in cong_items], log=log)
            for p in cong_items:
                ev = cong_events.get(p["code"]) or {}
                p["event"] = ev if ev else None
                if ev.get("level") == "block":
                    p["event_blocked"] = True
                    tag = "公告利空：" + (events.short_reason(ev) or "详见公告")
                    if tag not in (p.get("defects") or []):
                        p.setdefault("defects", []).append(tag)
                    p["status"] = "公告硬利空 · 不建议跟踪"
            cong_items.sort(key=lambda p: (bool(p.get("event_blocked")),
                                           p.get("stage") != "shape",
                                           -p["fit"]["score"], p["code"]))
            cong_result["event_blocked_count"] = sum(1 for p in cong_items if p.get("event_blocked"))
            cong_result["note"] = (cong_result.get("note") or "") + " 已叠加公告事件过滤（block级沉底）。"
        payload["cong"] = cong_result
        log("旱地拔葱：池内%d只，形态预筛%d只，接近形态观察%d只，日K不足/过期%d只，公告利空%d只，板块无共振降级%d只" % (
            len(cong_stocks), cong_result["shape_count"], cong_result["watch_count"],
            cong_result["missing"], cong_result.get("event_blocked_count", 0),
            cong_result.get("reson_weak_count", 0)))
        payload["source"] = source
        payload["boards"] = ("仅主板" if not (config.ALLOW_CHINEXT or config.ALLOW_STAR)
                             else "含创业板/科创板")
        payload["market"]["trend"] = load_index_trend()
        log("候选 %d 只，市场 %s" % (len(payload["picks"]), payload["market"]["regime"]))

        price_map = {s["code"]: s["price"] for s in map(engine.enrich, rows) if s["price"]}
        prev = store.previous()
        payload["alerts"] = build_alerts(prev, payload, price_map)
        payload["performance"] = track_performance(price_map)
        log("信号跟踪 %d 条，胜率 %s，平均 %.2f%%"
            % (payload["performance"]["n"],
               "待验证" if payload["performance"]["win_rate"] is None
               else "%.1f%%" % payload["performance"]["win_rate"],
               payload["performance"]["avg_ret"]))

        if args.no_ai:
            payload["ai"] = dict(payload["ai"], status="off", notes="本次跳过了模型研判。")
        else:
            log("调用模型研判（%s）…" % config.MODEL)
            payload = ai.run_ai(payload)
            log("模型状态：%s（%.1fs）" % (payload["ai"].get("status"), payload["ai"].get("elapsed", 0)))

        payload["ai"]["market_view"] = payload["ai"].get("market_view") or ""
        payload["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        store.save(payload)
        tmp = config.LATEST_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(config.LATEST_PATH))

        log("完成：候选 %d 只，市场 %s，AI %s"
            % (len(payload["picks"]), payload["market"]["regime"], payload["ai"]["status"]))
        for p in payload["picks"][:3]:
            log("  %s %s %.2f%% 评分%.1f %s 买区%.2f-%.2f 止损%.2f"
                % (p["code"], p["name"], p["pct"], p["score"], p["intent"],
                   p["plan"]["buy_low"], p["plan"]["buy_high"], p["plan"]["stop"]))
        return 0
    except Exception as e:  # noqa: BLE001
        log("运行失败：%s" % e)
        log(traceback.format_exc()[-1200:])
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
