#!/usr/bin/env python3
"""bootstrap_app.py — 把 skl 技能庫裡挑好的技能「複製」進另一個 App 的 repo。

為什麼用複製而不是外掛市集：雲端工作階段（claude.ai/code、iPhone）從 GitHub
外掛市集安裝外掛不一定會自動完成；而 repo 裡的 `.claude/skills/` 每次開工作階段
都一定會被讀到。所以複製進去最穩，代價是要更新時重跑一次 `update`。

用法（在「別的 App」的工作階段裡）：
  git clone --depth 1 https://github.com/xin7355-collab/skl /tmp/skl
  python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py list
  python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py install --target . --packs core,pwa
  python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py install --target . --skills engineering/skills/focused-fix
  python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py update --target .
  python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py selftest

結束碼：0 = 全部成功；1 = 有技能沒裝成功（會列出是哪幾個、為什麼）；2 = 參數錯誤。
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.abspath(os.path.join(SKILL_DIR, "..", "..", ".."))
PACKS_FILE = os.path.join(SKILL_DIR, "assets", "packs.json")
CLAUDE_TEMPLATE = os.path.join(SKILL_DIR, "assets", "CLAUDE.template.md")
ALWAYS = ["xin-toolkit/skills/app-guardrails-audit"]
MANIFEST = ".skl-vendor.json"
MAX_FILE_BYTES = 2 * 1024 * 1024  # 技能裡不該有大檔；有的話多半是誤放的資料，不複製。
AUDITOR_REL = "engineering/skills/skill-security-auditor/scripts/skill_security_auditor.py"
# 人工審查過的誤判清單：掃描 FAIL 但逐行看過是誤判的技能（例如正規表達式的 .exec()、驅動本機瀏覽器）。
# 用內容指紋綁定：技能有任何一個檔案改變，指紋就對不上，會重新被擋，必須再審一次。
REVIEWED_REL = "xin-toolkit/skills/app-bootstrap/assets/security-reviewed.json"
HOOK_REL = os.path.join(".claude", "hooks", "skl-session-start.sh")
HOOK_CMD = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/skl-session-start.sh'

# 每次開 Claude Code 工作階段時自動執行：離線快速健檢＋技能太久沒更新就提醒。
# 一律 exit 0：健檢失敗不能擋住使用者開工。
HOOK_SCRIPT = r"""#!/bin/bash
# 由 skl 的 app-bootstrap 安裝。開工作階段時做一次離線健檢，並提醒技能是否太久沒更新。
set -u
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
A=.claude/skills/app-guardrails-audit/scripts/guardrails_audit.py
if [ -f "$A" ]; then
  out=$(timeout 60 python3 "$A" audit . 2>/dev/null | grep '^掃描' | tail -1)
  [ -n "$out" ] && echo "[skl 健檢] $out 要看細節請說「跑一次防禦健檢」。"
fi
M=.claude/skills/.skl-vendor.json
if [ -f "$M" ]; then
  python3 - "$M" <<'PY' 2>/dev/null
import datetime, json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
t = datetime.datetime.strptime(m.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
days = (datetime.datetime.now(datetime.timezone.utc) - t).days
if days >= 14:
    print(f"[skl] 技能已 {days} 天沒更新，可以說「把技能更新到最新版」。")
PY
fi
exit 0
"""


# 技能清單的成本（官方 Skills／設定文件）：每個技能的名稱＋說明每一輪都放進對話；總長超過預算
# （上下文的 1%，保底 8,000 字元）時，最少用的技能說明會被拿掉，Claude 就不容易自己想到要用它。
# 每個技能的說明最多顯示 1,536 字元。裝太多、或兩個技能做同一件事，都會讓「該用哪個」變難判斷。
LISTING_BUDGET = 8000
LISTING_DESC_CAP = 1536
LISTING_MANY = 15
_STOP = set("use when the and for with this that from into your skill skills user users asks ask using used also "
            "any are can not you all its via etc such".split())
_NAME_STOP = set("app apps development driven senior skill skills pro max the and for with expert helper tool tools".split())


def skill_meta(skill_md):
    """讀 SKILL.md 開頭的 frontmatter（只取單層 key: value，支援多行說明）。讀不到回空 dict。"""
    try:
        with open(skill_md, encoding="utf-8", errors="ignore") as f:
            text = f.read(20000)
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    out, key = {}, None
    for line in text[3:end if end > 0 else len(text)].splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, _, v = line.partition(":")
            key, v = key.strip(), v.strip()
            out[key] = "" if v in (">", "|", ">-", "|-") else v.strip("\"'")
        elif key and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def _words(text):
    import re
    t = text.lower()
    w = {x for x in re.findall(r"[a-z][a-z0-9]{2,}", t) if x not in _STOP}
    for run in re.findall(r"[\u4e00-\u9fff]+", t):   # 中文用兩字一組比對
        w |= {run[i:i + 2] for i in range(len(run) - 1)}
    return w


def listing_report(skill_mds):
    """skill_mds：{技能資料夾名: SKILL.md 路徑}。回傳清單總長、最大的幾個、可能重疊的配對。"""
    items = []
    for folder, path in sorted(skill_mds.items()):
        m = skill_meta(path)
        if str(m.get("disable-model-invocation", "")).lower() == "true":
            continue  # 只能手動叫用的技能，說明不放進清單
        name = m.get("name") or folder
        text = (m.get("description", "") + " " + m.get("when_to_use", "")).strip()
        items.append({"name": name, "chars": len(name) + min(len(text), LISTING_DESC_CAP), "words": _words(text)})
    overlaps = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if not a["words"] or not b["words"]:
                continue
            j = len(a["words"] & b["words"]) / len(a["words"] | b["words"])
            na = {x for x in a["name"].lower().replace("_", "-").split("-") if len(x) > 2 and x not in _NAME_STOP}
            nb = {x for x in b["name"].lower().replace("_", "-").split("-") if len(x) > 2 and x not in _NAME_STOP}
            # 名稱有共同的字且說明相近，或說明本身就很像，才算「可能重疊」；只是提醒，不擋安裝
            if (na & nb and j >= 0.08) or j >= 0.25:
                overlaps.append((a["name"], b["name"], round(j, 2)))
    items.sort(key=lambda x: -x["chars"])
    return {"count": len(items), "chars": sum(x["chars"] for x in items),
            "top": [(x["name"], x["chars"]) for x in items[:3]], "overlaps": overlaps}


def listing_notes(rep):
    notes = []
    top = "、".join(f"{n} {c:,}" for n, c in rep["top"])
    if rep["chars"] > LISTING_BUDGET:
        notes.append(f"技能清單約 {rep['chars']:,} 字元（{rep['count']} 個），超過官方保底預算 {LISTING_BUDGET:,} 字元："
                     f"最少用的技能說明會被拿掉、比較不會被自動叫用，而且每一輪都多花用量。建議移除用不到的；最大的是 {top}。")
    elif rep["count"] > LISTING_MANY:
        notes.append(f"這個 App 裝了 {rep['count']} 個技能（清單約 {rep['chars']:,} 字元）：建議只留 5～15 個真的用得到的，"
                     "越多越難判斷該用哪個。")
    for a, b, _ in rep["overlaps"][:5]:
        notes.append(f"可能重疊：{a} 和 {b} 的說明很像，確認兩個都需要；只留一個比較不會用錯。")
    return notes


def load_packs(path=PACKS_FILE):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _source_commit(root):
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _ignore(src_dir, names):
    skip = []
    for n in names:
        p = os.path.join(src_dir, n)
        if n in {"__pycache__", ".DS_Store"} or n.endswith(".pyc"):
            skip.append(n)
        elif os.path.isfile(p) and os.path.getsize(p) > MAX_FILE_BYTES:
            skip.append(n)
    return skip


def _read_manifest(skills_root):
    p = os.path.join(skills_root, MANIFEST)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def security_scan(src, source_root=REPO_ROOT, auditor=None):
    """用技能庫內建的 skill-security-auditor 掃描一個技能。
    回傳 (verdict, 說明)；verdict 為 PASS／WARN／FAIL／ERROR。
    掃描工具壞掉時回 ERROR，由呼叫端「不安裝」——寧可少裝，也不在沒掃描的情況下裝。"""
    auditor = auditor or os.path.join(source_root, AUDITOR_REL)
    if not os.path.isfile(auditor):
        return "ERROR", "找不到安全掃描工具（skill-security-auditor）"
    try:
        out = subprocess.run([sys.executable, auditor, src, "--json"],
                             capture_output=True, text=True, timeout=120)
        rep = json.loads(out.stdout)
    except subprocess.TimeoutExpired:
        return "ERROR", "安全掃描逾時"
    except (OSError, json.JSONDecodeError, ValueError):
        return "ERROR", "安全掃描工具輸出無法解讀"
    verdict = rep.get("verdict", "ERROR")
    cats = sorted({f.get("category", "?") for f in rep.get("findings", [])})
    return verdict, ("、".join(cats) if cats else "")


# ---- skl 自己的第二道掃描（extra_scan）----
# 為什麼不只靠上游 skill-security-auditor：它看到抑制標記就整行跳過（外來技能寫一句註解就能藏住
# 危險程式），不可見字元只抓 5 種、而且只掃 .md。上游檔案照原樣保留方便同步，所以在這裡補一道。
# 標記字串用拼接寫，免得這支檔案本身被 grep 成「有用抑制標記」。
SUPPRESS_MARKERS = ("noqa: " + "SEC-AUDITOR", "auditor:" + "ignore-line")
# FAIL：看起來跟原文一樣、實際內容不同（Trojan Source 雙向控制字元、ASCII 走私用的 Tag 字元）。
# 字元清單參考 affaan-m/ECC scripts/ci/check-unicode-safety.js（MIT，Copyright (c) 2026 Affaan Mustafa）。
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
# WARN：零寬與填充字元。可能藏指令，但也會出現在正常文字裡（複製貼上帶進來），所以只提醒。
_INVISIBLE = {0x200B, 0x200C, 0x200D, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0x180E, 0x3164,
              0x115F, 0x1160, 0x00AD, 0xFEFF} | set(range(0xFE00, 0xFE10)) | set(range(0xE0100, 0xE01F0))
# 英格蘭／蘇格蘭等地區旗幟 emoji 本來就是「黑旗＋Tag 字元＋結束 Tag」組成，不能誤判成走私。
_FLAG_TAG_SEQ = re.compile("\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F")
# 寫死個人家目錄：只在那台電腦能跑，也洩漏使用者名稱。規則與白名單參考 K-Dense-AI/scientific-agent-skills
# tests/_contract/structure.py（MIT，Copyright (c) 2025 K-Dense Inc.）。
_PERSONAL_PATH = re.compile(r"(?:/(?:mnt/[a-z]/Users|home|Users)/|[A-Za-z]:\\{1,2}Users\\{1,2})(?![<$%{\[])([A-Za-z0-9._-]+)[/\\]")
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_IMPERSONAL = {"dnanexus", "ubuntu", "root", "runner", "ec2-user", "jovyan", "vscode", "airflow", "nextflow",
               "opt", "shared", "public", "default", "linuxbrew", "user", "username", "you", "me", "youruser",
               "your-user", "your_user", "yourname", "your-name", "your_name", "name", "admin", "node", "app",
               "foo", "bob", "alice", "john", "jane", "jdoe", "johndoe", "example", "sample", "demo", "test", "data",
               "x", "xxx", "someone", "dev", "developer"}  # 文件裡常見的範例帳號，不是誰的真電腦


def _emojiish(cp):
    """粗略判斷「emoji 或會配 FE0F 的符號」。Python 標準庫沒有 Extended_Pictographic 屬性，用區段近似。"""
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF
            or 0x2B00 <= cp <= 0x2BFF or 0x2190 <= cp <= 0x21FF or 0x25A0 <= cp <= 0x25FF
            or 0x2100 <= cp <= 0x214F or cp in {0xA9, 0xAE, 0x203C, 0x2049, 0x2934, 0x2935, 0x3030,
                                                0x303D, 0x3297, 0x3299, 0x20E3, 0xFE0F})


def _joiner_script(cp):
    # 阿拉伯、波斯、印度系文字正常就會用 ZWJ／ZWNJ 控制連字
    return 0x0600 <= cp <= 0x0DFF


def _hidden_chars(text):
    """回傳 [(等級, 類別, 行號, 碼位字串)]。只看真的字元，不看跳脫寫法（\\u200b 這種字面文字無害）。"""
    out = []
    skip = set()
    for m in _FLAG_TAG_SEQ.finditer(text):
        skip.update(range(m.start(), m.end()))
    line = 1
    n = len(text)
    for i, ch in enumerate(text):
        if ch == "\n":
            line += 1
            continue
        cp = ord(ch)
        if cp < 0xAD or i in skip:
            continue
        prev = ord(text[i - 1]) if i else 0
        nxt = ord(text[i + 1]) if i + 1 < n else 0
        if cp in _BIDI:
            out.append(("FAIL", "HIDDEN-BIDI", line, f"U+{cp:04X}"))
        elif 0xE0000 <= cp <= 0xE007F:
            out.append(("FAIL", "HIDDEN-TAG", line, f"U+{cp:04X}"))
        elif cp in _INVISIBLE:
            if cp == 0xFEFF and i == 0:
                continue  # 檔頭 BOM
            if cp == 0x200D and ((_emojiish(prev) and _emojiish(nxt)) or _joiner_script(prev) or _joiner_script(nxt)):
                continue  # emoji 組合（👨‍👩‍👧）或連字
            if cp == 0x200C and (_joiner_script(prev) or _joiner_script(nxt)):
                continue
            if cp in (0xFE0E, 0xFE0F) and (_emojiish(prev) or chr(prev) in "0123456789#*"):
                continue  # ❤️、1️⃣ 這類 emoji 呈現選擇
            if 0xE0100 <= cp <= 0xE01EF and (0x3400 <= prev <= 0x9FFF or 0x20000 <= prev <= 0x3FFFF):
                continue  # 漢字異體字選擇符
            out.append(("WARN", "INVISIBLE-CHAR", line, f"U+{cp:04X}"))
    return out


def _personal_paths(line):
    urls = [m.span() for m in _URL.finditer(line)]
    hits = []
    for m in _PERSONAL_PATH.finditer(line):
        if any(a <= m.start() < b for a, b in urls):
            continue  # 網址裡的 /home/xxx/ 是網站路徑，不是誰的電腦
        if m.group(1).lower() in _IMPERSONAL:
            continue
        hits.append(m.group(0))
    return hits


def extra_scan(src, own=False):
    """skl 自己的補充檢查。回傳 (verdict, 類別 set, 說明清單)；verdict 為 PASS／WARN／FAIL。
    own=True 表示是 skl 自製技能（xin-toolkit/），允許使用掃描抑制標記（掃描工具本身要寫到那些字串）。"""
    findings = []   # (等級, 類別, 說明)

    def add(sev, cat, msg):
        findings.append((sev, cat, msg))

    stdlib = {m for m in getattr(sys, "stdlib_module_names", ()) if not m.startswith("_")}
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in {"__pycache__", ".git", "node_modules"})
        for name in sorted(files):
            p = os.path.join(root, name)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            if os.path.islink(p):
                continue  # 符號連結由 install() 直接拒絕，這裡不跟進去讀技能外的檔案
            stem, ext = os.path.splitext(name)
            # 跟標準函式庫同名：腳本所在資料夾會排在 sys.path 最前面，import json 就載到這支，不是真的 json。
            # 參考 K-Dense-AI/scientific-agent-skills tests/_contract/structure.py（MIT，Copyright (c) 2025 K-Dense Inc.）
            if ext == ".py" and stem in stdlib:
                add("FAIL", "STDLIB-SHADOW", f"{rel}：檔名和 Python 標準模組 {stem} 一樣，會被誤當標準模組載入")
            try:
                if os.path.getsize(p) > MAX_FILE_BYTES:
                    continue  # 大檔不會被複製，不必掃
                with open(p, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if b"\0" in raw:
                continue  # 二進位檔
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not own:
                for no, ln in enumerate(text.split("\n"), 1):
                    if any(mk in ln for mk in SUPPRESS_MARKERS):
                        add("FAIL", "SCANNER-BYPASS",
                            f"{rel}:{no}：有掃描抑制標記，這個標記會讓安全掃描跳過該行，外來技能不接受")
            hidden = _hidden_chars(text)
            for sev, cat in sorted({(s, c) for s, c, _, _ in hidden}):
                rows = [h for h in hidden if h[1] == cat]
                where = "、".join(f"第 {ln} 行 {cp}" for _, _, ln, cp in rows[:3])
                more = f" 等 {len(rows)} 處" if len(rows) > 3 else ""
                what = ("雙向控制字元（畫面顯示的順序和實際內容不同）" if cat == "HIDDEN-BIDI" else
                        "Tag 字元（看不見、但模型讀得到，可用來藏指令）" if cat == "HIDDEN-TAG" else
                        "零寬／不可見字元")
                add(sev, cat, f"{rel}：{what}，{where}{more}")
            for no, ln in enumerate(text.split("\n"), 1):
                for hit in _personal_paths(ln):
                    add("WARN", "PERSONAL-PATH", f"{rel}:{no}：寫死個人家目錄 {hit}，換台電腦就找不到")
        for d in dirs:
            if d in stdlib and os.path.isfile(os.path.join(root, d, "__init__.py")):
                rel = os.path.relpath(os.path.join(root, d), src).replace(os.sep, "/")
                add("FAIL", "STDLIB-SHADOW", f"{rel}/：套件名稱和 Python 標準模組 {d} 一樣，會被誤當標準模組載入")
    verdict = "FAIL" if any(s == "FAIL" for s, _, _ in findings) else ("WARN" if findings else "PASS")
    # FAIL 的說明排前面，呼叫端只顯示前幾條
    findings.sort(key=lambda x: x[0] != "FAIL")
    return verdict, {c for _, c, _ in findings}, [m for _, _, m in findings]


def combine_scan(verdict, detail, extra):
    """把上游掃描與 extra_scan 合併成一個 verdict 與類別字串（類別字串要能給 reviewed_ok 比對）。"""
    ev, ecats, _ = extra
    if verdict == "ERROR":
        return verdict, detail  # 上游掃描壞掉一律不裝，不因為補充檢查通過就放行
    rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    v = verdict if rank.get(verdict, 2) >= rank[ev] else ev
    cats = {c for c in detail.split("、") if c} | ecats
    return v, "、".join(sorted(cats))


def symlinks_in(src):
    """技能資料夾裡的符號連結（含資料夾本身）。連結可能指向技能外（例如 ~/.ssh），複製時會把那些檔案帶進 App。"""
    if os.path.islink(src):
        return ["."]
    out = []
    for root, dirs, files in os.walk(src):
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p):
                out.append(os.path.relpath(p, src).replace(os.sep, "/"))
    return out


def tree_hash(src):
    """技能資料夾的內容指紋（路徑＋內容，略過與複製時相同的雜檔），用來確認「審查的就是這一版」。"""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in {"__pycache__"})
        for n in sorted(files):
            if n == ".DS_Store" or n.endswith(".pyc"):
                continue
            p = os.path.join(root, n)
            h.update(os.path.relpath(p, src).replace(os.sep, "/").encode() + b"\0")
            with open(p, "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def reviewed_ok(rel, src, detail, source_root=REPO_ROOT):
    """FAIL 的技能若在人工審查清單、指紋相同、且這次的問題類別都審查過，回傳審查說明；否則 None。"""
    try:
        with open(os.path.join(source_root, REVIEWED_REL), encoding="utf-8") as f:
            entry = json.load(f).get("skills", {}).get(rel.strip("/"))
    except (OSError, json.JSONDecodeError):
        return None
    if not entry or entry.get("sha256") != tree_hash(src):
        return None
    cats = {c for c in detail.split("、") if c}
    if not cats <= set(entry.get("categories", [])):
        return None
    return entry.get("note", "已人工審查")


def install_hook(target, res):
    """安裝 SessionStart hook；合併既有 .claude/settings.json，不覆蓋使用者原本的設定。"""
    settings_path = os.path.join(target, ".claude", "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, json.JSONDecodeError):
            res["notes"].append("既有 .claude/settings.json 不是合法 JSON，沒有加上開工自動健檢；修好後重跑 install。")
            return
    hook_path = os.path.join(target, HOOK_REL)
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(HOOK_SCRIPT)
    os.chmod(hook_path, 0o755)
    groups = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    already = any(h.get("command") == HOOK_CMD for g in groups for h in g.get("hooks", []))
    if not already:
        groups.append({"hooks": [{"type": "command", "command": HOOK_CMD}]})
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write("\n")
        res["notes"].append("已加上開工自動健檢（.claude/hooks/skl-session-start.sh）：每次開對話會先回報問題數量。")


def install(target, pack_names, force=False, dry_run=False, write_claude_md=True,
            source_root=REPO_ROOT, packs=None, extra_skills=(), scan=True, auditor=None,
            with_hook=True):
    """回傳 (結果 dict, 結束碼)。失敗不中斷，逐一記錄，最後一起報。"""
    packs = packs if packs is not None else load_packs()
    unknown = [p for p in pack_names if p not in packs]
    if unknown:
        return {"error": f"不認識的組合：{', '.join(unknown)}。可用：{', '.join(packs)}"}, 2
    if not os.path.isdir(target):
        return {"error": f"找不到目標資料夾：{target}"}, 2

    # 單選技能（網頁勾選產生的）：只接受 repo 內的相對路徑，擋掉 .. 與絕對路徑，
    # 免得貼錯的指令把 repo 外的資料夾複製進目標。
    bad = [s for s in extra_skills
           if os.path.isabs(s) or ".." in s.replace("\\", "/").split("/")]
    if bad:
        return {"error": f"技能路徑不合法：{', '.join(bad)}。請用網頁複製的路徑，例如 engineering/skills/focused-fix"}, 2

    wanted = list(ALWAYS)
    for s in extra_skills:
        if s.strip("/") not in wanted:
            wanted.append(s.strip("/"))
    for p in pack_names:
        for s in packs[p]["skills"]:
            if s not in wanted:
                wanted.append(s)

    skills_root = os.path.join(target, ".claude", "skills")
    manifest = _read_manifest(skills_root) or {"skills": []}
    owned = set(manifest.get("skills", []))
    # 裝進去當下的內容指紋。舊版 manifest 沒有這欄：照舊覆蓋，裝完補記。
    hashes = dict(manifest.get("hashes", {}))
    res = {"installed": [], "updated": [], "skipped": [], "failed": [], "notes": [], "warnings": []}

    for rel in wanted:
        src = os.path.join(source_root, rel)
        name = os.path.basename(rel.rstrip("/"))
        dst = os.path.join(skills_root, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            res["failed"].append((name, f"來源不存在或缺 SKILL.md：{rel}"))
            continue
        exists = os.path.exists(dst)
        # 同名但不是我們裝的：那是這個 App 自己的技能，除非明確 --force 否則不碰。
        if exists and name not in owned and not force:
            res["skipped"].append((name, "目標已有同名技能且不是本工具安裝的；要覆蓋請加 --force"))
            continue
        # App 裡有人手改過（例如針對這個 App 調了 SKILL.md）：整個換新會把那些修改無聲蓋掉。
        if exists and not force and name in hashes:
            try:
                changed = tree_hash(dst) != hashes[name]
            except OSError:
                changed = True  # 讀不到就當成改過，寧可不覆蓋
            if changed:
                res["warnings"].append((name, "這個技能在 App 裡被改過，沒有覆蓋；要覆蓋加 --force"))
                continue
        # 符號連結可能指向技能外（例如 ~/.ssh、整個家目錄），複製時會把那些檔案帶進 App 的 repo。
        # 不受 --no-security-scan 影響：這不是「掃描誤判」能審查放行的問題。
        links = symlinks_in(src)
        if links:
            res["failed"].append((name, f"技能裡有符號連結（{'、'.join(links[:3])}），可能把技能外的檔案帶進來，沒有安裝"))
            continue
        if scan:
            verdict, detail = security_scan(src, source_root, auditor)
            extra = extra_scan(src, own=rel.strip("/").startswith("xin-toolkit/"))
            verdict, detail = combine_scan(verdict, detail, extra)
            where = "；".join(extra[2][:3]) + (f"；…共 {len(extra[2])} 條" if len(extra[2]) > 3 else "")
            note = reviewed_ok(rel, src, detail, source_root) if verdict == "FAIL" else None
            if note:
                verdict, detail, where = "WARN", f"掃描 FAIL 為誤判，已人工審查：{note}", ""
            if verdict in ("FAIL", "ERROR"):
                res["failed"].append((name, f"安全掃描未通過（{verdict}：{detail}）" + (f"：{where}" if where else "")
                                      + "，沒有安裝。確認安全後可加 --no-security-scan 強制安裝"))
                continue
            if verdict == "WARN":
                res["warnings"].append((name, f"安全掃描提醒（{detail}）" + (f"：{where}" if where else "")
                                        + "。有連網或讀寫檔案的技能常見，已安裝，建議看一下用途是否合理"))
        if dry_run:
            (res["updated"] if exists else res["installed"]).append(name)
            continue
        try:
            os.makedirs(skills_root, exist_ok=True)
            # 先複製到暫存資料夾再換上去：複製到一半失敗時，舊版技能還在，不會留下半套。
            tmp = dst + ".skl-tmp"
            if os.path.exists(tmp):
                shutil.rmtree(tmp)
            # symlinks=True：就算檢查之後才冒出連結，也只複製連結本身、不跟進去讀技能外的檔案；
            # 複製完再查一次，有就整個丟掉。
            shutil.copytree(src, tmp, ignore=_ignore, symlinks=True)
            if symlinks_in(tmp):
                shutil.rmtree(tmp)
                res["failed"].append((name, "複製時出現符號連結，沒有安裝"))
                continue
            if exists:
                shutil.rmtree(dst)
            os.replace(tmp, dst)
            (res["updated"] if exists else res["installed"]).append(name)
            owned.add(name)
            hashes[name] = tree_hash(dst)
        except OSError as e:
            res["failed"].append((name, f"複製失敗：{e}"))

    if write_claude_md:
        cm = os.path.join(target, "CLAUDE.md")
        if os.path.exists(cm):
            res["notes"].append("已有 CLAUDE.md，沒有覆蓋。要套用工作守則，請把範本內容合併進去："
                                "xin-toolkit/skills/app-bootstrap/assets/CLAUDE.template.md")
        elif not dry_run:
            shutil.copyfile(CLAUDE_TEMPLATE, cm)
            res["notes"].append("已建立 CLAUDE.md（工作守則範本）。記得填第 7 節「本專案資訊」；"
                                "不是 PWA + GitHub Actions 架構就刪掉第 6 節。")

    if with_hook and not dry_run and (res["installed"] or res["updated"]):
        install_hook(target, res)

    # 裝完（或試跑時預估裝完）整個 App 的技能清單有多大、有沒有重疊
    mds = {}
    if os.path.isdir(skills_root):
        for n in os.listdir(skills_root):
            if os.path.isfile(os.path.join(skills_root, n, "SKILL.md")):
                mds[n] = os.path.join(skills_root, n, "SKILL.md")
    if dry_run:
        for rel in wanted:
            n = os.path.basename(rel.rstrip("/"))
            if n in res["installed"] or n in res["updated"]:
                mds[n] = os.path.join(source_root, rel, "SKILL.md")
    res["listing"] = listing_report(mds)
    res["notes"] += listing_notes(res["listing"])

    if not dry_run and (res["installed"] or res["updated"]):
        manifest.update({
            "source_repo": "https://github.com/xin7355-collab/skl",
            "source_commit": _source_commit(source_root),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "packs": sorted(set(manifest.get("packs", [])) | set(pack_names)),
            "extra_skills": sorted(set(manifest.get("extra_skills", [])) | {s.strip("/") for s in extra_skills}),
            "skills": sorted(owned),
            "hashes": {k: v for k, v in sorted(hashes.items()) if k in owned},
        })
        with open(os.path.join(skills_root, MANIFEST), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return res, (1 if res["failed"] else 0)


def update(target, dry_run=False, source_root=REPO_ROOT, packs=None, scan=True, auditor=None,
           with_hook=True, force=False):
    m = _read_manifest(os.path.join(target, ".claude", "skills"))
    if not m:
        return {"error": "這個 repo 還沒用本工具裝過技能（找不到 .claude/skills/.skl-vendor.json），請先 install。"}, 2
    # force 只在使用者明確加 --force 時才覆蓋 App 裡手改過的技能
    return install(target, m.get("packs", []), force=force, dry_run=dry_run,
                   write_claude_md=False, source_root=source_root, packs=packs,
                   extra_skills=m.get("extra_skills", []), scan=scan, auditor=auditor,
                   with_hook=with_hook)


def catalog_lines(source_root=REPO_ROOT):
    """全部技能的精簡清單（依類別分組），給目標 App 的 Claude 自己挑選用。
    直接讀 skl 的網頁目錄產生器，確保和網頁看到的中文說明、分類一致。"""
    sys.path.insert(0, os.path.join(source_root, "web"))
    import build_catalog  # noqa: E402
    cat, _, _ = build_catalog.build(source_root,
                                    os.path.join(source_root, "web", "zh.json"),
                                    os.path.join(source_root, "xin-toolkit", "skills", "app-bootstrap", "assets", "packs.json"),
                                    os.path.join(source_root, "web", "categories.json"))
    names = {c["id"]: c["zh"] for c in cat["categories"]}
    out = [f"# skl 技能目錄：{len(cat['skills'])} 個。格式：路徑｜中文說明。安裝用 install --skills 路徑1,路徑2"]
    for cid, zh in names.items():
        items = [s for s in cat["skills"] if s["cat"] == cid]
        out.append(f"\n## {zh}（{len(items)}）")
        out += [f"{s['path']}｜{s['zh'] or s['desc'][:80]}" for s in items]
    return out


def _print(res, dry_run):
    if "error" in res:
        print(res["error"], file=sys.stderr)
        return
    verb = "（試跑，未實際寫入）" if dry_run else ""
    print(f"新裝 {len(res['installed'])}、更新 {len(res['updated'])}、略過 {len(res['skipped'])}、失敗 {len(res['failed'])}{verb}")
    for n in res["installed"]:
        print(f"  + {n}")
    for n in res["updated"]:
        print(f"  ↻ {n}")
    for n, why in res["skipped"]:
        print(f"  – {n}：{why}")
    for n, why in res["failed"]:
        print(f"  ✗ {n}：{why}")
    for n, why in res.get("warnings", []):
        print(f"  ⚠ {n}：{why}")
    lst = res.get("listing")
    if lst:
        print(f"技能清單：{lst['count']} 個、每一輪約 {lst['chars']:,} 字元（官方保底預算 {LISTING_BUDGET:,}）")
    for note in res["notes"]:
        print(f"※ {note}")
    if res["installed"] or res["updated"]:
        print("下一步：git add .claude CLAUDE.md && git commit，之後在這個 repo 開的工作階段就會自動載入這些技能。")
        # 使用者要的是「裝完就接著用」，不是只把檔案放進來；細節在 SKILL.md 第 7 步
        print("接著：先列執行方案（每個新技能要做什麼、改哪些檔、套件寫進哪個設定檔、怎麼驗證、有沒有費用），"
              "再直接把技能用在這個 App（見 app-bootstrap SKILL.md 第 7 步）。")


def selftest():
    ok = True

    def check(cond, name):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    packs = load_packs()
    check(set(packs) >= {"core", "pwa", "actions", "backend", "security"}, "packs.json 可讀且有五組")
    all_paths = sorted({s for p in packs.values() for s in p["skills"]} | set(ALWAYS))
    missing = [s for s in all_paths if not os.path.isfile(os.path.join(REPO_ROOT, s, "SKILL.md"))]
    check(not missing, "packs.json 裡每個技能路徑都存在" + (f"（缺：{missing}）" if missing else ""))

    with tempfile.TemporaryDirectory() as d:
        # 自己造一個小來源庫，測試不依賴真實技能內容。
        src = os.path.join(d, "src")
        for rel in ["xin-toolkit/skills/app-guardrails-audit", "a/skills/alpha", "a/skills/beta"]:
            os.makedirs(os.path.join(src, rel, "scripts", "__pycache__"))
            with open(os.path.join(src, rel, "SKILL.md"), "w") as f:
                f.write("---\nname: x\ndescription: y\n---\n")
            open(os.path.join(src, rel, "scripts", "__pycache__", "junk.pyc"), "w").close()
        with open(os.path.join(src, "a/skills/beta/big.bin"), "wb") as f:
            f.write(b"0" * (MAX_FILE_BYTES + 1))
        # hook 測試要真的跑健檢，所以放入真正的掃描腳本。
        shutil.copyfile(os.path.join(REPO_ROOT, "xin-toolkit/skills/app-guardrails-audit/scripts/guardrails_audit.py"),
                        os.path.join(src, "xin-toolkit/skills/app-guardrails-audit/scripts/guardrails_audit.py"))
        # 假的安全掃描工具：名稱含 evil 判 FAIL、含 beta 判 WARN，其餘 PASS。
        fa = os.path.join(src, AUDITOR_REL)
        os.makedirs(os.path.dirname(fa), exist_ok=True)
        with open(fa, "w") as f:
            f.write("import json,sys,os\nn=os.path.basename(sys.argv[1].rstrip('/'))\n"
                    "v='FAIL' if 'evil' in n else ('WARN' if 'beta' in n else 'PASS')\n"
                    "print(json.dumps({'verdict':v,'findings':[{'category':'NET-EXFIL'}] if v!='PASS' else []}))\n")
        for rel in ["a/skills/evil"]:
            os.makedirs(os.path.join(src, rel))
            with open(os.path.join(src, rel, "SKILL.md"), "w") as f:
                f.write("---\nname: evil\ndescription: y\n---\n")
        fake = {"one": {"desc": "", "skills": ["a/skills/alpha", "a/skills/beta"]},
                "bad": {"desc": "", "skills": ["a/skills/ghost"]}}
        tgt = os.path.join(d, "app")
        os.makedirs(os.path.join(tgt, ".claude", "skills", "beta"))  # App 自己的同名技能

        r, code = install(tgt, ["nope"], source_root=src, packs=fake)
        check(code == 2 and "不認識" in r["error"], "未知組合回傳參數錯誤")

        r, code = install(tgt, ["one"], dry_run=True, source_root=src, packs=fake)
        check(code == 0 and not os.path.exists(os.path.join(tgt, ".claude/skills/alpha")), "試跑不寫入")

        r, code = install(tgt, ["one"], source_root=src, packs=fake)
        sk = os.path.join(tgt, ".claude", "skills")
        check(code == 0 and os.path.isfile(os.path.join(sk, "alpha", "SKILL.md")), "技能被複製進 .claude/skills")
        check(os.path.isfile(os.path.join(sk, "app-guardrails-audit", "SKILL.md")), "防禦掃描技能一律安裝")
        check(any(n == "beta" for n, _ in r["skipped"]), "App 自己的同名技能不被覆蓋")
        check(not os.path.exists(os.path.join(sk, "alpha", "scripts", "__pycache__")), "__pycache__ 不複製")
        check(os.path.isfile(os.path.join(tgt, "CLAUDE.md")), "沒有 CLAUDE.md 時建立範本")
        m = _read_manifest(sk)
        check(m and "alpha" in m["skills"] and "beta" not in m["skills"], "manifest 只記錄本工具裝的技能")

        with open(os.path.join(tgt, "CLAUDE.md"), "w") as f:
            f.write("我的規則")
        r, code = update(tgt, source_root=src, packs=fake)
        check(code == 0 and "alpha" in r["updated"], "update 會重新複製已裝技能")
        with open(os.path.join(tgt, "CLAUDE.md")) as f:
            check(f.read() == "我的規則", "update 不動既有 CLAUDE.md")

        r, code = install(tgt, ["one"], force=True, source_root=src, packs=fake)
        check("beta" in r["updated"] and not os.path.exists(os.path.join(sk, "beta", "big.bin")),
              "--force 覆蓋同名技能，且大檔不複製")

        r, code = install(tgt, ["bad"], source_root=src, packs=fake)
        check(code == 1 and r["failed"] and r["failed"][0][0] == "ghost", "來源缺失時結束碼 1 並列出是哪個")

        r, code = install(tgt, [], source_root=src, packs=fake, extra_skills=["../evil"])
        check(code == 2 and "不合法" in r["error"], "--skills 擋掉 .. 路徑")
        tgt2 = os.path.join(d, "app2")
        os.makedirs(tgt2)
        r, code = install(tgt2, [], source_root=src, packs=fake, extra_skills=["a/skills/alpha/"])
        check(code == 0 and r["installed"] == ["app-guardrails-audit", "alpha"], "--skills 單選技能可安裝")
        r, code = update(tgt2, source_root=src, packs=fake)
        check(code == 0 and "alpha" in r["updated"], "update 會帶上單選技能")

        r, code = update(os.path.join(d, "src"), source_root=src, packs=fake)
        check(code == 2, "沒裝過就 update 會提示先 install")


        # 安全掃描
        tgt3 = os.path.join(d, "app3")
        os.makedirs(os.path.join(tgt3, ".claude"))
        with open(os.path.join(tgt3, ".claude", "settings.json"), "w") as f:
            json.dump({"permissions": {"allow": ["Bash(ls)"]}}, f)
        r, code = install(tgt3, ["one"], source_root=src, packs=fake, extra_skills=["a/skills/evil"])
        check(code == 1 and any(n == "evil" and "FAIL" in w for n, w in r["failed"]), "安全掃描 FAIL 的技能不安裝")
        check(not os.path.exists(os.path.join(tgt3, ".claude/skills/evil")), "FAIL 的技能確實沒有被複製")
        # 人工審查清單：指紋相同且類別都審過 → 改列提醒照裝；內容一改或多出新類別 → 照樣擋
        rv = os.path.join(src, REVIEWED_REL)
        os.makedirs(os.path.dirname(rv), exist_ok=True)
        evil_src = os.path.join(src, "a/skills/evil")
        def write_rv(cats, sha):
            with open(rv, "w") as f:
                json.dump({"skills": {"a/skills/evil": {"sha256": sha, "categories": cats, "note": "測試"}}}, f)
        for n in ("rv4", "rv5", "rv6"):
            os.makedirs(os.path.join(d, n))
        write_rv(["NET-EXFIL"], tree_hash(evil_src))
        rr, _ = install(os.path.join(d, "rv4"), [], source_root=src, packs=fake, extra_skills=["a/skills/evil"])
        check("evil" in rr["installed"] and any(n == "evil" and "人工審查" in w for n, w in rr["warnings"]),
              "審查過且內容沒變的 FAIL 技能改列提醒照裝")
        write_rv(["CMD-INJECT"], tree_hash(evil_src))
        rr, _ = install(os.path.join(d, "rv5"), [], source_root=src, packs=fake, extra_skills=["a/skills/evil"])
        check(any(n == "evil" for n, _ in rr["failed"]), "出現沒審查過的問題類別照樣擋")
        write_rv(["NET-EXFIL"], tree_hash(evil_src))
        with open(os.path.join(evil_src, "SKILL.md"), "a") as f:
            f.write("changed\n")
        rr, _ = install(os.path.join(d, "rv6"), [], source_root=src, packs=fake, extra_skills=["a/skills/evil"])
        check(any(n == "evil" for n, _ in rr["failed"]), "審查後內容被改過（指紋不同）照樣擋")
        os.remove(rv)
        check(any(n == "beta" for n, _ in r["warnings"]) and "beta" in r["installed"], "WARN 的技能照裝但列出提醒")
        r2, _ = install(tgt3, [], source_root=src, packs=fake, extra_skills=["a/skills/evil"], scan=False)
        check("evil" in r2["installed"], "--no-security-scan 可強制安裝")
        r3, code3 = install(os.path.join(d, "app2"), ["one"], source_root=src, packs=fake,
                            auditor=os.path.join(d, "nope.py"))
        check(code3 == 1 and all("ERROR" in w for _, w in r3["failed"]), "找不到掃描工具時一律不安裝（fail closed）")

        # 開工自動健檢 hook
        with open(os.path.join(tgt3, ".claude", "settings.json")) as f:
            st = json.load(f)
        cmds = [h["command"] for g in st["hooks"]["SessionStart"] for h in g["hooks"]]
        check(st["permissions"]["allow"] == ["Bash(ls)"] and cmds == [HOOK_CMD], "hook 合併進既有 settings.json，原設定保留")
        check(os.access(os.path.join(tgt3, HOOK_REL), os.X_OK), "hook 腳本可執行")
        install(tgt3, ["one"], source_root=src, packs=fake)
        with open(os.path.join(tgt3, ".claude", "settings.json")) as f:
            st = json.load(f)
        check(len(st["hooks"]["SessionStart"]) == 1, "重複安裝不會重複加 hook")
        tgt4 = os.path.join(d, "app4")
        os.makedirs(os.path.join(tgt4, ".claude"))
        with open(os.path.join(tgt4, ".claude", "settings.json"), "w") as f:
            f.write("{壞掉")
        r, _ = install(tgt4, ["one"], source_root=src, packs=fake)
        with open(os.path.join(tgt4, ".claude", "settings.json")) as f:
            check(f.read() == "{壞掉" and any("不是合法 JSON" in n for n in r["notes"]),
                  "settings.json 壞掉時不覆蓋並提示")
        hp = os.path.join(tgt3, HOOK_REL)
        out = subprocess.run(["bash", hp], capture_output=True, text=True, timeout=120,
                             env={**os.environ, "CLAUDE_PROJECT_DIR": tgt3})
        check(out.returncode == 0 and "[skl 健檢]" in out.stdout, "hook 實際執行會輸出健檢摘要且不擋開工")

        # ---- skl 補充掃描（extra_scan）：上游掃描會被一句註解騙過、看不見的字元只抓一部分 ----
        def mkskill(rel, files, binary=None):
            base = os.path.join(src, rel)
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: t\ndescription: y\n---\n")
            for fr, txt in files.items():
                p = os.path.join(base, fr)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(txt)
            for fr, data in (binary or {}).items():
                with open(os.path.join(base, fr), "wb") as f:
                    f.write(data)
            return base

        def scan_one(rel, files, own=False, **kw):
            return extra_scan(mkskill(rel, files, **kw), own=own)

        mk0, mk1 = SUPPRESS_MARKERS
        v, cats, notes = scan_one("a/skills/sneaky", {"scripts/run.py": f"x = 1  # {mk0}\n"})
        check(v == "FAIL" and cats == {"SCANNER-BYPASS"} and "跳過該行" in notes[0] and "run.py:1" in notes[0],
              "外來技能用 noqa 抑制標記 → FAIL，並指出檔案與行號")
        v, cats, _ = scan_one("a/skills/sneaky-md", {"references/a.json": f'{{"k": "v"}} <!-- {mk1} -->\n'})
        check(v == "FAIL" and cats == {"SCANNER-BYPASS"}, "抑制標記在 .json 等非程式檔也抓得到")
        v, _, _ = scan_one("xin-toolkit/skills/own", {"scripts/scan.py": f"P = 'x'  # {mk0}\n"}, own=True)
        check(v == "PASS", "skl 自製技能（xin-toolkit）可以用抑制標記")
        v, cats, _ = scan_one("a/skills/bidi", {"scripts/a.py": "ok = 1  # \u202e evil\n"})
        check(v == "FAIL" and cats == {"HIDDEN-BIDI"}, "雙向控制字元（.py 裡也算）→ FAIL")
        v, cats, _ = scan_one("a/skills/tag", {"a.yml": "k: v\U000E0049\U000E0047\n"})
        check(v == "FAIL" and cats == {"HIDDEN-TAG"}, "Tag 字元 → FAIL")
        ok_text = ("\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F 英格蘭旗\n"
                   "\U0001F468\u200d\U0001F469\u200d\U0001F467 家庭\n\u2764\ufe0f 愛心\n1\ufe0f\u20e3 鍵帽\n"
                   "\U0001F44D\U0001F3FD 膚色\nक्\u200dष 天城文連字\nمی\u200cخواهم 波斯文\n")
        v, cats, notes = scan_one("a/skills/emoji", {"README.md": "\ufeff" + ok_text})
        check(v == "PASS", "旗幟 Tag 序列、emoji 的 ZWJ／FE0F、印度與波斯文的連字、檔頭 BOM 都不誤判"
              + (f"（{notes}）" if notes else ""))
        for i, (label, txt) in enumerate([("零寬空格", "a\u200bb"), ("檔案中間的 BOM", "a\ufeffb"), ("軟連字號", "a\u00adb"),
                           ("夾在英文字中間的 ZWJ", "a\u200db"), ("沒跟在 emoji 後面的 FE0F", "a\ufe0fb"),
                           ("Hangul 填充字元", "a\u3164b")]):
            v, cats, _ = scan_one(f"a/skills/zw{i}", {"x.sh": f"echo {txt}\n"})
            check(v == "WARN" and cats == {"INVISIBLE-CHAR"}, f"{label} → WARN")
        v, _, _ = scan_one("a/skills/bin", {}, binary={"blob.bin": b"\0\x01" + "\u202e".encode()})
        check(v == "PASS", "二進位檔不當文字掃")
        v, _, _ = scan_one("a/skills/escape", {"x.py": "ZW = '\\u200b\\u202e'\n"})
        check(v == "PASS", "跳脫寫法（\\u200b 字面）不是真的字元，不報")
        v, cats, notes = scan_one("a/skills/shadow", {"scripts/json.py": "x = 1\n", "scripts/email/__init__.py": ""})
        check(v == "FAIL" and cats == {"STDLIB-SHADOW"} and len(notes) == 2, "scripts/json.py 與 email/ 套件跟標準模組同名 → FAIL")
        v, _, _ = scan_one("a/skills/noshadow", {"scripts/json_utils.py": "x = 1\n", "scripts/__init__.py": ""})
        check(v == "PASS", "json_utils.py、__init__.py 不算同名")
        # 路徑用拼接產生，免得這支檔案本身被補充掃描判成「寫死家目錄」
        v, cats, notes = scan_one("a/skills/home", {"a.py": "P = '/Users/" + "xinchen/data/a.csv'\n",
                                                    "b.md": "見 C:\\Users\\" + "kevin\\Desktop\\x.txt\n"})
        check(v == "WARN" and cats == {"PERSONAL-PATH"} and len(notes) == 2, "寫死 /Users/名字/ 與 C:\\Users\\名字\\ → WARN")
        v, _, notes = scan_one("a/skills/home-ok", {"a.md": "/home/user/x /home/$USER/x /Users/<you>/x C:\\Users\\%USERNAME%\\x "
                                                            "https://example.com/home/jason/page /home/runner/work/\n"})
        check(v == "PASS", "佔位符、CI 帳號、網址裡的 /home/ 不報" + (f"（{notes}）" if notes else ""))

        # 併入安裝流程：FAIL 不裝、可被人工審查放行（指紋綁定）、WARN 照裝
        mkskill("a/skills/marked", {"scripts/run.py": f"import os  # {mk0}\n"})
        os.makedirs(os.path.join(d, "ex1"))
        r, code = install(os.path.join(d, "ex1"), [], source_root=src, packs=fake, extra_skills=["a/skills/marked"])
        check(code == 1 and any(n == "marked" and "SCANNER-BYPASS" in w and "run.py:1" in w for n, w in r["failed"]),
              "安裝時補充掃描 FAIL 的技能不裝，原因寫出檔案行號")
        with open(rv, "w") as f:
            json.dump({"skills": {"a/skills/marked": {"sha256": tree_hash(os.path.join(src, "a/skills/marked")),
                                                      "categories": ["SCANNER-BYPASS"], "note": "測試"}}}, f)
        os.makedirs(os.path.join(d, "ex2"))
        r, code = install(os.path.join(d, "ex2"), [], source_root=src, packs=fake, extra_skills=["a/skills/marked"])
        check(code == 0 and "marked" in r["installed"], "補充掃描的 FAIL 一樣可用人工審查清單放行")
        os.remove(rv)
        os.makedirs(os.path.join(d, "ex3"))
        r, code = install(os.path.join(d, "ex3"), [], source_root=src, packs=fake, extra_skills=["a/skills/home"])
        check(code == 0 and "home" in r["installed"] and any(n == "home" and "PERSONAL-PATH" in w for n, w in r["warnings"]),
              "補充掃描 WARN 照裝並列出")

        # 符號連結：可能指向技能外（例如 ~/.ssh），一律拒絕，連 --no-security-scan 也一樣
        outside = os.path.join(d, "secret.txt")
        with open(outside, "w") as f:
            f.write("TOKEN")
        lk = mkskill("a/skills/linky", {})
        os.symlink(outside, os.path.join(lk, "notes.txt"))
        for n, sc in (("ln1", True), ("ln2", False)):
            os.makedirs(os.path.join(d, n))
            r, code = install(os.path.join(d, n), [], source_root=src, packs=fake, extra_skills=["a/skills/linky"], scan=sc)
            check(code == 1 and any(x == "linky" and "符號連結" in w for x, w in r["failed"])
                  and not os.path.exists(os.path.join(d, n, ".claude/skills/linky")),
                  "技能含符號連結 → 不安裝" + ("（--no-security-scan 也一樣）" if not sc else ""))

        # 更新時不覆蓋 App 裡手改過的技能
        t5 = os.path.join(d, "app5")
        os.makedirs(t5)
        install(t5, ["one"], source_root=src, packs=fake, with_hook=False)
        sk5 = os.path.join(t5, ".claude", "skills")
        m = _read_manifest(sk5)
        check(m.get("hashes", {}).get("alpha") == tree_hash(os.path.join(sk5, "alpha")), "安裝時把每個技能的指紋記進 manifest")
        a_md = os.path.join(sk5, "alpha", "SKILL.md")
        with open(a_md, "a") as f:
            f.write("App 自己加的規則\n")
        r, code = update(t5, source_root=src, packs=fake, with_hook=False)
        with open(a_md) as f:
            kept = "App 自己加的規則" in f.read()
        check(code == 0 and "alpha" not in r["updated"] and kept
              and any(n == "alpha" and "被改過" in w and "--force" in w for n, w in r["warnings"]),
              "App 裡改過的技能 update 時跳過並提醒加 --force")
        r, code = update(t5, source_root=src, packs=fake, with_hook=False, dry_run=True)
        check("alpha" not in r["updated"], "試跑也會預告不覆蓋")
        r, code = update(t5, source_root=src, packs=fake, with_hook=False, force=True)
        with open(a_md) as f:
            check("alpha" in r["updated"] and "App 自己加的規則" not in f.read(), "update --force 才覆蓋改過的技能")
        with open(a_md, "a") as f:
            f.write("又改了\n")
        m = _read_manifest(sk5)
        del m["hashes"]
        with open(os.path.join(sk5, MANIFEST), "w") as f:
            json.dump(m, f)
        r, _ = update(t5, source_root=src, packs=fake, with_hook=False)
        m = _read_manifest(sk5)
        check("alpha" in r["updated"] and m.get("hashes", {}).get("alpha") == tree_hash(os.path.join(sk5, "alpha")),
              "舊版 manifest 沒有指紋：照舊覆蓋並補記")

    # 技能清單大小與重疊
    with tempfile.TemporaryDirectory() as d:
        def mk(name, desc, extra=""):
            os.makedirs(os.path.join(d, name))
            with open(os.path.join(d, name, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(f"---\nname: {name}\ndescription: >\n  {desc}\n{extra}---\n# x\n")
            return os.path.join(d, name, "SKILL.md")
        small = {"a": mk("a", "Short helper for dates."), "b": mk("b", "Parse CSV files quickly.")}
        rep = listing_report(small)
        check(rep["count"] == 2 and rep["chars"] == len("a") + 23 + len("b") + 24 and not listing_notes(rep),
              "小清單：長度照實算（多行說明也讀得到），不出提醒")
        big = dict(small, **{f"s{i}": mk(f"s{i}", "x" * 3000) for i in range(6)})
        rep = listing_report(big)
        check(rep["chars"] > LISTING_BUDGET and rep["top"][0][1] == len("s0") + LISTING_DESC_CAP
              and any("超過官方保底預算" in n for n in listing_notes(rep)), "超過預算會提醒，每個說明最多算 1,536 字元")
        hidden = {"h": mk("h", "y" * 9000, "disable-model-invocation: true\n")}
        check(listing_report(hidden)["count"] == 0, "只能手動叫用的技能不算進清單")
        many = {f"m{i}": mk(f"m{i}", f"Tool number {i} for topic{i}.") for i in range(LISTING_MANY + 1)}
        check(any("5～15" in n for n in listing_notes(listing_report(many))), "裝超過 15 個會建議精簡")
        dup = {"database-designer": mk("database-designer", "Design relational database schemas, tables, indexes and migrations."),
               "database-schema-designer": mk("database-schema-designer", "Design database schemas with tables, indexes, migrations and ERD."),
               "pomodoro": mk("pomodoro", "Focus timer with breaks.")}
        rep = listing_report(dup)
        check([o[:2] for o in rep["overlaps"]] == [("database-designer", "database-schema-designer")]
              and any("可能重疊" in n for n in listing_notes(rep)), "名稱和說明都相近的技能列為可能重疊，不相關的不列")
        zh = {"x": mk("x", "整理台股月營收與殖利率"), "y": mk("y", "台股月營收與殖利率整理工具")}
        check(listing_report(zh)["overlaps"], "中文說明也比得出重疊")

    lines = catalog_lines()
    paths = [ln.split("｜")[0] for ln in lines if "｜" in ln and not ln.startswith("#")]
    check(len(paths) > 300 and all(os.path.isfile(os.path.join(REPO_ROOT, p, "SKILL.md")) for p in paths),
          "catalog 列出全部技能，且每個路徑都能直接拿去 install --skills")

    print("\n自我測試" + ("全部通過" if ok else "有失敗"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="把 skl 技能複製進其他 App 的 repo")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="列出技能組合")
    sub.add_parser("catalog", help="列出全部技能（依類別分組），給 App 自己挑選")
    i = sub.add_parser("install", help="安裝技能組合")
    i.add_argument("--target", default=".", help="目標 App 的 repo 根目錄（預設目前資料夾）")
    i.add_argument("--packs", default=None, help="逗號分隔，例如 core,pwa,actions（沒給 --skills 時預設 core）")
    i.add_argument("--skills", default="", help="逗號分隔的技能路徑，例如 engineering/skills/focused-fix")
    i.add_argument("--force", action="store_true", help="覆蓋目標裡同名、但不是本工具裝的技能，以及 App 裡手改過的技能")
    i.add_argument("--dry-run", action="store_true", help="只列出會做什麼，不寫入")
    i.add_argument("--no-claude-md", action="store_true", help="不建立 CLAUDE.md 範本")
    i.add_argument("--no-security-scan", action="store_true", help="跳過安裝前的安全掃描（確認來源可信才用）")
    i.add_argument("--no-hook", action="store_true", help="不加開工自動健檢")
    u = sub.add_parser("update", help="把已裝的技能更新成 skl 最新版")
    u.add_argument("--target", default=".")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--no-security-scan", action="store_true")
    u.add_argument("--no-hook", action="store_true")
    u.add_argument("--force", action="store_true", help="連 App 裡手改過的技能也覆蓋成 skl 最新版")
    lp = sub.add_parser("listing", help="檢查一個 repo 的技能清單有多大、有沒有功能重疊的技能")
    lp.add_argument("--target", default=".")
    sub.add_parser("selftest", help="離線自我測試")
    hp = sub.add_parser("hash", help="印出技能資料夾的內容指紋（加進 security-reviewed.json 用）")
    hp.add_argument("path")
    args = ap.parse_args(argv)

    if args.cmd == "hash":
        print(tree_hash(args.path))
        return 0
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "catalog":
        print("\n".join(catalog_lines()))
        return 0
    if args.cmd == "listing":
        root = os.path.join(os.path.abspath(args.target), ".claude", "skills")
        mds = {n: os.path.join(root, n, "SKILL.md") for n in (os.listdir(root) if os.path.isdir(root) else [])
               if os.path.isfile(os.path.join(root, n, "SKILL.md"))}
        if not mds:
            print("這個 repo 沒有裝任何技能（.claude/skills/ 是空的）。")
            return 0
        rep = listing_report(mds)
        print(f"技能清單：{rep['count']} 個、每一輪約 {rep['chars']:,} 字元（官方保底預算 {LISTING_BUDGET:,}）")
        print("最大的：" + "、".join(f"{n} {c:,}" for n, c in rep["top"]))
        for note in listing_notes(rep):
            print("※ " + note)
        if not listing_notes(rep):
            print("沒有超過預算，也沒有看起來重疊的技能。")
        return 0
    if args.cmd == "list":
        for name, p in load_packs().items():
            size = listing_report({os.path.basename(x): os.path.join(REPO_ROOT, x, "SKILL.md") for x in p["skills"]})["chars"]
            print(f"{name:9} {p['desc']}（清單約 {size:,} 字元）")
            for s in p["skills"]:
                print(f"          - {os.path.basename(s)}")
        print("（app-guardrails-audit 每次都會裝）")
        return 0
    if args.cmd == "install":
        skills = [x.strip() for x in args.skills.split(",") if x.strip()]
        packs_arg = args.packs if args.packs is not None else ("" if skills else "core")
        packs = [p.strip() for p in packs_arg.split(",") if p.strip()]
        res, code = install(os.path.abspath(args.target), packs, args.force, args.dry_run,
                            not args.no_claude_md, extra_skills=skills,
                            scan=not args.no_security_scan, with_hook=not args.no_hook)
    elif args.cmd == "update":
        res, code = update(os.path.abspath(args.target), args.dry_run,
                           scan=not args.no_security_scan, with_hook=not args.no_hook, force=args.force)
    else:
        ap.print_help()
        return 2
    _print(res, getattr(args, "dry_run", False))
    return code


if __name__ == "__main__":
    sys.exit(main())
