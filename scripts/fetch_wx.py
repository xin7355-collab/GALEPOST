#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把中央氣象署的雨量站、測站陣風、豪大雨／雷雨特報、縣市天氣預報抓下來，存成 data/wx.json。

**為什麼需要這支程式**

前端的雨量、陣風、特報原本都要使用者自己在設定裡填授權碼才抓得到；沒填的同事
「出門評估」「站臺豪雨紀錄」全是空的。後端用 repo Secret 抓好放成同源檔，大家都有。

**為什麼這裡要自己取欄位（不像 typhoon.json 只存原始 records）**

雨量站一千多站、氣象站幾百站，原始 records 一份就一兩 MB，每一輪都 commit 會把 repo 撐爆。
所以只取前端用得到的幾個欄位、壓成陣列；欄位名稱跟前端 parseRainStation／parseObsStation
讀的一樣（新版 API：GeoInfo.Coordinates 的 WGS84、RainfallElement.Past1hr…、
WeatherElement.GustInfo.PeakGustSpeed）。特報（W-C0033-002）本來就小，照原始 records 存，
解析交給前端那一份。

輸出：data/wx.json
  {
    "v": 1, "hasKey": true, "updated": "2026-10-23T09:10:00+08:00",
    "rain": {"t": "2026-10-23T09:00:00+08:00",
             "counties": ["新北市", ...],
             "rows": [["林口", 25.0712, 121.3852, 0, 0.5, 0.5, 1, 12.5], ["五股", 25.08, 121.43, 0], ...]},
             # [站名, 緯度, 經度, 縣市（counties 的第幾個）, 10分鐘, 1小時, 3小時, 24小時]；
             # 尾巴的 0 省略（乾的站只剩四欄），null＝測站沒給這個值（不是 0）。
             # 縣市要留：前端拿最近雨量站的縣市判斷站臺／現在位置在哪個縣市（停班停課、特報）
    "wind": {"t": "...", "counties": [...], "rows": [["淡水", 25.16, 121.44, 0, 3.2, 7.5], ...]},   # 平均風、陣風 m/s
    "adv": {...W-C0033-002 原始 records...} | null,
    "fc": {"t": "2026-10-23T18:00:00+08:00",                # 第一格的開始時間
           "wx": ["多雲", "短暫陣雨", ...],                   # 天氣現象文字表（各格用序號，省得重複）
           "towns": [["臺中市", 24.1477, 120.6736, [[0, 3, 1, 60, 26, 5], ...]], ...]},
           # 每格 [距 t 幾小時, 長幾小時, 天氣現象序號, 降雨機率%, 溫度, 平均風蒲福風級]，沒給的是 null。
           # F-D0047-089（逐 3 小時約 3 天）接 F-D0047-091（一週、逐 12 小時）——出門建議要講到明天，
           # 沒填授權碼的同事原本完全沒有預報。
    "errors": {"rain": "..."}            # 只有失敗的那一份才有
  }
沒有 CWA_API_KEY 時寫 hasKey:false 的空殼；某一份抓失敗時保留上一次成功的那份（標在 errors），
全部失敗也不交出空檔案。

離線自我測試（不連網、不需要金鑰）：
  python3 scripts/fetch_wx.py selftest
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

KEY = os.environ.get("CWA_API_KEY", "").strip()
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "wx.json")
TW = timezone(timedelta(hours=8))
DATASTORE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"
SETS = {"rain": "O-A0002-001", "wind": "O-A0001-001", "adv": "W-C0033-002", "fc": ("F-D0047-089", "F-D0047-091")}
RETRY_HTTP = {429, 500, 502, 503, 504}
BUDGET_SEC = 90                  # 同一個工作流程裡還有落雷、颱風、停班停課要跑
STALE_SEC = 2 * 3600             # 比全體最新觀測舊兩小時以上的測站（故障、停報）不收
REFRESH_SEC = 3 * 3600           # 內容沒變也每 3 小時刷新時間戳，前端才分得出「沒變」和「後端停了」
_T0 = time.monotonic()


class KeyRejected(Exception):
    pass


def now_iso():
    return datetime.now(TW).isoformat(timespec="seconds")


def _left():
    return BUDGET_SEC - (time.monotonic() - _T0)


def scrub(msg):
    """錯誤訊息會寫進公開檔，金鑰一律遮掉。"""
    msg = str(msg)
    for k in {KEY, quote(KEY, safe="")} - {""}:
        msg = msg.replace(k, "***")
    return msg


def grab(url, headers=None, tries=3):
    for i in range(tries):
        left = _left()
        if left < 5:
            raise TimeoutError("超過本輪時間預算")
        try:
            h = {"User-Agent": "galepost-wx/1.0", "Accept": "application/json"}
            h.update(headers or {})
            with urlopen(Request(url, headers=h), timeout=min(30, left)) as r:
                return r.read()
        except HTTPError as e:
            if e.code not in RETRY_HTTP or i == tries - 1:
                raise
        except (URLError, OSError, HTTPException):   # 讀到一半斷線（IncompleteRead）也要重試，不是 OSError
            if i == tries - 1:
                raise
        wait = min(2 ** (i + 1), 16)
        if _left() < wait + 5:
            raise TimeoutError("超過本輪時間預算")
        time.sleep(wait)


def fetch_records(ds):
    """授權碼放 HTTP 標頭（網址會出現在錯誤訊息與記錄裡）；被拒才退回網址參數試一次。"""
    base = f"{DATASTORE}{ds}?format=JSON"
    rejected = None
    for mode in ("header", "query"):
        try:
            raw = grab(base, {"Authorization": KEY}) if mode == "header" else grab(f"{base}&Authorization={quote(KEY, safe='')}")
        except HTTPError as e:
            if e.code in (401, 403):
                rejected = e
                continue
            raise
        j = json.loads(raw.decode("utf-8", "replace"))
        if str(j.get("success", "true")).lower() == "false":
            rejected = ValueError(str(j.get("message") or "success=false")[:200])
            continue
        rec = j.get("records")
        if not isinstance(rec, dict):
            raise ValueError("回應裡沒有 records")
        return rec
    raise KeyRejected(scrub(f"{ds}: 授權碼被拒（{rejected}）"))


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    # 氣象署用 -99、-998 之類的值表示故障或沒觀測
    return None if x != x or x <= -90 else round(x, 1)


def _get(o, *keys):
    for k in keys:
        if isinstance(o, dict) and k in o:
            return o[k]
    return None


def base(st):
    """站名、WGS84 座標、觀測時間。一站同時給 TWD67 與 WGS84，要挑 WGS84（差幾百公尺會選錯站）。"""
    name = _get(st, "StationName", "stationName")
    geo = _get(st, "GeoInfo", "geoInfo") or {}
    co = _get(geo, "Coordinates", "coordinates") or []
    if not name or not isinstance(co, list) or not co:
        return None
    w = next((c for c in co if "WGS84" in str(_get(c, "CoordinateName", "coordinateName") or "").upper()), co[0])
    try:
        lat = round(float(_get(w, "StationLatitude", "stationLatitude")), 4)
        lng = round(float(_get(w, "StationLongitude", "stationLongitude")), 4)
    except (TypeError, ValueError):
        return None
    t = str(_get(_get(st, "ObsTime", "obsTime") or {}, "DateTime", "dateTime") or "")
    return (str(name).strip(), lat, lng, t, str(_get(geo, "CountyName", "countyName") or "").strip(),
            str(_get(geo, "TownName", "townName") or "").strip())


def _ts(t):
    try:
        return datetime.fromisoformat(t).timestamp()
    except (TypeError, ValueError):
        return None


def _trim(row, keep):
    """尾巴是 0 的欄位省略（乾的站只剩站名座標）；null 不能省，那是「沒資料」不是「沒下雨」。"""
    while len(row) > keep and row[-1] == 0:
        row.pop()
    return row


def compact(stations, pick_vals, keep=4):
    rows, times = [], []
    for st in stations if isinstance(stations, list) else []:
        b = base(st)
        if not b:
            continue
        vals = pick_vals(st)
        if vals is None or all(v is None for v in vals):
            continue
        rows.append((b, vals))
        times.append(_ts(b[3]))
    newest = max((t for t in times if t), default=None)
    keep_rows = [(b, v) for b, v in rows if not (newest and _ts(b[3]) and newest - _ts(b[3]) > STALE_SEC)]
    counties = sorted({b[4] for b, _ in keep_rows})                # 排序固定：沒變就不 commit
    towns = sorted({b[5] for b, _ in keep_rows})
    ci, ti = {c: i for i, c in enumerate(counties)}, {c: i for i, c in enumerate(towns)}
    pairs = [(_trim([name, lat, lng, ci[cty], *vals], keep), ti[town]) for (name, lat, lng, t, cty, town), vals in keep_rows]
    pairs.sort(key=lambda p: (-p[0][1], p[0][2]))                 # 由北至南
    t = datetime.fromtimestamp(newest, TW).isoformat(timespec="seconds") if newest else ""
    # 鄉鎮另放一列（tw[i] 是第 i 列在 towns 的第幾個）：「現在位置」要寫得出「臺中市大里區」,
    # 塞進每一列的話舊版前端會把它當成雨量讀
    return {"t": t, "counties": counties, "rows": [p[0] for p in pairs], "towns": towns, "tw": [p[1] for p in pairs]}


def rain_vals(st):
    re_ = _get(st, "RainfallElement", "rainfallElement")
    if not isinstance(re_, dict):
        return None
    g = lambda *ks: num(_get(_get(re_, *ks) or {}, "Precipitation", "precipitation"))
    return [g("Past10Min", "past10Min"), g("Past1hr", "past1hr"), g("Past3hr", "past3hr"), g("Past24hr", "past24hr")]


def wind_vals(st):
    we = _get(st, "WeatherElement", "weatherElement")
    if not isinstance(we, dict):
        return None
    gi = _get(we, "GustInfo", "gustInfo") or {}
    return [num(_get(we, "WindSpeed", "windSpeed")), num(_get(gi, "PeakGustSpeed", "peakGustSpeed"))]


# ── 縣市天氣預報（F-D0047-089／091）──────────────────────────
# 新版 Locations/Location/WeatherElement/Time/ElementValue[{Weather…}]，舊版 locations/location/weatherElement/
# time/elementValue[{value, measures}]，兩種都接（前端 parseTownFc 也是兩種都接，欄位對照跟它一樣）。

def _lst(o):
    return [] if o is None else o if isinstance(o, list) else [o]


def _tw_ts(v):
    """氣象署舊版時間沒有時區（2026-10-23 21:00:00）。runner 是 UTC，不補 +08:00 整條預報會差 8 小時。"""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).strip().replace(" ", "T"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=TW)).timestamp()


def _fc_cat(name):
    n = str(name or "")
    if n in ("天氣現象", "Wx"):
        return "wx"
    if "降雨機率" in n or n.startswith("PoP"):
        return "pop"
    if n in ("溫度", "T", "平均溫度"):
        return "t"
    if n in ("風速", "WS"):
        return "ws"
    return None


def _fc_val(T):
    """一格的值與蒲福風級。風級在新版是 BeaufortScale、舊版是 measures 寫「蒲福風級」的那個 elementValue，
    更舊的在 parameter[]（風級）。"""
    v = bf = None
    for e in _lst(_get(T, "ElementValue", "elementValue")):
        if isinstance(e, (str, int, float)):
            v = e if v is None else v
            continue
        if not isinstance(e, dict):
            continue
        if "蒲福" in str(e.get("measures", "")):
            bf = e.get("value") if bf is None else bf
            continue
        if bf is None:
            bf = _get(e, "BeaufortScale", "beaufortScale")
        if v is None:
            v = _get(e, "Weather", "WeatherDescription", "ProbabilityOfPrecipitation", "Temperature", "WindSpeed", "value")
    for p in _lst(_get(T, "parameter", "Parameter")):
        if str(_get(p, "parameterName", "ParameterName") or "") == "風級" and bf is None:
            bf = _get(p, "parameterValue", "ParameterValue")
    return v, bf


def _int(v):
    x = num(v)
    return None if x is None else int(round(x))


def parse_fc(rec):
    towns = []
    for w in _lst(_get(rec, "Locations", "locations")) or [rec]:
        for L in _lst(_get(w, "Location", "location")):
            name = _get(L, "LocationName", "locationName")
            if not name:
                continue
            els = {}
            for E in _lst(_get(L, "WeatherElement", "weatherElement")):
                c = _fc_cat(_get(E, "ElementName", "elementName"))
                if not c or c in els:              # 同一類取第一個（逐 3 小時的降雨機率排在 12 小時前面）
                    continue
                ts = []
                for T in _lst(_get(E, "Time", "time")):
                    a = _tw_ts(_get(T, "StartTime", "startTime", "DataTime", "dataTime"))
                    if a is None:
                        continue
                    v, bf = _fc_val(T)
                    ts.append((a, _tw_ts(_get(T, "EndTime", "endTime")), v, bf))
                if ts:
                    els[c] = sorted(ts, key=lambda x: x[0])     # 官方回傳不保證照時間排
            if "wx" not in els:
                continue
            try:
                lat = round(float(_get(L, "Latitude", "lat")), 4)
                lng = round(float(_get(L, "Longitude", "lon", "lng")), 4)
            except (TypeError, ValueError):
                lat = lng = None
            towns.append({"n": str(name).strip(), "lat": lat, "lng": lng, "els": els})
    return towns


def _fc_at(arr, ts):
    """涵蓋 ts 的那一格。溫度、風速常是時間點（沒有 EndTime），當成往後 3 小時。"""
    for a, b, v, bf in arr or []:
        if a <= ts < (b or a + 3 * 3600):
            return v, bf
    return None, None


def fc_slots(t):
    wx, out = t["els"]["wx"], []
    for i, (a, b, v, _) in enumerate(wx):
        b = b or (wx[i + 1][0] if i + 1 < len(wx) else a + 3 * 3600)
        pop, _x = _fc_at(t["els"].get("pop"), a)
        tmp, _x = _fc_at(t["els"].get("t"), a)
        _v, bf = _fc_at(t["els"].get("ws"), a)
        out.append([a, b, str(v or "").strip(), _int(pop), _int(tmp), _int(bf)])
    return out


def compact_fc(parsed):
    """多支資料集照順序接：細的（逐 3 小時）在前，粗的（一週）只接在細的結束之後。"""
    towns = {}
    for lst in parsed:
        for t in lst:
            sl = fc_slots(t)
            cur = towns.get(t["n"])
            if not cur:
                towns[t["n"]] = {"lat": t["lat"], "lng": t["lng"], "s": sl}
                continue
            end = max(x[1] for x in cur["s"])
            cur["s"] += [x for x in sl if x[0] >= end]
            if cur["lat"] is None:
                cur["lat"], cur["lng"] = t["lat"], t["lng"]
    if not towns:
        return None
    t0 = min(x[0] for v in towns.values() for x in v["s"])
    wxs = sorted({x[2] for v in towns.values() for x in v["s"]})
    wi = {w: i for i, w in enumerate(wxs)}
    rows = [[n, v["lat"], v["lng"], [[round((x[0] - t0) / 3600), round((x[1] - x[0]) / 3600), wi[x[2]], x[3], x[4], x[5]]
                                     for x in v["s"]]] for n, v in towns.items()]
    rows.sort(key=lambda r: (-(r[1] if r[1] is not None else -999), r[2] or 0, r[0]))     # 由北至南，固定順序
    return {"t": datetime.fromtimestamp(t0, TW).isoformat(timespec="seconds"), "wx": wxs, "towns": rows}


def stations_of(rec):
    return _get(rec, "Station", "station", "location", "Location") or []


def load_prev():
    try:
        with open(OUT, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def build_fc(fetcher):
    """逐 3 小時那支是主體；一週那支只是往後接。一週那支失敗照樣交出，原因記在 errors。"""
    parsed, errs = [], []
    for ds in SETS["fc"]:
        try:
            lst = parse_fc(fetcher(ds))
            if not lst:
                raise ValueError("找不到任何地點的天氣現象（欄位格式可能變了）")
            parsed.append(lst)
        except KeyRejected:
            raise
        except Exception as e:  # noqa: BLE001
            errs.append(f"{ds}: {scrub(e)[:120]}")
    if not parsed:
        raise ValueError("；".join(errs))
    return compact_fc(parsed), "；".join(errs)


def build(prev, fetcher):
    """抓四份。某一份失敗：沿用上一次成功的那份並記下原因；授權碼被拒往上丟（要紅燈換 Secret）。"""
    doc = {"v": 1, "hasKey": True, "updated": now_iso()}
    errors = {}
    for part, ds in SETS.items():
        try:
            if part == "fc":
                doc[part], half = build_fc(fetcher)
                if half:
                    errors[part] = half
                continue
            rec = fetcher(ds)
            if part == "rain":
                doc[part] = compact(stations_of(rec), rain_vals)
            elif part == "wind":
                doc[part] = compact(stations_of(rec), wind_vals)
            else:
                doc[part] = rec
            if part != "adv" and not doc[part]["rows"]:
                raise ValueError("一站都讀不出來（欄位格式可能變了）")
        except KeyRejected:
            raise
        except Exception as e:  # noqa: BLE001 —— 一份失敗不拖累其他幾份
            errors[part] = scrub(e)[:200]
            doc[part] = prev.get(part) if prev.get("hasKey") else None
    if errors:
        doc["errors"] = errors
    return doc


def write(doc, prev):
    a, b = dict(doc), dict(prev)
    a.pop("updated", None)
    b.pop("updated", None)
    try:
        age = (datetime.now(TW) - datetime.fromisoformat(prev.get("updated", ""))).total_seconds()
    except (TypeError, ValueError):
        age = float("inf")
    if a == b and age < REFRESH_SEC:
        print("wx: unchanged — no commit")
        return False
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    return True


def main():
    prev = load_prev()
    if not KEY:
        doc = {"v": 1, "hasKey": False, "updated": now_iso(), "note": "repo 尚未設定 CWA_API_KEY Secret"}
        if prev.get("hasKey") is False:
            print("wx: placeholder unchanged")
            return 0
        write(doc, {})
        return 0
    try:
        doc = build(prev, fetch_records)
    except KeyRejected as e:
        print(f"wx: {e}")
        return 1
    if all(doc.get(p) is None for p in SETS):
        # 全部失敗、手上也沒有舊的：不交出空檔案，保留原檔、只在記錄裡講
        print(f"wx: all failed {doc.get('errors')}")
        return 0
    write(doc, prev)
    r, w, f = doc.get("rain") or {}, doc.get("wind") or {}, doc.get("fc") or {}
    print(f"wx: rain {len(r.get('rows', []))} 站、wind {len(w.get('rows', []))} 站、預報 {len(f.get('towns', []))} 地、"
          f"errors {doc.get('errors', {})}")
    return 0


def _st(name, lat, lng, t, **el):
    o = {"StationName": name, "ObsTime": {"DateTime": t},
         "GeoInfo": {"CountyName": el.pop("county", "新北市"), "TownName": el.pop("town", ""), "Coordinates": [{"CoordinateName": "TWD67", "StationLatitude": lat - 0.002, "StationLongitude": lng + 0.002},
                                     {"CoordinateName": "WGS84", "StationLatitude": lat, "StationLongitude": lng}]}}
    o.update(el)
    return o


def selftest():
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            print("  ✗", msg)
            sys.exit(1)
        ok += 1
        print("  ✓", msg)

    T = "2026-10-23T09:00:00+08:00"
    P = lambda v: {"Precipitation": v}
    rain = {"Station": [
        _st("林口", 25.07123, 121.38521, T, town="林口區", RainfallElement={"Past10Min": P(0.5), "Past1hr": P(12.5), "Past3hr": P(30), "Past24hr": P(88)}),
        _st("五股", 25.08, 121.43, T, RainfallElement={"Past10Min": P(0), "Past1hr": P(0), "Past3hr": P(0), "Past24hr": P(0)}),
        _st("故障", 24.5, 121.0, T, county="桃園市", RainfallElement={"Past10Min": P(-99), "Past1hr": P(5), "Past3hr": P(-998), "Past24hr": P(0)}),
        _st("停報", 24.0, 121.0, "2026-10-23T05:00:00+08:00", RainfallElement={"Past1hr": P(50)}),
        {"StationName": "沒座標", "RainfallElement": {"Past1hr": P(9)}},
    ]}
    r = compact(stations_of(rain), rain_vals)
    rows = {x[0]: x for x in r["rows"]}
    check(r["counties"] == ["新北市", "桃園市"], "縣市表排序固定")
    check(rows["林口"] == ["林口", 25.0712, 121.3852, 0, 0.5, 12.5, 30.0, 88.0], "取 WGS84 座標（不是 TWD67）、四位小數、縣市、四個雨量")
    check(rows["五股"] == ["五股", 25.08, 121.43, 0], "乾的站尾巴的 0 省略，只剩站名座標縣市")
    check(rows["故障"] == ["故障", 24.5, 121.0, 1, None, 5.0, None], "-99、-998（故障）記成 null，不能當成 0")
    check("停報" not in rows, "比最新觀測舊兩小時以上的站不收")
    check("沒座標" not in rows, "沒座標的站不收")
    check(r["t"] == T and [x[0] for x in r["rows"]] == ["五股", "林口", "故障"], "記最新觀測時間；由北至南排")
    check(r["towns"][r["tw"][1]] == "林口區" and len(r["tw"]) == len(r["rows"]), "鄉鎮另一列、跟排序後的列對齊")

    wind = {"Station": [_st("淡水", 25.16, 121.44, T, WeatherElement={"WindSpeed": 3.2, "GustInfo": {"PeakGustSpeed": 7.5}}),
                        _st("無風", 25.0, 121.0, T, WeatherElement={"WindSpeed": -99, "GustInfo": {"PeakGustSpeed": -99}})]}
    w = compact(stations_of(wind), wind_vals)
    check(w["rows"] == [["淡水", 25.16, 121.44, 0, 3.2, 7.5]], "風：平均風與陣風；兩個都故障的站不收")

    # 預報：新版（089 逐 3 小時）＋舊版（091 一週，時間沒時區）
    def tm(a, b=None, **ev):
        o = {"StartTime": a, "ElementValue": [ev]} if b else {"DataTime": a, "ElementValue": [ev]}
        if b:
            o["EndTime"] = b
        return o
    D = "2026-10-23T"
    new = {"Locations": [{"Location": [{"LocationName": "臺中市", "Latitude": "24.14773", "Longitude": "120.67364", "WeatherElement": [
        {"ElementName": "天氣預報綜合描述", "Time": [tm(D + "18:00:00+08:00", D + "21:00:00+08:00", WeatherDescription="很長的一段話")]},
        {"ElementName": "天氣現象", "Time": [tm(D + "21:00:00+08:00", "2026-10-24T00:00:00+08:00", Weather="短暫陣雨或雷雨", WeatherCode="15"),
                                          tm(D + "18:00:00+08:00", D + "21:00:00+08:00", Weather="短暫陣雨", WeatherCode="08")]},
        {"ElementName": "3小時降雨機率", "Time": [tm(D + "18:00:00+08:00", D + "21:00:00+08:00", ProbabilityOfPrecipitation="60"),
                                             tm(D + "21:00:00+08:00", "2026-10-24T00:00:00+08:00", ProbabilityOfPrecipitation="-")]},
        {"ElementName": "溫度", "Time": [tm(D + "18:00:00+08:00", Temperature="26"), tm(D + "21:00:00+08:00", Temperature="25")]},
        {"ElementName": "風速", "Time": [tm(D + "18:00:00+08:00", WindSpeed="9", BeaufortScale="5"),
                                      tm(D + "21:00:00+08:00", WindSpeed="12", BeaufortScale="6")]},
    ]}]}]}
    old = {"locations": [{"location": [{"locationName": "臺中市", "lat": "24.14773", "lon": "120.67364", "weatherElement": [
        {"elementName": "Wx", "time": [{"startTime": "2026-10-23 18:00:00", "endTime": "2026-10-24 06:00:00", "elementValue": [{"value": "陰短暫雨", "measures": "自定義 Wx 文字"}]},
                                       {"startTime": "2026-10-24 06:00:00", "endTime": "2026-10-24 18:00:00", "elementValue": [{"value": "多雲", "measures": "自定義 Wx 文字"}]}]},
        {"elementName": "PoP12h", "time": [{"startTime": "2026-10-24 06:00:00", "endTime": "2026-10-24 18:00:00", "elementValue": [{"value": "20", "measures": "百分比"}]}]},
        {"elementName": "WS", "time": [{"startTime": "2026-10-24 06:00:00", "endTime": "2026-10-24 18:00:00",
                                        "elementValue": [{"value": "3", "measures": "公尺/秒"}, {"value": "2", "measures": "蒲福風級"}]}]},
    ]}, {"locationName": "沒有天氣現象", "weatherElement": [{"elementName": "T", "time": []}]}]}]}
    pn, po = parse_fc(new), parse_fc(old)
    check([t["n"] for t in pn] == ["臺中市"] and [t["n"] for t in po] == ["臺中市"], "預報新舊兩種格式都讀得到；沒有天氣現象的地點不收")
    check([x[2] for x in pn[0]["els"]["wx"]] == ["短暫陣雨", "短暫陣雨或雷雨"], "時間亂序也照時間排")
    check(po[0]["els"]["wx"][0][0] == datetime(2026, 10, 23, 18, tzinfo=TW).timestamp(), "舊版沒時區的時間當臺灣時間（runner 是 UTC）")
    f = compact_fc([pn, po])
    s0, s1 = f["towns"][0][3][0], f["towns"][0][3][1]
    check(f["t"] == D + "18:00:00+08:00" and f["towns"][0][:3] == ["臺中市", 24.1477, 120.6736], "第一格時間、地點、座標")
    check(s0 == [0, 3, f["wx"].index("短暫陣雨"), 60, 26, 5], "一格：時距、天氣、降雨機率、溫度（時間點往後 3 小時）、風級")
    check(s1[3] is None and s1[5] == 6, "降雨機率給「-」記 null（沒資料，不是 0%）")
    sl = f["towns"][0][3]
    check(len(sl) == 3 and sl[2][:2] == [12, 12] and sl[2][3] == 20 and sl[2][5] == 2,
          "一週那支只接在逐 3 小時結束之後（重疊的 18–06 那格不收）；舊版風級從「蒲福風級」那個值拿")

    calls = []

    def fake(ds):
        calls.append(ds)
        if ds == SETS["wind"]:
            raise HTTPError("u", 503, "x", {}, None)
        return rain if ds == SETS["rain"] else {"record": []}
    prev = {"hasKey": True, "wind": {"t": "old", "rows": [["舊站", 1, 2, 3, 4]]}}
    d = build(prev, fake)
    check(d["rain"]["rows"] and d["adv"] == {"record": []}, "雨量、特報照常寫入")
    check(d["wind"] == prev["wind"] and "wind" in d["errors"], "風抓失敗：沿用上一次成功的、記下原因")
    check(d["fc"] is None and "fc" in d["errors"], "預報兩支都讀不到：沒有舊的就是 null，原因記下")

    def half(ds):
        if ds == "F-D0047-091":
            raise HTTPError("u", 500, "x", {}, None)
        return new if ds == "F-D0047-089" else rain
    d3 = build({}, half)
    check(d3["fc"]["towns"] and "F-D0047-091" in d3["errors"]["fc"], "一週那支失敗：逐 3 小時照樣交出，原因記下")
    d2 = build({}, lambda ds: (_ for _ in ()).throw(ValueError("壞了")))
    check(all(d2[p] is None for p in SETS), "全部失敗又沒有舊的：每份都是 null（main 不會寫出這種檔）")

    def rejected(ds):
        raise KeyRejected("被拒")
    try:
        build({}, rejected)
        check(False, "授權碼被拒要往上丟")
    except KeyRejected:
        check(True, "授權碼被拒要往上丟（工作流程紅燈，去換 Secret）")
    import http.client as hc
    global urlopen
    real, n = urlopen, [0]

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            n[0] += 1
            if n[0] == 1:
                raise hc.IncompleteRead(b"x" * 10, 100)
            return b'{"ok":1}'
    urlopen = lambda *a, **k: Resp()
    _sleep, time.sleep = time.sleep, (lambda s: None)
    try:
        check(grab("https://x") == b'{"ok":1}' and n[0] == 2, "回應讀到一半斷線（IncompleteRead）會重試，不是整份放棄")
    finally:
        urlopen, time.sleep = real, _sleep
    global KEY
    KEY, old = "SECRET123", KEY
    check("SECRET123" not in scrub("https://x?Authorization=SECRET123 失敗"), "錯誤訊息裡的金鑰遮掉")
    KEY = old
    print(f"全部通過（{ok} 項）")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        sys.exit(main())
