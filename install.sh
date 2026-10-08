#!/usr/bin/env bash
# install.sh — Cortex Console + AI Server 安装/卸载
#
# 用法:
#   sudo ./install.sh                          # 完整安装（默认路径）
#   sudo ./install.sh --no-ai-server           # 只装 Cortex Console（监控面板）
#   sudo ./install.sh --admin-password 'xxx'   # 指定初始管理员密码
#   sudo ./install.sh --launcher quasar        # 指定初始默认后端
#   sudo ./install.sh --uninstall              # 卸载（保留配置/日志）
#   sudo ./install.sh --uninstall --purge      # 连安装目录一起删
#   ./install.sh --help
set -euo pipefail

DASH_DIR=/mnt/models/cortex-console
LAUNCHERS_DIR=/mnt/models/ai-launchers
INITIAL_LAUNCHER=""
ADMIN_PW=""
FORCE_CONF=0
NO_AI=0
UNINSTALL=0
PURGE=0
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

STEPS=()
note()  { printf '  [ok]   %s\n' "$*"; STEPS+=("$*"); }
warn()  { printf '  [warn] %s\n' "$*"; }
info()  { printf '  ·      %s\n' "$*"; }
die()   { printf '\n错误: %s\n' "$*" >&2; exit 1; }
on_err() {
  printf '\n安装中断，已完成的步骤:\n' >&2
  for s in "${STEPS[@]:-}"; do [ -n "$s" ] && printf '  - %s\n' "$s" >&2; done
  exit 1
}
trap on_err ERR

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

# ---------- 参数 ----------
while [ $# -gt 0 ]; do
  case "$1" in
    --dash-dir)        DASH_DIR="${2:?}"; shift 2 ;;
    --launchers-dir)   LAUNCHERS_DIR="${2:?}"; shift 2 ;;
    --launcher)        INITIAL_LAUNCHER="${2:?}"; shift 2 ;;
    --admin-password)  ADMIN_PW="${2:?}"; shift 2 ;;
    --force-conf)      FORCE_CONF=1; shift ;;
    --no-ai-server)    NO_AI=1; shift ;;
    --uninstall)       UNINSTALL=1; shift ;;
    --purge)           PURGE=1; shift ;;
    -h|--help)         usage ;;
    *) die "未知参数: $1（--help 查看用法）" ;;
  esac
done

# ---------- 通用检查 ----------
command -v python3 >/dev/null || die "未找到 python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "python3 版本过低: $(python3 -V 2>&1)（需要 >= 3.8）"

have_systemd=0
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  have_systemd=1
fi

# ---------- 卸载 ----------
if [ "$UNINSTALL" = 1 ]; then
  [ "$EUID" = 0 ] || die "卸载需要 root: sudo $0 --uninstall"
  for unit in cortex-console ai-server; do
    [ "$NO_AI" = 1 ] && [ "$unit" = ai-server ] && continue
    f="/etc/systemd/system/${unit}.service"
    if [ -f "$f" ]; then
      systemctl disable --now "$unit" >/dev/null 2>&1 || true
      rm -f "$f"
      note "已停用并删除 ${unit}.service"
    else
      info "未找到 ${unit}.service，跳过"
    fi
  done
  [ "$have_systemd" = 1 ] && systemctl daemon-reload
  if [ "$PURGE" = 1 ]; then
    for d in "$DASH_DIR" "$LAUNCHERS_DIR"; do
      if [ "$NO_AI" = 1 ] && [ "$d" = "$LAUNCHERS_DIR" ]; then continue; fi
      # 真正的风险是用户 shell 的 cwd 在被删目录里（删后 shell 丢失工作目录）；
      # 脚本文件本身在 Linux 上可安全删除（内核保留 inode 至进程退出）
      case "$(pwd)" in
        "$d"|"$d"/*) warn "$d 包含当前工作目录，请先 cd 到其他目录再重跑 --purge，跳过"; continue ;;
      esac
      if [ -d "$d" ]; then
        rm -rf "$d"
        note "已删除目录 $d"
      fi
    done
  else
    info "保留: /etc/ai-server.conf、$DASH_DIR/admin.json、cmd_history.json、journal 日志"
  fi
  echo "卸载完成"
  exit 0
fi

# ---------- 安装前置 ----------
[ "$EUID" = 0 ] || die "需要 root 权限（写 /etc）: sudo $0 $*"
[ -f "$SRC_DIR/app.py" ] || die "找不到 $SRC_DIR/app.py（请在仓库内运行本脚本）"

# 端口预检（仅警告）
if command -v ss >/dev/null; then
  for p in 80 8090; do
    if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"; then
      warn "端口 $p 已被占用，Cortex Console 将跳过该端口"
    fi
  done
fi

# ---------- 部署 cortex-console 代码 ----------
echo "==> 部署 cortex-console 到 $DASH_DIR"
mkdir -p "$DASH_DIR"
for f in app.py index.html admin.html themes.html style.css themes.css \
         dispatch.sh README.md INSTALL.md cortex-console.service.in ai-server.service.in; do
  src="$SRC_DIR/$f" dst="$DASH_DIR/$f"
  [ -f "$src" ] || continue
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    info "跳过（相同）: $f"
  else
    cp -f "$src" "$dst"
    note "已部署: $f"
  fi
done

# ---------- 管理员账号 ----------
if [ -f "$DASH_DIR/admin.json" ]; then
  info "跳过（已存在）: admin.json"
else
  if [ -z "$ADMIN_PW" ]; then
    ADMIN_PW="$(python3 -c 'import secrets,string; cs=string.ascii_letters+string.digits; print("".join(secrets.choice(cs) for _ in range(12)))')"
    pw_file="$DASH_DIR/admin-password.txt"
    printf '初始管理员密码（首次登录后请立即修改并删除本文件）:\n%s\n' "$ADMIN_PW" > "$pw_file"
    chmod 600 "$pw_file"
    note "已生成随机管理员密码，写入 $pw_file (600)"
  else
    note "使用 --admin-password 指定的初始密码"
  fi
  python3 - "$ADMIN_PW" "$DASH_DIR/admin.json" <<'EOF'
import hashlib, json, secrets, sys
pw, path = sys.argv[1], sys.argv[2]
salt = secrets.token_hex(8)
h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1).hex()
with open(path, "w") as f:
    json.dump({"salt": salt, "hash": h}, f)
EOF
  chmod 600 "$DASH_DIR/admin.json"
  note "已生成 admin.json (scrypt 加盐哈希, 600)"
fi

# ---------- ai-server ----------
SRC_LAUNCHERS="$SRC_DIR/../ai-launchers"
if [ "$NO_AI" = 1 ]; then
  info "--no-ai-server: 跳过 launcher / conf / ai-server.service"
elif [ ! -d "$SRC_LAUNCHERS" ]; then
  # 源目录缺失：若目标目录已有 dispatch.sh（就地升级场景）则用目标自身作源，
  # 否则只部署仓库内的 dispatch.sh，首个 launcher 可在管理页“ai-server 配置”里新建
  if [ -x "$LAUNCHERS_DIR/dispatch.sh" ]; then
    SRC_LAUNCHERS="$LAUNCHERS_DIR"
    warn "launcher 源目录不存在，改用已安装的 $LAUNCHERS_DIR 自身"
  else
    mkdir -p "$LAUNCHERS_DIR"
    if [ -f "$SRC_DIR/dispatch.sh" ]; then
      cp -f "$SRC_DIR/dispatch.sh" "$LAUNCHERS_DIR/dispatch.sh"
      chmod +x "$LAUNCHERS_DIR/dispatch.sh"
      note "已部署 dispatch.sh（无 launcher 源，首个 launcher 请在管理页新建）"
    fi
    warn "launcher 源目录不存在（$SRC_LAUNCHERS），跳过 conf / ai-server.service"
    NO_AI=1
  fi
fi
if [ "$NO_AI" = 1 ]; then
  :  # 已在上方输出说明
else
  echo "==> 部署 launcher 到 $LAUNCHERS_DIR"
  mkdir -p "$LAUNCHERS_DIR"
  for f in "$SRC_LAUNCHERS"/*.sh "$SRC_LAUNCHERS"/README.md; do
    [ -f "$f" ] || continue
    dst="$LAUNCHERS_DIR/$(basename "$f")"
    if [ -f "$dst" ] && cmp -s "$f" "$dst"; then
      info "跳过（相同）: $(basename "$f")"
    else
      cp -f "$f" "$dst"
      [ "${f##*.}" = sh ] && chmod +x "$dst"
      note "已部署: $(basename "$f")"
    fi
  done
  [ -x "$LAUNCHERS_DIR/dispatch.sh" ] || die "dispatch.sh 缺失或不可执行"
  for f in "$LAUNCHERS_DIR"/*.sh; do bash -n "$f"; done
  note "launcher 脚本 bash -n 语法校验通过"

  # /etc/ai-server.conf
  if [ -n "$INITIAL_LAUNCHER" ]; then
    [ -f "$SRC_LAUNCHERS/${INITIAL_LAUNCHER}.sh" ] \
      || die "launcher 脚本不存在: ${INITIAL_LAUNCHER}.sh"
  else
    # 未指定默认后端：取排序后的第一个脚本，保证新装即可用
    INITIAL_LAUNCHER="$(for f in "$SRC_LAUNCHERS"/*.sh; do
      [ "$(basename "$f")" = dispatch.sh ] && continue
      basename "$f" .sh
    done | sort | head -1)"
    [ -n "$INITIAL_LAUNCHER" ] || die "launcher 目录为空，无法确定默认后端"
    info "未指定 --launcher，默认使用: $INITIAL_LAUNCHER"
  fi
  CONF=/etc/ai-server.conf
  gen_conf() {
    {
      echo "# /etc/ai-server.conf — AI Server 后端选择"
      echo "#"
      echo "# 用法:"
      echo "#   1. 修改下方 LAUNCHER 值（只保留一个有效行，另一行注释掉）"
      echo "#   2. systemctl restart ai-server"
      echo "#"
      echo "# 可选值:"
      local name
      for f in "$LAUNCHERS_DIR"/*.sh; do
        [ "$(basename "$f")" = dispatch.sh ] && continue
        name="$(basename "$f" .sh)"
        case "$name" in
          ninfer-v100-duo)      echo "#   $name   NInfer V100 Duo (TP2, MTP, 端口 8080)" ;;
          ninfer-v100x2)        echo "#   $name   NInfer V100X2 (TP2, NVFP4, MTP3, 端口 8080)" ;;
          ninfer-v100x2-dflash) echo "#   $name   NInfer V100X2 (TP2, NVFP4, DFlash2x7, 98304 capacity, 端口 8080)" ;;
          quasar)               echo "#   $name   NInfer V100X2 QUASAR 模型 (TP2, NVFP4, MTP3, 端口 8080)" ;;
          llama-server)         echo "#   $name   llama.cpp (TP2, MTP, 端口 8080)" ;;
          *)                    echo "#   $name" ;;
        esac
      done
      echo ""
      for f in "$LAUNCHERS_DIR"/*.sh; do
        [ "$(basename "$f")" = dispatch.sh ] && continue
        name="$(basename "$f" .sh)"
        if [ "$name" = "$INITIAL_LAUNCHER" ]; then
          echo "LAUNCHER=$name"
        else
          echo "#LAUNCHER=$name"
        fi
      done
    }
  }
  if [ -f "$CONF" ] && [ "$FORCE_CONF" = 0 ]; then
    info "跳过（已存在，--force-conf 可覆盖）: $CONF"
  else
    [ -f "$CONF" ] && cp -f "$CONF" "$CONF.bak"
    gen_conf > "$CONF"
    note "已生成 $CONF$([ -n "$INITIAL_LAUNCHER" ] && echo "（默认后端: $INITIAL_LAUNCHER）")"
  fi

  # systemd unit：ai-server 部分（仅 NO_AI=0）
  if [ "$have_systemd" = 1 ]; then
    install_unit() {  # $1=模板 $2=unit名 $3=是否安装
      local tpl="$1" unit="$2"
      [ "$3" = 1 ] || return 0
      local tmp; tmp="$(mktemp)"
      sed -e "s|__DASH_DIR__|$DASH_DIR|g" \
          -e "s|__LAUNCHERS_DIR__|$LAUNCHERS_DIR|g" \
          -e "s|__WORK_DIR__|$(dirname "$LAUNCHERS_DIR")|g" \
          "$SRC_DIR/$tpl" > "$tmp"
      local dst="/etc/systemd/system/${unit}.service"
      if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
        info "跳过（相同）: ${unit}.service"
      elif [ -f "$dst" ]; then
        warn "${unit}.service 已存在且内容不同，不覆盖（diff 如下，如需更新请手工合并):"
        diff -u "$dst" "$tmp" | sed 's/^/    /' || true
      else
        cp -f "$tmp" "$dst"
        note "已安装 ${unit}.service"
      fi
      rm -f "$tmp"
    }
    install_unit ai-server.service.in ai-server 1
  fi
fi

# ---------- systemd unit：cortex-console（始终安装） ----------
if [ "$have_systemd" = 1 ]; then
  install_unit() {  # 同上，重新定义作用域
    local tpl="$1" unit="$2"
    local tmp; tmp="$(mktemp)"
    sed -e "s|__DASH_DIR__|$DASH_DIR|g" \
        -e "s|__LAUNCHERS_DIR__|$LAUNCHERS_DIR|g" \
        -e "s|__WORK_DIR__|$(dirname "$LAUNCHERS_DIR")|g" \
        "$SRC_DIR/$tpl" > "$tmp"
    local dst="/etc/systemd/system/${unit}.service"
    if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
      info "跳过（相同）: ${unit}.service"
    elif [ -f "$dst" ]; then
      warn "${unit}.service 已存在且内容不同，不覆盖（diff 如下，如需更新请手工合并):"
      diff -u "$dst" "$tmp" | sed 's/^/    /' || true
    else
      cp -f "$tmp" "$dst"
      note "已安装 ${unit}.service"
    fi
    rm -f "$tmp"
  }
  install_unit cortex-console.service.in cortex-console
  systemctl daemon-reload
  systemctl enable --now cortex-console
  note "cortex-console.service 已启用并启动"
  if [ "$NO_AI" = 1 ]; then
    info "跳过 ai-server.service（--no-ai-server）"
  else
    systemctl enable --now ai-server
    note "ai-server.service 已启用并启动"
  fi
else
  warn "未检测到 systemd，跳过 unit 安装。手动启动方式:"
  echo "    cd $DASH_DIR && nohup python3 app.py > /var/log/cortex-console.log 2>&1 &"
  [ "$NO_AI" = 1 ] || echo "    $LAUNCHERS_DIR/dispatch.sh &"
fi

# ---------- 完成 ----------
echo
echo "安装完成"
echo "  监控页:   http://<ip>:80  (或 :8090)"
echo "  管理页:   http://<ip>:80/admin"
if [ "$NO_AI" = 0 ]; then
echo "  查看服务: systemctl status ai-server cortex-console"
echo "  切换后端: admin 页 → 服务控制 → 后端切换"
fi
if [ -f "$DASH_DIR/admin-password.txt" ]; then
echo "  初始密码: 见 $DASH_DIR/admin-password.txt（登录后请修改密码并删除该文件）"
fi