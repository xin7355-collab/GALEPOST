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
"""
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
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


def grab(url):
    req = Request(url, headers={"User-Agent": "galepost-lightning/1.0"})
    with urlopen(req, timeout=45) as r:
        return r.read()


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


def write(obj):
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
        print("no CWA_API_KEY — wrote placeholder")
        return 0

    tried = []
    for tag, url in candidates(KEY):
        try:
            raw = grab(url)
        except (URLError, HTTPError, OSError) as e:
            tried.append(f"{tag}: {e}")
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
            tried.append(f"{tag}: parse {e}")
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


if __name__ == "__main__":
    sys.exit(main())
