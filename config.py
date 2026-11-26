# -*- coding: utf-8 -*-
"""全局配置。"""
import glob
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
DB_PATH = DATA_DIR / "astock.db"
LATEST_PATH = DATA_DIR / "latest.json"
LOG_PATH = DATA_DIR / "pipeline.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# 演示模式：只用仓库里预置的 demo_data/ 快照，不抓实时数据、不写回。
# 必须在导入 single/store 等模块之前调用，它们会在导入时绑定路径。
DEMO_DATA_DIR = BASE_DIR / "demo_data"
DEMO_MODE = False


def use_demo_dir():
    """把数据目录切到 demo_data/，让看板在完全离线的情况下也能跑起来。"""
    global DATA_DIR, DB_PATH, LATEST_PATH, LOG_PATH, DEMO_MODE
    DATA_DIR = DEMO_DATA_DIR
    DB_PATH = DATA_DIR / "astock.db"
    LATEST_PATH = DATA_DIR / "latest.json"
    LOG_PATH = DATA_DIR / "demo.log"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEMO_MODE = True

# ---- 模型（走本机 opencode-cli，复用当前登录态）----
MODEL = "opencode/deepseek-v4.1-flash-free"


def _find_cli():
    env = os.environ.get("OPENCODE_CLI")
    if env and Path(env).exists():
        return env
    base = Path(os.environ.get("APPDATA", "")) / "ai.opencode.desktop" / "cli"
    hits = sorted(glob.glob(str(base / "*" / "opencode-cli.exe")))
    return hits[-1] if hits else "opencode-cli"


CLI_PATH = _find_cli()
AI_TIMEOUT = 240
AI_ENABLED = True

# ---- 服务 ----
# 部署到容器/云主机时用 ASTOCK_HOST=0.0.0.0 暴露到外部，ASTOCK_PORT 改端口
HOST = os.environ.get("ASTOCK_HOST", "127.0.0.1")
PORT = int(os.environ.get("ASTOCK_PORT", "8765"))

# ---- 选股参数（全A短线 1-3 日）----
MIN_PRICE = 3.0
MAX_PRICE = 100.0
MIN_FLOAT_CAP = 30e8       # 流通市值下限
MAX_FLOAT_CAP = 600e8      # 流通市值上限
MIN_AMOUNT = 1.5e8         # 当日成交额下限
MIN_TURNOVER = 1.5
MAX_TURNOVER = 18.0
MIN_VOL_RATIO = 0.9
MAX_PCT = 7.5              # 不追高
MIN_PCT = -4.0
MIN_LIST_DAYS = 90

# 可交易板块：创业板（300/301/302）、科创板（688/689）
ALLOW_CHINEXT = False
ALLOW_STAR = False

# 子进程（curl / 模型 CLI）不弹控制台窗口
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEEP_COUNT = 60            # 规则初筛进入日K精算的候选数
MODEL_EXTRA_COUNT = 30     # 从面板模型排名靠前、但规则初筛未进60的股票中补充的精算数
PICK_COUNT = 8             # 最终展示候选数
AI_PICK_LIMIT = 4          # 交给模型挑选的上限

# 评分权重（满分100）：趋势优先
SCORE_WEIGHTS = {"trend": 30, "vol": 25, "fund": 20, "sector": 10, "form": 15}

# 单票仓位上限（%）
POSITION_TIERS = [(68, 20), (60, 15), (52, 10), (0, 6)]
MAX_TOTAL_POS = 60         # 组合总仓位上限（%）

# 绝对买入门槛（不是排序分）：
# 横截面模型只能说明“相对强弱”，未通过以下门槛的标的只进观察池，仓位强制 0%。
# 2026-10 用近 120 个交易日验证期回测（次日开盘买、第3日收盘卖、扣0.2%成本）选定：
# 旧门槛 胜率约 43.6%、平均 -0.42%；新门槛 胜率约 49%、平均 +0.41%，信号明显变少。
ABS_PTB_MIN = 52.0         # 三重障碍胜率下限（%）
ABS_PUP_MIN = 55.0         # 风控要求，非已验证最优参数
LIVE_MIN_DAYS = 5
LIVE_MIN_HIT = 50.0
ABS_RR_MIN = 1.15          # 盈亏比下限
ABS_RANK_MIN = 0.90        # 横截面排名分位下限（前 10%）
ABS_BLOCK_RISK_OFF = True  # 大盘偏弱（上涨家数或站上20日线比例<40%）时不给买入
ABS_EXP_MIN = 0.0          # 3日超额收益下限（%）

# 试探仓：差一点没过绝对门槛、但没有明显硬伤的标的，允许小仓位试错。
# 想关掉试探仓，把 TRIAL_ENABLED 改成 False 即可。
TRIAL_ENABLED = False
TRIAL_PTB_MIN = 40.0       # 三重障碍胜率下限（%）
TRIAL_PUP_MIN = 40.0       # 3日上涨概率下限（%）
TRIAL_RR_MIN = 1.0         # 盈亏比下限
TRIAL_RANK_MIN = 0.40      # 横截面排名分位下限（前 60%）
TRIAL_EXP_MIN = 0.0        # 3日超额收益下限（%）
TRIAL_POSITION = 5         # 试探仓单票仓位（%）
TRIAL_MAX_COUNT = 2        # 每天最多几只试探仓

# 止损：从买入区上沿算起，最多亏这么多就止损（%）
MAX_STOP_PCT = 5.0

# ---- 公告事件风险过滤（events.py）----
# 背景：2026-11-10 煌上煌控股股东密集减持，11-12/11-13 两连跌停，技术形态完全失明。
# 规则只读东财公告标题：block 级（控股股东减持/立案/退市风险/冻结/违约）强制候选进观察池、
# 仓位 0%，旱地拔葱沉底；warn 级（一般减持/质押/解禁/预亏等）只标注不否决。
# 标题规则会有漏判误判，最终需人工复核公告全文。
EVENT_FILTER_ENABLED = True
EVENT_LOOKBACK_DAYS = 30       # 只看最近 N 天公告
EVENT_CACHE_TTL = 6 * 3600     # 每代码缓存 6 小时（公告盘后集中披露）

DISCLAIMER = (
    "本系统输出为量化统计与模型推演结果，仅用于研究复盘，不构成任何投资建议。"
    "股市有风险，决策需自行判断并独立承担后果。"
)


def setup_stdout():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
