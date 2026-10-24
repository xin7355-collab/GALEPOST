#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建一份西北太平洋颱風強度表（data/tyhist.json），給前端算「今年第幾強、1951 年以來第幾強」。

**資料來源**：IBTrACS v04r01（NOAA NCEI 整理的國際最佳路徑），西北太平洋（WP）那一份，
取日本氣象廳（TOKYO_* 欄位）的 10 分鐘平均風與中心氣壓——日本氣象廳是西北太平洋的
世界氣象組織區域專業中心，1951 年起有最佳路徑，口徑一致。
實際跑過才知道的兩個缺口（第一次建出來 1951–1976 與最近兩年都是 0 顆）：
- 日本氣象廳 1977 年以前沒有分析最大風速，只有等級（9＝輕度颱風以上）→ 用等級判斷是不是命名颱風。
- 最近一兩年日本氣象廳的最終分析還沒進 IBTrACS，暫定資料只有美國聯合颱風警報中心（JTWC，USA_* 欄位）
  → 這幾顆改用 JTWC 的氣壓，標來源 "U"（口徑不同：JTWC 是 1 分鐘平均風，前端註明）。
當年的颱風是 IBTrACS 的「暫定」資料（provisional），每週更新，會比現場慢幾天。

**排名用最低中心氣壓**（越低越強），同氣壓再比最大風速。前端拿目前這顆颱風的
中央氣象署分析值去比——同一顆颱風兩個單位的分析常差幾到十幾百帕，所以前端註明排名是約略的、並寫出來源。

原始檔七、八十 MB：**逐行串流讀**，每顆颱風只留最低氣壓與最大風速，不整份載入記憶體。

輸出：data/tyhist.json
  {"v":2, "src":"...", "built":"...", "through":"2026-10-15 18:00:00", "from":1951,
   "storms":[[1979,"TIP",870,140,"J"], ...]}   # [年份, 英文名, 最低氣壓 hPa, 最大風速 kt（0＝沒有）, 來源 J／U]，依氣壓排序

  python3 scripts/build_tyhist.py build      # 下載並重建（GitHub Actions 每週跑一次）
  python3 scripts/build_tyhist.py selftest   # 離線自我測試
"""
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = os.environ.get("IBTRACS_WP_URL", "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-"
                     "stewardship-ibtracs/v04r01/access/csv/ibtracs.WP.list.v04r01.csv")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tyhist.json")
TW = timezone(timedelta(hours=8))
FROM_YEAR = 1951          # 日本氣象廳最佳路徑的起點
MIN_KT = 34               # 只收命名颱風（輕度颱風以上）；臺灣說的「今年第 N 號颱風」也是這樣數
NAMED_GRADES = {3, 4, 5, 9}   # 日本氣象廳等級：3 輕度、4 強烈熱帶風暴、5 颱風、9＝1977 年前「輕度颱風以上」
MIN_STORMS = 1500         # 1951 年以來約 1,900 顆；少於這個數就是抓到殘缺的檔（或又漏了一段年份），不要覆蓋舊的


def num(v):
    try:
        x = float(str(v).strip())
        return x if x == x else None
    except ValueError:
        return None


def aggregate(lines):
    """逐行讀 IBTrACS CSV（第二行是單位列），每個 SID 留：年份、英文名、
    日本氣象廳的最低氣壓／最大風速／是否達命名等級、JTWC 的最低氣壓／最大風速，以及資料到哪一天。"""
    rd = csv.reader(lines)
    head = next(rd)
    ix = {k.strip(): i for i, k in enumerate(head)}
    need = ["SID", "SEASON", "NAME", "ISO_TIME", "TRACK_TYPE", "TOKYO_WIND", "TOKYO_PRES"]
    miss = [k for k in need if k not in ix]
    if miss:
        raise ValueError(f"欄位不見了：{','.join(miss)}（IBTrACS 改格式？）")
    col = lambda row, k: row[ix[k]] if k in ix else ""     # 選用欄位：沒有就當空的
    next(rd, None)                                   # 單位列
    S, last = {}, ""
    for row in rd:
        if len(row) < len(head):
            continue
        if "spur" in row[ix["TRACK_TYPE"]].strip().lower():
            continue                                 # spur＝分岔的備選路徑，不算；main／PROVISIONAL 都收
        season = num(row[ix["SEASON"]])
        if season is None or season < FROM_YEAR:
            continue
        sid = row[ix["SID"]].strip()
        w, p = num(row[ix["TOKYO_WIND"]]), num(row[ix["TOKYO_PRES"]])
        # 暫定資料有時只填 WMO 欄位（西北太平洋的 WMO 機構就是日本氣象廳），拿它補
        if (w is None or p is None) and col(row, "WMO_AGENCY").strip().lower() == "tokyo":
            w = w if w is not None else num(col(row, "WMO_WIND"))
            p = p if p is not None else num(col(row, "WMO_PRES"))
        g, uw, up = num(col(row, "TOKYO_GRADE")), num(col(row, "USA_WIND")), num(col(row, "USA_PRES"))
        t = row[ix["ISO_TIME"]].strip()
        s = S.get(sid)
        if s is None:
            name = row[ix["NAME"]].strip().upper()
            s = S[sid] = {"y": int(season), "n": "" if name in ("NOT_NAMED", "UNNAMED") else name,
                          "p": None, "w": None, "named": False, "up": None, "uw": None}
        if p is not None and p > 800 and (s["p"] is None or p < s["p"]):
            s["p"] = p
        if w is not None and (s["w"] is None or w > s["w"]):
            s["w"] = w
        if g is not None and int(g) in NAMED_GRADES:
            s["named"] = True
        if up is not None and up > 800 and (s["up"] is None or up < s["up"]):
            s["up"] = up
        if uw is not None and (s["uw"] is None or uw > s["uw"]):
            s["uw"] = uw
        if t > last:
            last = t
    out = []
    for s in S.values():
        if s["p"] is not None:
            # 日本氣象廳有這顆：風速達輕度颱風，或等級是命名颱風（1977 年前沒有風速，只有等級）
            if s["named"] or (s["w"] or 0) >= MIN_KT:
                out.append([s["y"], s["n"], int(s["p"]), int(s["w"] or 0), "J"])
        elif s["up"] is not None and (s["uw"] or 0) >= MIN_KT:
            # 日本氣象廳的最終分析還沒進來（最近一兩年）：先用 JTWC 的
            out.append([s["y"], s["n"], int(s["up"]), int(s["uw"]), "U"])
    out.sort(key=lambda r: (r[2], -r[3], r[0]))
    return out, last


def download(url, tries=3):
    err = ""
    for i in range(tries):
        try:
            return urlopen(Request(url, headers={"User-Agent": "data-fetch/1.0"}), timeout=60)
        except HTTPError as e:
            err = f"HTTP {e.code}"
            if 400 <= e.code < 500:
                break
        except (URLError, TimeoutError, OSError) as e:
            err = str(getattr(e, "reason", e))
        time.sleep(2 ** (i + 1))
    raise RuntimeError(err or "下載失敗")


def build():
    r = download(URL)
    storms, last = aggregate(io.TextIOWrapper(r, encoding="utf-8", errors="replace", newline=""))
    if len(storms) < MIN_STORMS:
        # 不交出殘缺的檔：保留舊的、紅燈讓人知道
        print(f"只算出 {len(storms)} 顆，少於 {MIN_STORMS}，不覆蓋舊檔")
        return 1
    years = {r[0] for r in storms}
    print("tyhist: 各年代顆數 " + " ".join(f"{d}s:{sum(1 for r in storms if r[0] // 10 * 10 == d)}"
                                         for d in range(FROM_YEAR // 10 * 10, max(years) + 1, 10))
          + f"；JTWC 補的 {sum(1 for r in storms if r[4] == 'U')} 顆")
    # 起始年照實際有資料的寫：要是哪天又漏了早年的，前端不會繼續寫「1951 年以來」
    doc = {"v": 2, "src": "IBTrACS v04r01，今年的是暫定資料",
           "built": datetime.now(TW).isoformat(timespec="seconds"), "through": last, "from": min(years),
           "storms": storms}
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("storms") == storms and old.get("through") == last:
            print("tyhist: no change")
            return 0
    except (OSError, ValueError):
        pass
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"tyhist: {len(storms)} 顆，資料到 {last}，最強 {storms[0]}")
    return 0


SAMPLE = """SID,SEASON,NUMBER,BASIN,SUBBASIN,NAME,ISO_TIME,NATURE,LAT,LON,WMO_WIND,WMO_PRES,WMO_AGENCY,TRACK_TYPE,USA_WIND,USA_PRES,TOKYO_GRADE,TOKYO_WIND,TOKYO_PRES
 ,Year, , , , ,,,degrees_north,degrees_east,kts,mb,,,kts,mb,,kts,mb
1979279N06173,1979,1,WP,MM,TIP,1979-10-10 00:00:00,TS,10,140,90,920,tokyo,main,100,915,5,90,920
1979279N06173,1979,1,WP,MM,TIP,1979-10-12 00:00:00,TS,15,135,140,870,tokyo,main,165,870,5,140,870
1979279N06173,1979,1,WP,MM,TIP,1979-10-13 00:00:00,TS,18,133,130,880,tokyo,main,150,875,5,130,880
2026290N10140,2026,29,WP,MM,CHAMPI,2026-10-18 00:00:00,TS,15,135,105,915,tokyo,PROVISIONAL,120,905,5,105,915
2026200N10140,2026,12,WP,MM,WEAKA,2026-07-18 00:00:00,TS,15,135,40,996,tokyo,main,45,995,3,40,996
2026201N10140,2026,13,WP,MM,NOT_NAMED,2026-07-19 00:00:00,TD,15,135,25,1004,tokyo,main,30,1002,2,25,1004
1950201N10140,1950,1,WP,MM,OLDIE,1950-07-19 00:00:00,TS,15,135,100,930,tokyo,main,,,9,,930
2026202N10140,2026,14,WP,MM,SPURRY,2026-07-20 00:00:00,TS,15,135,80,950,tokyo,spur-2026202N10140,90,945,4,80,950
2026203N10140,2026,15,WP,MM,BLANKP,2026-07-21 00:00:00,TS,15,135,60, ,tokyo,main,,,3,60,
2026210N10140,2026,16,WP,MM,WMOONLY,2026-07-28 00:00:00,TS,15,135,70,960,tokyo,PROVISIONAL,,,,,
1958250N10140,1958,21,WP,MM,IDA,1958-09-24 00:00:00,TS,15,135,,,tokyo,main,,,9,,877
1958251N10140,1958,22,WP,MM,LOWTD,1958-09-25 00:00:00,TD,15,135,,,tokyo,main,,,2,,1000
2025300N10140,2025,30,WP,MM,FRESHU,2025-11-01 00:00:00,TS,15,135,,,,PROVISIONAL,95,940,,,
2025301N10140,2025,31,WP,MM,WEAKU,2025-11-02 00:00:00,TD,15,135,,,,PROVISIONAL,30,1000,,,
"""


def selftest():
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            print("  ✗", msg)
            sys.exit(1)
        ok += 1
        print("  ✓", msg)

    storms, last = aggregate(io.StringIO(SAMPLE))
    names = [s[1] for s in storms]
    by = {s[1]: s for s in storms}
    check(storms[0] == [1979, "TIP", 870, 140, "J"], "一顆颱風取日本氣象廳的最低氣壓與最大風速（TIP 870 hPa、140 kt）")
    check(names == ["TIP", "IDA", "CHAMPI", "FRESHU", "WMOONLY", "WEAKA"], "依氣壓由低到高排")
    check(by["WMOONLY"] == [2026, "WMOONLY", 960, 70, "J"], "日本氣象廳欄位空著時拿 WMO 欄位補（暫定資料）")
    check(by["IDA"] == [1958, "IDA", 877, 0, "J"], "1977 年前沒有風速：等級 9（輕度颱風以上）照樣收，風速記 0")
    check("LOWTD" not in names, "1977 年前等級 2（熱帶性低氣壓）不收")
    check(by["FRESHU"] == [2025, "FRESHU", 940, 95, "U"], "日本氣象廳還沒有的（最近一兩年）改用 JTWC，標 U")
    check("WEAKU" not in names, "JTWC 也沒達 34 kt 的不收")
    check(by["CHAMPI"][4] == "J", "兩邊都有時用日本氣象廳的（PROVISIONAL 大寫也認得）")
    check("OLDIE" not in names, "1951 年以前的不收")
    check("" not in names, "熱帶性低氣壓（沒達 34 kt）不收")
    check("SPURRY" not in names, "分岔的備選路徑（spur-…）不算")
    check("BLANKP" not in names, "沒有氣壓的不收（不能排名）")
    check(last == "2026-10-18 00:00:00", "記下資料到哪一天")
    old = aggregate(io.StringIO("\n".join(l for l in SAMPLE.splitlines() if "FRESHU" not in l and "WEAKU" not in l)
                               .replace(",USA_WIND,USA_PRES,TOKYO_GRADE", "").replace(",kts,mb,,kts,mb", ",kts,mb")
                               .replace(",100,915,5,", ",").replace(",165,870,5,", ",").replace(",150,875,5,", ",")))
    check(old[0][0] == [1979, "TIP", 870, 140, "J"], "沒有 USA_*、TOKYO_GRADE 欄位的舊格式照樣讀得動")
    try:
        aggregate(io.StringIO("SID,SEASON\n"))
        check(False, "缺欄位要報錯")
    except ValueError as e:
        check("欄位不見了" in str(e), "缺欄位要報錯（IBTrACS 改格式時不默默算錯）")
    print(f"全部通過（{ok} 項）")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "selftest":
        selftest()
    elif cmd == "build":
        sys.exit(build())
    else:
        print(__doc__)
        sys.exit(2)
