#!/usr/bin/env python3
# guardrails: ignore-file  （本檔內含大量故意寫錯的測試範例）
"""guardrails_audit.py — 依「低資源 App 防禦準則」靜態掃描一個專案。

掃的是「在免費主機／iPhone／GitHub Actions 上會出事」的寫法：
記憶體爆掉、SQLite 鎖死、併發風暴打爆外部 API、金鑰外洩、iOS 分享檔案被吃掉、
Actions 被逾時砍掉等。只用標準函式庫，離線可跑。

為什麼逐行讀檔：這支工具本身也要守 OOM 準則——大型 repo 或 1GB RAM 主機上
不能把整個檔案讀進記憶體；超過 --max-file-kb 的檔案直接略過並在總結列出。

用法：
  python3 guardrails_audit.py audit [路徑] [--json] [--max-file-kb 1024]
  python3 guardrails_audit.py selftest

刻意保留的寫法（例如測試用假金鑰）：在該行加註解 `guardrails: ignore`；
整個檔案都是測試範例時，在前 5 行內加 `guardrails: ignore-file`。

結束碼：0 = 沒有 error 等級問題；1 = 至少一個 error；2 = 參數錯誤（例如路徑不存在）。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

# 這些資料夾不是專案自己的程式：第三方套件、建置產物、以及
# 從技能庫複製進來的 .claude/skills（裡面的範例程式會造成大量誤報）。
SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".venv", "venv", "env",
    "__pycache__", "site", "vendor", ".next", ".cache", "coverage",
}
SKIP_REL_PREFIXES = (os.path.join(".claude", "skills"),)

CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm",
            ".vue", ".svelte", ".sh", ".yml", ".yaml"}
JS_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".vue", ".svelte"}
PY_EXT = {".py"}
HTML_EXT = {".html", ".htm"}

SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


def _rule(rid, sev, exts, pattern, msg, fix, fake=None):
    return {"id": rid, "sev": sev, "exts": exts,
            "re": re.compile(pattern) if pattern else None, "msg": msg, "fix": fix, "fake": fake}


def _fake_secret(tok):
    """文件與測試常見的假金鑰：AWS 官方範例 AKIA…EXAMPLE、整串同一個字（sk-ant-aaaa…）。
    這些不是外洩，報成「必修」只會讓人習慣忽略紅燈。"""
    if "EXAMPLE" in tok.upper():
        return True
    body = re.sub(r"^(sk-ant-|sk-proj-|gh[pousr]_|github_pat_|AIza|xox[bp]-|AKIA)", "", tok)
    return len(set(body.replace("-", "").replace("_", ""))) <= 3


# 單行規則：一行命中就回報。
LINE_RULES = [
    _rule("secret-in-url", "error", CODE_EXT,
          r"""https?://[^\s"'`]*[?&](api[_-]?key|key|token|access_token|secret|password)=""",
          "金鑰／Token 放在網址參數裡",
          "改用 HTTP 標頭（例如 Authorization / x-api-key）；網址會出現在錯誤訊息、代理記錄與瀏覽器歷史。"),
    _rule("hardcoded-secret", "error", CODE_EXT | {".json", ".env", ".txt", ".md"},
          # AWS／OpenAI 專案金鑰／GitHub 各種 token（ghp_ gho_ ghu_ ghs_ ghr_）／私鑰檔頭參考
          # rehan-remade/universal-modder um/publish.py（MIT，Copyright (c) 2026 Rehan and universal-modder contributors）
          r"(sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
          r"|AIza[0-9A-Za-z_-]{30,}|xox[bp]-[A-Za-z0-9-]{20,}|\bAKIA[0-9A-Z]{16}\b"
          # 私鑰檔頭要後面「接著就是金鑰內容」才算（行尾，或 JSON 裡的 \n 加 base64）；
          # 掃描工具的規則字串、文件表格只寫檔頭，不是外洩。
          r"|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----(?:[\"']*\s*$|\\n[A-Za-z0-9+/]{20,}))",
          "疑似把真的金鑰寫進程式碼",
          "立刻到服務商後台撤銷這把金鑰，改放 GitHub Secrets / 環境變數；git 歷史裡也要清掉。",
          fake=_fake_secret),
    _rule("log-secret", "warn", CODE_EXT,
          r"(print\(|console\.(log|info|debug)\(|echo ).*\b(token|api_?key|secret|password)\b",
          "可能把金鑰印到 log",
          "只印長度或前 4 碼遮罩，例如 f\"token=***{len(t)}\"。"),
    _rule("unbounded-promise-all", "warn", JS_EXT,
          r"Promise\.(all|allSettled)\(\s*[\w.]+\.map\(",
          "Promise.all + map：清單有多長就同時打多少個請求（併發風暴）",
          "改用固定併發數的佇列（例如一次 3 個），並對 429/5xx 做指數退避重試。"),
    _rule("unbounded-gather", "warn", PY_EXT,
          r"asyncio\.gather\(\s*\*",
          "asyncio.gather(*清單)：沒有併發上限",
          "用 asyncio.Semaphore(N) 包住每個工作，或分批（chunk）送出。"),
    _rule("file-to-arraybuffer", "warn", JS_EXT,
          r"(readAsArrayBuffer\(|\.arrayBuffer\(\)|readAsDataURL\()",
          "把整個檔案讀進記憶體（iPhone 上大檔會讓分頁當掉）",
          "上傳時直接把 File 當 body 送（XHR 可顯示進度）；需要處理內容就用 file.slice() 分段。"),
    _rule("pandas-no-chunks", "info", PY_EXT,
          r"pd\.read_(csv|json|table)\((?!.*chunksize)",
          "pandas 一次讀完整份檔案",
          "資料可能變大時加 chunksize=… 分批處理，1GB RAM 主機才不會 OOM。"),
    _rule("hardcoded-model", "warn", CODE_EXT | {".json"},
          r"""["'](claude-(opus|sonnet|haiku|fable)-[\w.-]+|gpt-\d[\w.-]*|gemini-\d[\w.-]*)["']""",
          "寫死外部 AI 模型名稱（常改名／下架）",
          "改成讀環境變數（例如 MODEL_ID），沒設定時呼叫供應商的模型清單 API 自動挑。"),
]


# 機密檔名：這些檔案本身就是金鑰或帳密，內容掃描不一定抓得到（.p12 是二進位、.env 的值沒有固定格式）。
# 檔名規則參考 Graphify-Labs/graphify graphify/detect.py 的 _is_sensitive（Apache-2.0／MIT，Copyright (c) 2026 Safi Shamsi）。
_ENV_TEMPLATE = (".example", ".sample", ".template", ".dist")


def _sensitive_name(name, path):
    """回傳 (等級, 說明) 或 None。等級是「確定被 commit」時用的；沒 commit 時呼叫端會降級。"""
    low = name.lower()
    if re.match(r"\.env(rc)?(\..+)?$", low):
        return None if low.endswith(_ENV_TEMPLATE) else ("error", ".env 環境變數檔（通常放金鑰）")
    if low in {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}:
        return ("error", "SSH 私鑰")
    if low.endswith((".p12", ".pfx")):
        return ("error", "憑證＋私鑰封裝檔")
    if low.endswith(".pem"):
        # .pem 也可能只是公開的 CA 憑證（例如 cacert.pem）；只有裡面真的有私鑰才算
        try:
            with open(path, "rb") as f:
                return ("error", "PEM 私鑰") if b"PRIVATE KEY" in f.read(65536) else None
        except OSError:
            return ("error", "PEM 檔（讀不到內容，當成私鑰處理）")
    if low in {".git-credentials", ".netrc", "_netrc"}:
        return ("error", "Git／網路登入帳密檔")
    if re.match(r"service[-_]?account.*\.json$", low):
        return ("error", "雲端服務帳號金鑰（JSON）")
    if low in {".npmrc", ".pypirc"}:
        # 常常只有套件來源設定、沒有 token，所以只建議檢查
        return ("warn", "套件發佈設定檔（可能含發佈 token）")
    return None


def _git_status_sets(root):
    """回傳 (已追蹤檔案, 未追蹤也沒被忽略的檔案)；不是 git repo 或沒有 git 時回 None。"""
    def ls(*extra):
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", *extra],
                             capture_output=True, timeout=60)
        if out.returncode != 0:
            raise OSError(out.stderr.decode("utf-8", "replace")[:200])
        return {p.replace("/", os.sep) for p in out.stdout.decode("utf-8", "replace").split("\0") if p}
    try:
        return ls(), ls("--others", "--exclude-standard")
    except (OSError, subprocess.SubprocessError):
        return None


def _iter_files(root, max_bytes, skipped, sensitive=None):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not os.path.normpath(os.path.join(rel_dir, d)).startswith(SKIP_REL_PREFIXES)]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if sensitive is not None:
                hit = _sensitive_name(name, path)
                if hit:
                    sensitive.append((os.path.relpath(path, root), hit))
            ext = os.path.splitext(name)[1].lower()
            if name.startswith(".env"):
                ext = ".env"
            if ext not in CODE_EXT | {".json", ".env", ".txt", ".md"}:
                continue
            try:
                size = os.path.getsize(path)
            except OSError as e:
                skipped.append((os.path.relpath(path, root), f"讀不到：{e.strerror}"))
                continue
            if size > max_bytes:
                skipped.append((os.path.relpath(path, root), f"檔案 {size // 1024} KB 超過上限，未掃描"))
                continue
            yield path, ext


def _lines(path, skipped, root):
    """逐行讀；遇到二進位或壞編碼就略過並記錄，不讓整次掃描失敗。"""
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as f:
            for i, line in enumerate(f, 1):
                if "\x00" in line:
                    skipped.append((os.path.relpath(path, root), "看起來是二進位檔"))
                    return
                yield i, line
    except UnicodeDecodeError:
        skipped.append((os.path.relpath(path, root), "不是 UTF-8 文字檔"))
    except OSError as e:
        skipped.append((os.path.relpath(path, root), f"讀不到：{e.strerror}"))


def audit(root, max_kb=1024):
    root = os.path.abspath(root)
    findings, skipped = [], []
    scanned = 0
    has_pwa_marker = False
    pwa_marker_file = None
    has_app_ver = False

    sensitive = []
    for path, ext in _iter_files(root, max_kb * 1024, skipped, sensitive):
        rel = os.path.relpath(path, root)
        base = os.path.basename(path)
        is_workflow = rel.replace(os.sep, "/").startswith(".github/workflows/") and ext in {".yml", ".yaml"}
        if base in {"manifest.json", "manifest.webmanifest", "sw.js", "service-worker.js"}:
            has_pwa_marker, pwa_marker_file = True, rel

        # 檔案層級規則需要的旗標，一邊逐行掃一邊累積，不另外把整檔讀進來。
        flags = {"journal_mode": False, "busy_timeout": False, "retry": False,
                 "noindex": False, "head": False, "runs_on": False, "timeout": False,
                 "mkdir": False}
        sqlite_lines, http_line, tee_lines, share_window = [], None, [], 0
        share_start, share_has_files, share_has_title = None, False, False
        wf_lines = []   # 工作流檔很小，整份留著做跨行判斷（job 權限、checkout 步驟、run 區塊）
        scanned += 1

        for no, line in _lines(path, skipped, root):
            if no <= 5 and "guardrails: ignore-file" in line:
                skipped.append((rel, "檔案標記 guardrails: ignore-file"))
                findings[:] = [f for f in findings if f["file"] != rel]
                sqlite_lines, http_line, tee_lines, share_window, wf_lines = [], None, [], 0, []
                flags = dict.fromkeys(flags, True)
                scanned -= 1
                break
            low = line.lower()
            # 測試用的假金鑰／故意寫錯的範例，在行尾加 guardrails: ignore 就不回報單行規則。
            ignored = "guardrails: ignore" in line
            for r in LINE_RULES if not ignored else ():
                if ext in r["exts"] and any(not (r["fake"] and r["fake"](m.group(0))) for m in r["re"].finditer(line)):
                    findings.append(_f(r["id"], r["sev"], rel, no, r["msg"], r["fix"], line))
            if "APP_VER" in line:
                has_app_ver = True

            if ext in PY_EXT | JS_EXT:
                if "journal_mode" in low:
                    flags["journal_mode"] = True
                if "busy_timeout" in low or re.search(r"\btimeout\s*=", line):
                    flags["busy_timeout"] = True
                if re.search(r"retry|retries|backoff|tenacity|attempt", low):
                    flags["retry"] = True
                # 唯讀連線（mode=ro）不會寫入，不需要 WAL 與等鎖設定；標了 ignore 的測試範例也不算。
                if (not ignored and "mode=ro" not in line
                        and re.search(r"sqlite3\.connect\(|new\s+Database\(|better-sqlite3", line)):
                    sqlite_lines.append((no, line))
                if http_line is None and re.search(r"requests\.(get|post|put|delete)\(|urlopen\(|httpx\.|\bfetch\(|axios\.", line):
                    http_line = (no, line)
                # navigator.share 的參數可能跨好幾行，所以往後看 8 行。
                if "navigator.share(" in line and not ignored:
                    share_start, share_window = (no, line), 8
                    share_has_files = share_has_title = False
                if share_window > 0:
                    share_has_files |= bool(re.search(r"\bfiles\s*:", line))
                    share_has_title |= bool(re.search(r"\btitle\s*:", line))
                    share_window -= 1
                    if share_window == 0 or (")" in line and share_start[0] != no):
                        _share_finding(findings, rel, share_start, share_has_files, share_has_title)
                        share_window = 0
            if ext in HTML_EXT:
                if "<head" in low:
                    flags["head"] = True
                if "noindex" in low:
                    flags["noindex"] = True
            if is_workflow:
                wf_lines.append((no, line.rstrip("\r\n")))
                if "runs-on:" in line:
                    flags["runs_on"] = True
                if "timeout-minutes" in line:
                    flags["timeout"] = True
                if "mkdir -p" in line:
                    flags["mkdir"] = True
                if re.search(r"\|\s*tee\s+(-a\s+)?[\w.$\{\}-]+/", line):
                    tee_lines.append((no, line))

        # 檔案在 8 行視窗內就結束時，上面的迴圈沒機會收尾，這裡補判一次。
        if share_window > 0:
            _share_finding(findings, rel, share_start, share_has_files, share_has_title)

        for no, line in sqlite_lines:
            if not flags["journal_mode"]:
                findings.append(_f("sqlite-no-wal", "warn", rel, no,
                                   "開 SQLite 連線但整個檔案沒設定 journal_mode",
                                   "連線後執行 PRAGMA journal_mode=WAL，讀寫才不會互相鎖住。", line))
            if not flags["busy_timeout"]:
                findings.append(_f("sqlite-no-timeout", "warn", rel, no,
                                   "SQLite 連線沒有等待鎖的逾時設定（遇鎖立刻丟 database is locked）",
                                   "Python 用 sqlite3.connect(path, timeout=30)；或 PRAGMA busy_timeout=30000，並對寫入加重試。", line))
        if http_line and not flags["retry"]:
            findings.append(_f("http-no-retry", "info", rel, http_line[0],
                               "有對外 HTTP 請求，但整個檔案看不到重試／退避邏輯",
                               "對 429、5xx、逾時做指數退避重試（2/4/8/16 秒，最多 4 次），並尊重 Retry-After。",
                               http_line[1]))
        if ext in HTML_EXT and flags["head"] and not flags["noindex"]:
            findings.append(_f("html-no-noindex", "warn", rel, 1,
                               "網頁沒有 noindex（公開 repo 的站會被搜尋引擎收錄）",
                               '在 <head> 加 <meta name="robots" content="noindex, nofollow">。', ""))
        if is_workflow and flags["runs_on"] and not flags["timeout"]:
            findings.append(_f("workflow-no-timeout", "warn", rel, 1,
                               "工作流沒有 timeout-minutes（預設 6 小時，卡住會燒光分鐘數）",
                               "每個 job 加 timeout-minutes，並在程式裡設時間預算、做完一個存一個進度。", ""))
        if is_workflow and tee_lines and not flags["mkdir"]:
            no, line = tee_lines[0]
            findings.append(_f("workflow-tee-no-mkdir", "warn", rel, no,
                               "tee 寫到子資料夾但沒有先 mkdir -p（pipefail 下整步失敗）",
                               "在那一步前面加 mkdir -p <資料夾>。", line))
        if is_workflow and wf_lines:
            _workflow_security(findings, rel, wf_lines)

    if has_pwa_marker and not has_app_ver:
        findings.append(_f("pwa-no-version", "info", pwa_marker_file, 1,
                           "看起來是 PWA，但找不到 APP_VER 版本號",
                           "前端加 APP_VER，每次改版更新；頁面定期比對伺服器版本，不同就自動重載。", ""))

    if sensitive:
        git_sets = _git_status_sets(root)
        for rel, (sev, what) in sensitive:
            if git_sets is None:
                state, sev = "（不是 git repo，無法確認有沒有 commit）", "warn"
            elif rel in git_sets[0]:
                state = "已被 git 追蹤（已 commit 或已 add）"
            elif rel in git_sets[1]:
                state, sev = "還沒 commit，但沒被 .gitignore 擋住，git add -A 就會帶進去", "warn"
            else:
                continue  # 已被 .gitignore 忽略：正確做法
            findings.append(_f("secret-file-committed", sev, rel, 1, f"機密檔：{what}{state}",
                               "加進 .gitignore 並 git rm --cached；裡面的金鑰視同外洩，到服務商後台撤銷重發，"
                               "改放 GitHub Secrets／環境變數。範本檔請改名成 .env.example 且只放假值。", ""))

    findings.sort(key=lambda x: (SEV_ORDER[x["severity"]], x["file"], x["line"]))
    return {"root": root, "scanned_files": scanned, "skipped": skipped, "findings": findings}


# ---- GitHub Actions 安全規則 ----
# 規則參考 affaan-m/ECC scripts/ci/validate-workflow-security.js（MIT，Copyright (c) 2026 Affaan Mustafa），
# 改成逐 job 判斷權限（只有一個 job 要寫入時，其他唯讀 job 的 checkout 不必報）。
_EXPR = re.compile(r"\$\{\{(.*?)\}\}")
# 使用者可控制的內容：PR 標題／分支名／Issue 內文／手動輸入。直接展開進 shell 指令就能注入命令。
_UNTRUSTED = re.compile(r"\b(github\.event\.[\w.\[\]'\"-]+|inputs\.[\w-]+|github\.head_ref)")
# 數字、SHA、ID 不可能夾帶指令，不報，避免雜訊
_SAFE_FIELD = re.compile(r"(\.number|\.id|sha|\.run_id|\.run_number|\.run_attempt)$")
_INSTALL = re.compile(r"\b(npm\s+(ci|install|i)|pnpm\s+(install|i)|bun\s+install)\b(?!.*--ignore-scripts)")
_CHECKOUT = re.compile(r"uses:\s*['\"]?actions/checkout@")
_PR_HEAD = re.compile(r"github\.event\.pull_request\.head\.(ref|sha|repo\.full_name)|github\.head_ref|refs/(remotes/)?pull/")


def _indent(s):
    return len(s) - len(s.lstrip(" "))


def _is_code(s):
    t = s.strip()
    return bool(t) and not t.startswith("#")


def _perm_write(lines, i):
    """lines[i] 是 permissions: 那一行；回傳這個權限區塊有沒有任何寫入權限。"""
    text = lines[i][1]
    val = text.split(":", 1)[1].split("#")[0].strip().strip("'\"")
    if val:
        return val == "write-all"
    base = _indent(text)
    for _, t in lines[i + 1:]:
        if not _is_code(t):
            continue
        if _indent(t) <= base:
            break
        if re.search(r":\s*['\"]?write\b", t):
            return True
    return False


def _workflow_security(findings, rel, lines):
    def add(rid, sev, no, msg, fix, src):
        if "guardrails: ignore" not in src:
            findings.append(_f(rid, sev, rel, no, msg, fix, src))

    jobs_i = next((i for i, (_, t) in enumerate(lines) if re.match(r"jobs:\s*(#.*)?$", t)), len(lines))
    header = lines[:jobs_i]
    pr_target = any(_is_code(t) and "pull_request_target" in t for _, t in header)
    top_write = any(re.match(r"permissions:", t) and _perm_write(lines, i) for i, (_, t) in enumerate(header))

    # 切出每個 job：jobs: 底下第一層的 key
    jobs, job_indent = [], None
    for i in range(jobs_i + 1, len(lines)):
        t = lines[i][1]
        if not _is_code(t):
            continue
        if job_indent is None:
            job_indent = _indent(t)
        if _indent(t) == job_indent:
            if jobs:
                jobs[-1][1] = i
            jobs.append([i, len(lines)])
        elif _indent(t) < job_indent:
            if jobs:
                jobs[-1][1] = i
            break
    for start, end in jobs:
        body = lines[start + 1:end]
        body_indent = next((_indent(t) for _, t in body if _is_code(t)), None)
        write = top_write
        for j, (_, t) in enumerate(body):
            if _indent(t) == body_indent and re.match(r"\s*permissions:", t):
                write = _perm_write(body, j)  # job 自己的權限取代工作流層級的
        # 步驟：「- 」開頭，到縮排回到同層或更淺為止
        steps, cur = [], None
        for no, t in body:
            if cur and (not t.strip() or _indent(t) > cur["indent"]):
                cur["lines"].append((no, t))
                continue
            m = re.match(r"(\s*)-\s", t)
            cur = {"indent": len(m.group(1)), "lines": [(no, t)]} if m else None
            if cur:
                steps.append(cur)
        for st in steps:
            text = "\n".join(t for _, t in st["lines"])
            if not _CHECKOUT.search(text):
                continue
            no0, src0 = st["lines"][0]
            if pr_target:
                for no, t in st["lines"]:
                    if re.match(r"\s*(-\s+)?(ref|repository):", t) and _PR_HEAD.search(t):
                        add("workflow-pr-target-checkout", "error", no,
                            "pull_request_target 工作流 checkout 了 PR 的程式（帶著 repo 的金鑰與寫入權限執行陌生人的程式碼）",
                            "改用 pull_request 事件；真的需要權限時，先在 pull_request 跑完測試，"
                            "再用 workflow_run 只讀取結果，不要 checkout PR head。", t)
            if write and not re.search(r"persist-credentials:\s*['\"]?false", text):
                add("checkout-persist-credentials", "warn", no0,
                    "有寫入權限的 job 用 actions/checkout 但沒設 persist-credentials: false（token 會留在 .git/config，後面每一步都讀得到）",
                    "checkout 加 with: persist-credentials: false；這個 job 需要 git push 的話才保留，並在註解寫明。", src0)

    # run: 區塊裡的命令注入與安裝腳本
    i = 0
    while i < len(lines):
        no, t = lines[i]
        m = re.match(r"(\s*)(-\s+)?run:\s*(.*)$", t)
        i += 1
        if not m or not _is_code(t):
            continue
        key_indent = len(m.group(1)) + len(m.group(2) or "")
        block = [(no, m.group(3))]
        if m.group(3).strip()[:1] in ("|", ">"):
            block = []
            while i < len(lines) and (not lines[i][1].strip() or _indent(lines[i][1]) > key_indent):
                block.append(lines[i])
                i += 1
        for bno, bt in block:
            if not _is_code(bt):
                continue
            bad = [u for e in _EXPR.findall(bt) for u in _UNTRUSTED.findall(e) if not _SAFE_FIELD.search(u)]
            if bad:
                add("workflow-expr-injection", "warn", bno,
                    f"run: 裡直接展開 ${{{{ {bad[0]} }}}}（內容可能來自使用者輸入，可夾帶 shell 指令）",
                    "改用 env 傳入：在步驟加 env: VAL: ${{ ... }}，指令裡寫 \"$VAL\"（加雙引號）。", bt)
            if _INSTALL.search(bt):
                add("npm-install-scripts", "info", bno,
                    "安裝套件時會執行每個套件的 install 腳本（供應鏈攻擊最常見的入口）",
                    "加 --ignore-scripts（例如 npm ci --ignore-scripts）；真的需要編譯步驟的套件另外跑 npm rebuild <套件>。", bt)


def _share_finding(findings, rel, start, has_files, has_title):
    if has_files and has_title:
        findings.append(_f("ios-share-title", "error", rel, start[0],
                           "navigator.share 同時帶 files 與 title：iOS 只會送出 title 文字，檔案被丟掉",
                           "只傳 {files:[f]}；不支援時退回 <a download>。", start[1]))


def _f(rid, sev, rel, line, msg, fix, src):
    return {"rule": rid, "severity": sev, "file": rel, "line": line,
            "message": msg, "fix": fix, "source": src.strip()[:160]}


def _print_report(res):
    counts = {s: sum(1 for f in res["findings"] if f["severity"] == s) for s in SEV_ORDER}
    label = {"error": "🔴 必修", "warn": "🟡 建議修", "info": "🔵 提醒"}
    for f in res["findings"]:
        print(f"{label[f['severity']]} [{f['rule']}] {f['file']}:{f['line']}")
        print(f"   問題：{f['message']}")
        print(f"   怎麼修：{f['fix']}")
        if f["source"]:
            print(f"   原始碼：{f['source']}")
    print()
    print(f"掃描 {res['scanned_files']} 個檔案；必修 {counts['error']}、建議修 {counts['warn']}、提醒 {counts['info']}。")
    if res["skipped"]:
        print(f"略過 {len(res['skipped'])} 個檔案：")
        for rel, why in res["skipped"][:20]:
            print(f"   - {rel}：{why}")
        if len(res["skipped"]) > 20:
            print(f"   …另外 {len(res['skipped']) - 20} 個（用 --json 看完整清單）")
    if res["scanned_files"] == 0:
        print("⚠️ 沒有掃到任何程式檔——路徑是否指錯了？")


def selftest():
    """離線自我測試：造出會觸發／不會觸發的檔案，確認每條規則的邊界。"""
    ok = True

    def check(cond, name):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as d:
        def w(rel, text):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)

        # 空資料夾：不該當掉，也不該有問題。
        res = audit(d)
        check(res["scanned_files"] == 0 and not res["findings"], "空資料夾不報錯")

        w("bad/db.py", "import sqlite3\nconn = sqlite3.connect('a.db')\n")
        w("ok/ro.py", "import sqlite3\nc = sqlite3.connect(f'file:{p}?mode=ro', uri=True)\n")
        w("good/db.py", "import sqlite3\nc = sqlite3.connect('a.db', timeout=30)\nc.execute('PRAGMA journal_mode=WAL')\n")
        w("bad/api.py", "import requests\nr = requests.get('https://x.io/v1?api_key=' + k)\nprint('token', token)\n")  # guardrails: ignore
        w("good/api.py", "import requests\n# retry with backoff\nr = requests.get(url, headers={'x-api-key': k})\n")
        w("bad/app.js", "await Promise.all(urls.map(u => fetch(u)))\nconst b = await file.arrayBuffer()\n"
                        "navigator.share({\n  title: 'x',\n  files: [f],\n})\n")
        w("good/app.js", "const APP_VER = '3';\nnavigator.share({\n  files: [f],\n})\n// retry\nfetch(u)\n")
        w("bad/share1.js", "navigator.share({files: [f], title: 'x'})")  # guardrails: ignore
        w("ok/ignored.py", "K = 'https://a.io/?token=1'  # guardrails: " + "ignore\n")
        w("ok/fixture.py", "# guardrails: " + "ignore-file\nimport sqlite3\nsqlite3.connect('a')\n")
        w("bad/page.html", "<html><head><title>x</title></head></html>")
        w("good/page.html", '<html><head><meta name="robots" content="noindex, nofollow"></head></html>')
        w("bad/gather.py", "import asyncio\nawait asyncio.gather(*[f(x) for x in xs])\n")
        # 模型名稱用拼接產生，避免這支工具本身被 repo 的「過時模型名稱」檢查誤判。
        w("bad/model.py", "MODEL = '" + "gpt-" + "5-mini" + "'\n")
        w("bad/secret.py", "K = '" + "sk-ant-" + "api03-aB3dE5fG7hJ9kL1mN2pQ4rS6" + "'\n")
        w(".github/workflows/bad.yml", "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make | tee out/log.txt\n")
        w(".github/workflows/good.yml", "jobs:\n  a:\n    runs-on: ubuntu-latest\n    timeout-minutes: 20\n    steps:\n"
                                        "      - run: mkdir -p out && make | tee out/log.txt\n")
        # 被複製進來的技能庫範例不該被掃。
        w(".claude/skills/x/bad.py", "import sqlite3\nsqlite3.connect('a')\n")
        # 超過上限的大檔要被略過並列出，而不是默默消失。
        w("big/huge.js", "// x\n" * 400000)
        # 二進位內容要被略過並列出。
        with open(os.path.join(d, "bin.js"), "wb") as fh:
            fh.write(b"\x00\x01\x02abc")
        w("pwa/manifest.json", "{}")

        res = audit(d, max_kb=1024)
        hits = {(f["rule"], f["file"].replace(os.sep, "/")) for f in res["findings"]}
        rules_by_file = lambda p: {r for r, f in hits if f == p}  # noqa: E731

        check({"sqlite-no-wal", "sqlite-no-timeout"} <= rules_by_file("bad/db.py"), "SQLite 沒 WAL／沒 timeout 會被抓")
        check(not rules_by_file("good/db.py"), "SQLite 有 WAL + timeout 不誤報")
        check(not rules_by_file("ok/ro.py"), "SQLite 唯讀連線不報 WAL")
        check({"secret-in-url", "log-secret", "http-no-retry"} <= rules_by_file("bad/api.py"), "金鑰在網址／印金鑰／沒重試會被抓")
        check(not rules_by_file("good/api.py"), "金鑰放標頭且有重試不誤報")
        check({"unbounded-promise-all", "file-to-arraybuffer", "ios-share-title"} <= rules_by_file("bad/app.js"),
              "併發風暴／arrayBuffer／share 帶 title 會被抓")
        check(not rules_by_file("good/app.js"), "share 只帶 files 不誤報")
        check("ios-share-title" in rules_by_file("bad/share1.js"), "share 單行寫在檔尾也會被抓")
        check("html-no-noindex" in rules_by_file("bad/page.html"), "缺 noindex 會被抓")
        check(not rules_by_file("good/page.html"), "有 noindex 不誤報")
        check(not rules_by_file("ok/ignored.py"), "行尾 guardrails: ignore 會跳過")
        check("unbounded-gather" in rules_by_file("bad/gather.py"), "asyncio.gather(*) 會被抓")
        check("hardcoded-model" in rules_by_file("bad/model.py"), "寫死模型名稱會被抓")
        check("hardcoded-secret" in rules_by_file("bad/secret.py"), "寫死金鑰會被抓")
        check({"workflow-no-timeout", "workflow-tee-no-mkdir"} <= rules_by_file(".github/workflows/bad.yml"),
              "工作流沒 timeout／tee 沒 mkdir 會被抓")
        check(not rules_by_file(".github/workflows/good.yml"), "工作流正確寫法不誤報")
        check(not any(f.startswith(".claude/skills") for _, f in hits), ".claude/skills 被略過")
        skipped = dict(res["skipped"])
        check(not rules_by_file("ok/fixture.py") and "ok/fixture.py" in {k.replace(os.sep, "/") for k in skipped},
              "guardrails: ignore-file 整檔跳過且有列出")
        check(any("huge.js" in k for k in skipped), "超大檔被略過且有列出")
        check(any("bin.js" in k for k in skipped), "二進位檔被略過且有列出")
        check(not any(r == "pwa-no-version" for r, _ in hits), "有 APP_VER 時不報 PWA 版本號")
        check(any(f["severity"] == "error" for f in res["findings"]), "有 error 時結束碼應為 1（由 main 處理）")

    # ---- 金鑰格式（字串拼接產生，避免這支工具本身被別的掃描器當成外洩）----
    with tempfile.TemporaryDirectory() as d:
        def w(rel, text):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        mix = "aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xY"
        pk = "-----BEGIN " + "{}PRIVATE KEY-----"
        cases = {
            "aws.py": ("K = '" + "AKIA" + "Q7RZ3T5V2X4Y6W8N" + "'\n", True),
            "aws_doc.md": ("範例：" + "AKIA" + "IOSFODNN7" + "EXAMPLE" + "\n", False),
            "openai.js": ("const k = '" + "sk-" + "proj-" + mix + "12'\n", True),
            "gho.json": ('{"t": "' + "gho" + "_" + mix + "1234" + '"}\n', True),
            "ghs.sh": ("T=" + "ghs" + "_" + mix + "5678\n", True),
            "fake.py": ("K = '" + "sk-ant-" + "a" * 24 + "'\n", False),
            "pem.py": ('KEY = """' + pk.format("OPENSSH ") + "\n", True),
            "sa.json": ('{"private_key": "' + pk.format("") + "\\n" + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + '"}\n', True),
            "rule.py": ("regex=r'" + pk.format("RSA ") + "',\n", False),
            "table.md": ("| 私鑰 | `" + pk.format("EC ") + "` | high |\n", False),
        }
        for name, (text, _) in cases.items():
            w("k/" + name, text)
        hit = {f["file"].replace(os.sep, "/") for f in audit(d)["findings"] if f["rule"] == "hardcoded-secret"}
        for name, (_, want) in cases.items():
            check((("k/" + name) in hit) == want,
                  f"金鑰格式 {name}：{'要抓' if want else '不誤報（範例／假值／規則字串）'}")

    # ---- 機密檔被 commit（檔名比對，依 git 狀態分級）----
    import shutil as _sh
    if _sh.which("git"):
        with tempfile.TemporaryDirectory() as d:
            def w(rel, text):
                p = os.path.join(d, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            subprocess.run(["git", "init", "-q", d], check=True, capture_output=True)
            w(".gitignore", ".env.local\n")
            files = {".env": "A=1", ".env.example": "A=", ".env.sample": "A=", ".env.local": "A=1",
                     "id_rsa": "x", "id_rsa.pub": "x", "keys/id_ed25519": "x", "ca/cacert.pem": "BEGIN CERTIFICATE",
                     "ca/server.pem": "-----BEGIN " + "PRIVATE KEY-----", "app.p12": "x", "win.pfx": "x",
                     ".npmrc": "registry=x", ".pypirc": "[pypi]", ".git-credentials": "x", ".netrc": "x",
                     "gcp/service-account-prod.json": "{}", "src/service.json": "{}", "src/env.py": "x = 1\n"}
            for rel, text in files.items():
                w(rel, text)
            tracked = [r for r in files if r not in {"id_rsa", ".env.local"}]
            subprocess.run(["git", "-C", d, "add", "--", *tracked, ".gitignore"], check=True, capture_output=True)
            got = {f["file"].replace(os.sep, "/"): f["severity"] for f in audit(d)["findings"]
                   if f["rule"] == "secret-file-committed"}
            want = {".env": "error", "keys/id_ed25519": "error", "ca/server.pem": "error", "app.p12": "error",
                    "win.pfx": "error", ".git-credentials": "error", ".netrc": "error",
                    "gcp/service-account-prod.json": "error", ".npmrc": "warn", ".pypirc": "warn", "id_rsa": "warn"}
            check(got == want, "機密檔名：已 commit 的必修、還沒 commit 也沒被忽略的建議修、"
                               ".env.example／.pub／公開 CA 憑證／被 .gitignore 擋掉的不報"
                  + ("" if got == want else f"（多：{sorted(set(got.items()) - set(want.items()))}，"
                                             f"少：{sorted(set(want.items()) - set(got.items()))}）"))
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w") as fh:
                fh.write("A=1")
            got = [f for f in audit(d)["findings"] if f["rule"] == "secret-file-committed"]
            check(len(got) == 1 and got[0]["severity"] == "warn" and "不是 git repo" in got[0]["message"],
                  "不是 git repo 時無法確認，降為建議修並說明")
    else:
        print("SKIP 機密檔名測試：這台機器沒有 git")

    # ---- GitHub Actions 安全規則 ----
    with tempfile.TemporaryDirectory() as d:
        def w(rel, text):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        E = "${{ "
        w(".github/workflows/bad.yml",
          "on:\n  pull_request_target:\n  workflow_dispatch:\n    inputs:\n      ref: {}\n"
          "permissions:\n  contents: write\n"
          "jobs:\n  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n"
          "      - uses: actions/checkout@v5\n"                                   # 13
          "        with:\n          ref: " + E + "github.event.pull_request.head.sha }}\n"  # 15
          "      - run: npm ci\n"                                                  # 16
          "      - name: x\n        run: |\n"
          "          echo \"" + E + "github.event.inputs.ref }}\"\n"            # 19
          "          echo \"" + E + "github.event.pull_request.title }}\"\n"    # 20
          "          echo \"" + E + "github.event.pull_request.number }}\"\n"   # 21
          "      - run: echo " + E + "inputs.name }}\n")                          # 22
        w(".github/workflows/good.yml",
          "on:\n  pull_request:\n  push:\npermissions:\n  contents: read\n"
          "jobs:\n  test:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n"
          "      - uses: actions/checkout@v5\n"
          "      - run: npm ci --ignore-scripts --no-audit\n"
          "      - env:\n          REF: " + E + "github.event.inputs.ref }}\n        run: echo \"$REF\"\n"
          "  deploy:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n"
          "    permissions:\n      contents: write\n    steps:\n"
          "      - uses: actions/checkout@v5\n        with:\n          persist-credentials: false\n"
          "      # npm " + "install 寫在註解裡不算\n"
          "      - run: echo \"" + E + "github.event.pull_request.head.sha }} " + E + "github.run_id }}\"\n")
        w(".github/workflows/mixed.yml",
          "on: push\njobs:\n  read:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n"
          "    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@v5\n"   # 9
          "  publish:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n"
          "    permissions: write-all\n    steps:\n      - name: co\n        uses: actions/checkout@v5\n")  # 15
        w(".github/workflows/override.yml",
          "on: push\npermissions:\n  contents: write\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
          "    timeout-minutes: 5\n    permissions:\n      contents: read\n    steps:\n"
          "      - uses: actions/checkout@v5\n")
        w(".github/workflows/prt-safe.yml",
          "on:\n  pull_request_target:\npermissions:\n  pull-requests: write\njobs:\n  label:\n"
          "    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n"
          "      - uses: actions/checkout@v5\n        with:\n          persist-credentials: false\n")
        found = {(f["rule"], f["file"].replace(os.sep, "/").split("/")[-1], f["line"]) for f in audit(d)["findings"]}
        sec_rules = {"workflow-pr-target-checkout", "checkout-persist-credentials", "workflow-expr-injection",
                     "npm-install-scripts"}
        by = lambda fn: {(r, ln) for r, f, ln in found if f == fn and r in sec_rules}  # noqa: E731
        check(("workflow-pr-target-checkout", 15) in by("bad.yml"), "pull_request_target＋checkout PR head → 必修")
        check(("checkout-persist-credentials", 13) in by("bad.yml"), "工作流有寫入權限、checkout 沒關 persist-credentials → 建議修")
        check(("npm-install-scripts", 16) in by("bad.yml"), "npm ci 沒加 --ignore-scripts → 提醒")
        inj = {ln for r, ln in by("bad.yml") if r == "workflow-expr-injection"}
        check(inj == {19, 20, 22}, f"run: 裡的 github.event.*／inputs.* 要報、PR 編號不報（實際：{sorted(inj)}）")
        check(not by("good.yml"), "env 傳入、--ignore-scripts、persist-credentials: false、註解、SHA 都不誤報"
              + (f"（{sorted(by('good.yml'))}）" if by("good.yml") else ""))
        check(by("mixed.yml") == {("checkout-persist-credentials", 15)}, "逐 job 判斷：只報有寫入權限（write-all）的那個 job")
        check(not by("override.yml"), "job 自己設唯讀權限時，蓋過工作流層級的寫入權限")
        check(not by("prt-safe.yml"), "pull_request_target 只 checkout 預設分支（不碰 PR head）不報")

    print("\n自我測試" + ("全部通過" if ok else "有失敗"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="低資源 App 防禦準則靜態掃描")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("audit", help="掃描專案")
    a.add_argument("path", nargs="?", default=".")
    a.add_argument("--json", action="store_true", help="輸出 JSON")
    a.add_argument("--max-file-kb", type=int, default=1024, help="超過這個大小的檔案略過（預設 1024）")
    sub.add_parser("selftest", help="離線自我測試")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    if args.cmd != "audit":
        ap.print_help()
        return 2
    if not os.path.isdir(args.path):
        print(f"找不到資料夾：{args.path}。請給專案根目錄的路徑，例如 audit .", file=sys.stderr)
        return 2
    res = audit(args.path, args.max_file_kb)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_report(res)
    return 1 if any(f["severity"] == "error" for f in res["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
