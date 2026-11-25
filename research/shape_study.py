# -*- coding: utf-8 -*-
"""用 klines.db 验证形态细节的区分度：
在'旱地拔葱'形态层条件成立的日子里，按「均线粘合度 / 金叉反复次数 / 涨停基因」分组，
看后 1/3/5 日收益与涨跌停概率差异。
"""
import sqlite3, json, sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(str(ROOT / 'data' / 'klines.db'))
rows = con.execute('select secid, date, body from bars order by secid, date').fetchall()

series = defaultdict(list)
for secid, date, body in rows:
    try:
        k = json.loads(body)
    except Exception:
        continue
    series[secid].append(k)


def ema(v, n):
    k = 2.0 / (n + 1)
    out = [v[0]]
    for x in v[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


groups = defaultdict(list)
n_stocks = 0
for secid, bars in series.items():
    if len(bars) < 70:
        continue
    n_stocks += 1
    c = [float(b['close']) for b in bars]
    v = [float(b['volume']) for b in bars]
    h = [float(b['high']) for b in bars]
    lo = [float(b['low']) for b in bars]
    e12, e26 = ema(c, 12), ema(c, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]
    crosses = [i for i in range(1, len(c)) if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]]

    for i in range(65, len(c) - 6):
        ma5 = sum(c[i - 4:i + 1]) / 5
        ma10 = sum(c[i - 9:i + 1]) / 10
        ma20 = sum(c[i - 19:i + 1]) / 20
        ma20_old = sum(c[i - 24:i - 4]) / 20
        ma60 = sum(c[i - 59:i + 1]) / 60
        before = [sum(c[i - n:i]) / n for n in (5, 10, 20)]
        spread = (max(before) / min(before) - 1) * 100
        # 形态层核心：近6根内金叉、DIF>=0、红柱正、价站上MA20、粘合<=5%、MA20不下行
        recent = [x for x in crosses if 0 < i - x <= 6]
        if not recent:
            continue
        if not (dif[i] >= 0 and hist[i] > max(0, hist[i - 1]) and c[i] > ma20 and ma20 >= ma20_old):
            continue
        if spread > 5:
            continue
        r20 = (c[i - 1] / c[i - 21] - 1) * 100
        r60 = (c[i - 1] / c[i - 61] - 1) * 100
        if not (-5 <= r20 <= 15 and -10 <= r60 <= 30):
            continue
        vr = v[i] / (sum(v[i - 5:i]) / 5)
        if not (0.8 <= vr <= 4):
            continue
        # 特征
        cross20 = sum(1 for x in crosses if 0 < i - x <= 20)
        lim40 = sum(1 for j in range(max(0, i - 40), i) if c[j] / c[j - 1] - 1 >= 0.095)
        amp20 = (max(h[i - 20:i]) / min(lo[i - 20:i]) - 1) * 100
        f1 = (c[i + 1] / c[i] - 1) * 100
        f3 = (c[i + 3] / c[i] - 1) * 100
        f5 = (c[i + 5] / c[i] - 1) * 100
        rec = dict(code=secid, date=bars[i]['date'], spread=spread, cross20=cross20,
                   lim40=lim40, f1=f1, f3=f3, f5=f5, vr=vr, amp20=amp20,
                   up1=f1 >= 9.7, dn1=f1 <= -9.7)
        sb = 'A 粘合≤1%' if spread <= 1 else ('B 1-2%' if spread <= 2 else ('C 2-3.5%' if spread <= 3.5 else 'D 3.5-5%'))
        cb = '金叉≤2次' if cross20 <= 2 else ('金叉3次' if cross20 == 3 else '金叉≥4次')
        lb = '有涨停基因' if lim40 >= 1 else '无涨停基因'
        groups['全样本'].append(rec)
        groups['spread|' + sb].append(rec)
        groups['cross|' + cb].append(rec)
        groups['limit|' + lb].append(rec)
        groups['spread+limit|%s / %s' % (sb, lb)].append(rec)
        tb = 'MA20>MA60' if ma20 > ma60 else 'MA20<MA60'
        groups['trend|' + tb].append(rec)
        groups['trend|' + ('r60>0' if r60 > 0 else 'r60<=0')].append(rec)
        groups['trend2|%s + %s' % (tb, 'r60>0' if r60 > 0 else 'r60<=0')].append(rec)


def stat(g):
    n = len(g)
    if not n:
        return None
    return dict(n=n, f1=sum(x['f1'] for x in g) / n, f3=sum(x['f3'] for x in g) / n,
                f5=sum(x['f5'] for x in g) / n,
                up=sum(1 for x in g if x['up1']) / n * 100,
                dn=sum(1 for x in g if x['dn1']) / n * 100,
                win=sum(1 for x in g if x['f5'] > 0) / n * 100)


print('样本股票数:', n_stocks)
print()
for key in ['全样本'] + sorted(k for k in groups if k.startswith('spread|')) + \
           sorted(k for k in groups if k.startswith('cross|')) + \
           sorted(k for k in groups if k.startswith('limit|')) + \
           sorted(k for k in groups if k.startswith('spread+limit|')) +            sorted(k for k in groups if k.startswith('trend|')) +            sorted(k for k in groups if k.startswith('trend2|')):
    s = stat(groups[key])
    if not s:
        continue
    print('%-34s n=%6d 次日%+6.2f%% 3日%+6.2f%% 5日%+6.2f%% 5日胜率%5.1f%% 次日涨停%5.2f%% 次日跌停%5.2f%%'
          % (key, s['n'], s['f1'], s['f3'], s['f5'], s['win'], s['up'], s['dn']))

out = ROOT / 'data' / 'shape_study.json'
out.parent.mkdir(parents=True, exist_ok=True)
json.dump({k: stat(v) for k, v in groups.items()}, open(out, 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
