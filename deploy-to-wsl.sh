#!/usr/bin/env bash
# 部署脚本：把 a2a-workbench 的 grid/（执行网格）刷到 WSL 运行环境
# （/root/a2a-agents，路径沿用旧部署，venv 与 systemd 单元不动），
# 并让 systemd 单元接管四个 agent。
#
# 用法（Windows 侧 PowerShell / Git Bash）：
#   wsl -u root bash /mnt/d/Dev/a2a/a2a-workbench/deploy-to-wsl.sh
#
# 为什么存在：2026-08 服务迁 WSL 后靠手工拷贝同步，修复只落开发副本、
# 活环境静默停旧版两周（收敛复盘）。此脚本是唯一部署通道：
# 改代码 → git commit → 跑本脚本 → Agent Card 的 version 字段即部署证据。
set -euo pipefail

REPO=/mnt/d/Dev/a2a/a2a-workbench
SRC=$REPO/grid
DST=/root/a2a-agents
AGENTS=(pi claude codex dsh)
declare -A SERVER_SCRIPT=(
  [pi]=pi/server_pi.py
  [claude]=claude/server.py
  [codex]=codex/server.py
  [dsh]=dsh/server.py
)

[ "$(id -u)" = 0 ] || { echo "必须以 root 运行：wsl -u root bash $0"; exit 1; }
[ -d "$SRC" ] || { echo "源目录不存在：$SRC"; exit 1; }

GIT_HASH=$(git -C "$REPO" -c safe.directory="$REPO" rev-parse --short HEAD 2>/dev/null || echo nogit)
if [ "$GIT_HASH" != "nogit" ]; then
  if ! git -C "$REPO" -c safe.directory="$REPO" diff --quiet HEAD 2>/dev/null; then
    GIT_HASH="${GIT_HASH}+dirty"
  fi
fi
STAMP="${GIT_HASH}-$(date +%Y%m%d-%H%M%S)"

echo "== rsync $SRC -> $DST =="
rsync -a --delete \
  --exclude .git \
  --exclude venv \
  --exclude data \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  --exclude rooms \
  --exclude tmp \
  --exclude .a2a-payloads \
  --exclude .backups \
  --exclude .hermes \
  --exclude '*.db' \
  --exclude '*.log' \
  --exclude VERSION \
  "$SRC/" "$DST/"
printf '%s\n' "$STAMP" > "$DST/VERSION"

echo "== 安装/刷新 systemd 单元 =="
for name in "${AGENTS[@]}"; do
  cat > "/etc/systemd/system/a2a-$name.service" <<UNIT
[Unit]
Description=A2A $name agent (${SERVER_SCRIPT[$name]})
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$DST/venv/bin/python $DST/${SERVER_SCRIPT[$name]}
WorkingDirectory=$DST
# systemd 系统单元默认不带 HOME：pi 的 habi-token.sh（HOME 未定义即退出）、
# dsh 的凭据服务都要按 ~ 找配置，缺 HOME 时表现为 API key 解析失败。
Environment=HOME=/root
Restart=always
RestartSec=3
StandardOutput=append:/var/log/a2a-$name.log
StandardError=append:/var/log/a2a-$name.log

[Install]
WantedBy=multi-user.target
UNIT
done
systemctl daemon-reload
systemctl enable a2a-pi a2a-claude a2a-codex a2a-dsh >/dev/null 2>&1 || true

echo "== 停掉 systemd 之外的旧实例（如有），交给 systemd 接管 =="
for name in "${AGENTS[@]}"; do systemctl stop "a2a-$name" 2>/dev/null || true; done
pkill -f 'a2a-agents/venv/bin/python' 2>/dev/null || true
sleep 1
systemctl start a2a-pi a2a-claude a2a-codex a2a-dsh

echo "== 验收：四张 Agent Card 版本戳必须等于 $STAMP =="
sleep 3
ok=1
for port in 10000 10001 10002 10003; do
  got=$(curl -s --max-time 5 "http://127.0.0.1:$port/.well-known/agent-card.json" \
        | "$DST/venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' \
        2>/dev/null || true)
  if [ "$got" = "$STAMP" ]; then
    echo "  port $port OK  version=$got"
  else
    echo "  port $port FAIL got='$got' want='$STAMP'"
    ok=0
  fi
done
if [ "$ok" = 1 ]; then
  echo "DEPLOY OK $STAMP"
else
  echo "DEPLOY FAILED —— 查 /var/log/a2a-*.log 与 systemctl status a2a-*"
  exit 1
fi
