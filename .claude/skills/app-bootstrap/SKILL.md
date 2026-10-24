---
name: app-bootstrap
description: 把 skl 技能庫裡挑好的技能組合（core／pwa／actions／backend／security）與 CLAUDE.md 工作守則，一鍵複製進另一個 App 的 repo，讓那個 App 之後每次開 Claude Code 工作階段都自動有這些技能。使用時機：使用者說「幫這個 App 裝技能」「把 skl 套到這個專案」「初始化新專案」「更新這個 repo 的技能」。
---

# 把技能庫套用到其他 App

## 為什麼用「複製」

雲端工作階段（claude.ai/code、手機）不一定會自動安裝外部 GitHub 外掛市集的外掛；
但 repo 內的 `.claude/skills/` 每次都會被讀到。所以把技能直接複製進目標 repo 最穩。
代價：技能庫更新後，要在目標 repo 跑一次 `update`。

## 流程（在「目標 App」的工作階段裡做）

1. 取得技能庫（公開 repo，唯讀淺層複製即可）：
   ```bash
   git clone --depth 1 https://github.com/xin7355-collab/skl /tmp/skl
   ```
2. 看有哪些組合，依專案性質挑：
   ```bash
   python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py list
   ```
   判斷方式：有 `manifest.json`／service worker → `pwa`；有 `.github/workflows` → `actions`；
   有資料庫或 API 伺服器 → `backend`；處理金鑰、登入、使用者資料 → `security`；
   有會自己查資料、做事的 AI（AI Agent）→ `ai`＋`agent`。`core` 一律裝。
3. 先試跑再安裝：
   ```bash
   python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py install --target . --packs core,pwa --dry-run
   python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py install --target . --packs core,pwa
   ```
   - 目標已有同名技能且不是本工具裝的 → 預設略過（不覆蓋使用者自己的技能）。
   - **安裝前自動安全掃描**（skill-security-auditor）：FAIL 不安裝、WARN 照裝但列出；
     掃描工具壞掉時一律不裝（確認來源可信才用 `--no-security-scan`）。
   - **skl 補充掃描**（上游掃描看到抑制標記就整行跳過、不可見字元只看 .md，所以再掃一次所有文字檔）：
     外來技能有掃描抑制標記（SEC-AUDITOR／ignore-line 那兩種）、雙向控制字元、Tag 字元、跟 Python 標準模組同名的檔案
     → FAIL；零寬等不可見字元、寫死個人家目錄 → WARN。emoji 組合字、檔頭 BOM 不算。
     誤判可照舊用 `assets/security-reviewed.json`（內容指紋綁定）人工放行。
   - 技能裡有**符號連結**一律不裝（可能把技能外的檔案帶進 App；`--no-security-scan` 也不放行）。
   - **開工自動健檢**：加上 SessionStart hook（`.claude/hooks/skl-session-start.sh`），每次開對話先回報
     問題數量，技能超過 14 天沒更新會提醒；合併進既有 `.claude/settings.json`，不覆蓋原設定（`--no-hook` 可關）。
   - 目標沒有 `CLAUDE.md` → 建立工作守則範本；已有 → 不覆蓋，提示合併。
   - **技能清單大小與重疊**：每個技能的名稱＋說明每一輪都會放進對話，總長超過官方保底預算（8,000 字元）時，
     最少用的技能說明會被拿掉、比較不會被自動叫用。安裝（含試跑）最後會列出清單大小；超過預算、裝超過 15 個、
     或有兩個技能說明很像時會提醒。照提醒精簡：只留這個 App 真的用得到的 5～15 個，重疊的留一個。
     隨時可以用 `bootstrap_app.py listing --target .` 檢查。
4. 若新建了 `CLAUDE.md`：依專案實際情況填第 7 節；不是 PWA + Actions 架構就刪掉第 6 節。
5. 裝完立刻跑一次 `app-guardrails-audit` 做健檢，把結果回報給使用者。
6. commit：`.claude/skills/` 與 `CLAUDE.md`（commit 訊息寫裝了哪些組合、來源 commit）。
7. **裝完接著做，不要停在「裝好了」**（使用者要求）：
   - 先列「執行方案」：每個新技能在這個 App 要拿來做什麼、會改哪些檔案、怎麼驗證、有沒有費用或要使用者準備的東西。
   - 技能需要的套件或工具（npm／pip 套件、Chrome、ffmpeg、字型）寫進 repo 的設定檔：`package.json`、
     `requirements.txt`、工作流的安裝步驟或 SessionStart hook，並固定版本。雲端工作階段的容器每次都是新的，
     只在容器裡 `pip install` 的東西下次就不見了。
   - 列完就直接照方案做，不用等回覆。需要使用者提供的東西（金鑰、帳號、付款），或目標 repo 的 CLAUDE.md
     沒授權的動作，才先停下來問。使用者說「只裝不做」、或明說「只是先問問題」時，就停在安裝。
   - 每一項做完就驗證（有測試就跑；網頁用 Playwright 開手機尺寸看），照 CLAUDE.md 合併推送，
     回報做了什麼、驗過什麼、沒驗到什麼。

只要個別技能（例如網頁上勾選複製來的路徑）時，用 `--skills`，可與 `--packs` 併用：
```bash
python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py install --target . --skills engineering/skills/focused-fix,marketing-skill/skills/seo-audit
```

## 更新

```bash
git clone --depth 1 https://github.com/xin7355-collab/skl /tmp/skl
python3 /tmp/skl/xin-toolkit/skills/app-bootstrap/scripts/bootstrap_app.py update --target .
```
只更新 `.claude/skills/.skl-vendor.json` 記錄的技能，不動 CLAUDE.md。
安裝時會把每個技能的內容指紋記進 `.skl-vendor.json` 的 `hashes`；更新時發現 App 裡改過某個技能
（指紋對不上）就**跳過不覆蓋**並列出，確定要換成 skl 版本才加 `--force`。舊版記錄沒有指紋的照舊覆蓋並補記。

## 自我測試

```bash
python3 scripts/bootstrap_app.py selftest
```
