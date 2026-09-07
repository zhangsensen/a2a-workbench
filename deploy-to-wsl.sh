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
  --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
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
find "$DST" -maxdepth 1 -name '*.sh' -exec chmod 755 {} +
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

echo "== rooms: rsync $REPO/rooms -> /root/a2a-rooms =="
rsync -a --delete \
  --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
  --exclude .git --exclude data --exclude __pycache__ --exclude '*.pyc' \
  --exclude logs --exclude '*.db' --exclude '*.log' --exclude .pytest_cache \
  --exclude VERSION --exclude .venv \
  "$REPO/rooms/" /root/a2a-rooms/
find /root/a2a-rooms -maxdepth 1 -name '*.sh' -exec chmod 755 {} +
printf '%s\n' "$STAMP" > /root/a2a-rooms/VERSION

cat > /etc/systemd/system/a2a-rooms.service <<UNIT
[Unit]
Description=A2A rooms (persistent roundtable, port 41241)
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$DST/venv/bin/python /root/a2a-rooms/roundtable.py
WorkingDirectory=/root/a2a-rooms
Environment=HOME=/root
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH=/root/a2a-agents
Environment=A2A_MEMBERS=codex,claude
Environment=A2A_EXECUTORS=pi=http://127.0.0.1:10000,claude=http://127.0.0.1:10001,codex=http://127.0.0.1:10002,dsh=http://127.0.0.1:10003
Environment=A2A_ROOM_DATA=/root/a2a-rooms-data
Restart=always
RestartSec=3
StandardOutput=append:/var/log/a2a-rooms.log
StandardError=append:/var/log/a2a-rooms.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable a2a-rooms >/dev/null 2>&1 || true
systemctl restart a2a-rooms

# 验收三重：①Agent Card 版本戳 ②systemd 单元 active ③端口监听 PID == 单元 MainPID。
# 为什么要③：2026-09-07 抓到真事故——cron watchdog 曾在 systemd 之外 setsid 拉起
# server_pi.py，孤儿进程占着 10000 导致 a2a-pi 单元 failed，而它从同一部署目录起、
# Agent Card 版本戳完全正确，仅查版本的验收一路全绿。单 owner 必须由 PID 归属证明。
# （该 watchdog 的 A2A 段已于同日移除，此检查是防回归的护栏。）
echo "== 验收：版本戳 + systemd 单一 owner（PID 归属）=="
sleep 3
ok=1
check_owner() {  # $1=unit $2=port
  local unit=$1 port=$2 active mainpid listener
  active=$(systemctl is-active "$unit" 2>/dev/null || true)
  mainpid=$(systemctl show -p MainPID --value "$unit" 2>/dev/null || true)
  listener=$(ss -ltnp 2>/dev/null | grep ":$port " | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  if [ "$active" != active ]; then
    echo "  $unit FAIL 单元状态=$active"; return 1
  fi
  if [ -z "$listener" ]; then
    echo "  $unit FAIL 端口 $port 无监听"; return 1
  fi
  if [ "$listener" != "$mainpid" ]; then
    echo "  $unit FAIL 端口 $port 监听 PID=$listener 不等于 MainPID=$mainpid（疑似 systemd 之外的孤儿进程占用）"; return 1
  fi
  return 0
}
for pair in 10000:pi 10001:claude 10002:codex 10003:dsh; do
  port=${pair%%:*}; name=${pair##*:}
  got=$(curl -s --max-time 5 "http://127.0.0.1:$port/.well-known/agent-card.json" \
        | "$DST/venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' \
        2>/dev/null || true)
  if [ "$got" != "$STAMP" ]; then
    echo "  port $port FAIL got='$got' want='$STAMP'"
    ok=0
  elif ! check_owner "a2a-$name" "$port"; then
    ok=0
  else
    echo "  port $port OK  version=$got  owner=a2a-$name"
  fi
done
rooms_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:41241/healthz" || true)
if [ "$rooms_code" = 200 ] && check_owner a2a-rooms 41241; then
  echo "  rooms 41241 OK  owner=a2a-rooms"
else
  [ "$rooms_code" = 200 ] || echo "  rooms 41241 FAIL http=$rooms_code（查 /var/log/a2a-rooms.log）"
  ok=0
fi
if [ "$ok" = 1 ]; then
  echo "DEPLOY OK $STAMP"
else
  echo "DEPLOY FAILED —— 查 /var/log/a2a-*.log 与 systemctl status a2a-*"
  exit 1
fi
