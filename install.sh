#!/usr/bin/env bash
# install.sh — Cortex Console + AI Server 安装/卸载
#
# 用法:
#   sudo ./install.sh                          # 完整安装（默认路径）
#   sudo ./install.sh --no-ai-server           # 只装 Cortex Console（监控面板）
#   sudo ./install.sh --admin-password 'xxx'   # 指定初始管理员密码
#   sudo ./install.sh --launcher quasar        # 指定初始默认 section（conf 中的 section 名）
#   sudo ./install.sh --uninstall              # 卸载（保留配置/日志）
#   sudo ./install.sh --uninstall --purge      # 连安装目录一起删
#   ./install.sh --help
set -euo pipefail

DASH_DIR=/mnt/models/cortex-console
# 辅助目录：旧版 launcher 脚本所在（升级时迁移成 section）、unit 的 WorkingDirectory、
# 请求日志输出目录。ai-server 本身的启动入口在 $DASH_DIR/service/dispatch.sh。
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
if [ -n "$INITIAL_LAUNCHER" ] && ! [[ "$INITIAL_LAUNCHER" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  die "非法 section 名: $INITIAL_LAUNCHER（仅允许字母/数字/._-，且以字母或数字开头）"
fi

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
    info "保留: /etc/cortex-console/（ai-server.conf + models.d）、$DASH_DIR/admin.json、cmd_history.json、journal 日志"
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
CODE_UPDATED=0
for f in app.py index.html admin.html themes.html style.css themes.css \
         service/dispatch.sh service/ai_conf.py README.md INSTALL.md \
         cortex-console.service.in ai-server.service.in; do
  src="$SRC_DIR/$f" dst="$DASH_DIR/$f"
  [ -f "$src" ] || continue
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    info "跳过（相同）: $f"
  else
    mkdir -p "$(dirname "$dst")"
    cp -f "$src" "$dst"
    CODE_UPDATED=1
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

# systemd unit 安装（$1=模板 $2=unit名 $3=是否安装 $4=旧版本自动升级）
# $4=1 时：已存在的 unit 若 ExecStart 指向本仓库旧版 dispatch.sh（即本项目的旧安装），
# 备份后直接覆盖；用户自己改过的 unit 仍然只 diff 不碰。
install_unit() {
  local tpl="$1" unit="$2"
  [ "${3:-0}" = 1 ] || return 0
  local force="${4:-0}"
  local tmp; tmp="$(mktemp)"
  sed -e "s|__DASH_DIR__|$DASH_DIR|g" \
      -e "s|__LAUNCHERS_DIR__|$LAUNCHERS_DIR|g" \
      -e "s|__WORK_DIR__|$(dirname "$LAUNCHERS_DIR")|g" \
      "$SRC_DIR/$tpl" > "$tmp"
  local dst="/etc/systemd/system/${unit}.service"
  if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
    info "跳过（相同）: ${unit}.service"
  elif [ -f "$dst" ] && [ "$force" = 1 ] && grep -qE '^ExecStart=.*dispatch\.sh$' "$dst" \
       && diff <(grep -v '^ExecStart=' "$dst") <(grep -v '^ExecStart=' "$tmp") >/dev/null; then
    # 旧入口是本仓库旧版 dispatch.sh，且除 ExecStart 外其余行与新模板完全一致
    # → 确认是“本项目的旧安装”；用户自改过（Environment/ExecStartPre/资源限制等）不进这个分支
    cp -f "$dst" "$dst.pre-section.bak"
    cp -f "$tmp" "$dst"
    note "已升级 ${unit}.service（旧 ExecStart 指向 dispatch.sh；备份 $dst.pre-section.bak）"
  elif [ -f "$dst" ]; then
    warn "${unit}.service 已存在且内容不同，不覆盖（diff 如下，如需更新请手工合并):"
    diff -u "$dst" "$tmp" | sed 's/^/    /' || true
  else
    cp -f "$tmp" "$dst"
    note "已安装 ${unit}.service"
  fi
  rm -f "$tmp"
}

# ---------- ai-server ----------
CONF_DIR=/etc/cortex-console
CONF="$CONF_DIR/ai-server.conf"
if [ "$NO_AI" = 1 ]; then
  info "--no-ai-server: 跳过 conf / ai-server.service"
else
  echo "==> 部署后端配置"
  mkdir -p "$LAUNCHERS_DIR" "$CONF_DIR/models.d"
  bash -n "$DASH_DIR/service/dispatch.sh"
  note "service/dispatch.sh bash -n 语法校验通过"

  # --launcher 必须在某个来源里真的存在对应 section：先扇描再开工，
  # 避免“旧 conf 已改名 / 新 conf 已写”才发现参数无效、留下半完成态
  if [ -n "$INITIAL_LAUNCHER" ]; then
    found=0
    for f in "$CONF" "$CONF_DIR"/models.d/*.conf "$SRC_DIR"/conf.sample \
             "$SRC_DIR"/conf.sample.d/*.conf /etc/ai-server.conf; do
      [ -f "$f" ] || continue
      if grep -q "^\[${INITIAL_LAUNCHER}\]" "$f" \
         || grep -qE "^(#[[:space:]]*)?LAUNCHER=${INITIAL_LAUNCHER}$" "$f"; then
        found=1; break
      fi
    done
    [ "$found" = 1 ] \
      || die "--launcher $INITIAL_LAUNCHER 没有对应的 section（conf / models.d / conf.sample.d / 旧 conf 里都没有）"
  fi

  # 旧版（LAUNCHER 模式）配置自动迁移：/etc/ai-server.conf → section 配置
  LEGACY_CONF=/etc/ai-server.conf
  MIGRATED=0
  if [ ! -f "$CONF" ] && [ -f "$LEGACY_CONF" ]; then
    echo "==> 检测到旧版配置 $LEGACY_CONF（LAUNCHER 模式），迁移到 section 配置"
    if python3 - "$LEGACY_CONF" "$SRC_DIR/conf.sample" "$CONF" "$CONF_DIR/models.d" "$LAUNCHERS_DIR" <<'EOF'
import os, re, sys
legacy, sample, conf, models_d, launchers = sys.argv[1:6]
os.makedirs(models_d, exist_ok=True)
names, default = [], None
for line in open(legacy, errors="replace"):
    s = line.strip()
    m = re.match(r"^(#\s*)?LAUNCHER=([A-Za-z0-9._-]+)$", s)
    if not m:
        continue
    name = m.group(2)
    if name not in names:
        names.append(name)
    if default is None and not m.group(1):
        default = name
if not names:
    sys.exit("旧 conf 里没找到 LAUNCHER 行")
if not default:
    default = names[0]
with open(sample) as f:
    text = f.read()
text = re.sub(r"(?m)^default\s*=.*$", "default = %s" % default, text, count=1)
with open(conf, "w") as f:
    f.write(text)
os.chmod(conf, 0o644)
def q(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
moved = 0
for name in names:
    script = os.path.join(launchers, name + ".sh")
    if not os.path.isfile(script):
        print("  跳过 %s：launcher 脚本不存在 %s" % (name, script))
        continue
    stype = "llama.cpp" if ("llama" in name.lower() or
                            ("llama" in open(script, errors="replace").read().lower())) else "ninfer"
    dst = os.path.join(models_d, name + ".conf")
    with open(dst, "w") as f:
        f.write("# 从旧版 launcher 脚本 %s 迁移而来（launcher 脚本本身就是启动入口）\n"
                "# 要改成直连推理二进制：在管理页「启动参数」里改主程序路径\n"
                "[%s]\nserver-type = %s\nexecute = %s\n" % (script, name, q(stype), q(script)))
    os.chmod(dst, 0o644)
    moved += 1
    print("  已迁移 section: %s -> %s" % (name, dst))
if not moved:
    sys.exit("旧 conf 里的 LAUNCHER 均无对应脚本，无法迁移")
os.rename(legacy, legacy + ".migrated")
print("  旧 conf 已改名为 %s.migrated（不再被读取）" % legacy)
print("  default = %s" % default)
EOF
    then
      MIGRATED=1
      note "旧配置迁移完成（原文件保留为 $LEGACY_CONF.migrated）"
    else
      warn "旧配置自动迁移失败，改用 conf.sample 生成新配置（旧文件 $LEGACY_CONF 原样保留）"
    fi
  fi

  # /etc/cortex-console/ai-server.conf（主文件，从 conf.sample 生成）+ models.d 示例 section
  if [ -f "$CONF" ] && [ "$MIGRATED" = 0 ] && [ "$FORCE_CONF" = 0 ]; then
    info "跳过（已存在，--force-conf 可覆盖）: $CONF"
    [ -n "$INITIAL_LAUNCHER" ] && warn "--launcher $INITIAL_LAUNCHER 未生效（conf 已存在，需 --force-conf 覆盖）"
  else
    [ -f "$CONF" ] && [ "$MIGRATED" = 0 ] && cp -f "$CONF" "$CONF.bak"
    if [ ! -f "$SRC_DIR/conf.sample" ]; then
      die "仓库内找不到 conf.sample"
    fi
    if [ "$MIGRATED" = 0 ]; then
      cp -f "$SRC_DIR/conf.sample" "$CONF"
      chmod 644 "$CONF"
      note "已生成 $CONF（默认 section: ${INITIAL_LAUNCHER:-example-ninfer}）"
    fi
  fi
  # 示例 section 骨架部署到 models.d/（已存在不覆盖；旧配置迁移过则不塞示例）
  if [ "$MIGRATED" = 0 ]; then
    for f in "$SRC_DIR"/conf.sample.d/*.conf; do
      [ -f "$f" ] || continue
      dst="$CONF_DIR/models.d/$(basename "$f")"
      if [ -f "$dst" ]; then
        info "跳过（已存在）: $dst"
      else
        cp -f "$f" "$dst"
        chmod 644 "$dst"
        note "已部署示例 section: $dst"
      fi
    done
  fi
  [ -d "$CONF_DIR/models.d" ] && note "models.d 目录就绪（每个后端一个 <name>.conf，自动 include）"

  # 统一把 default 设为 --launcher 指定的 section（新建/迁移两条路径都适用，幂等）
  if [ -n "$INITIAL_LAUNCHER" ] && ! grep -q "^default = ${INITIAL_LAUNCHER}$" "$CONF"; then
    sed -i "s/^default[[:space:]]*=.*/default = ${INITIAL_LAUNCHER}/" "$CONF"
    grep -q "^default = ${INITIAL_LAUNCHER}$" "$CONF" \
      || die "改写 default 失败（conf 格式可能变化）: $CONF"
    note "default 设为 $INITIAL_LAUNCHER"
  fi

  # systemd unit：ai-server 部分（仅 NO_AI=0；旧版本 unit 自动升级）
  if [ "$have_systemd" = 1 ]; then
    install_unit ai-server.service.in ai-server 1 1
  fi
fi

# ---------- systemd unit：cortex-console（始终安装） ----------
if [ "$have_systemd" = 1 ]; then
  install_unit cortex-console.service.in cortex-console 1
  systemctl daemon-reload
  systemctl enable --now cortex-console
  note "cortex-console.service 已启用并启动"
  # 代码有更新且服务在运行 → 重启加载新代码（否则重跑安装后仍是旧代码）
  if [ "$CODE_UPDATED" = 1 ] && systemctl is-active --quiet cortex-console; then
    systemctl restart cortex-console
    note "代码有更新，cortex-console 已重启以加载新代码"
  fi
  if [ "$NO_AI" = 1 ]; then
    info "跳过 ai-server.service（--no-ai-server）"
  else
    # 预检 default section（execute/model 路径真实存在）才 start：
    # 否则 Restart=on-failure 会每 5s 重试刷日志，新机器装完就是 crash loop
    if precheck_out="$(python3 "$DASH_DIR/service/ai_conf.py" check "$CONF" 2>&1)"; then
      systemctl enable --now ai-server
      note "ai-server.service 已启用并启动"
    else
      systemctl enable ai-server
      warn "default section 配置无效，未启动 ai-server（否则会 crash loop 刷日志）"
      warn "  预检: ${precheck_out}"
      warn "  请在管理页「启动参数 → 编辑配置」填真实 execute/model 路径，再 systemctl start ai-server"
    fi
  fi
else
  warn "未检测到 systemd，跳过 unit 安装。手动启动方式:"
  echo "    cd $DASH_DIR && nohup python3 app.py > /var/log/cortex-console.log 2>&1 &"
  [ "$NO_AI" = 1 ] || echo "    $DASH_DIR/service/dispatch.sh &"
  [ "$CODE_UPDATED" = 1 ] && warn "代码有更新，请手动重启 cortex-console 进程以加载新代码"
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