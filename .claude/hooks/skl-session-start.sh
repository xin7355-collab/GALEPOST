#!/bin/bash
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
