#!/usr/bin/env bash
# Cortex Console — ai-server 统一启动入口
#
# 读取 /etc/cortex-console/ai-server.conf 的 default 指向的 section，exec 对应推理服务。
# 配置格式（INI 风格，值可选引号包裹）：
#   # 注释
#   default = <section 名>
#
#   [<name>]
#   execute = "推理服务可执行文件"     # 必填
#   model   = "模型路径"               # 可选
#   model_flag = "-m"                  # 可选：""=位置参数 / "-m"=标志 / "model="=键值融合
#   env     = "K=V K2=V2"              # 可选：exec 前设置的环境变量（空格分隔）
#   cmdline = "其余参数 ..."            # 可选
#
#   include /etc/cortex-console/models.d/*.conf   # 支持 glob，可多行
#
# 解析用内嵌 python3（与 dashboard 同一套逻辑），保证 include/section 处理稳健。
set -euo pipefail

CONF="/etc/cortex-console/ai-server.conf"
if [[ ! -f "${CONF}" ]]; then
    echo "ERROR: 配置不存在: ${CONF}" >&2
    exit 1
fi

exec python3 - "${CONF}" <<'PYEOF'
import glob, os, shlex, sys

def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1]:
        if v[0] == '"':
            v = v[1:-1]
            out, i = [], 0
            while i < len(v):
                if v[i] == "\\" and i + 1 < len(v):
                    out.append(v[i + 1]); i += 2
                else:
                    out.append(v[i]); i += 1
            v = "".join(out)
        elif v[0] == "'":
            v = v[1:-1]
    return v

def collect(path, seen):
    """解析单个文件：先合并 include 的 section，再叠加本文件（本文件优先）。"""
    secs = {}
    real = os.path.realpath(path)
    if real in seen:
        return secs
    seen.add(real)
    try:
        with open(path) as f:
            lines = [l.strip() for l in f]
    except OSError as e:
        print("ERROR: 无法读取配置 %s: %s" % (path, e), file=sys.stderr)
        return secs
    for line in lines:
        if line.lower().startswith("include "):
            for inc in sorted(glob.glob(line[8:].strip())):
                secs.update(collect(inc, seen))
    cur = None
    for line in lines:
        if not line or line[0] in "#;":
            continue
        if line.lower().startswith("include "):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            secs.setdefault(cur, {})
            continue
        if cur is None or "=" not in line:
            continue
        k, v = line.split("=", 1)
        secs[cur][k.strip()] = _unquote(v)
    return secs

def main_default(path):
    """default 只取主文件第一个 default 行。"""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#;":
                    continue
                if line.lower().startswith("default"):
                    if "=" in line:
                        return _unquote(line.split("=", 1)[1])
                    break
    except OSError:
        pass
    return None

conf = sys.argv[1]
sections = collect(conf, set())
default = main_default(conf)
if not default:
    print("ERROR: 配置缺少 default 行", file=sys.stderr); sys.exit(1)
sec = sections.get(default)
if not sec:
    print("ERROR: default 指向的 section [%s] 不存在" % default, file=sys.stderr); sys.exit(1)
execute = sec.get("execute", "").strip()
if not execute:
    print("ERROR: section [%s] 缺少 execute" % default, file=sys.stderr); sys.exit(1)

model = sec.get("model", "").strip()
flag = sec.get("model_flag", "").strip()
if model and not os.path.isfile(model):
    print("ERROR: model 文件不存在: %s" % model, file=sys.stderr)
    sys.exit(1)
argv = [execute]
if model:
    if flag:
        if flag.endswith("="):
            argv.append(flag + model)            # 键值融合：model=/path
        else:
            argv.append(flag); argv.append(model)  # 标志：-m /path
    else:
        argv.append(model)                         # 位置参数
argv += shlex.split(sec.get("cmdline", ""))

for kv in shlex.split(sec.get("env", "")):
    if "=" in kv:
        k, v = kv.split("=", 1)
        os.environ[k] = v

print("cortex: 启动 [%s] %s" % (default, " ".join(argv)), file=sys.stderr)
if os.environ.get("CORTEX_DRY_RUN") == "1":
    # 预览模式：打印将执行的命令与环境变量，不真正 exec（用于测试/排障）
    for kv in shlex.split(sec.get("env", "")):
        if "=" in kv:
            print("  env: " + kv, file=sys.stderr)
    print("  argv: " + " ".join(repr(a) for a in argv), file=sys.stderr)
    sys.exit(0)
try:
    os.execvp(argv[0], argv)
except FileNotFoundError:
    print("ERROR: 可执行文件不存在: %s" % argv[0], file=sys.stderr)
    sys.exit(127)
except OSError as e:
    print("ERROR: 启动失败 %s: %s" % (argv[0], e), file=sys.stderr)
    sys.exit(1)
PYEOF