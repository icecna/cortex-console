#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llama.cpp + 硬件监控 Dashboard（纯标准库，无依赖）

后台每 2 秒采样一次：
  - llama-server /metrics  (吞吐、MTP 投机解码接受率)
  - llama-server /props    (模型参数)
  - nvidia-smi             (GPU 温度/利用率/显存/功耗/ECC)
  - /proc                  (CPU/内存/磁盘 IO/进程)
  - EDAC                   (系统内存 ECC)
浏览器访问 http://<ip>:80 或 :8090
"""
import glob
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORTS = (80, 8090)
LLAMA = "http://127.0.0.1:8080"
POLL = 2.0        # 硬件采样间隔
POLL_LLAMA = 0.5  # llama metrics 采样间隔（速度类指标更跟手）
HISTORY_LEN = 300  # 10 分钟 @ 2s
NPROC = os.cpu_count()
BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 基础读取 ----------------

def _run(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""

def _get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None

def parse_prom(text):
    scalars, labeled = {}, {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, val = line.rpartition(" ")
        try:
            v = float(val)
        except ValueError:
            continue
        if "{" in name:
            base, lab = name.split("{", 1)
            kv = {}
            for pair in lab.rstrip("}").split(","):
                if "=" in pair:
                    k, vv = pair.split("=", 1)
                    kv[k.strip()] = vv.strip().strip('"')
            key = tuple(sorted(kv.items()))
            labeled.setdefault(base, {})[key] = v
        else:
            scalars[name] = v
    return scalars, labeled

# ---------------- CPU ----------------

_cpu_prev = None

def cpu_usage():
    global _cpu_prev
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = list(map(int, parts[1:]))
    except Exception:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    usage = None
    if _cpu_prev is not None:
        dt, di = total - _cpu_prev[1], idle - _cpu_prev[0]
        if dt > 0:
            usage = max(0.0, min(100.0, 100.0 * (dt - di) / dt))
    _cpu_prev = (idle, total)
    return usage

# ---------------- 内存 ----------------

def mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0])  # kB
    total = info["MemTotal"] / 1048576
    avail = info.get("MemAvailable", 0) / 1048576
    used = total - avail
    st = info.get("SwapTotal", 0) / 1048576
    sf = info.get("SwapFree", 0) / 1048576
    return {
        "total_gb": round(total, 1),
        "used_gb": round(used, 1),
        "avail_gb": round(avail, 1),
        "pct": round(100 * used / total, 1) if total else 0,
        "swap_used_gb": round(st - sf, 1),
        "swap_total_gb": round(st, 1),
    }

# ---------------- GPU ----------------

GPU_Q = ("index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,"
         "power.draw,clocks.sm,ecc.mode.current,"
         "ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total,"
         "ecc.errors.corrected.aggregate.total,ecc.errors.uncorrected.aggregate.total")

def _i(x):
    try:
        return int(x)
    except (ValueError, TypeError):
        return None

def _f(x):
    try:
        return float(x)
    except (ValueError, TypeError):
        return None

def gpus():
    out = _run(["nvidia-smi", "--query-gpu=" + GPU_Q, "--format=csv,noheader,nounits"])
    res = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 13:
            continue
        res.append({
            "index": _i(p[0]), "name": p[1],
            "temp": _i(p[2]), "util": _i(p[3]),
            "vram_used": _i(p[4]), "vram_total": _i(p[5]),
            "power": _f(p[6]), "clock": _i(p[7]),
            "ecc_mode": p[8],
            "ecc_ce_vol": _i(p[9]), "ecc_ue_vol": _i(p[10]),
            "ecc_ce_agg": _i(p[11]), "ecc_ue_agg": _i(p[12]),
        })
    return res

# ---------------- 系统 ECC (EDAC) ----------------

def edac():
    ce = ue = 0
    found = False
    for d in glob.glob("/sys/devices/system/edac/mc/mc*"):
        found = True
        for name, is_ce in (("ce_count", True), ("ue_count", False)):
            try:
                with open(os.path.join(d, name)) as f:
                    v = int(f.read().strip())
                if is_ce:
                    ce += v
                else:
                    ue += v
            except Exception:
                pass
    # 没有内存控制器（VM）时返回 None，前端显示 N/A
    return {"ce": ce, "ue": ue} if found else None

# ---------------- 磁盘 ----------------

_io_prev = None

def disks():
    global _io_prev
    io = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                name = p[2]
                if re.match(r"^(sd[a-z]+|nvme\d+n\d+)$", name):
                    io[name] = (int(p[5]), int(p[9]))  # 512B 扇区
    except Exception:
        pass
    read_mbps = write_mbps = 0.0
    now = time.monotonic()
    if _io_prev is not None:
        dt = now - _io_prev[0]
        if dt > 0:
            for name, (r, w) in io.items():
                pr, pw = _io_prev[1].get(name, (r, w))
                read_mbps += max(0, r - pr) * 512 / dt / 1048576
                write_mbps += max(0, w - pw) * 512 / dt / 1048576
    _io_prev = (now, io)

    mounts, seen = [], set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) < 3 or p[2] not in ("ext4", "xfs", "btrfs"):
                    continue
                mp = p[1]
                if mp in seen or mp.startswith(("/proc", "/sys", "/dev")):
                    continue
                seen.add(mp)
                try:
                    st = os.statvfs(mp)
                    size = st.f_frsize * st.f_blocks / 1073741824
                    used = size - st.f_frsize * st.f_bfree / 1073741824
                    mounts.append({
                        "mount": mp, "dev": p[0],
                        "size_gb": round(size, 1), "used_gb": round(used, 1),
                        "pct": round(100 * used / size, 1) if size else 0,
                    })
                except Exception:
                    pass
    except Exception:
        pass
    mounts.sort(key=lambda m: m["mount"])
    return {"mounts": mounts, "read_mbps": round(read_mbps, 2), "write_mbps": round(write_mbps, 2)}

# ---------------- 慢速后台采集（避免每次采样都跑） ----------------

_disk_temp = {}

def _disk_temp_worker():
    global _disk_temp
    while True:
        temps = {}
        for dev in sorted(glob.glob("/dev/sd?") + glob.glob("/dev/nvme*n1")):
            if not os.path.exists(dev):
                continue
            out = _run(["smartctl", "-a", dev], timeout=10)
        m = re.search(r"(?:Temperature:|Current Drive Temperature:)\s*([0-9]+)", out)
        # QEMU 虚拟盘报 0，视为不可用
        if m and int(m.group(1)) > 0:
            temps[dev] = int(m.group(1))
        _disk_temp = temps
        time.sleep(60)

_cpu_temp = None

def _cpu_temp_worker():
    global _cpu_temp
    while True:
        out = _run(["sensors"], timeout=5)
        m = re.search(r"(?:Package id 0|Tctl|Tdie)\s*:?\s+.*?([0-9]+(?:\.[0-9]+)?)°C", out)
        _cpu_temp = float(m.group(1)) if m else None
        time.sleep(30)

# ---------------- llama-server 进程 ----------------

_proc_prev = None

def llama_proc():
    global _proc_prev
    pid = None
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % p) as f:
                if f.read().strip() == "llama-server":
                    pid = int(p)
                    break
        except Exception:
            pass
    if pid is None:
        _proc_prev = None
        return None
    try:
        with open("/proc/%d/stat" % pid) as f:
            st = f.read().rsplit(")", 1)[1].split()
        # rsplit(")") 后 st[0]=state，即 st[i] = 第 i+3 个字段
        ticks = int(st[11]) + int(st[12])  # utime + stime
        rss_kb = int(st[21]) * 4           # rss（页数 × 4KB）
        starttime = int(st[19])            # starttime（时钟刻度）
    except Exception:
        return {"pid": pid}
    now = time.monotonic()
    cpu = None
    if _proc_prev is not None:
        dt = now - _proc_prev[1]
        if dt > 0:
            cpu = max(0.0, (ticks - _proc_prev[0]) / 100.0 / dt * 100.0)
    _proc_prev = (ticks, now)
    up = None
    try:
        with open("/proc/uptime") as f:
            boot = float(f.read().split()[0])
        up = max(0.0, boot - starttime / 100.0)
    except Exception:
        pass
    cmdline = None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode().strip()
    except Exception:
        pass
    return {
        "pid": pid,
        "cpu_pct": round(cpu, 1) if cpu is not None else None,
        "rss_gb": round(rss_kb / 1048576, 2),
        "uptime_s": round(up) if up is not None else None,
        "cmdline": cmdline,
    }

# ---------------- llama-server metrics / props ----------------

_llama_prev = None
_gen_hold = None  # (tps, ts) 最近一次非零生成速度，用于结束后短暂保留显示

def llama_metrics():
    global _llama_prev, _gen_hold
    text = _get(LLAMA + "/metrics")
    if text is None:
        _llama_prev = None
        return None
    s, l = parse_prom(text)
    now = time.monotonic()  # 单调时钟，不受 NTP 校时跳变影响
    prev = _llama_prev
    _llama_prev = (now, s)
    gen_tps = prompt_tps = mtp_inst = None
    if prev is not None:
        dt = now - prev[0]
        # dt 异常偏小（时钟跳变等）时跳过本窗口速率，避免假尖峰
        if dt >= 0.15:
            # max(0, ...)：llama-server 重启后计数器归零，避免负值
            gen_tps = max(0.0, s.get("llamacpp:tokens_predicted_total", 0)
                          - prev[1].get("llamacpp:tokens_predicted_total", 0)) / dt
            prompt_tps = max(0.0, s.get("llamacpp:prompt_tokens_total", 0)
                             - prev[1].get("llamacpp:prompt_tokens_total", 0)) / dt
            dd = (s.get("llamacpp:spec_decode_num_draft_tokens_total", 0)
                  - prev[1].get("llamacpp:spec_decode_num_draft_tokens_total", 0))
            da = (s.get("llamacpp:spec_decode_num_accepted_tokens_total", 0)
                  - prev[1].get("llamacpp:spec_decode_num_accepted_tokens_total", 0))
            if dd > 0:
                mtp_inst = 100.0 * da / dd
    if gen_tps is not None and gen_tps > 0:
        _gen_hold = (gen_tps, now)
    hold = _gen_hold[0] if _gen_hold is not None and now - _gen_hold[1] <= 5.0 else None
    drafts = s.get("llamacpp:spec_decode_num_drafts_total", 0)
    draft_tokens = s.get("llamacpp:spec_decode_num_draft_tokens_total", 0)
    accepted = s.get("llamacpp:spec_decode_num_accepted_tokens_total", 0)
    pos_acc = {}
    for key, v in l.get("llamacpp:spec_decode_num_accepted_tokens_per_pos_total", {}).items():
        p = dict(key).get("position")
        if p is not None and drafts > 0:
            pos_acc[p] = round(100.0 * v / drafts, 1)
    return {
        "gen_tps": round(gen_tps, 1) if gen_tps is not None else None,
        "gen_hold": round(hold, 1) if hold is not None else None,
        "prompt_tps": round(prompt_tps, 1) if prompt_tps is not None else None,
        "gen_tps_avg": s.get("llamacpp:predicted_tokens_seconds"),
        "mtp": {
            "accept": round(100.0 * accepted / draft_tokens, 1) if draft_tokens else None,
            "accept_inst": round(mtp_inst, 1) if mtp_inst is not None else None,
            "pos": pos_acc,
            "drafts": drafts,
            "draft_tokens": draft_tokens,
            "accepted": accepted,
        },
        "tokens": {
            "prompt": s.get("llamacpp:prompt_tokens_total", 0),
            "cached": s.get("llamacpp:prompt_tokens_cached_total", 0),
            "predicted": s.get("llamacpp:tokens_predicted_total", 0),
            "predicted_seconds": s.get("llamacpp:tokens_predicted_seconds_total", 0),
            "max_seq": s.get("llamacpp:n_tokens_max", 0),
        },
        "requests": {
            "processing": s.get("llamacpp:requests_processing", 0),
            "deferred": s.get("llamacpp:requests_deferred", 0),
        },
    }

# ---------------- llama-server 日志速度 (journalctl) ----------------

_log_speeds = None  # (data, ts)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

def llama_log_speeds():
    """解析 llama-server 日志里它自己测的填充/生成速度：
    - 生成中每 3s:  n_gen = N, tg = X t/s, tg_3s = Y t/s
    - 请求结束:      prompt eval time = A ms / N tokens (X tokens per second)
                     eval time = B ms / N tokens (X tokens per second)
    """
    global _log_speeds
    now = time.monotonic()
    if _log_speeds is not None and now - _log_speeds[1] < 1.5:
        return _log_speeds[0]
    data = {"prompt": None, "gen": None, "gen_live": None}
    # -n 500 ≈ 25 分钟日志，保证长请求结束时的汇总行还在窗口内
    out = _run(["journalctl", "-u", "llama-server", "-n", "500", "--no-pager", "-q"], timeout=5)
    if out:
        ts_now = time.time()
        year = time.localtime().tm_year
        for line in out.splitlines():
            ts = None
            m = re.match(r"(\w{3}) +(\d{1,2}) (\d{2}:\d{2}:\d{2})", line)
            if m:
                mm = _MONTHS.get(m.group(1))
                if mm:
                    hh, mi, ss = (int(x) for x in m.group(3).split(":"))
                    ts = time.mktime((year, mm, int(m.group(2)), hh, mi, ss, 0, 0, -1))
            m = re.search(r"prompt eval time =\s+([\d.]+) ms / +(\d+) tokens \(\s*[\d.]+ ms per token,\s+([\d.]+) tokens per second\)", line)
            if m:
                data["prompt"] = {"ms": float(m.group(1)), "tokens": int(m.group(2)), "tps": float(m.group(3)), "ts": ts}
            m = re.search(r"(?<!prompt )eval time =\s+([\d.]+) ms / +(\d+) tokens \(\s*[\d.]+ ms per token,\s+([\d.]+) tokens per second\)", line)
            if m:
                data["gen"] = {"ms": float(m.group(1)), "tokens": int(m.group(2)), "tps": float(m.group(3)), "ts": ts}
            m = re.search(r"n_gen = +(\d+), tg = +([\d.]+) t/s, tg_3s = +([\d.]+) t/s", line)
            # 只认 10 秒内的实时行，避免把旧请求的 tg 当成当前速度
            if m and ts is not None and 0 <= ts_now - ts <= 10:
                data["gen_live"] = {"n_gen": int(m.group(1)), "tg": float(m.group(2)), "tg_3s": float(m.group(3))}
    _log_speeds = (data, now)
    return data

_props_cache, _props_ts = None, 0

def llama_props():
    global _props_cache, _props_ts
    if _props_cache is not None and time.time() - _props_ts < 30:
        return _props_cache
    text = _get(LLAMA + "/props")
    if text is None:
        return _props_cache
    try:
        j = json.loads(text)
    except Exception:
        return _props_cache
    _props_cache = {
        "alias": j.get("model_alias"),
        "ftype": j.get("model_ftype"),
        "path": j.get("model_path"),
        "n_ctx": j.get("n_ctx"),
        "slots": j.get("total_slots"),
        "modalities": j.get("modalities"),
        "sleeping": j.get("is_sleeping"),
    }
    _props_ts = time.time()
    return _props_cache

# ---------------- 快照 + 历史 ----------------

_history = {k: [] for k in (
    "gen_tps", "mtp", "cpu", "ram",
    "gpu_util0", "gpu_util1", "gpu_temp0", "gpu_temp1", "vram0", "vram1",
    "disk_r", "disk_w",
)}

def _push(d):
    for k, v in d.items():
        h = _history[k]
        h.append(v)
        if len(h) > HISTORY_LEN:
            del h[:len(h) - HISTORY_LEN]

def snapshot(m=None):
    props = llama_props()
    if m is None:
        m = llama_metrics()
    proc = llama_proc()
    # /props 没有 n_ctx，从启动命令 -c 解析
    if props.get("n_ctx") is None and proc and proc.get("cmdline"):
        cm = re.search(r"(?:^|\s)-c\s+(\d+)", proc["cmdline"])
        if cm:
            props["n_ctx"] = int(cm.group(1))
    g = gpus()
    c = cpu_usage()
    memd = mem()
    d = disks()
    e = edac()
    log = llama_log_speeds()

    h = {
        # metrics 的 tokens_predicted_total 只在请求结束时跳变，
        # 生成中的速度曲线改用日志里服务端自测的 tg_3s
        "gen_tps": (log["gen_live"]["tg_3s"] if log.get("gen_live") else None),
        "mtp": (m["mtp"]["accept_inst"] if m and m["mtp"]["accept_inst"] is not None
                else (m["mtp"]["accept"] if m else None)),
        "cpu": c,
        "ram": memd["pct"],
        "disk_r": d["read_mbps"],
        "disk_w": d["write_mbps"],
    }
    for i in range(2):
        h["gpu_util%d" % i] = g[i]["util"] if i < len(g) else None
        h["gpu_temp%d" % i] = g[i]["temp"] if i < len(g) else None
        h["vram%d" % i] = g[i]["vram_used"] if i < len(g) else None
    _push(h)

    return {
        "t": time.time(),
        "llama": {"up": m is not None, "props": props, "metrics": m, "proc": proc,
                  "log": log},
        "cpu": {
            "usage": c,
            "cores": NPROC,
            "load": [round(float(x), 2) for x in open("/proc/loadavg").read().split()[:3]],
            "temp": _cpu_temp,
        },
        "mem": memd,
        "gpus": g,
        "disks": d,
        "disk_temps": dict(_disk_temp),
        "edac": e,
        "history": {k: list(v) for k, v in _history.items()},
    }

_latest = None
_last_hw = 0.0

def sampler():
    """llama metrics 每 0.5s 采一次（速度更跟手），硬件每 2s 采一次。"""
    global _latest, _last_hw
    while True:
        t0 = time.time()
        m = llama_metrics()
        if _latest is None or t0 - _last_hw >= POLL:
            try:
                _latest = snapshot(m)
            except Exception as ex:
                print("snapshot error:", ex, flush=True)
            _last_hw = t0
        elif _latest is not None:
            _latest["llama"]["metrics"] = m
            _latest["llama"]["up"] = m is not None
            _latest["t"] = t0
        time.sleep(max(0.1, POLL_LLAMA - (time.time() - t0)))

# ---------------- 管理（密码 + 服务控制 + 日志） ----------------

ADMIN_FILE = os.path.join(BASE, "admin.json")
TOKEN_TTL = 12 * 3600   # 登录 token 有效期 12 小时
_tokens = {}            # token -> 过期时间戳
_login_fail_ts = []     # 失败登录时间戳（限流用）

def _scrypt(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1).hex()

def _load_admin():
    try:
        with open(ADMIN_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def _check_password(pw):
    a = _load_admin()
    if not a:
        return False
    return secrets.compare_digest(_scrypt(pw, a["salt"]), a["hash"])

def admin_login(pw):
    """成功返回 (token, None)，失败返回 (None, 错误信息)。"""
    now = time.time()
    _login_fail_ts[:] = [t for t in _login_fail_ts if now - t < 60]
    if len(_login_fail_ts) >= 5:
        return None, "失败次数过多，60 秒后再试"
    if not _check_password(pw):
        _login_fail_ts.append(now)
        return None, "密码错误"
    tok = secrets.token_hex(16)
    _tokens[tok] = now + TOKEN_TTL
    return tok, None

def change_password(old, new):
    if not _check_password(old):
        return "当前密码错误"
    if len(new) < 6:
        return "新密码至少 6 位"
    salt = secrets.token_hex(8)
    tmp = ADMIN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"salt": salt, "hash": _scrypt(new, salt)}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ADMIN_FILE)
    return None

def service_info():
    # 不用 --value：多属性时输出顺序不保证，按 Key=Value 解析
    out = _run(["systemctl", "show", "llama-server",
                "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                "-p", "ExecMainStartTimestamp"], timeout=5)
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            d[k] = v
    return {
        "active": d.get("ActiveState", "unknown"),
        "sub": d.get("SubState", ""),
        "pid": int(d["MainPID"]) if d.get("MainPID", "0").isdigit() and d["MainPID"] != "0" else None,
        "started": d.get("ExecMainStartTimestamp", "") or "",
    }

def service_action(action):
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": "未知操作"}
    r = subprocess.run(["systemctl", action, "llama-server"],
                       capture_output=True, text=True, timeout=120)
    time.sleep(1)  # 等状态落定再读
    info = service_info()
    return {"ok": r.returncode == 0,
            "error": r.stderr.strip() or None,
            "state": info}

def proc_health(pid):
    """从 /proc 读进程 CPU 占用（0.5s 采样）、内存、运行时长。"""
    try:
        hz = os.sysconf("SC_CLK_TCK")
        def read_stat():
            with open("/proc/%d/stat" % pid) as f:
                s = f.read()
            rest = s[s.rfind(")") + 2:].split()
            return int(rest[11]) + int(rest[12]), int(rest[19])  # utime+stime, starttime
        t1, starttime = read_stat()
        time.sleep(0.5)
        t2, _ = read_stat()
        cpu = (t2 - t1) / hz / 0.5 * 100
        rss_kb = 0
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
        with open("/proc/uptime") as f:
            up_now = float(f.read().split()[0])
        return {"cpu": round(cpu, 1), "rss_mb": rss_kb // 1024,
                "uptime": max(0.0, up_now - starttime / hz)}
    except (OSError, ValueError, IndexError):
        return None

def probe_llama():
    """探测 llama-server API 响应状态与延迟（有 HTTP 响应即视为存活）。"""
    t0 = time.time()
    for path in ("/health", "/"):
        try:
            with urllib.request.urlopen(LLAMA + path, timeout=3) as r:
                return {"ok": True, "ms": int((time.time() - t0) * 1000), "code": r.status}
        except urllib.error.HTTPError:
            continue  # 服务有响应但该端点不存在，换下一个
        except Exception:
            break
    return {"ok": False, "ms": None}

def health_info():
    svc = service_info()
    return {"proc": proc_health(svc["pid"]) if svc.get("pid") else None,
            "api": probe_llama()}

def logs_tail(n, prio):
    if prio not in ("info", "warning", "err"):
        prio = "info"
    n = max(1, min(5000, int(n)))
    return _run(["journalctl", "-u", "llama-server", "-n", str(n),
                 "-p", prio, "--no-pager", "-q"], timeout=10)

def _spec_counters():
    """读投机解码累计计数器（快速测试前后差值用）。"""
    text = _get(LLAMA + "/metrics")
    if text is None:
        return None
    s, _ = parse_prom(text)
    return {"drafts": s.get("llamacpp:spec_decode_num_drafts_total", 0),
            "draft_tokens": s.get("llamacpp:spec_decode_num_draft_tokens_total", 0),
            "accepted": s.get("llamacpp:spec_decode_num_accepted_tokens_total", 0)}

def test_generation(prompt, n_predict):
    n_predict = max(1, min(1024, int(n_predict)))
    before = _spec_counters()
    body = json.dumps({"prompt": prompt, "n_predict": n_predict,
                       "stream": False}).encode()
    req = urllib.request.Request(LLAMA + "/completion", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            j = json.loads(r.read().decode())
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    t = j.get("timings", {})  # /completion 的计时数据嵌套在 timings 下
    after = _spec_counters()
    mtp = None
    if before and after:
        dd = after["draft_tokens"] - before["draft_tokens"]
        da = after["accepted"] - before["accepted"]
        if dd > 0:
            mtp = {"draft_tokens": dd, "accepted": da,
                   "accept": round(100.0 * da / dd, 1),
                   "drafts": after["drafts"] - before["drafts"]}
    return {"ok": True,
            "content": j.get("content", ""),
            "timing": {
                "prompt_n": t.get("prompt_n"), "prompt_ms": t.get("prompt_ms"),
                "predicted_n": t.get("predicted_n"), "predicted_ms": t.get("predicted_ms"),
                "predicted_per_second": t.get("predicted_per_second"),
                "draft_n": t.get("draft_n"),
            },
            "mtp": mtp}

# ---------------- 启动参数（systemd 单元 + 历史） ----------------

UNIT_FILE = "/etc/systemd/system/llama-server.service"
HIST_FILE = os.path.join(BASE, "cmd_history.json")
HIST_LEN = 20

def _read_unit_cmd():
    """从单元文件读 ExecStart（处理反斜杠续行）。"""
    try:
        with open(UNIT_FILE) as f:
            text = f.read()
    except Exception:
        return None
    parts, in_exec = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("ExecStart="):
            in_exec = True
            s = s[len("ExecStart="):]
        if in_exec:
            if s.endswith("\\"):
                parts.append(s[:-1])
            else:
                parts.append(s)
                in_exec = False
    if not parts:
        return None
    raw = " ".join(p.strip() for p in parts if p.strip())
    return " ".join(raw.split())  # 归一化空白，保证"未修改"判断可靠

def _write_unit_cmd(cmd):
    try:
        with open(UNIT_FILE) as f:
            lines = f.read().splitlines()
    except Exception:
        return "无法读取单元文件"
    out, i, n = [], 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith("ExecStart="):
            out.append("ExecStart=" + cmd)
            i += 1
            if s.endswith("\\"):
                while i < n and lines[i].strip().endswith("\\"):
                    i += 1
                if i < n:
                    i += 1  # 旧块最后一行（无尾反斜杠）
            continue
        out.append(lines[i])
        i += 1
    tmp = UNIT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, UNIT_FILE)
    r = subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return r.stderr.strip() or "daemon-reload 失败"
    return None

def _load_hist():
    try:
        with open(HIST_FILE) as f:
            h = json.load(f)
        return h if isinstance(h, list) else []
    except Exception:
        return []

def _save_hist(h):
    tmp = HIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, HIST_FILE)

# 表单字段：数字 / 下拉（固定选项）/ 文本 / 文件 / 开关
NUM_FIELDS = [
    ("-c", "c"), ("-ngl", "ngl"), ("-t", "t"), ("-np", "np"),
    ("-b", "b"), ("-ub", "ub"), ("--port", "port"), ("--spec-draft-n-max", "draft_n_max"),
    ("--cache-ram", "cache_ram"), ("--temp", "temp"), ("--top-k", "top_k"),
    ("--top-p", "top_p"), ("--min-p", "min_p"),
]
# 下拉菜单：值必须是固定选项之一（选项列表见 admin.html）
SEL_FIELDS = [
    ("-fa", "fa"), ("--split-mode", "split"), ("-ctk", "ctk"), ("-ctv", "ctv"),
    ("--load-mode", "load_mode"), ("--spec-type", "spec_type"), ("--reasoning", "reasoning"),
]
TEXT_FIELDS = [
    ("--alias", "alias"), ("--device", "device"), ("--tensor-split", "tensor_split"),
    ("--host", "host"), ("--chat-template-kwargs", "chat_kwargs"),
]
FILE_FIELDS = [("-m", "model"), ("--mmproj", "mmproj"), ("--model-draft", "draft")]
# 草稿模型文件的三种等价写法（--spec-draft-model / --model-draft / -md），解析时都识别
DRAFT_FLAG = r"(?:--spec-draft-model|--model-draft|-md)"
BOOL_FIELDS = [("--metrics", "metrics"), ("--no-host", "no_host"),
               ("--no-mmproj-offload", "no_mmproj_offload")]

def parse_cmd(cmd):
    """把启动命令拆成表单字段，未识别的参数留在 custom。"""
    d = {k: "" for _, k in NUM_FIELDS + SEL_FIELDS + TEXT_FIELDS + FILE_FIELDS}
    d.update({k: False for _, k in BOOL_FIELDS})
    d["binary"] = ""
    d["custom"] = ""
    rest = cmd
    m = re.match(r"(\S+)", rest)
    if m:
        d["binary"] = m.group(1)
        rest = rest[m.end():]
    for p, key in NUM_FIELDS + SEL_FIELDS + TEXT_FIELDS + FILE_FIELDS:
        pat = DRAFT_FLAG if key == "draft" else re.escape(p)
        m = re.search(r"(?:^|\s)" + pat + r"\s+(\S+)", rest)
        if m:
            d[key] = m.group(1)
            rest = rest[:m.start()] + " " + rest[m.end():]
    for p, key in BOOL_FIELDS:
        m = re.search(r"(?:^|\s)" + re.escape(p) + r"(?=\s|$)", rest)
        if m:
            d[key] = True
            rest = rest[:m.start()] + " " + rest[m.end():]
    d["custom"] = " ".join(rest.split())
    return d

def cmd_info():
    cur = _read_unit_cmd()
    h = _load_hist()
    if cur and (not h or h[0].get("cmd") != cur):
        # 当前命令不在历史顶部时置顶显示（不落盘）
        h = [{"ts": time.time(), "cmd": cur}] + [x for x in h if x.get("cmd") != cur]
    return {"current": cur,
            "parsed": parse_cmd(cur) if cur else None,
            "history": h[:HIST_LEN]}

# ---------------- 服务器文件浏览（选模型文件用） ----------------

FILES_ROOT = "/mnt/models"

def list_files(path):
    p = os.path.realpath(path or FILES_ROOT)
    if p != FILES_ROOT and not p.startswith(FILES_ROOT + os.sep):
        p = FILES_ROOT
    dirs, files = [], []
    try:
        for name in sorted(os.listdir(p)):
            full = os.path.join(p, name)
            if os.path.isdir(full):
                dirs.append(name)
            else:
                try:
                    files.append({"name": name, "size": os.path.getsize(full)})
                except Exception:
                    files.append({"name": name, "size": 0})
    except Exception:
        pass
    parent = os.path.dirname(p)
    if parent == p or not parent.startswith(FILES_ROOT):
        parent = None
    return {"path": p, "parent": parent, "dirs": dirs, "files": files}

SPEC_TYPES = {"none", "draft-simple", "draft-eagle3", "draft-mtp", "draft-dflash",
              "draft-dspark", "ngram-simple", "ngram-map-k", "ngram-map-k4v",
              "ngram-mod", "ngram-cache"}
SPEC_NEED_DRAFT = {"draft-simple", "draft-eagle3", "draft-dflash", "draft-dspark"}

def list_devices():
    """列出可用 GPU 设备（nvidia-smi 只读查询，不占用显存、不影响运行中的服务）。"""
    out = []
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 4:
                    out.append({"id": "CUDA" + parts[0], "name": parts[1],
                               "mem_total": int(parts[2]), "mem_free": int(parts[3])})
    except Exception:
        pass
    return out

def _check_cmd_files(cmd):
    """保存前校验命令引用的模型文件是否存在、投机解码与多 GPU 配置是否有效。"""
    p = parse_cmd(cmd)
    types = [t.strip() for t in (p.get("spec_type") or "").split(",") if t.strip()]
    for t in types:
        if t not in SPEC_TYPES:
            return "未知投机解码类型: " + t
    if "none" not in types and any(t in SPEC_NEED_DRAFT for t in types) and not p.get("draft"):
        return ("投机解码类型 " + ",".join(t for t in types if t in SPEC_NEED_DRAFT)
                + " 需要草稿模型文件（--model-draft）")
    devs = [d.strip() for d in (p.get("device") or "").split(",") if d.strip()]
    ts = [x.strip() for x in (p.get("tensor_split") or "").split(",") if x.strip()]
    if len(devs) >= 2 and ts and len(ts) != len(devs):
        return "tensor-split 比例数量（%d）需等于所选设备数（%d）" % (len(ts), len(devs))
    checks = [
        (p.get("model"), "模型文件 (-m)"),
        (p.get("mmproj"), "多模态投影器文件 (--mmproj)"),
        (p.get("draft"), "草稿模型文件 (--model-draft)"),
    ]
    for path, name in checks:
        if not path:
            if name.startswith("模型文件"):
                return "缺少" + name
            continue
        if not os.path.isfile(path):
            return name + " 不存在: " + path
    return None

def cmd_save(new_cmd, restart=False):
    new_cmd = " ".join(new_cmd.split())
    if not new_cmd or "llama-server" not in new_cmd:
        return {"ok": False, "error": "命令为空或不含 llama-server"}
    err = _check_cmd_files(new_cmd)
    if err:
        return {"ok": False, "error": err}
    cur = _read_unit_cmd()
    if cur == new_cmd:
        return {"ok": True, "changed": False, "error": None, "state": None}
    err = _write_unit_cmd(new_cmd)
    if err:
        return {"ok": False, "error": err}
    if cur:
        h = _load_hist()
        h = [{"ts": time.time(), "cmd": cur}] + [x for x in h if x.get("cmd") != cur]
        _save_hist(h[:HIST_LEN])
    info = None
    if restart:
        r = subprocess.run(["systemctl", "restart", "llama-server"],
                           capture_output=True, text=True, timeout=180)
        time.sleep(1)
        info = service_info()
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr.strip() or "重启失败", "state": info}
    return {"ok": True, "changed": True, "error": None, "state": info}

# ---------------- HTTP ----------------

PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/themes": "themes.html",
    "/themes.html": "themes.html",
    "/style.css": "style.css",
    "/themes.css": "themes.css",
    "/admin": "admin.html",
    "/admin.html": "admin.html",
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        d = urllib.parse.parse_qs(q)
        return {k: v[0] for k, v in d.items()}

    def _admin_token(self):
        tok = self.headers.get("X-Admin-Token") or self._query().get("token")
        if not tok:
            return False
        exp = _tokens.get(tok)
        if exp is None or time.time() > exp:
            _tokens.pop(tok, None)
            return False
        return True

    def _sse_logs(self, prio):
        """SSE 实时推送 journalctl -f 输出。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        p = subprocess.Popen(
            ["journalctl", "-u", "llama-server", "-f", "-p", prio, "--no-pager", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for line in p.stdout:
                self.wfile.write(("data: " + line.rstrip("\n") + "\n\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            p.terminate()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            body = json.dumps(_latest or {}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/admin/service":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            self._json(200, service_info())
            return
        if path == "/api/admin/health":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            self._json(200, health_info())
            return
        if path == "/api/admin/cmd":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            self._json(200, cmd_info())
            return
        if path == "/api/admin/files":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            self._json(200, list_files(self._query().get("path", "")))
            return
        if path == "/api/admin/devices":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            self._json(200, {"devices": list_devices()})
            return
        if path == "/api/admin/logs":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            q = self._query()
            self._json(200, {"lines": logs_tail(q.get("n", "200"), q.get("prio", "info")).splitlines()})
            return
        if path == "/api/admin/logs/stream":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            self._sse_logs(self._query().get("prio", "info"))
            return
        if path == "/api/admin/logs/download":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            q = self._query()
            text = logs_tail(q.get("n", "2000"), q.get("prio", "info"))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="llama-server.log"')
            self.end_headers()
            self.wfile.write(text.encode())
            return
        if path not in PAGES:
            self.send_error(404)
            return
        try:
            with open(os.path.join(BASE, PAGES[path]), "rb") as f:
                body = f.read()
        except Exception:
            self.send_error(404)
            return
        ctype = "text/css" if path.endswith(".css") else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            self._json(400, {"error": "请求格式错误"})
            return
        if path == "/api/admin/login":
            tok, err = admin_login(data.get("password", ""))
            if err:
                self._json(403, {"error": err})
            else:
                self._json(200, {"token": tok})
            return
        if not self._admin_token():
            self._json(401, {"error": "未登录"})
            return
        if path == "/api/admin/service":
            self._json(200, service_action(data.get("action", "")))
            return
        if path == "/api/admin/test":
            self._json(200, test_generation(data.get("prompt", ""), data.get("n_predict", 64)))
            return
        if path == "/api/admin/cmd":
            self._json(200, cmd_save(data.get("cmd", ""), bool(data.get("restart"))))
            return
        if path == "/api/admin/password":
            err = change_password(data.get("old", ""), data.get("new", ""))
            if err:
                self._json(400, {"error": err})
            else:
                self._json(200, {"ok": True})
            return
        self._json(404, {"error": "未找到"})

def main():
    ThreadingHTTPServer.daemon_threads = True
    threading.Thread(target=_disk_temp_worker, daemon=True).start()
    threading.Thread(target=_cpu_temp_worker, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    # 预热一次差值，让第一帧就有速率
    cpu_usage(); llama_proc(); llama_metrics()
    for port in PORTS:
        try:
            srv = ThreadingHTTPServer((HOST, port), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print("dashboard listening on http://%s:%d" % (HOST, port), flush=True)
        except OSError as ex:
            print("port %d unavailable: %s" % (port, ex), flush=True)
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
