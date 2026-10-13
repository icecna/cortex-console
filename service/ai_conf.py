#!/usr/bin/env python3
"""ai-server 后端配置解析 —— dispatch.sh 与 cortex-console（app.py）共用的唯一实现。

配置文件（INI 风格，值可用引号包裹，# 或 ; 开头为注释）：

    default = <section 名>                        # 只取主文件第一个 default 行
    include /etc/cortex-console/models.d/*.conf   # glob，可多行

    [<name>]
    server-type = "ninfer"                        # ninfer / llama.cpp
    execute = "/path/to/server"                   # 必填，绝对路径
    model   = "/path/to/model"                    # 可选
    model_flag = "-m"                             # ""=位置参数 / "-m"=标志 / "model="=键值融合
    env     = "K=V K2=V2"                         # 可选，exec 前设置（shlex 分割）
    cmdline = "--tp 2 --port 8080"                # 可选，不含 model

同名 section：主文件整体覆盖 include 里的同名 section（不是按 key 合并）。

命令行用法：
    ai_conf.py launch <conf>   # 解析 + 校验 + exec（ai-server 的启动方式）
    ai_conf.py check <conf>    # 只校验 default section 能否启动，不 exec（install.sh 用）
    ai_conf.py dump <conf>     # 打印解析结果（排障用）
"""
import glob
import os
import re
import shlex
import shutil
import sys

DEFAULT_CONF = "/etc/cortex-console/ai-server.conf"

# section 内认识的键（写入 conf 时也按此顺序）
SEC_KEYS = ("server-type", "execute", "model", "model_flag", "env", "cmdline")

_DEFAULT_RE = re.compile(r"^default\s*=", re.I)
_INCLUDE_RE = re.compile(r"^include\s+", re.I)


def unquote(v):
    """去掉外层成对引号；双引号内支持 \\ 转义。"""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1]:
        if v[0] == '"':
            v = v[1:-1]
            out, i = [], 0
            while i < len(v):
                if v[i] == "\\" and i + 1 < len(v):
                    out.append(v[i + 1])
                    i += 2
                else:
                    out.append(v[i])
                    i += 1
            v = "".join(out)
        elif v[0] == "'":
            v = v[1:-1]
    return v


def _collect(path, seen, origin):
    """解析单个文件：先合并 include 的 section，再叠加本文件（本文件整体覆盖同名 section）。"""
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
    for line in lines:  # 先 include（本文件 section 优先）
        m = _INCLUDE_RE.match(line)
        if m:
            for inc in sorted(glob.glob(line[m.end():].strip())):
                secs.update(_collect(inc, seen, origin))
    own = set()
    cur = None
    for line in lines:
        if not line or line[0] in "#;":
            continue
        if _INCLUDE_RE.match(line):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            if cur not in own:
                own.add(cur)
                secs[cur] = {}          # 整 section 覆盖（含 include 带来的同名项）
                origin[cur] = path
            continue
        if cur is None or "=" not in line:
            continue
        k, v = line.split("=", 1)
        secs[cur][k.strip()] = unquote(v)
    return secs


def main_default(path):
    """default 只取主文件第一个 default 行（严格 `default =`，不误匹配 defaults/default_xxx）。"""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#;":
                    continue
                if _DEFAULT_RE.match(line):
                    return unquote(line.split("=", 1)[1])
                if line.startswith("["):
                    break               # section 体内不找 default
    except OSError:
        pass
    return None


def parse_conf(path=None):
    """返回 {"default": name|None, "sections": {name: {...}}, "origin": {name: 定义文件}}。"""
    path = path or DEFAULT_CONF
    origin = {}
    sections = _collect(path, set(), origin)
    return {"default": main_default(path), "sections": sections, "origin": origin}


def build_argv(sec, check_model=True):
    """section → 推理服务 argv（execute + [model_flag] model + cmdline）。
    缺 execute 抛 ValueError；check_model 时 model 不存在抛 FileNotFoundError
    （启动必须校验；管理页渲染命令做编辑基线时不校验，允许路径还没填对）。"""
    argv = [(sec.get("execute") or "").strip()]
    if not argv[0]:
        raise ValueError("section 缺少 execute")
    model = (sec.get("model") or "").strip()
    flag = (sec.get("model_flag") or "").strip()
    if model:
        if check_model and not os.path.isfile(model):
            raise FileNotFoundError("model 文件不存在: %s" % model)
        if flag.endswith("="):
            argv.append(flag + model)             # 键值融合：model=/path
        elif flag:
            argv += [flag, model]                 # 标志：-m /path
        else:
            argv.append(model)                    # 位置参数
    argv += shlex.split(sec.get("cmdline") or "")
    return argv


def env_pairs(sec):
    """section 的 env → [(k, v)]；空 key（如 "=V"）跳过，不炸启动。"""
    out = []
    for kv in shlex.split(sec.get("env") or ""):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        if not k:
            print("WARN: 忽略非法环境变量（key 为空）: %s" % kv, file=sys.stderr)
            continue
        out.append((k, v))
    return out


def cmd_string(sec):
    """section 渲染成完整命令字符串（管理页展示 / 参数表单基线用）。"""
    try:
        argv = build_argv(sec, check_model=False)
    except ValueError:
        return ""
    return " ".join(shlex.quote(a) for a in argv)


def resolve(path=None):
    """定位 default 指向的 section → (name, sec)。失败抛 ValueError。"""
    path = path or DEFAULT_CONF
    if not os.path.isfile(path):
        raise ValueError("配置不存在: %s" % path)
    conf = parse_conf(path)
    name = conf["default"]
    if not name:
        raise ValueError("配置缺少 default 行: %s" % path)
    sec = conf["sections"].get(name)
    if not sec:
        raise ValueError("default 指向的 section [%s] 不存在" % name)
    return name, sec


def main(argv):
    mode = argv[0] if argv else "launch"
    path = argv[1] if len(argv) > 1 else DEFAULT_CONF
    try:
        name, sec = resolve(path)
        args = build_argv(sec)
    except (ValueError, FileNotFoundError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    if mode != "dump":
        # execute 必须存在且可执行（否则 systemd Restart=on-failure 会无限重试刷日志）
        exe = args[0]
        real = exe if os.path.isabs(exe) else (shutil.which(exe) or "")
        if not real or os.path.isdir(real) or not os.access(real, os.X_OK):
            print("ERROR: execute 不存在或不可执行: %s" % exe, file=sys.stderr)
            return 127
    if mode == "check":
        print("OK: [%s] %s" % (name, " ".join(shlex.quote(a) for a in args)))
        return 0
    if mode == "dump":
        print("default = %s" % name)
        for k in SEC_KEYS:
            print("  %-11s = %s" % (k, sec.get(k, "")))
        print("  argv        = %r" % (args,))
        return 0
    for k, v in env_pairs(sec):
        os.environ[k] = v
    print("cortex: 启动 [%s] %s" % (name, " ".join(shlex.quote(a) for a in args)),
          file=sys.stderr)
    if os.environ.get("CORTEX_DRY_RUN") == "1":
        # 预览模式：只打印将执行的命令与环境变量，不真正 exec（测试/排障/安装预检）
        for k, v in env_pairs(sec):
            print("  env: %s=%s" % (k, v), file=sys.stderr)
        print("  argv: %s" % " ".join(repr(a) for a in args), file=sys.stderr)
        return 0
    try:
        os.execvp(args[0], args)
    except FileNotFoundError:
        print("ERROR: 可执行文件不存在: %s" % args[0], file=sys.stderr)
        return 127
    except OSError as e:
        print("ERROR: 启动失败 %s: %s" % (args[0], e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
