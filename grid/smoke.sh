#!/bin/bash
# A2A 底座冒烟测试：端口 / 列表 / 四 agent 调用 / 任务库 / 进程结构
# 用法：bash smoke.sh
cd "$(dirname "$0")"
PY="./venv/Scripts/python.exe"
PASS=0; FAIL=0

check() {
    local name="$1" ok="$2" detail="$3"
    if [ "$ok" = "1" ]; then echo "  PASS  $name"; PASS=$((PASS+1))
    else echo "  FAIL  $name  $detail"; FAIL=$((FAIL+1)); fi
}

echo "=== 1. 四端口 card ==="
for p in 10000 10001 10002 10003; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$p/.well-known/agent-card.json")
    [ "$code" = "200" ] && check "port $p" 1 "" || check "port $p" 0 "code=$code"
done

echo "=== 2. --list ==="
out=$(PYTHONIOENCODING=utf-8 "$PY" a2a_call.py --list 2>&1)
echo "$out" | grep -q "dsh" && check "list 含 4 agent" 1 "" || check "list" 0 "$out"

echo "=== 3. 四 agent 真实调用 ==="
for a in pi claude codex dsh; do
    r=$(PYTHONIOENCODING=utf-8 timeout 150 "$PY" a2a_call.py "$a" "回复OK" 2>&1 | tail -1)
    [ -n "$r" ] && [ "$r" != "(无文本回复)" ] && [ "${r:0:4}" != "(调用" ] && [ "${r:0:4}" != "(无输" ] \
        && check "call $a" 1 "回复=$r" || check "call $a" 0 "回复=$r"
done

echo "=== 4. 任务库（SQLite 落盘）==="
for a in pi claude codex dsh; do
    [ -f "data/$a-tasks.db" ] && check "db $a" 1 "" || check "db $a" 0 "缺文件"
done

echo "=== 5. 进程结构 ==="
loops=$(powershell.exe -NoProfile -Command "\$me=\$PID; (Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { \$_.ProcessId -ne \$me -and \$_.CommandLine -like '*agent_loop*' }).Count" 2>/dev/null | tr -d ' \r\n')
[ "$loops" = "4" ] && check "agent_loop=4" 1 "实际=$loops" || check "agent_loop" 0 "实际=$loops"

echo ""
echo "=== 冒烟结果: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" = "0" ] && echo "ALL GREEN" || echo "有失败项"
