#!/usr/bin/env python3
"""這一輪要不要抓（cwa.yml 第一步）。

使用者：沒有颱風、沒有大雨雷雨的時候不用那麼頻繁抓。
- 有事（有颱風、有任何豪大雨／雷雨／颱風特報、過去 1 小時有落雷）→ 每一輪都抓（排程與外部觸發約 10 分鐘一次）。
- 沒事 → 上一次抓到現在不到 CALM_MIN 分鐘就跳過。
- 讀不到、看不懂上一份資料 → 照抓。寧可多抓一次，也不要因為判斷壞掉而停在舊資料。

時間對齊：排程在每 10 分鐘的第 3 分鐘（3、13、23…），氣象署 10 分鐘一筆的觀測整點過後幾分鐘才出來，
晚一點抓才拿得到「剛發布」的那一筆，不會一直慢一輪。

「上一次抓的時間」記在 .gate/last（工作流用 actions/cache 帶到下一輪）：資料檔的 updated 不能用——
內容沒變時抓取腳本最多一小時才刷新它，拿它判斷會變成每輪都抓。

用法：python scripts/fetch_gate.py         → 在 $GITHUB_OUTPUT 寫 run=1/0、why=…
      python scripts/fetch_gate.py stamp   → 抓完記下現在時間
      python scripts/fetch_gate.py selftest
"""
import json
import os
import sys
from datetime import datetime, timezone

CALM_MIN = int(os.environ.get("CALM_MIN", "30"))
DATA = os.environ.get("DATA_DIR", "data")
STAMP = os.environ.get("GATE_STAMP", ".gate/last")


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def _ts(s):
    t = datetime.fromisoformat(str(s))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def decide(now, ty, ltn, wx, last, force=False):
    """回傳 (要不要抓, 原因)。ty／ltn／wx 是三個資料檔的內容，讀不到就給 None；last 是上一次抓的時間（沒有給 None）。"""
    if force:
        return True, "手動指定照抓"
    if ty is None or ltn is None or wx is None:
        return True, "上一份資料讀不到，照抓"
    try:
        if (ty.get("count") or 0) > 0:
            return True, "有颱風"
        recs = ((wx.get("adv") or {}).get("record")) or []
        if recs:
            return True, f"有 {len(recs)} 份特報"
        if (ltn.get("n") or 0) > 0:
            return True, "過去 1 小時有落雷"
    except Exception as e:                    # 格式變了也不能停：照抓，並把原因寫出來
        return True, f"看不懂上一份資料（{e}），照抓"
    if last is None:
        return True, "沒有上一次抓取的紀錄，照抓"
    age = (now - last).total_seconds() / 60
    if age >= CALM_MIN:
        return True, f"沒事，但上一次已經 {age:.0f} 分鐘前"
    return False, f"沒事，上一次 {age:.0f} 分鐘前（沒事時每 {CALM_MIN} 分鐘抓一次）"


def main():
    def get(name):
        try:
            return _load(name)
        except Exception:
            return None
    last = None
    try:
        with open(STAMP, encoding="utf-8") as f:
            last = _ts(f.read().strip())
    except Exception:
        last = None
    run, why = decide(datetime.now(timezone.utc), get("typhoon.json"), get("lightning.json"), get("wx.json"), last,
                      force=os.environ.get("FORCE") == "1")
    print(("抓：" if run else "跳過：") + why)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'1' if run else '0'}\nwhy={why}\n")
    return 0


def stamp():
    os.makedirs(os.path.dirname(STAMP) or ".", exist_ok=True)
    with open(STAMP, "w", encoding="utf-8") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    return 0


def selftest():
    now = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)
    ago = lambda m: datetime.fromtimestamp(now.timestamp() - m * 60, timezone.utc)
    ty, ltn, wx = {"count": 0}, {"n": 0}, {"adv": {"record": []}}
    ok = 0
    def check(cond, msg):
        nonlocal ok
        if not cond:
            print("✗", msg)
            sys.exit(1)
        ok += 1
        print("  ✓", msg)
    check(decide(now, ty, ltn, wx, ago(10))[0] is False, "沒事、10 分鐘前抓過 → 跳過")
    check(decide(now, ty, ltn, wx, ago(31))[0] is True, "沒事、31 分鐘前 → 抓")
    check(decide(now, ty, ltn, wx, None)[0] is True, "沒有上一次的紀錄（快取被清掉）→ 照抓")
    check(decide(now, {"count": 1}, ltn, wx, ago(5)) == (True, "有颱風"), "有颱風 → 每輪都抓")
    check(decide(now, ty, {"n": 3}, wx, ago(5))[0] is True, "有落雷 → 抓")
    check(decide(now, ty, ltn, {"adv": {"record": [{}]}}, ago(5))[0] is True, "有特報 → 抓")
    check(decide(now, ty, ltn, {"adv": None}, ago(5))[0] is False, "特報欄位是 null（後端沒抓到）不算有事")
    check(decide(now, None, ltn, wx, ago(5))[0] is True, "讀不到檔 → 照抓")
    check(decide(now, ty, ltn, "壞掉的檔", ago(5))[0] is True, "看不懂 → 照抓")
    check(decide(now, ty, ltn, wx, ago(5), force=True)[0] is True, "手動指定 → 抓")
    print(f"全部通過（{ok} 項）")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1:]
    sys.exit(selftest() if arg == ["selftest"] else stamp() if arg == ["stamp"] else main())
