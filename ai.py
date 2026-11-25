# -*- coding: utf-8 -*-
"""调用本机 opencode-cli（当前模型）生成盘中研判。"""
import json
import re
import subprocess
import tempfile
import os
import time
from pathlib import Path

import config

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _fmt_yi(v):
    return round(v / 1e8, 2)


def _pick_brief(p):
    m = p.get("model") or {}
    brief = {
        "代码": p["code"],
        "名称": p["name"],
        "现价": p["price"],
        "涨幅%": p["pct"],
        "规则分": p.get("heuristic_score", p["score"]),
        "综合分": p["score"],
        "主力净占比%": p["main_pct"],
        "超大单净占比%": p["super_pct"],
        "5日主力净额(亿)": _fmt_yi(p.get("flow5", 0)),
        "量比": p["volratio"],
        "换手%": p["turnover"],
        "成交额(亿)": _fmt_yi(p["amount"]),
        "流通市值(亿)": _fmt_yi(p["floatcap"]),
        "60日涨幅%": p["pct60"],
        "所属行业": p["industry"],
        "意图": p["intent"],
        "均线": p["km"],
        "量化买区": [p["plan"]["buy_low"], p["plan"]["buy_high"]],
        "量化止损": p["plan"]["stop"],
        "量化目标": [p["plan"]["target1"], p["plan"]["target2"]],
        "可买": bool(p.get("actionable", True)),
        "未触发买入原因": p.get("watch_reasons") or [],
        "风险点": p["flags"],
    }
    if m.get("ok"):
        brief["3日模型"] = {
            "模型分": m.get("model_score"),
            "横截面排名前%": round((1 - float(m.get("rank_pct") or 0)) * 100, 1),
            "3日超额%": m.get("exp_excess_3d"),
            "3日上涨概率%": m.get("p_up_3d"),
            "三重障碍胜率%": m.get("p_tb"),
            "融合权重": m.get("weight"),
            "驱动因子": [x.get("name") + ("↑" if x.get("dir") == "+" else "↓")
                         for x in (m.get("drivers") or [])],
        }
    return brief


def build_prompt(payload):
    m = payload["market"]
    idx = "；".join("%s %.2f%%" % (i["name"], i["pct"]) for i in m["indices"])
    sec = "、".join("%s %.2f%%(主力%.2f亿)" % (s["name"], s["pct"], _fmt_yi(s["main"]))
                    for s in payload["sectors"]["top"][:5])
    weak = "、".join("%s %.2f%%" % (s["name"], s["pct"])
                    for s in payload["sectors"].get("bottom", [])[:3])
    trend = m.get("trend") or {}
    trend_txt = "；".join(
        "%s 收%.2f MA5=%.2f MA20=%.2f(%s) 5日%.2f%%"
        % (k, v["price"], v["ma5"], v["ma20"], "在20日线上" if v["above20"] else "在20日线下", v["pct5"])
        for k, v in trend.items()
    )
    perf = payload.get("performance") or {}
    session_cn = {"open": "交易中", "closed": "已收盘", "lunch": "午间休市",
                  "auction": "集合竞价", "weekend": "周末"}.get(payload.get("session"), "")
    cands = [_pick_brief(p) for p in payload["picks"]]
    data = {
        "时间": payload["generated_at"],
        "时段": session_cn,
        "交易日": payload["trade_date"],
        "数据源": payload.get("source"),
        "指数": idx,
        "指数趋势": trend_txt,
        "涨跌家数": {"上涨": m["up"], "下跌": m["down"], "平盘": m["flat"]},
        "涨跌停": {"涨停": m["limit_up"], "跌停": m["limit_down"]},
        "中位涨幅%": m["median_pct"],
        "两市成交额(亿)": round(m["amount"] / 1e8, 0),
        "市场状态": m["regime"],
        "行业资金流入前五": sec,
        "板块领跌": weak,
        "历史信号": {"跟踪数": perf.get("n", 0), "胜率%": perf.get("win_rate", 0),
                     "平均收益%": perf.get("avg_ret", 0)},
        "3日横截面模型": payload.get("ranker") or {},
        "绝对买入门槛": payload.get("trade_gate") or {},
        "候选池": cands,
        "旱地拔葱独立复核池": [
            {k: v for k, v in p.items() if k not in ("chart", "review", "pending")}
            for p in (payload.get("cong") or {}).get("items", [])[:12]
        ],
    }
    return (
        "你是A股短线交易分析员，只做1-3个交易日的短线研判。下面是刚抓取的实时/收盘数据。"
        "候选池由两层打分：规则五因子（趋势30、量价25、资金20、板块10、形态15）和"
        "每日重训的3日横截面排序模型（预测未来3日相对全市场中位数的超额，并给出三重障碍胜率）。"
        "综合分是两者按滚动RankIC自适应融合的结果，不是单纯规则分。\n\n"
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + "\n\n请完成：\n"
        "1) 用2-3句话判断当前市场情绪、主线板块和主要风险；\n"
        "2) 从候选池中挑不超过4只，说明主力意图（吸筹/进攻/出货/分歧）、入选逻辑、操作节奏（低吸/回踩/观察/回避）；\n"
        "3) 每只票必须判断“规则分、3日模型、消息面/资金面”是否一致，明确写“一致”或“背离/部分背离”；"
        "如果规则分高但3日模型排名靠后、3日超额为负，或模型分高但三重障碍胜率低/资金流出，必须提示背离；\n"
        "4) 若候选的“可买”为 false 或“未触发买入原因”非空，只能写观察/回避，不能写低吸/回踩，并明确说明未触发原因；\n"
        "5) 给出一句盘中风控提示。试探仓已关闭，禁止建议试仓、试探仓或小仓位试错；"
        "近期命中率是历史预测统计，不是真实成交胜率，不得称为实盘命中率。\n"
        "6) 旱地拔葱独立复核池不受原策略买入门槛排序影响。逐只核对defects，"
        "stage=watch表示接近形态观察池，尚未通过形态预筛，必须解释还需等待什么，不得称为形态通过。"
        "输出cong_reviews数组，每项为code、decision（保留观察/回避）、reason。"
        "解释瑕疵可否容忍以及需要的确认条件；没有筹码及逐笔数据，禁止声称筹码真空或持续买盘已确认，"
        "不能给买入结论，不能捏造缺失数据。盘中成交量不足不能直接断言全天缩量。\n\n"
        "硬性要求：picks和avoid只能使用原候选池代码；cong_reviews只能使用旱地拔葱独立复核池代码，不得虚构；不追高、不打板；符合T+1；"
        + ("候选池已剔除创业板、科创板股票，不要推荐这两类板块；"
           if not (config.ALLOW_CHINEXT or config.ALLOW_STAR) else "")
        + "不承诺收益；不要输出Markdown，只输出一个JSON对象，格式：\n"
        '{"market_view":"...","risk":"低|中|高","picks":[{"code":"600000","name":"...",'
        '"intent":"...","logic":"...","action":"低吸|回踩|观察|回避","confidence":75,'
        '"divergence":"一致|部分背离|背离"}],"avoid":[{"code":"...","reason":"..."}],'
        '"cong_reviews":[{"code":"600000","decision":"保留观察|回避","reason":"瑕疵判断与待确认条件"}],"notes":"..."}'
        + ("\n注意：本轮部分资金/量比字段缺失（数据源降级），相关结论需更谨慎，并在notes中说明。"
           if payload.get("source") != "eastmoney" else "")
    )


def _extract_json(text):
    text = ANSI.sub("", text)
    text = text.replace("```json", "").replace("```", "")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    raw = text[start:end + 1]
    for s in (raw, raw.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")):
        try:
            return json.loads(s)
        except Exception:
            continue
    return None


def comment_stock(a):
    """针对单只股票 + 用户买入价，返回包含预测、卖出价位和消息面的模型点评。"""
    st = a["stock"]
    plan = a["plan"]
    v = a["verdict"]
    fc = a.get("forecast") or {}
    sell = a.get("sell_plan") or {}
    news = a.get("news") or {}
    data = {
        "代码": st["code"], "名称": st["name"], "行业": st["industry"],
        "现价": st["price"], "涨幅%": st["pct"], "拟买入价": a["buy_price"],
        "量化分": a["score"], "分项": a["factors"],
        "主力意图": a["intent"], "资金细节": a["intent_detail"],
        "主力净占比%": a["main_pct"], "5日主力净额(亿)": a["flow5"],
        "均线": a["km"], "买区": [plan["buy_low"], plan["buy_high"]],
        "止损": plan["stop"], "目标": [plan["target1"], plan["target2"]],
        "盈亏比": v["rr"], "买入价结论": v["level"], "风险点": a["flags"],
        "未来2日预测": {
            "中性预期%": fc.get("median_return"),
            "80%区间%": [fc.get("low_return"), fc.get("high_return")],
            "目标价位": [fc.get("low_price"), fc.get("expected_price"), fc.get("high_price")],
            "上涨概率%": fc.get("p_up"), "方向": fc.get("direction"),
            "置信度": fc.get("confidence"), "方法": fc.get("method"),
            "校准": fc.get("validation"),
        },
        "量化卖出计划": {
            "动作": sell.get("action"), "信号": sell.get("signal"),
            "衰竭分": sell.get("score"), "原因": sell.get("reasons"),
            "价位": [
                {"名称": x.get("name"), "价格": x.get("price"), "比例": x.get("portion")}
                for x in (sell.get("levels") or [])
            ],
            "时间止损": sell.get("time_stop"),
        },
        "消息面": {
            "规则初筛": news.get("summary"),
            "标题": [
                {"时间": x.get("time"), "标题": x.get("title"),
                 "来源": x.get("source"), "初筛": x.get("tone")}
                for x in (news.get("items") or [])[:6]
            ],
        },
        "市场环境": a.get("market"),
    }
    prompt = (
        "你是A股短线交易分析员，只做1-3个交易日的判断。下面是单只股票的量化结果、"
        "未来2日概率预测、量化卖出计划、消息面标题和用户拟买入价：\n"
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + "\n\n请用5-6句话点评，必须覆盖：\n"
        "1) 买入价是否合理，是否追高；\n"
        "2) 2日预测的中性价位、80%区间与卖出计划（T1/T2/移动止损）是否自洽；\n"
        "3) 消息面是支持还是背离量化结论，只能引用上面给出的标题，不得编造新闻；"
        "如果冲突，明确写“消息面与量化结论背离”；\n"
        "4) 最大的一个风险和执行建议。\n\n"
        "不要输出Markdown或列表，不超过300字；不承诺收益；结尾注明“不构成投资建议”。"
    )
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="astock_one_") as td:
            proc = subprocess.run(
                [config.CLI_PATH, "run", "-m", config.MODEL,
                 "--title", "个股点评 " + st["code"],
                 "--format", "default", prompt],
                cwd=td,
                capture_output=True,
                timeout=config.AI_TIMEOUT,
                stdin=subprocess.DEVNULL,
                creationflags=config.NO_WINDOW,
            )
        out = ANSI.sub("", (proc.stdout or b"").decode("utf-8", "ignore")).strip()
        if not out:
            err = (proc.stderr or b"").decode("utf-8", "ignore").strip()
            raise RuntimeError(err[-160:] or "模型无输出")
        return {"ok": True, "text": out[:1200], "elapsed": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:180], "elapsed": round(time.time() - t0, 1)}


def apply_cong_reviews(payload, reviews):
    allowed = {p["code"]: p for p in (payload.get("cong") or {}).get("items", [])[:12]}
    if not isinstance(reviews, list):
        return
    for review in reviews:
        if not isinstance(review, dict):
            continue
        p = allowed.get(str(review.get("code")))
        decision = review.get("decision")
        reason = review.get("reason")
        if p is None or decision not in ("保留观察", "回避") or not isinstance(reason, str) or not reason.strip():
            continue
        p["review"] = {"status": "ok", "decision": decision, "reason": reason[:500]}
        p["actionable"] = False


def run_ai(payload):
    if not config.AI_ENABLED:
        payload["ai"] = dict(payload["ai"], status="off", notes="模型研判已关闭")
        return payload

    picks_by_code = {p["code"]: p for p in payload["picks"]}
    prompt = build_prompt(payload)
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="astock_ai_") as td:
            prompt_path = os.path.join(td, "analysis_prompt.txt")
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(prompt)
            proc = subprocess.run(
                [config.CLI_PATH, "run", "-m", config.MODEL,
                 "--title", "A股研判 " + payload["generated_at"],
                 "--format", "default", "--file", prompt_path,
                 "请依据附件中的数据和要求进行研判，仅返回要求的JSON对象。"],
                cwd=td,
                capture_output=True,
                timeout=config.AI_TIMEOUT,
                stdin=subprocess.DEVNULL,
                creationflags=config.NO_WINDOW,
            )
        out = (proc.stdout or b"").decode("utf-8", "ignore")
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        data = _extract_json(out)
        if not data:
            raise RuntimeError("模型未返回可解析JSON: " + (out or err)[-180:])
        apply_cong_reviews(payload, data.get("cong_reviews"))
        ai_picks = []
        for item in (data.get("picks") or [])[:config.AI_PICK_LIMIT]:
            code = str(item.get("code") or "").strip()
            p = picks_by_code.get(code)
            if not p:
                continue
            action = str(item.get("action") or "观察")[:10]
            # 未通过绝对门槛的票，模型只能给观察/回避，不能给买点动作。
            if p.get("actionable") is False and not p.get("trial") and action not in ("观察", "回避"):
                action = "观察"
            p["ai"] = {
                "intent": str(item.get("intent") or "")[:40],
                "logic": str(item.get("logic") or "")[:300],
                "action": action,
                "confidence": int(item.get("confidence") or p["confidence"]),
                "divergence": str(item.get("divergence") or "")[:20],
                "actionable": bool(p.get("actionable", True)),
            }
            ai_picks.append(code)
        payload["ai"] = {
            "status": "ok",
            "market_view": str(data.get("market_view") or payload["ai"]["market_view"])[:600],
            "risk": str(data.get("risk") or "中")[:4],
            "notes": str(data.get("notes") or "")[:400],
            "avoid": [
                {"code": str(x.get("code") or ""), "reason": str(x.get("reason") or "")[:120]}
                for x in (data.get("avoid") or [])[:6] if isinstance(x, dict)
            ],
            "elapsed": round(time.time() - t0, 1),
            "picked": ai_picks,
        }
        for item in payload["ai"]["avoid"]:
            p = picks_by_code.get(item["code"])
            if p and "ai" not in p:
                p["ai"] = {
                    "intent": "模型回避",
                    "logic": item["reason"],
                    "action": "回避",
                    "confidence": 0,
                }
                p["plan"]["position"] = "0%"
    except Exception as e:  # noqa: BLE001
        payload["ai"] = dict(
            payload["ai"],
            status="fallback",
            notes="模型研判调用失败（%s），已回退到规则结论。" % str(e)[:120],
            elapsed=round(time.time() - t0, 1),
        )
    return payload
