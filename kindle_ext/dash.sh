#!/bin/sh
# Kindle 看板主循环 ——「不停 framework + 保守重绘 + PC 自动发现」方案
# 思路：framework 始终运行（保留原生界面 / KUAL / 触屏 / 看其他书的能力），
#       每 REPAINT_INTERVAL 秒把缓存帧 eips 局部重绘一次压住原生主页重绘，
#       每 DAY_INTERVAL 秒重新拉一次新图（PC 端每次拉图实时重建）。
# 退出：touch /mnt/us/dashboard/stop 后下一轮检测到即退出，原生界面恢复、KUAL 可进。
#
# PC 地址自动发现（适配 PC 经常换 wifi / IP 变化）：
#   启动（及连续取图失败 3 次后）扫描本机网段，找响应正确 token 的 PC:8765，
#   缓存到 dashboard/pc_ip。server.conf 里的 HOST 作为优先缓存，可留空。
D=/mnt/us/extensions/kdashboard
CFG_DIR=/mnt/us/dashboard
CONF=$CFG_DIR/server.conf
LOG=$CFG_DIR/dash.log
FL_CFG=$CFG_DIR/fl_intensity
STOP=$CFG_DIR/stop
COUNT=$CFG_DIR/refresh_count
PC_IP_FILE=$CFG_DIR/pc_ip

# ---- 默认值 ----
HOST=""
PORT=8765
TOKEN=""
DAY_INTERVAL=60         # 拉新图间隔（秒）= 60 秒（PC 端每次拉图实时重建，时间误差<=60s）
REPAINT_INTERVAL=15     # 重绘缓存帧间隔（秒）= 15 秒（压住原生 UI 重绘，不停 framework）
NIGHT_INTERVAL=3600     # 夜间拉图间隔（秒）= 1 小时（屏仍常显，仅放慢内容更新省电）
NIGHT_START=1           # 夜间开始小时（含）
NIGHT_END=6             # 夜间结束小时（不含）
FULL_REFRESH_RATE=5     # 每 N 次拉图做 1 次全刷（防残影）
DEF_FL=24

# ---- 读取 server.conf（KEY=VALUE）----
if [ -f "$CONF" ]; then
  . "$CONF"
fi
# 兼容：server.conf 若用 INTERVAL 则映射到 DAY_INTERVAL
[ -n "${INTERVAL:-}" ] && DAY_INTERVAL=$INTERVAL

# ---- 前光 ----
FL=$DEF_FL
[ -f "$FL_CFG" ] && FL=$(cat "$FL_CFG" 2>/dev/null)
case "$FL" in ''|*[!0-9]*) FL=$DEF_FL ;; esac
[ "$FL" -lt 0 ] && FL=0
[ "$FL" -gt 24 ] && FL=24

apply_fl() {
  lipc-set-prop -i com.lab126.powerd flIntensity "$FL" 2>&1
}

log() {
  echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"
}

[ -z "$TOKEN" ] && { log "ERROR: server.conf 未配置 TOKEN"; exit 1; }

# ---- 电量读取与上报（供 SYS STATUS 面板显示 Kindle 剩余电量）----
get_batt() {
  local b=""
  for p in /sys/class/power_supply/*/capacity; do
    [ -r "$p" ] && b=$(cat "$p" 2>/dev/null) && [ -n "$b" ] && break
  done
  [ -z "$b" ] && b=$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)
  echo "$b"
}

report_battery() {
  local b c
  b=$(get_batt)
  c=$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null)
  case "$c" in
    y|Y|true|TRUE|1) c=1 ;;
    *) c=0 ;;
  esac
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 10 -o /dev/null "http://$PC_HOST:$PORT/report?t=$TOKEN&b=$b&c=$c" 2>/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 10 -O /dev/null "http://$PC_HOST:$PORT/report?t=$TOKEN&b=$b&c=$c" 2>/dev/null
  fi
}

fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 30 -o /tmp/dash.png "$URL"; return $?
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 30 -O /tmp/dash.png "$URL"; return $?
  fi
  return 1
}

# ---- PC 地址自动发现 ----
# 扫描本机网段，找响应正确 token 的 PC:PORT，结果写入 PC_IP_FILE 缓存。
# 返回 0 表示发现成功（全局 PC_HOST 已设置），1 表示未发现。
discover_pc() {
  # 本机 IP（busybox ifconfig 格式 inet addr:IP；失败回退 ip addr）
  MYIP=$(ifconfig 2>/dev/null | grep -o 'inet addr:[0-9.]*' | grep -v '127.0.0.1' | head -1 | cut -d: -f2)
  [ -z "$MYIP" ] && MYIP=$(ip -4 addr 2>/dev/null | grep -o 'inet [0-9.]*' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' | cut -d/ -f1)
  [ -z "$MYIP" ] && { log "discover: 无法获取本机 IP"; return 1; }
  NET=$(echo "$MYIP" | cut -d. -f1-3)
  log "discover: 本机 $MYIP 网段 $NET.0/24 扫描中..."

  # 候选：ARP 邻居中同网段 IP 优先（快），否则扫描整段
  CAND=$( (ip neigh show 2>/dev/null; cat /proc/net/arp 2>/dev/null) | awk -v net="$NET" '$1 ~ "^"net"[.]" {print $1}' | sort -u )
  if [ -z "$CAND" ]; then
    CAND=$(seq 1 254 | sed "s|^|$NET.|")
  fi

  # 并行探测开放 PORT 的主机（xargs -P；busybox 1.3x 支持，旧版回退串行）
  if echo | xargs -P 2 >/dev/null 2>&1; then PAR="-P 16"; else PAR=""; fi
  OPEN=$(echo "$CAND" | xargs $PAR -I{} sh -c 'code=$(curl -s -m1 -o /dev/null -w "%{http_code}" "http://{}:'$PORT'/health" 2>/dev/null); [ "$code" = "200" ] && echo {}' 2>/dev/null)

  # 对开放 PORT 的主机校验 token（/report?t=TOKEN 返回 200 即正确 PC）
  FOUND=""
  for ip in $OPEN; do
    code=$(curl -s -m2 -o /dev/null -w "%{http_code}" "http://$ip:$PORT/report?t=$TOKEN" 2>/dev/null)
    [ "$code" = "200" ] && { FOUND=$ip; break; }
  done

  if [ -n "$FOUND" ]; then
    echo "$FOUND" > "$PC_IP_FILE"
    log "discover: 发现 PC @ $FOUND"
    PC_HOST="$FOUND"
    return 0
  fi
  log "discover: 未发现 PC（确认 PC 服务端已启动且 Kindle 与 PC 同 wifi）"
  return 1
}

paint_full() {
  [ -s /tmp/dash.png ] || return 1
  eips -g /tmp/dash.png -w gc16 -f 2>>"$LOG" || eips -g /tmp/dash.png 2>>"$LOG" || true
}

paint_light() {
  [ -s /tmp/dash.png ] || return 1
  eips -g /tmp/dash.png -w gl16 2>>"$LOG" || true
}

# ---- 解析 PC 地址（自动发现，适配 PC 常换 wifi）----
PC_HOST=""
# 1) 缓存文件
if [ -f "$PC_IP_FILE" ]; then
  ip=$(cat "$PC_IP_FILE" 2>/dev/null)
  [ -n "$ip" ] && curl -s -m 3 -o /dev/null "http://$ip:$PORT/health" 2>/dev/null && PC_HOST="$ip"
fi
# 2) server.conf 里写的 HOST（若有且可达）
if [ -z "$PC_HOST" ] && [ -n "$HOST" ]; then
  curl -s -m 3 -o /dev/null "http://$HOST:$PORT/health" 2>/dev/null && PC_HOST="$HOST"
fi
# 3) 扫描发现
if [ -z "$PC_HOST" ]; then
  discover_pc || true
fi
if [ -z "$PC_HOST" ]; then
  log "ERROR: 未发现 PC，确认服务端已启动且 Kindle 与 PC 同 wifi"
  exit 1
fi
URL="http://$PC_HOST:$PORT/dash.png?t=$TOKEN"

# ---- 计数（跨重启持久化）----
N=0
[ -f "$COUNT" ] && N=$(cat "$COUNT" 2>/dev/null)
case "$N" in ''|*[!0-9]*) N=0 ;; esac

# ---- 启动：仅防息屏 + 应用前光（不停 framework）----
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>&1
apply_fl
report_battery
log "看板主循环启动(不停 framework) PC=$PC_HOST PORT=$PORT FL=$FL"

last_fetch=0
fail_cnt=0
while true; do
  # 停止检查（最多延迟一个重绘周期生效）
  if [ -f "$STOP" ]; then
    log "检测到 stop 标志，退出主循环（framework 未停，原生界面保留）"
    rm -f "$STOP"
    lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>&1
    exit 0
  fi

  sleep "$REPAINT_INTERVAL"

  # 夜间放慢拉图间隔（重绘仍按 REPAINT_INTERVAL，屏常显压原生 UI）
  hr=$(date +%H)
  if [ "$hr" -ge "$NIGHT_START" ] && [ "$hr" -lt "$NIGHT_END" ]; then
    eff_interval=$NIGHT_INTERVAL
  else
    eff_interval=$DAY_INTERVAL
  fi

  now=$(date +%s)
  do_fetch=0
  if [ "$last_fetch" -eq 0 ] || [ $((now - last_fetch)) -ge "$eff_interval" ]; then
    do_fetch=1
  fi

  if [ "$do_fetch" -eq 1 ]; then
    if fetch; then
      fail_cnt=0
      report_battery
      N=$((N + 1))
      echo "$N" > "$COUNT"
      if [ $((N % FULL_REFRESH_RATE)) -eq 0 ]; then
        paint_full
        log "draw FULL (#$N)"
      else
        paint_light
        log "draw partial (#$N)"
      fi
      last_fetch=$now
    else
      log "WARN: 取图失败，重绘缓存帧"
      paint_light
      fail_cnt=$((fail_cnt + 1))
      # 连续失败 3 次：重新发现 PC（适配 PC 换 wifi / IP 变化）
      if [ "$fail_cnt" -ge 3 ]; then
        log "连续失败 $fail_cnt 次，重新发现 PC..."
        discover_pc || true
        if [ -n "$PC_HOST" ]; then
          URL="http://$PC_HOST:$PORT/dash.png?t=$TOKEN"
          fail_cnt=0
          log "已更新 PC 地址为 $PC_HOST"
        fi
      fi
    fi
  else
    # 未到拉图时机：仅重绘缓存帧压住原生 UI 重绘
    paint_light
  fi
done
