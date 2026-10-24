#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把中央氣象署的落雷資料抓下來，轉成前端直接讀得懂的一份 JSON。

**為什麼需要這支程式**

O-A0039-001（過去 1 小時閃電偵測資料）的端點**沒有送 CORS 標頭**，
瀏覽器直連一定被擋（實機訊息是 `Load failed`）。這不是網址寫錯，
也不是格式不對——瀏覽器就是不准讀。

所以正解跟 `data/typhoon.json` 一樣：**由後端抓好，放成同源的靜態檔**。
同源就沒有 CORS 這件事，不需要代理、不需要使用者做任何設定。

沒有 CWA_API_KEY 時寫一個 `hasKey:false` 的空殼，前端會自動略過，
不會在畫面上留下一個永遠失敗的區塊。

輸出：data/lightning.json
  {
    "hasKey": true,
    "updated": "2026-08-30T18:05:00+08:00",   # 這支程式跑的時間
    "window": "2026-08-30 17:05 ~ 2026-08-30 18:05",
    "source": "fileapi KMZ",
    "n": 124, "cg": 33, "ic": 91,
    "strikes": [{"lat":24.599,"lng":120.824,"t":"...","cg":true}, ...]
  }

離線自我測試（不連網、不需要金鑰）：
  python3 scripts/fetch_lightning.py selftest
"""
import io
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from http.client import HTTPException
from xml.etree import ElementTree as ET

DS = os.environ.get("CWA_LIGHTNING_DATASET", "O-A0039-001")
KEY = os.environ.get("CWA_API_KEY", "").strip()
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "lightning.json")
TW = timezone(timedelta(hours=8))
FILEAPI = "https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/"
DATASTORE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"


def candidates(key):
    k = quote(key, safe="")
    return [
        ("fileapi KMZ", f"{FILEAPI}{DS}?Authorization={k}&downloadType=WEB&format=KMZ"),
        ("fileapi KML", f"{FILEAPI}{DS}?Authorization={k}&downloadType=WEB&format=KML"),
        ("datastore JSON", f"{DATASTORE}{DS}?Authorization={k}&format=JSON"),
    ]


# 整支程式的時間預算。排程每 10 分鐘一次，而且 concurrency 會砍掉還在跑的上一輪——
# 超過預算就放棄、寫錯誤檔，總比卡到被下一輪砍掉、什麼都沒寫好。
BUDGET_SEC = 240
_T0 = time.monotonic()
RETRY_HTTP = {429, 500, 502, 503, 504}


def _left():
    return BUDGET_SEC - (time.monotonic() - _T0)


def _retry_after(e):
    try:
        return max(0, int(e.headers.get("Retry-After", "")))
    except (TypeError, ValueError, AttributeError):
        return 0


def grab(url, tries=3):
    """暫時性錯誤（429、5xx、逾時、連線中斷）退避 2、4 秒再試；有 Retry-After 就照它。

    401／403／404 是金鑰或網址的問題，重試幾次都一樣，直接丟出去讓下一個端點上場。
    """
    for i in range(tries):
        left = _left()
        if left < 5:
            raise TimeoutError("超過本輪時間預算")
        try:
            req = Request(url, headers={"User-Agent": "galepost-lightning/1.0"})
            with urlopen(req, timeout=min(30, left)) as r:
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
    """錯誤訊息會寫進 data/lightning.json——那是**公開**的 GitHub Pages 檔案。
    urllib 目前的錯誤字串不含網址，但不能賭它永遠不含：金鑰一律遮掉再寫出去。"""
    msg = str(msg)
    if KEY:
        for k in {KEY, quote(KEY, safe="")}:
            msg = msg.replace(k, "***")
    return msg


def kml_from(raw):
    """KMZ 就是一個 ZIP，裡面通常只有 doc.kml。KML 則原樣回傳。"""
    if raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ValueError("ZIP 裡沒有 .kml")
            return z.read(names[0]).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


CG_RE = re.compile("對地")
NS = {"k": "http://www.opengis.net/kml/2.2"}


def parse_kml(text):
    """一筆 Placemark：description 寫閃電種類與時間，Point/coordinates 是 lng,lat。

    種類分「對地」與「雲間」——**只有對地會打到地面設備**，
    兩者一定要分開，合在一起會把數字灌水一倍以上
    （官方樣本 124 筆裡有 91 筆是雲間）。
    """
    root = ET.fromstring(text)
    window = ""
    for nm in root.iter("{http://www.opengis.net/kml/2.2}name"):
        t = (nm.text or "").strip()
        if "~" in t:
            window = re.sub(r"^[^:：]*[:：]\s*", "", t)
            break
    out = []
    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        co = pm.find(".//k:Point/k:coordinates", NS)
        if co is None or not (co.text or "").strip():
            continue
        parts = co.text.strip().split(",")
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            continue
        desc = "".join(pm.itertext())
        when = pm.find(".//k:TimeStamp/k:when", NS)
        out.append({
            "lat": round(lat, 4), "lng": round(lng, 4),
            "t": (when.text or "").strip() if when is not None else "",
            "cg": bool(CG_RE.search(desc)),
        })
    return window, out


def parse_json(raw):
    """JSON 端點的形狀沒有文件。遞迴找同時具備經緯度的物件，
    而不是賭某一個固定路徑——賭錯就是整份靜靜落空。"""
    data = json.loads(raw.decode("utf-8", "replace"))
    out, seen = [], set()

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def walk(node, t="", kind=""):
        if isinstance(node, list):
            for x in node:
                walk(x, t, kind)
            return
        if not isinstance(node, dict):
            return
        k = str(node.get("LightningType") or node.get("lightningType")
                or node.get("type") or kind or "")
        tt = str(node.get("LightningTime") or node.get("lightningTime")
                 or node.get("DateTime") or node.get("dataTime") or t or "")
        lat = num(node.get("Latitude") or node.get("latitude") or node.get("lat"))
        lng = num(node.get("Longitude") or node.get("longitude")
                  or node.get("lon") or node.get("lng"))
        if lat is not None and lng is not None and abs(lat) <= 90 and abs(lng) <= 180:
            sig = (round(lat, 4), round(lng, 4), tt)
            if sig not in seen:
                seen.add(sig)
                out.append({"lat": round(lat, 4), "lng": round(lng, 4),
                            "t": tt, "cg": bool(CG_RE.search(k))})
        for v in node.values():
            walk(v, tt or t, k or kind)

    walk(data.get("records", data))
    return "", out


# 內容沒變時，最多每小時刷新一次時間戳
REFRESH_SEC = 55 * 60


def write(obj):
    """只有**內容**變了才真的寫檔。

    `updated` 每一次執行都會變，所以不能拿整個檔案去比對有沒有變動——
    那樣工作流程會每十分鐘 commit 一次（一天 144 次），而每一次 commit
    都會觸發一輪 GitHub Pages 重新部署。沒有雷的日子不該產生任何提交。

    但也不能永遠不更新時間戳：前端是用 `updated` 算資料年齡的，
    一直不動的話「現在沒有落雷」會被當成很舊的資料而被丟掉。
    折衷是內容沒變時**每小時**才刷新一次。
    """
    prev = None
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = None

    if isinstance(prev, dict):
        a, b = dict(obj), dict(prev)
        a.pop("updated", None)
        b.pop("updated", None)
        # 沒金鑰的空殼不必刷新時間戳：前端看到 hasKey:false 就直接略過，根本不看年齡。
        # 以前照樣每小時刷一次，等於一天十幾次沒有意義的 commit＋Pages 部署。
        if a == b and obj.get("hasKey") is False:
            print("placeholder unchanged — no commit")
            return
        if a == b:
            age = REFRESH_SEC + 1
            try:
                age = (datetime.now(TW)
                       - datetime.fromisoformat(prev.get("updated", ""))).total_seconds()
            except (TypeError, ValueError):
                pass
            if age < REFRESH_SEC:
                print(f"unchanged ({int(age)}s old) — leaving file alone, no commit")
                return

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def main():
    now = datetime.now(TW).isoformat(timespec="seconds")
    if not KEY:
        write({"hasKey": False, "updated": now, "strikes": [],
               "note": "repo 尚未設定 CWA_API_KEY Secret，後端管線未啟用；"
                       "前端讀到 hasKey:false 會自動略過。"})
        print("no CWA_API_KEY — placeholder only (backend pipeline disabled)")
        return 0

    tried = []
    for tag, url in candidates(KEY):
        try:
            raw = grab(url)
        except (URLError, HTTPError, OSError, HTTPException) as e:   # TimeoutError 也是 OSError；IncompleteRead 不是
            tried.append(scrub(f"{tag}: {e}"))
            continue
        if not raw:
            tried.append(f"{tag}: empty")
            continue
        try:
            if raw[:1] in (b"{", b"["):
                window, strikes = parse_json(raw)
            else:
                window, strikes = parse_kml(kml_from(raw))
        except Exception as e:                       # noqa: BLE001 — 什麼都可能壞，記下來換下一個
            tried.append(scrub(f"{tag}: parse {e}"))
            continue
        # 解析成功但一筆都沒有是**正常的**——沒有雷的時候本來就是空的。
        cg = sum(1 for s in strikes if s["cg"])
        write({"hasKey": True, "updated": now, "window": window, "source": tag,
               "n": len(strikes), "cg": cg, "ic": len(strikes) - cg,
               "strikes": strikes})
        print(f"{tag}: {len(strikes)} strikes ({cg} cloud-to-ground) window={window!r}")
        return 0

    write({"hasKey": True, "updated": now, "strikes": [], "n": 0, "cg": 0, "ic": 0,
           "error": "；".join(tried)})
    print("all endpoints failed:\n  " + "\n  ".join(tried), file=sys.stderr)
    # 寫了帶 error 的檔案就算完成——前端會把原因顯示出來，不要讓工作流程紅一片
    return 0


# 取自使用者提供的官方 KMZ 的形狀（座標、名稱格式照抄），兩筆：一筆雲間、一筆對地。
SAMPLE_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<name>閃電資料: 2026-08-30 17:05 ~ 2026-08-30 18:05</name>
<Placemark><name>雲間閃電 1-2026-08-30 17:05</name>
  <description> 閃電種類: 雲間
 時間: 2026-08-30 17:05
 經緯度: 120.814 , 24.566</description>
  <TimeStamp><when>2026-08-30T09:05Z</when></TimeStamp>
  <Point><coordinates>120.814,24.566</coordinates></Point></Placemark>
<Placemark><name>雲對地閃電 2-2026-08-30 17:09</name>
  <description> 閃電種類: 雲對地
 時間: 2026-08-30 17:09
 經緯度: 120.82412 , 24.59931</description>
  <TimeStamp><when>2026-08-30T09:09Z</when></TimeStamp>
  <Point><coordinates>120.82412,24.59931</coordinates></Point></Placemark>
<Placemark><name>壞掉的一筆</name><Point><coordinates>abc</coordinates></Point></Placemark>
</Document></kml>"""


def selftest():
    """離線自我測試：解析三種格式、空殼不重寫、金鑰遮蔽、暫時性錯誤會重試。"""
    global OUT, KEY
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok = ok and bool(cond)

    win, st = parse_kml(SAMPLE_KML)
    check(len(st) == 2, "KML：兩筆有效、一筆壞座標略過")
    check([s["cg"] for s in st] == [False, True], "KML：雲間／對地分得開")
    check(st[1]["lat"] == 24.5993 and st[1]["lng"] == 120.8241, "KML：座標取到小數 4 位")
    check(win.startswith("2026-08-30 17:05 ~"), f"KML：抓得到資料時間區間（{win!r}）")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", SAMPLE_KML)
    check(len(parse_kml(kml_from(buf.getvalue()))[1]) == 2, "KMZ：ZIP 解得開")

    js = json.dumps({"records": {"Lightning": [
        {"LightningType": "雲對地", "LightningTime": "2026-08-30 17:09",
         "Latitude": "24.6", "Longitude": "120.8"},
        {"LightningType": "雲間", "Latitude": 24.7, "Longitude": 120.9},
        {"LightningType": "雲間", "Latitude": 24.7, "Longitude": 120.9},  # 重複
        {"Latitude": 999, "Longitude": 120.9},                              # 不合理
    ]}}).encode()
    _, sj = parse_json(js)
    check(len(sj) == 2 and sj[0]["cg"] and not sj[1]["cg"], "JSON：去重、擋掉不合理座標、分對地")

    saved_out, saved_key = OUT, KEY
    with tempfile.TemporaryDirectory() as d:
        OUT = os.path.join(d, "data", "lightning.json")
        stub = {"hasKey": False, "updated": "2026-01-01T00:00:00+08:00", "strikes": [], "note": "x"}
        write(stub)
        first = open(OUT, encoding="utf-8").read()
        write(dict(stub, updated=datetime.now(TW).isoformat(timespec="seconds")))
        check(open(OUT, encoding="utf-8").read() == first, "空殼內容沒變：就算很舊也不重寫（不產生 commit）")
        write({"hasKey": True, "updated": "2026-01-01T00:00:00+08:00", "strikes": [], "n": 0})
        check('"hasKey":true' in open(OUT, encoding="utf-8").read(), "內容真的變了：照寫")

    KEY = "ab/c+d"
    check(scrub(f"x {KEY} y {quote(KEY, safe='')}") == "x *** y ***", "錯誤訊息裡的金鑰（含網址編碼）會遮掉")
    OUT, KEY = saved_out, saved_key

    # 暫時性錯誤：第一次 503、第二次成功 → 應該拿到資料，而且真的有等（但測試不真的睡）
    calls, slept = [], []
    real_open, real_sleep = globals()["urlopen"], time.sleep

    class _R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise HTTPError(req.full_url, 503, "busy", {}, None)
        return _R(b"PAYLOAD")

    def fake_401(req, timeout=None):
        calls.append(timeout)
        raise HTTPError(req.full_url, 401, "no", {}, None)

    globals()["urlopen"], time.sleep = fake_open, slept.append
    try:
        check(grab("https://example.invalid/x") == b"PAYLOAD" and slept == [2], "503 會退避 2 秒後重試成功")
        calls.clear(); slept.clear()
        globals()["urlopen"] = fake_401
        try:
            grab("https://example.invalid/x")
            check(False, "401 應該直接丟出")
        except HTTPError:
            check(len(calls) == 1 and not slept, "401 不重試（金鑰問題，重試也一樣）")
    finally:
        globals()["urlopen"], time.sleep = real_open, real_sleep

    print("全部通過" if ok else "有項目失敗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if sys.argv[1:2] == ["selftest"] else main())
