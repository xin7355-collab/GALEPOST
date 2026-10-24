#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把人事行政總處「天然災害停止上班及上課情形」那一頁抓下來，存成前端讀得懂的 data/dgpa.json。

**為什麼需要這支程式**

官方那一頁沒有 CORS 標頭，瀏覽器直連一定被擋。以前前端只靠一支公開的轉發 Worker（第三方），
使用者回報「停班課頁的更新壞掉了」——第三方代理一掛、被限流或被擋，前端就什麼都拿不到。
GitHub Actions 在伺服器端，沒有 CORS 這件事，直接抓就好；放成同源的靜態檔，
前端即時抓不到時改讀這一份（畫面會標「後端」與抓取時間，不會假裝是即時的）。

**只抓、只切，不解析縣市**：解析只在前端一份（index.html 的 parseDgpaHtml），
兩邊各寫一套遲早會不一致。這裡只把需要的三塊（日期標題、更新時間、那張表）切出來。

輸出：data/dgpa.json
  {
    "v": 1,
    "fetched": "2026-10-23T08:05:00+08:00",   # 最後一次「成功」抓到的時間
    "tried":   "2026-10-23T08:05:00+08:00",   # 最後一次嘗試的時間
    "src": "https://www.dgpa.gov.tw/typh/daily/nds.html",
    "html": "<div class=\"Header_YMD\">…</div><div class=\"Content_Updata\">…</div><table id=\"Table\">…</table>",
    "error": ""                                  # 這一次失敗的原因；成功時是空字串
  }
抓不到時**保留上一次成功的 html 與 fetched**，只更新 tried 與 error——
不交出空檔案，也不把舊的寫成新的（fetched 不動，前端看得出它多舊）。

離線自我測試（不連網）：
  python3 scripts/fetch_dgpa.py selftest
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from http.client import HTTPException
from urllib.request import Request, urlopen

URL = os.environ.get("DGPA_URL", "https://www.dgpa.gov.tw/typh/daily/nds.html")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dgpa.json")
TW = timezone(timedelta(hours=8))
MAX_HTML = 300_000          # 正常那張表幾十 KB；超過就是抓到別的東西了，不要把它整份推進 repo
UA = "Mozilla/5.0 (compatible; data-fetch/1.0)"


def now_iso():
    return datetime.now(TW).isoformat(timespec="seconds")


def decode(raw, ctype=""):
    """照 Content-Type → <meta charset> → UTF-8 → Big5（cp950）的順序解碼。
    政府網站偶爾還是 Big5；硬用 UTF-8 解會整頁亂碼、前端一列縣市都找不到。"""
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    cands = [m.group(1)] if m else []
    head = raw[:2048].decode("ascii", "ignore")
    m2 = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
    if m2:
        cands.append(m2.group(1))
    cands += ["utf-8", "cp950"]
    for enc in cands:
        enc = "cp950" if enc.lower() in ("big5", "big5-hkscs", "x-big5") else enc
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _block(html, pat):
    m = re.search(pat, html, re.I | re.S)
    return m.group(0) if m else ""


def extract(html):
    """切出日期標題、更新時間、那張表。找不到表就回空字串（交給呼叫端記成錯誤）。"""
    table = _block(html, r"<table\b[^>]*\bid\s*=\s*[\"']?Table\b[^>]*>.*?</table>")
    if not table:
        return ""
    head = _block(html, r"<(\w+)\b[^>]*class\s*=\s*[\"']?[^\"'>]*\bHeader_YMD\b[^>]*>.*?</\1>")
    upd = _block(html, r"<(\w+)\b[^>]*class\s*=\s*[\"']?[^\"'>]*\bContent_Updata\b[^>]*>.*?</\1>")
    # 拿掉 script／style，其餘原樣保留（紅字藍字的 <font color> 是判斷停不停的依據）
    out = re.sub(r"<(script|style)\b.*?</\1>", "", head + upd + table, flags=re.I | re.S)
    return out


def fetch(url, tries=3, timeout=20):
    """抓一次，失敗退避 2、4 秒再試；4xx 不重試（重試也一樣）。"""
    err = ""
    for i in range(tries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
            with urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except HTTPError as e:
            err = f"HTTP {e.code}"
            if 400 <= e.code < 500:
                break
        except (URLError, TimeoutError, OSError, HTTPException) as e:   # 讀到一半斷線（IncompleteRead）不是 OSError
            err = str(getattr(e, "reason", e)) or e.__class__.__name__
        if i < tries - 1:
            time.sleep(2 ** (i + 1))
    raise RuntimeError(err or "抓取失敗")


def update(old, fetcher):
    """給定上一次的內容與抓取函式，回傳這一次要寫出的內容。抓不到就保留上一次成功的資料。"""
    out = dict(old or {})
    out.update({"v": 1, "src": URL, "tried": now_iso()})
    try:
        raw, ctype = fetcher(URL)
        snippet = extract(decode(raw, ctype))
        if not snippet:
            raise RuntimeError("頁面裡找不到停班停課那張表（官方改版了？）")
        if len(snippet) > MAX_HTML:
            raise RuntimeError(f"切出來的內容 {len(snippet)} 字，太大，不像那張表")
        out.update({"html": snippet, "fetched": out["tried"], "error": ""})
    except Exception as e:  # noqa: BLE001 —— 任何失敗都記下來、保留舊資料，不讓整條管線紅燈
        out["error"] = str(e)[:300]
        out.setdefault("html", "")
        out.setdefault("fetched", "")
    return out


def main():
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    new = update(old, fetch)
    # 只有內容真的變了才改檔：沒變的話 tried 一直跳會讓每一輪都產生一次提交
    same = {k: v for k, v in new.items() if k != "tried"} == {k: v for k, v in old.items() if k != "tried"}
    if same and old:
        print("dgpa: no change" + (f"（{new['error']}）" if new.get("error") else ""))
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, separators=(",", ":"))
    print(f"dgpa: {'ok' if not new.get('error') else 'error: ' + new['error']}，{len(new.get('html', ''))} 字")
    return 0


SAMPLE = """<html><head><meta charset="utf-8"><title>停班停課</title><script>var x=1</script></head><body>
<div class="Header_YMD">115年10月23日 天然災害停止上班及上課情形</div>
<div class="Content_Updata">更新時間：2026/10/23 08:20:16</div>
<table id="Table" class="Table_Body"><tr><th>縣市</th><th>情形</th></tr>
<tr><td headers='city_Name'><FONT>基隆市</FONT></td><td headers='StopWorkSchool_Info'>
<FONT color=#FF0000>今天停止上班、停止上課。</FONT></td></tr>
<tr><td colspan=2>備註</td></tr></table><div>footer</div></body></html>"""


def selftest():
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            print("  ✗", msg)
            sys.exit(1)
        ok += 1
        print("  ✓", msg)

    s = extract(SAMPLE)
    check("Header_YMD" in s and "Content_Updata" in s and 'id="Table"' in s, "切出日期、更新時間與那張表")
    check("<script" not in s and "footer" not in s, "不帶 script 與頁尾")
    check("#FF0000" in s, "保留紅字（停不停的依據）")
    check(extract("<html><body>維護中</body></html>") == "", "找不到表 → 空字串")
    big5 = SAMPLE.replace('charset="utf-8"', 'charset="big5"').encode("cp950")
    check("基隆市" in decode(big5, "text/html"), "Big5 頁面依 meta charset 正確解碼")
    check("基隆市" in decode(SAMPLE.encode("utf-8"), "text/html; charset=UTF-8"), "UTF-8 依標頭解碼")
    none_day = SAMPLE.replace("<FONT color=#FF0000>今天停止上班、停止上課。</FONT>", "無停班停課訊息。")
    check("無停班停課訊息" in extract(none_day), "沒有停班停課的日子照樣切得出那張表")

    good = update({}, lambda u: (SAMPLE.encode("utf-8"), "text/html; charset=utf-8"))
    check(good["error"] == "" and good["html"] and good["fetched"] == good["tried"], "成功：寫入 html 與抓取時間")

    def boom(u):
        raise RuntimeError("HTTP 503")
    bad = update(good, boom)
    check(bad["html"] == good["html"] and bad["fetched"] == good["fetched"], "失敗：保留上一次成功的資料")
    check(bad["error"] == "HTTP 503", "失敗：記下原因")
    first = update({}, boom)
    check(first["html"] == "" and first["error"], "第一次就失敗：html 空、有原因（前端會講後端也抓不到）")
    junk = update({}, lambda u: (b"<html>" + b"x" * 10 + b"</html>", "text/html"))
    check("找不到" in junk["error"], "抓到別的頁面：記成錯誤，不寫進 html")
    huge = update({}, lambda u: (('<table id="Table">' + "x" * (MAX_HTML + 10) + "</table>").encode(), "text/html"))
    check("太大" in huge["error"] and not huge["html"], "內容大得不像那張表：不寫進 repo")
    print(f"全部通過（{ok} 項）")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        sys.exit(main())
