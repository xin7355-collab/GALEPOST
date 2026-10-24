#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把中央氣象署的颱風路徑與警報單抓下來，放成同源的 data/typhoon.json。

**為什麼需要這支程式**

前端直連 opendata.cwa.gov.tw 常被 CORS 擋，而且每個使用者都得自己申請授權碼。
由後端用 repo Secret 抓好、放成同源靜態檔，同事打開網頁就有官方路徑。

**為什麼只存原始 records、不在這裡解析**

前端本來就有一套解析器（parseCyclone／parseWarning），而且被實際回應修過好幾次
（PascalCase、座標分兩欄、單筆不包陣列……）。在 Python 再寫一份，
兩邊遲早漂移——某天氣象署改欄位，前端修好了、後端沒修，同一顆颱風兩條路看到兩種結果。
所以後端只負責「搬運」，解析只有一份，在前端。

輸出：data/typhoon.json
  {
    "v": 2, "hasKey": true,
    "updated": "2026-10-19T14:00:00+08:00",   # 內容最後一次確認的時間
    "count": 1,                                # 氣象署給了幾顆（前端拿來比對解析出幾顆）
    "raw": {"W-C0034-005": {...records...}, "W-C0034-001": {...records...} | null,
            "W-C0034-003": {"time": "...", "items": [{"name","p","polys","pts"}]} | null},
    "warnLog": [{"ty":"29","name":"薔琵","no":1,"kind":"SEA","headline":"海上颱風警報",
                 "effective":"...","seen":"..."}, ...],   # 收過的每一報（見 bulletins）
    "warnError": "...",                        # 只有警報單抓失敗時才有
    "probError": "..."                         # 只有侵襲機率抓失敗時才有
  }

**侵襲機率（W-C0034-003）是例外**：它是檔案型資料（fileapi，KMZ 壓縮的 KML 等值區），
前端讀不了壓縮檔、也不該為了它整包下載。這裡解壓、把 Placemark 的機率與多邊形取出來、
簡化座標，前端只做「縣市政府所在地落在哪一圈」。
抓不到路徑、而且手上沒有可用的舊資料時：{"v":2,"hasKey":true,"updated":...,"error":"..."}

離線自我測試（不連網、不需要金鑰）：
  python3 scripts/fetch_typhoon.py selftest
"""
import io
import json
import re
import os
import sys
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from http.client import HTTPException

KEY = os.environ.get("CWA_API_KEY", "").strip()
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "typhoon.json")
TW = timezone(timedelta(hours=8))
DATASTORE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"
PATH_SET = "W-C0034-005"   # 颱風路徑潛勢預報
WARN_SET = "W-C0034-001"   # 颱風警報單
PROB_SET = "W-C0034-003"   # 颱風侵襲機率（檔案型 fileapi，KMZ）
FILEAPI = "https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/"
PROB_TOL = 0.01            # 等值線簡化容許誤差（度，約 1 km）：縣市層級的判斷用不到更細

# 跟落雷同一個工作流程、同一個 10 分鐘排程；兩支加起來不能卡到下一輪。
BUDGET_SEC = 150
_T0 = time.monotonic()
RETRY_HTTP = {429, 500, 502, 503, 504}

# 內容沒變時多久刷新一次時間戳。前端超過 12 小時就不信這份檔（見 index.html fetchStatic），
# 3 小時刷一次留足四倍餘裕，又不會像每小時刷那樣多出一天二十幾次 commit＋Pages 部署。
REFRESH_SEC = 3 * 3600
# 抓失敗時，手上的舊資料在這個年齡內就先留著不動——一次暫時性失敗不該讓
# 同事畫面上的官方路徑消失、退回 GDACS。超過就改寫成錯誤檔，讓前端明講壞了。
KEEP_ON_FAIL_SEC = 6 * 3600


class KeyRejected(Exception):
    """401／403：授權碼錯了或被停用。重試沒用，要人去換 Secret。"""


def _left():
    return BUDGET_SEC - (time.monotonic() - _T0)


def _retry_after(e):
    try:
        return max(0, int(e.headers.get("Retry-After", "")))
    except (TypeError, ValueError, AttributeError):
        return 0


def grab(url, headers=None, tries=3):
    """暫時性錯誤（429、5xx、逾時、斷線）退避 2、4 秒再試；有 Retry-After 就照它。"""
    for i in range(tries):
        left = _left()
        if left < 5:
            raise TimeoutError("超過本輪時間預算")
        try:
            h = {"User-Agent": "galepost-typhoon/1.0", "Accept": "application/json"}
            h.update(headers or {})
            with urlopen(Request(url, headers=h), timeout=min(30, left)) as r:
                return r.read()
        except HTTPError as e:                       # HTTPError 是 URLError 的子類別，要先接
            if e.code not in RETRY_HTTP or i == tries - 1:
                raise
            wait = _retry_after(e) or 2 ** (i + 1)
        except (URLError, OSError, HTTPException):   # 逾時是 OSError；讀到一半斷線（IncompleteRead）不是，要另外接
            if i == tries - 1:
                raise
            wait = 2 ** (i + 1)
        wait = min(wait, 16)
        if _left() < wait + 5:
            raise TimeoutError("超過本輪時間預算")
        time.sleep(wait)


def scrub(msg):
    """錯誤訊息會寫進公開的 data/typhoon.json，金鑰一律遮掉再寫出去。"""
    msg = str(msg)
    if KEY:
        for k in {KEY, quote(KEY, safe="")}:
            msg = msg.replace(k, "***")
    return msg


def fetch_records(dataset):
    """授權碼優先放 HTTP 標頭——網址會出現在錯誤訊息、代理記錄裡。

    氣象署的 datastore 文件寫兩種都收；萬一標頭這條被拒（401／403），
    再用網址參數試一次，兩種都被拒才算是金鑰本身的問題。
    """
    base = f"{DATASTORE}{dataset}?format=JSON"
    rejected = None
    for mode in ("header", "query"):
        try:
            if mode == "header":
                raw = grab(base, {"Authorization": KEY})
            else:
                raw = grab(f"{base}&Authorization={quote(KEY, safe='')}")
        except HTTPError as e:
            if e.code in (401, 403):
                rejected = e
                continue
            raise
        j = json.loads(raw.decode("utf-8", "replace"))
        # 氣象署金鑰錯誤有時是 200＋success:"false"，不是 HTTP 401
        if str(j.get("success", "true")).lower() == "false":
            rejected = ValueError(str(j.get("message") or j.get("result") or "success=false")[:200])
            continue
        rec = j.get("records")
        if not isinstance(rec, dict):
            raise ValueError("回應裡沒有 records")
        print(f"{dataset}: auth via {mode}")
        return rec
    raise KeyRejected(scrub(f"{dataset}: 授權碼被拒（{rejected}）"))


def count_cyclones(rec):
    """跟前端 fetchCWAAll 同一套找法：大小寫都認、單筆物件也算一顆。"""
    def pick(o, *ks):
        for k in ks:
            if isinstance(o, dict) and o.get(k) is not None:
                return o[k]
        return None
    wrap = pick(rec, "tropicalCyclones", "TropicalCyclones") or {}
    lst = pick(wrap, "TropicalCyclone", "tropicalCyclone")
    if lst is None:
        return 0
    return len(lst) if isinstance(lst, list) else 1


def _as_list(x):
    return x if isinstance(x, list) else ([x] if isinstance(x, dict) else [])


def bulletins(rec):
    """從警報單取出「第幾報、什麼類別、幾點生效」。

    **為什麼這一段例外要在後端解析**：警報單只有「目前這一報」，
    第 4 報裡不會寫第 1 報是幾點發的。使用者要的「海上／陸上警報官方發布時間」
    只能靠每一報都記下來，而記錄只能在後端做——前端不會一直開著。
    只取這五個欄位，其餘照舊交給前端解析，不在這裡重寫一套。
    """
    out = []
    for i in _as_list((rec or {}).get("info")):
        head = str(i.get("headline") or "")
        eff = str(i.get("effective") or i.get("onset") or "")
        if not head or not eff:
            continue
        no, sub, kind, tyno, name = None, 0, "", "", ""
        desc = i.get("description") if isinstance(i.get("description"), dict) else {}
        for t in _as_list(desc.get("typhoon-info")):
            for sc in _as_list(t.get("section")):
                title, val = sc.get("title"), sc.get("value")
                if title == "警報報數":
                    # 有「5-1」這種加報：陸上警報常在兩個整報之間用加報發布（10/22 薔琵就是 5-1 報）。
                    # 以前只認純數字，加報整份被當成沒有報數，正好漏掉陸上警報的發布時間。
                    m = re.match(r"^\s*(\d+)\s*(?:[-－—]\s*(\d+))?\s*$", str(val))
                    if m:
                        no, sub = int(m.group(1)), int(m.group(2) or 0)
                elif title == "警報類別":
                    kind = str(val or "")
                elif title == "颱風編號":
                    tyno = str(val or "")
                elif title == "颱風資訊":
                    name = str(sc.get("cwa_typhoon_name") or "")
        out.append({"ty": tyno, "name": name, "no": no, "sub": sub, "kind": kind,
                    "headline": head, "effective": eff})
    return out


LOG_DAYS, LOG_MAX = 90, 300


def merge_log(old, new, seen):
    """同一報（颱風編號＋報數＋生效時間）只記一次；seen 是第一次抓到的時間，
    跟 effective 比得出「排程晚了多久才看到」。只留 90 天、300 筆。"""
    log = [x for x in (old or []) if isinstance(x, dict) and x.get("effective")]
    # 同一報＝同一顆颱風、同一個生效時間（報數可能是舊版解析漏掉的 None，不拿來當鍵）
    keys = {(x.get("ty"), x.get("effective")) for x in log}
    for b in new:
        k = (b["ty"], b["effective"])
        if k not in keys:
            log.append(dict(b, seen=seen))
            keys.add(k)
    cutoff = datetime.now(TW) - timedelta(days=LOG_DAYS)

    def recent(x):
        try:
            return datetime.fromisoformat(x["effective"]) >= cutoff
        except (TypeError, ValueError):
            return False
    log = sorted((x for x in log if recent(x)), key=lambda x: x["effective"])
    return log[-LOG_MAX:]


def kml_text(raw):
    """KMZ 就是一個 ZIP，裡面通常只有 doc.kml；KML 則原樣回傳。"""
    if raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ValueError("KMZ 裡沒有 .kml")
            return z.read(names[0]).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def _dp(pts, tol):
    """Douglas–Peucker（迭代版）。點是 (lat, lng)。"""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, c = stack.pop()
        (ay, ax), (cy, cx) = pts[a], pts[c]
        dx, dy = cx - ax, cy - ay
        span = (dx * dx + dy * dy) ** 0.5
        best, bi = -1.0, -1
        for i in range(a + 1, c):
            py, px = pts[i]
            d = abs(dy * px - dx * py + cx * ay - cy * ax) / span if span else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, c)]
    return [p for p, k in zip(pts, keep) if k]


def _coords(txt):
    out = []
    for tok in (txt or "").split():
        c = tok.split(",")
        try:
            lng, lat = float(c[0]), float(c[1])
        except (ValueError, IndexError):
            continue
        out.append((lat, lng))
    return out


_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?")


def parse_prob(text):
    """KML 的每個 Placemark：名稱或說明裡的「NN%」是機率，Polygon 是等值區（可能有洞），Point 是測點。

    還沒看過真的回應長什麼樣（開發環境連不到氣象署），所以照 KML 標準寫得寬一點：
    不認命名空間、多邊形與點都收、機率寫在 name 或 description 都認。
    一筆都解析不出來的時候，留檔頭前 1500 字在 head 欄，下一輪才查得出是格式變了還是真的沒資料。"""
    root = ET.fromstring(text.encode("utf-8"))
    tag = lambda el: el.tag.rsplit("}", 1)[-1]     # noqa: E731 — 去掉命名空間
    def first(el, name):
        for x in el.iter():
            if tag(x) == name:
                return x
        return None
    items = []
    for pm in root.iter():
        if tag(pm) != "Placemark":
            continue
        nm = (first(pm, "name").text or "").strip() if first(pm, "name") is not None else ""
        ds = first(pm, "description")
        desc = re.sub(r"<[^>]+>", " ", (ds.text or "") if ds is not None else "").strip()
        m = _PCT.search(nm + " " + desc)
        p = float(m.group(1)) if m else (float(nm) if re.fullmatch(r"\d+(?:\.\d+)?", nm) else None)
        polys, pts = [], []
        for pg in [x for x in pm.iter() if tag(x) == "Polygon"]:
            ob = first(pg, "outerBoundaryIs")
            co = first(ob if ob is not None else pg, "coordinates")
            outer = _dp(_coords(co.text if co is not None else ""), PROB_TOL)
            if len(outer) < 3:
                continue
            holes = []
            for ib in [x for x in pg.iter() if tag(x) == "innerBoundaryIs"]:
                hc = first(ib, "coordinates")
                h = _dp(_coords(hc.text if hc is not None else ""), PROB_TOL)
                if len(h) >= 3:
                    holes.append([[round(a, 3), round(b, 3)] for a, b in h])
            polys.append({"o": [[round(a, 3), round(b, 3)] for a, b in outer], "h": holes})
        for pt in [x for x in pm.iter() if tag(x) == "Point"]:
            co = first(pt, "coordinates")
            c = _coords(co.text if co is not None else "")
            if c:
                pts.append([round(c[0][0], 3), round(c[0][1], 3)])
        if polys or pts:
            items.append({"name": nm[:60], "desc": desc[:120], "p": p, "polys": polys, "pts": pts})
    tm = _ISO.search(text)
    out = {"time": tm.group(0) if tm else None, "items": items}
    if not items:
        out["head"] = text[:1500]
    return out


def fetch_prob():
    """侵襲機率是檔案型資料（fileapi）。授權碼一樣先放標頭，被拒才改網址參數。
    回傳 None＝目前沒有這份產品（沒有颱風時氣象署不出），不是錯誤。"""
    base = f"{FILEAPI}{PROB_SET}?downloadType=WEB&format=KMZ"
    rejected = None
    for mode in ("header", "query"):
        try:
            if mode == "header":
                raw = grab(base, {"Authorization": KEY, "Accept": "*/*"})
            else:
                raw = grab(f"{base}&Authorization={quote(KEY, safe='')}", {"Accept": "*/*"})
        except HTTPError as e:
            if e.code in (401, 403):
                rejected = e
                continue
            if e.code == 404:
                return None
            raise
        if not raw or not raw.strip():
            return None
        if raw.lstrip()[:1] in (b"{", b"["):          # 錯誤是用 JSON 回的（例如金鑰在標頭不被接受）
            j = json.loads(raw.decode("utf-8", "replace"))
            rejected = ValueError(str(j.get("message") or j.get("result") or j)[:200] if isinstance(j, dict) else str(j)[:200])
            continue
        print(f"{PROB_SET}: auth via {mode}, {len(raw)} bytes")
        return parse_prob(kml_text(raw))
    raise KeyRejected(scrub(f"{PROB_SET}: 授權碼被拒（{rejected}）"))


def load_prev():
    try:
        with open(OUT, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else None
    except (OSError, ValueError):
        return None


def age_of(doc):
    try:
        return (datetime.now(TW) - datetime.fromisoformat(doc.get("updated", ""))).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return float("inf")


def write(obj):
    """只有內容變了，或時間戳超過 REFRESH_SEC 才寫檔——每次寫檔都是一次 commit＋Pages 部署。"""
    prev = load_prev()
    if prev is not None:
        a, b = dict(obj), dict(prev)
        a.pop("updated", None)
        b.pop("updated", None)
        if a == b and obj.get("hasKey") is False:
            print("placeholder unchanged — no commit")
            return False
        if a == b and age_of(prev) < REFRESH_SEC:
            print(f"unchanged ({int(age_of(prev))}s old) — no commit")
            return False
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    return True


def main():
    now = datetime.now(TW).isoformat(timespec="seconds")
    # 收過的報數是**累積**的，每一種寫檔（包括錯誤檔）都要帶著，不然一次失敗就把歷史丟光
    old_log = (load_prev() or {}).get("warnLog") or []
    if not KEY:
        write({"v": 2, "hasKey": False, "updated": now,
               "note": "repo 尚未設定 CWA_API_KEY Secret，後端管線未啟用；前端讀到 hasKey:false 會自動略過。"})
        print("no CWA_API_KEY — placeholder only")
        return 0

    try:
        path = fetch_records(PATH_SET)
    except KeyRejected as e:
        # 金鑰本身的問題：寫錯誤檔讓畫面講清楚，並且讓工作流程紅燈通知人去換 Secret。
        write({"v": 2, "hasKey": True, "updated": now, "error": str(e), "warnLog": old_log})
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:                           # noqa: BLE001 — 網路、JSON 什麼都可能壞
        msg = scrub(f"{PATH_SET}: {e}")
        prev = load_prev()
        if prev and prev.get("raw") and age_of(prev) < KEEP_ON_FAIL_SEC:
            print(f"{msg} — keeping previous data ({int(age_of(prev))}s old)", file=sys.stderr)
            return 0
        write({"v": 2, "hasKey": True, "updated": now, "error": msg, "warnLog": old_log})
        print(msg, file=sys.stderr)
        return 0

    # 警報單只是加分資訊：抓不到不能拖垮路徑。
    warn, warn_err = None, ""
    try:
        warn = fetch_records(WARN_SET)
    except Exception as e:                           # noqa: BLE001
        warn_err = scrub(f"{WARN_SET}: {e}")
        print(warn_err, file=sys.stderr)

    # 侵襲機率也只是加分資訊：抓不到、解析不了都不影響路徑與警報
    prob, prob_err = None, ""
    try:
        prob = fetch_prob()
    except Exception as e:                           # noqa: BLE001
        prob_err = scrub(f"{PROB_SET}: {e}")
        print(prob_err, file=sys.stderr)

    doc = {"v": 2, "hasKey": True, "updated": now, "source": "datastore",
           "count": count_cyclones(path),
           "raw": {PATH_SET: path, WARN_SET: warn, PROB_SET: prob},
           "warnLog": merge_log(old_log, bulletins(warn) if warn else [], now)}
    if warn_err:
        doc["warnError"] = warn_err
    if prob_err:
        doc["probError"] = prob_err
    wrote = write(doc)
    size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    np = len(prob["items"]) if prob else 0
    print(f"{doc['count']} cyclone(s), warning={'ok' if warn is not None else 'fail'}, "
          f"prob={'fail' if prob_err else np}, {size // 1024} KB, {'written' if wrote else 'skipped'}")
    return 0


# 取自 10/22 實際的 W-C0034-001（薔琵第 4 報），只留用得到的欄位。
SAMPLE_WARN = {"info": [{"headline": "海上颱風警報", "effective": "2026-10-22T08:30:00+08:00",
    "description": {"section": [{"title": "命名與位置", "value": "…"}],
                    "typhoon-info": [{"section": [
                        {"title": "警報報數", "value": "4"}, {"title": "警報類別", "value": "SEA"},
                        {"title": "颱風編號", "value": "29"},
                        {"title": "颱風資訊", "typhoon_name": "CHAMPI", "cwa_typhoon_name": "薔琵"}]}]}}]}

# 形狀照前端註解記錄的實際回應：PascalCase、座標分兩欄、單筆不包陣列。
SAMPLE_PATH = {"tropicalCyclones": {"TropicalCyclone": {
    "TyphoonName": "TESTA", "CwaTyphoonName": "測試", "CwaTdNo": "22",
    "AnalysisData": {"Fix": [{"DateTime": "2026-10-19T08:00:00+08:00",
                              "CoordinateLatitude": "20.1", "CoordinateLongitude": "125.3",
                              "MaxWindSpeed": "33", "Circle15ms": {"Radius": "200"}}]},
    "ForecastData": {"Fix": [{"Tau": "24", "CoordinateLatitude": "22.0",
                              "CoordinateLongitude": "122.5", "MaxWindSpeed": "38"}]},
}}}


# 侵襲機率的 KML：還沒拿到真的回應（開發環境連不到氣象署），照 KML 標準造兩種寫法——
# 等值區多邊形（有洞、機率寫在 name）與測點（機率寫在 description 的 HTML 裡）。
SAMPLE_PROB_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>2026-10-22T14:00:00+08:00</name>
<Placemark><name>60%</name><Polygon><outerBoundaryIs><LinearRing><coordinates>
120.0,22.0,0 123.0,22.0,0 123.0,26.0,0 120.0,26.0,0 120.0,22.0,0</coordinates></LinearRing></outerBoundaryIs>
<innerBoundaryIs><LinearRing><coordinates>121.0,24.5 122.0,24.5 122.0,25.5 121.0,25.5 121.0,24.5</coordinates></LinearRing></innerBoundaryIs>
</Polygon></Placemark>
<Placemark><name>80</name><MultiGeometry><Polygon><outerBoundaryIs><LinearRing><coordinates>
121.0,24.5 122.0,24.5 122.0,25.5 121.0,25.5 121.0,24.5</coordinates></LinearRing></outerBoundaryIs></Polygon></MultiGeometry></Placemark>
<Placemark><name>臺北市</name><description><![CDATA[<b>侵襲機率</b> 100 %]]></description>
<Point><coordinates>121.563,25.038,0</coordinates></Point></Placemark>
</Document></kml>"""


def _kmz(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", text)
    return buf.getvalue()


def selftest():
    """離線自我測試：計數、空殼不重寫、失敗時保留舊資料、金鑰被拒紅燈、標頭優先、金鑰遮蔽、侵襲機率。"""
    global OUT, KEY
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok = ok and bool(cond)

    check(count_cyclones(SAMPLE_PATH) == 1, "單筆物件（沒包陣列）也算一顆")

    b = bulletins(SAMPLE_WARN)
    check(b == [{"ty": "29", "name": "薔琵", "no": 4, "sub": 0, "kind": "SEA", "headline": "海上颱風警報",
                 "effective": "2026-10-22T08:30:00+08:00"}], f"警報單取出報數、類別、編號、名稱（{b}）")
    w51 = json.loads(json.dumps(SAMPLE_WARN))
    w51["info"][0]["description"]["typhoon-info"][0]["section"][0]["value"] = "5-1"
    b51 = bulletins(w51)
    check(b51[0]["no"] == 5 and b51[0]["sub"] == 1, f"加報「5-1」解析成第 5 報之 1（{b51[0]['no']}, {b51[0]['sub']}）")
    check(bulletins({}) == [] and bulletins({"info": {"headline": ""}}) == [], "空的或缺欄位的警報單不會壞")
    lg = merge_log([], b, "S1")
    lg = merge_log(lg, b, "S2")
    check(len(lg) == 1 and lg[0]["seen"] == "S1", "同一報只記一次，保留第一次看到的時間")
    old = dict(b[0], effective="2020-01-01T00:00:00+08:00", no=1)
    check(len(merge_log([old], b, "S")) == 1, "超過 90 天的舊報數會被清掉")
    check(count_cyclones({"tropicalCyclones": {"tropicalCyclone": [{}, {}]}}) == 2, "小寫欄位、雙颱")
    check(count_cyclones({}) == 0 and count_cyclones({"tropicalCyclones": {}}) == 0, "沒有颱風是 0，不會壞")

    pr = parse_prob(SAMPLE_PROB_KML)
    ps = {it["name"]: it for it in pr["items"]}
    check(len(pr["items"]) == 3, f"侵襲機率：多邊形與測點都收（{len(pr['items'])} 筆）")
    check(ps["60%"]["p"] == 60 and len(ps["60%"]["polys"][0]["h"]) == 1, "名稱寫「60%」、等值區有洞也留著")
    check(ps["80"]["p"] == 80 and len(ps["80"]["polys"]) == 1, "名稱是純數字也當機率；MultiGeometry 裡的多邊形也收")
    check(ps["臺北市"]["p"] == 100 and ps["臺北市"]["pts"] == [[25.038, 121.563]], "說明裡的「100 %」（HTML 裡）也認得；點是 [緯, 經]")
    check(pr["time"] == "2026-10-22T14:00:00+08:00", "抓得到產品時間")
    check(parse_prob(kml_text(_kmz(SAMPLE_PROB_KML)))["items"] == pr["items"], "KMZ（ZIP）解得開")
    bad = parse_prob('<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>x</name></Document></kml>')
    check(bad["items"] == [] and "head" in bad, "一筆都解析不出來：留檔頭給人查，不是默默給空的")
    ring = [(0, 0)] + [(i / 1000, 0.00001 * (i % 2)) for i in range(1, 999)] + [(1, 0), (1, 1), (0, 1), (0, 0)]
    check(len(_dp(ring, PROB_TOL)) < 10, "等值線會被簡化（不會把上千點原樣寫進公開檔）")

    real_open, real_sleep = globals()["urlopen"], time.sleep
    saved_out, saved_key = OUT, KEY

    class _R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []

    def fake_ok(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        if PROB_SET in req.full_url:
            return _R(_kmz(SAMPLE_PROB_KML))
        ds = WARN_SET if WARN_SET in req.full_url else PATH_SET
        rec = SAMPLE_PATH if ds == PATH_SET else SAMPLE_WARN
        return _R(json.dumps({"success": "true", "records": rec}).encode())

    def fake_prob_down(req, timeout=None):
        if PROB_SET in req.full_url:
            raise URLError("prob down")
        return fake_ok(req, timeout)

    def fake_prob_none(req, timeout=None):
        if PROB_SET in req.full_url:
            return _R(b"")
        return fake_ok(req, timeout)

    def fake_down(req, timeout=None):
        raise URLError("down")

    def fake_401(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        raise HTTPError(req.full_url, 401, "no", {}, None)

    def fake_header_rejected(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        if req.get_header("Authorization"):
            raise HTTPError(req.full_url, 401, "no", {}, None)
        return fake_ok(req, timeout)

    try:
        time.sleep = lambda s: None
        with tempfile.TemporaryDirectory() as d:
            OUT = os.path.join(d, "data", "typhoon.json")
            KEY = "ab/c+d"

            globals()["urlopen"] = fake_ok
            check(main() == 0, "正常：結束碼 0")
            doc = json.load(open(OUT, encoding="utf-8"))
            check(doc["count"] == 1 and doc["raw"][PATH_SET] == SAMPLE_PATH, "原始 records 原樣保留")
            check(doc["raw"][WARN_SET] == SAMPLE_WARN, "警報單也帶上")
            check(len(doc.get("warnLog", [])) == 1 and doc["warnLog"][0]["no"] == 4, "收到的報數記進 warnLog")
            check(len((doc["raw"].get(PROB_SET) or {}).get("items", [])) == 3, "侵襲機率（KMZ）解析後一起寫進去")
            check(all(KEY not in u for u, _ in seen), "金鑰不放網址（走標頭）")
            check(all(h == KEY for _, h in seen), "標頭帶的是授權碼")
            first = open(OUT, encoding="utf-8").read()
            main()
            check(open(OUT, encoding="utf-8").read() == first, "內容沒變：不重寫（不產生 commit）")

            globals()["urlopen"] = fake_down
            check(main() == 0 and open(OUT, encoding="utf-8").read() == first,
                  "暫時抓不到：手上舊資料夠新就原封不動")
            stale = dict(doc, updated=(datetime.now(TW) - timedelta(hours=7)).isoformat(timespec="seconds"))
            json.dump(stale, open(OUT, "w", encoding="utf-8"))
            main()
            doc2 = json.load(open(OUT, encoding="utf-8"))
            check("error" in doc2 and "raw" not in doc2, "舊資料太舊又抓不到：改寫成錯誤檔，不假裝還有效")
            check(len(doc2.get("warnLog", [])) == 1, "寫錯誤檔時，收過的報數照樣留著")

            globals()["urlopen"] = fake_prob_down
            main()
            d3 = json.load(open(OUT, encoding="utf-8"))
            check(d3["raw"][PATH_SET] == SAMPLE_PATH and d3["raw"][PROB_SET] is None and "probError" in d3,
                  "侵襲機率抓不到：路徑照寫，另記 probError")
            globals()["urlopen"] = fake_prob_none
            main()
            d4 = json.load(open(OUT, encoding="utf-8"))
            check(d4["raw"][PROB_SET] is None and "probError" not in d4, "目前沒有侵襲機率產品（空回應）：不是錯誤")

            seen.clear()
            globals()["urlopen"] = fake_header_rejected
            check(main() == 0 and "raw" in json.load(open(OUT, encoding="utf-8")),
                  "標頭被拒：退回網址參數照樣抓得到")

            globals()["urlopen"] = fake_401
            check(main() == 1, "標頭、網址都被拒：結束碼 1（工作流程紅燈，要人換 Secret）")
            err = json.load(open(OUT, encoding="utf-8")).get("error", "")
            check("授權碼被拒" in err and KEY not in err and quote(KEY, safe="") not in err,
                  "錯誤檔講得出原因，而且不含金鑰")

            KEY = ""
            check(main() == 0 and json.load(open(OUT, encoding="utf-8"))["hasKey"] is False,
                  "沒設 Secret：寫 hasKey:false 空殼")
    finally:
        globals()["urlopen"], time.sleep = real_open, real_sleep
        OUT, KEY = saved_out, saved_key

    print("全部通過" if ok else "有項目失敗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if sys.argv[1:2] == ["selftest"] else main())
