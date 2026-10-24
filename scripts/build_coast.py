#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""產生「戰情圖」離線底圖用的海岸線：assets/coast.json。

**為什麼需要這支程式**

地圖原本只有圖磚底圖（衛星、街道、暗色），颱風天行動網路一斷，
圖磚抓不到，地圖就剩一片灰底，只看得到路徑線。
戰情圖改用向量海岸線自己畫：檔案跟程式一起被 Service Worker 快取，
裝過一次之後完全離線也畫得出臺灣與周邊陸地。

資料來源是 Natural Earth（公有領域，可自由使用）：
臺灣附近（FINE 框）用 1:10m 陸地與小島，保留約 400 m 的細節；
其餘全球用 1:50m 陸地——使用者縮小地圖時要看到完整的世界，不能只有西北太平洋一塊。
兩份用「環是不是整個落在 FINE 框裡」分家，不做裁切：裁切會在框邊留下一條假的海岸線。
再用 Douglas–Peucker 簡化（臺灣周邊約 3 km、亞太約 6 km、其他約 20 km），太小的礁不收。

臺灣（本島、澎湖、綠島、蘭嶼、小琉球、龜山島、金門、馬祖、彭佳嶼）另外分一組，
前端用發光的青色描邊，鄰近陸地用暗色。分組用「島的中心點落在哪個框」判斷，
金門北界取 24.54 度、本島框從東經 120 度起算 —— 再寬就會把大嶝島、南日島、湄洲島
這些福建沿海的島算成臺灣。

用法：
  python3 scripts/build_coast.py build   # 下載 Natural Earth、輸出 assets/coast.json（要連網）
  python3 scripts/build_coast.py selftest   # 離線自我測試
"""
import json
import math
import os
import sys
import tempfile
from urllib.request import urlopen

NE_URL = 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson'
FINE_FILES = ('ne_10m_land', 'ne_10m_minor_islands')
WORLD_FILE = 'ne_50m_land'
FINE = (117.5, 20.5, 123.5, 27.0)          # 臺灣附近保留較細的海岸線（經度西、緯度南、經度東、緯度北）
NEAR = (110.0, 12.0, 135.0, 35.0)          # 臺灣周邊：對焦站臺時看得到的範圍，簡化最細
AP = (95.0, -12.0, 165.0, 50.0)            # 亞太：颱風會經過的範圍，次細
# 簡化容許誤差（度）與最小面積（平方度）：臺灣周邊、亞太、其他。
# 全球都用最細的話檔案有 230 KB；照距離分三級是 140 KB 左右（gzip 約 50 KB）。
TOL = {'near': (0.03, 0.004), 'ap': (0.06, 0.01), 'world': (0.2, 0.15)}
TW_BOXES = [(120.0, 21.85, 122.1, 25.35),  # 本島、綠島、蘭嶼、小琉球、龜山島（東經 120 度以東才不會吃到福建沿海的島）
            (119.3, 23.15, 119.75, 23.8),  # 澎湖
            (118.2, 24.37, 118.5, 24.54),  # 金門、烈嶼（北界 24.54：再往北是大嶝島）
            (119.85, 25.9, 120.55, 26.42),  # 馬祖（含莒光、東引）
            (121.9, 25.55, 122.15, 25.7)]  # 彭佳嶼一帶
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', 'coast.json')


def inside(p, b):
    return b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3]


def simplify(pts, tol):
    """Douglas–Peucker（迭代版：大陸海岸線上萬個點，遞迴會超過 Python 的深度上限）。"""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, c = stack.pop()
        ax, ay = pts[a]
        cx, cy = pts[c]
        dx, dy = cx - ax, cy - ay
        span = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for i in range(a + 1, c):
            px, py = pts[i]
            d = (abs(dy * px - dx * py + cx * ay - cy * ax) / span) if span else math.hypot(px - ax, py - ay)
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, c)]
    return [p for p, k in zip(pts, keep) if k]


def area(r):
    return abs(sum(r[i][0] * r[i - 1][1] - r[i - 1][0] * r[i][1] for i in range(len(r)))) / 2


def is_taiwan(c):
    return any(inside(c, b) for b in TW_BOXES)


def _rings(gj):
    for ft in gj['features']:
        g = ft['geometry']
        polys = g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']]
        for poly in polys:
            for ring in poly:
                yield [tuple(p[:2]) for p in ring]


def _bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _flat(ring, nd):
    out = []
    for x, y in ring[:-1]:
        out += [round(x, nd), round(y, nd)]
    return out


def build(fine_gjs, world_gj):
    """fine_gjs：1:10m（陸地、小島），只取整個落在 FINE 框裡的環；world_gj：1:50m 陸地，取其餘全部。"""
    tw, land, seen = [], [], set()
    inbox = (lambda bb, b: bb[0] >= b[0] and bb[1] >= b[1] and bb[2] <= b[2] and bb[3] <= b[3])
    for gj in fine_gjs:
        for ring in _rings(gj):
            if len(ring) < 4 or not inbox(_bbox(ring), FINE):
                continue
            c = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
            tw_ring = is_taiwan(c)
            if ring[0] != ring[-1]:
                ring = ring + [ring[0]]
            s = simplify(ring, 0.004)
            if len(s) < 4:
                if not tw_ring:
                    continue
                s = ring                           # 臺灣的小島一個都不丟
            a = area(s)
            if not tw_ring and a < 0.0004:
                continue                           # 太小的礁：畫出來只是一個點,徒增檔案大小
            key = (round(c[0], 2), round(c[1], 2), round(a, 3))
            if key in seen:
                continue                           # 陸地檔與小島檔重疊的島只留一份
            seen.add(key)
            (tw if tw_ring else land).append(_flat(s, 3))
    for ring in _rings(world_gj):
        if len(ring) < 4:
            continue
        bb = _bbox(ring)
        if inbox(bb, FINE):
            continue                               # 臺灣附近用上面那份 1:10m 的
        hit = lambda b: not (bb[2] < b[0] or bb[0] > b[2] or bb[3] < b[1] or bb[1] > b[3])  # noqa: E731
        tol, min_a = TOL['near' if hit(NEAR) else 'ap' if hit(AP) else 'world']
        if ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        s = simplify(ring, tol)
        if len(s) < 4 or area(s) < min_a:
            continue
        land.append(_flat(s, 2))
    return {'src': 'Natural Earth 1:10m／1:50m（公有領域）', 'tw': tw, 'land': land}


def selftest():
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            raise SystemExit('FAIL: ' + msg)
        ok += 1

    # 簡化：一條直線上的中間點全部拿掉；明顯的轉角保留
    line = [(0, 0), (1, 0.0001), (2, 0), (3, 0.0001), (4, 0)]
    check(simplify(line, 0.01) == [(0, 0), (4, 0)], '共線的點被簡化掉')
    corner = [(0, 0), (1, 1), (2, 0)]
    check(simplify(corner + [(3, 0)], 0.01)[1] == (1, 1), '轉角保留')
    big = [(math.cos(i / 20000 * 2 * math.pi), math.sin(i / 20000 * 2 * math.pi)) for i in range(20001)]
    check(len(simplify(big, 0.01)) < 200, '兩萬點的環不會遞迴爆掉，而且有簡化')
    # 臺灣分組：本島、澎湖、金門、馬祖、綠島、蘭嶼、龜山島在內；福建沿海的島不能算進來
    for name, c in (('本島', (120.9, 23.7)), ('澎湖', (119.59, 23.56)), ('金門', (118.37, 24.45)),
                    ('烈嶼', (118.24, 24.43)), ('南竿', (119.93, 26.15)), ('東引', (120.49, 26.37)),
                    ('綠島', (121.48, 22.66)), ('蘭嶼', (121.55, 22.04)), ('龜山島', (121.94, 24.84)),
                    ('小琉球', (120.36, 22.34))):
        check(is_taiwan(c), f'{name}算臺灣')
    for name, c in (('廈門島', (118.12, 24.48)), ('大嶝島', (118.32, 24.56)), ('南日島', (119.51, 25.2)),
                    ('湄洲島', (119.47, 24.99)), ('與那國島', (123.0, 24.47))):
        check(not is_taiwan(c), f'{name}不算臺灣')
    # 整條管線：臺灣附近用 1:10m、其他用 1:50m；兩邊不重複、不裁切
    sq = lambda x0, y0, x1, y1: [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]  # noqa: E731
    fine = {'features': [
        {'geometry': {'type': 'Polygon', 'coordinates': sq(120.5, 23.0, 121.5, 24.5)}},        # 臺灣（框內）
        {'geometry': {'type': 'Polygon', 'coordinates': sq(118.6, 25.0, 118.9, 25.3)}},        # 福建外海的島（框內、非臺灣）
        {'geometry': {'type': 'MultiPolygon', 'coordinates': [sq(115.0, 22.0, 119.0, 30.0)]}},  # 大陸（跨出框）：1:10m 不收
    ]}
    world = {'features': [
        {'geometry': {'type': 'Polygon', 'coordinates': sq(120.4, 22.9, 121.6, 24.6)}},        # 臺灣的 1:50m 版本：不收
        {'geometry': {'type': 'Polygon', 'coordinates': sq(115.0, 22.0, 119.0, 30.0)}},        # 大陸：收（1:50m）
        {'geometry': {'type': 'Polygon', 'coordinates': sq(-100.0, 30.0, -80.0, 45.0)}},       # 美洲：收（全球都要畫）
        {'geometry': {'type': 'Polygon', 'coordinates': sq(10.0, 10.0, 10.05, 10.05)}},        # 遠方小礁：太小不收
    ]}
    out = build([fine], world)
    check(len(out['tw']) == 1 and out['tw'][0][:2] == [120.5, 23.0], '臺灣用 1:10m、座標三位小數')
    lands = sorted(r[0] for r in out['land'])
    check(lands == [-100.0, 115.0, 118.6], f'陸地：大陸與美洲取 1:50m、框內小島取 1:10m、遠方小礁不收（{lands}）')
    check(json.loads(json.dumps(out)) == out, '可以序列化成 JSON')
    print(f'selftest OK（{ok} 項）')


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'selftest':
        return selftest()
    if cmd != 'build':
        raise SystemExit(__doc__)
    gjs = {}
    with tempfile.TemporaryDirectory() as td:
        for name in FINE_FILES + (WORLD_FILE,):
            path = os.path.join(td, name + '.geojson')
            with urlopen(NE_URL.format(name), timeout=120) as res, open(path, 'wb') as f:
                f.write(res.read())
            with open(path, encoding='utf-8') as f:
                gjs[name] = json.load(f)
    out = build([gjs[n] for n in FINE_FILES], gjs[WORLD_FILE])
    if not out['tw'] or not out['land']:
        raise SystemExit('輸出是空的：Natural Earth 的格式可能變了，不覆蓋原檔')
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'), ensure_ascii=False)
    n = lambda rings: sum(len(r) // 2 for r in rings)  # noqa: E731
    print(f'臺灣 {len(out["tw"])} 個環 {n(out["tw"])} 點；鄰近陸地 {len(out["land"])} 個環 {n(out["land"])} 點 → {OUT}')


if __name__ == '__main__':
    main()
