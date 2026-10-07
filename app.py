#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推理服务 + 硬件监控 Dashboard（纯标准库，无依赖）

自动探测后端（llama-server / ninfer-serve），数据源：
  - llama-server: /metrics (吞吐、MTP 逐位置接受率) + /props (模型参数)
  - ninfer-serve: /metrics (累计 tokens、MTP 接受率、实时速度 counter 差值)
                   + journalctl 请求日志 (在途/排队、逐请求详情) + /v1/models
  - nvidia-smi             (GPU 温度/利用率/显存/功耗/ECC；PCIe 吞吐由常驻 dmon 线程 2s 采样)
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
import select
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORTS = (80, 8090)
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

# ---------------- 推理后端探测（llama-server / ninfer-serve） ----------------

_BACKEND_COMM = {"llama-server": "llama", "ninfer-serve": "ninfer"}
_svc = {"backend": None, "pid": None, "unit": None, "ts": 0.0}

def _proc_unit(pid):
    """进程所属 systemd 单元（/proc/<pid>/cgroup）；非 systemd 启动返回 None。"""
    try:
        with open("/proc/%d/cgroup" % pid) as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) == 3:
                    m = re.search(r"([\w][\w.-]*\.service)$", parts[2])
                    if m:
                        return m.group(1)[:-len(".service")]
    except Exception:
        pass
    return None

def detect_service():
    """扫 /proc 找运行中的推理进程（llama-server 或 ninfer-serve），5 秒缓存。
    返回 (backend, pid, unit)；未运行返回 (None, None, None)。"""
    global _svc
    now = time.monotonic()
    if _svc["ts"] and now - _svc["ts"] < 5:
        return _svc["backend"], _svc["pid"], _svc["unit"]
    backend, pid, unit = None, None, None
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % p) as f:
                comm = f.read().strip()
            if comm in _BACKEND_COMM:
                backend, pid = _BACKEND_COMM[comm], int(p)
                unit = _proc_unit(pid)
                break
        except Exception:
            continue
    _svc.update(backend=backend, pid=pid, unit=unit, ts=now)
    return backend, pid, unit

def log_unit():
    """journalctl 查询用单元名：优先运行进程的单元，未运行时回退 ai-server（默认入口）。"""
    _, _, unit = detect_service()
    return unit or "ai-server"

def _proc_cmdline(pid):
    if pid is None:
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\0", b" ").decode().strip()
    except Exception:
        return None

def _last_flag(cmd, flag):
    """cmd 中 flag 最后一次出现的取值（ninfer 的 cmdline 里 launcher 默认值
    可能被后续参数覆盖，以最后一次为准）。"""
    hits = re.findall(r"(?:^|\s)" + re.escape(flag) + r"\s+(\S+)", cmd or "")
    return hits[-1] if hits else None

_llama_base = None  # (url, ts) 缓存，避免每次探测都重读 /proc

def llama_base():
    """解析推理服务实际监听地址，构造本机可访问的 base URL（带缓存）。
    读进程 cmdline（真实运行命令）；服务未运行时回退 llama-server 单元文件
    ExecStart，再无则默认 127.0.0.1:8080。
    --host 为 0.0.0.0/::/空 时用 127.0.0.1，否则用 --host 指定的地址。"""
    global _llama_base
    now = time.monotonic()
    if _llama_base is not None and now - _llama_base[1] < 5:
        return _llama_base[0]
    host, port = "127.0.0.1", 8080
    backend, pid, _ = detect_service()
    cmd = _proc_cmdline(pid)
    if cmd is None and backend is None:
        cmd = _read_unit_cmd()  # llama-server 未运行回退；ninfer 未运行用默认地址
    if cmd:
        h = _last_flag(cmd, "--host")
        if h and h not in ("0.0.0.0", "::", "[::]"):
            host = h
        p = _last_flag(cmd, "--port")
        if p and p.isdigit():
            port = int(p)
    url = "http://%s:%d" % (host, port)
    _llama_base = (url, now)
    return url

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
    try:
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
    except Exception:
        # 唯一失败路径也兜底，避免炸掉整个 snapshot 丢一轮硬件数据
        return {"total_gb": None, "used_gb": None, "avail_gb": None, "pct": None,
                "swap_used_gb": None, "swap_total_gb": None}

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

_pcie_live = None  # (data, monotonic) 常驻 dmon 线程写入

def _pcie_worker():
    """常驻 nvidia-smi dmon -d 2 持续采样 PCIe 吞吐。
    一次性 -c 1 每次要等满 1s 采样间隔（2s 周期实际拖到 3-4s）且每轮 fork，
    常驻进程则 2s 出一帧，主循环直接读全局值。dmon 无空行分隔，
    用“同一 index 再次出现 = 新样本”切分。"""
    global _pcie_live
    while True:
        cur = {}
        p = None
        try:
            p = subprocess.Popen(["nvidia-smi", "dmon", "-s", "t", "-d", "2"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, bufsize=1)
            for line in p.stdout:
                parts = line.split()
                if len(parts) >= 3 and parts[0].isdigit():
                    idx = int(parts[0])
                    if idx in cur:  # index 重复 → 上一批完整，提交
                        _pcie_live = ([{"index": i, "rx": v[0], "tx": v[1]}
                                      for i, v in sorted(cur.items())], time.monotonic())
                    cur[idx] = (_f(parts[1]), _f(parts[2]))
        except Exception:
            pass
        finally:
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
        time.sleep(2)

def pcie():
    """GPU PCIe 链路实时吞吐（MB/s），用于卡间通讯（本机无 NVLink，走 PCIe PHB）。
    优先取常驻 dmon 线程的最新样本（10s 内新鲜）；异常时回退一次性查询。
    dmon -s t 输出:
        # gpu  rxpci  txpci
        # Idx   MB/s   MB/s
            0      7      0
    """
    d = _pcie_live
    if d is not None and time.monotonic() - d[1] < 10:
        return d[0]
    out = _run(["nvidia-smi", "dmon", "-s", "t", "-c", "1"], timeout=4)
    res = []
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 3 and p[0].isdigit():
            res.append({"index": int(p[0]), "rx": _f(p[1]), "tx": _f(p[2])})
    return res

# PCIe 各代单 lane 速率（GT/s）
_PCIE_GT = {1: 2.5, 2: 5.0, 3: 8.0, 4: 16.0, 5: 32.0}
_pcie_link_cache = None  # (data, ts) 30s TTL：链路降级（Gen 变化）后能自动更新

def pcie_link():
    """每卡 PCIe 链路规格 + 理论单向带宽（30s 缓存）。
    Gen1/2 用 8b/10b 编码 (0.8)，Gen3+ 用 128b/130b (128/130)。"""
    global _pcie_link_cache
    c = _pcie_link_cache
    if c is not None and time.time() - c[1] < 30:
        return c[0]
    out = _run(["nvidia-smi", "--query-gpu=index,pcie.link.gen.current,pcie.link.width.current",
                "--format=csv,noheader,nounits"])
    res = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 3:
            continue
        try:
            idx, gen, width = int(p[0]), int(p[1]), int(p[2])
        except ValueError:
            continue
        gt = _PCIE_GT.get(gen)
        gbps = round(gt * (0.8 if gen <= 2 else 128 / 130) * width, 1) if gt else None
        res.append({"index": idx, "gen": gen, "width": width, "gbps": gbps})
    if res:  # 查询失败（驱动启动中/smi 忙）时不缓存，下轮重试
        _pcie_link_cache = (res, time.time())
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
    """推理进程（llama-server / ninfer-serve）的 CPU / 内存 / 运行时长。"""
    global _proc_prev
    _, pid, _ = detect_service()
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
    if _proc_prev is not None and _proc_prev[2] == pid:
        dt = now - _proc_prev[1]
        if dt > 0:
            cpu = max(0.0, (ticks - _proc_prev[0]) / 100.0 / dt * 100.0)
    _proc_prev = (ticks, now, pid)
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
        # 脱敏：/api/status 无鉴权，不能把 --api-key 等凭据泄露给任意访问者
        cmdline = re.sub(r"(--[\w-]*[ak]ey[\w-]*\s+)\S+", r"\1****", cmdline) if cmdline else None
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
    backend, _, _ = detect_service()
    if backend == "ninfer":
        return ninfer_metrics()
    return _llama_metrics()

def _llama_metrics():
    global _llama_prev, _gen_hold
    text = _get(llama_base() + "/metrics")
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
    if detect_service()[0] == "ninfer":
        return ninfer_log_speeds()
    return _llama_log_speeds()

def _llama_log_speeds():
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
    out = _run(["journalctl", "-u", log_unit(), "-n", "500", "--no-pager", "-q"], timeout=5)
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

_log_err = None  # (data, ts)

def log_error_counts(hours=24, cache=300):
    """统计最近 N 小时推理服务日志里 错误 / 警告 的行数（带缓存）。
    llama 用 journalctl -p 按优先级过滤；ninfer 的级别写在正文（[error]/[warning]），
    systemd 统一按 warning 记录，需按正文匹配。"""
    global _log_err
    now = time.monotonic()
    if _log_err is not None and _log_err[0].get("hours") == hours and now - _log_err[1] < cache:
        return _log_err[0]
    unit = log_unit()
    if detect_service()[0] == "ninfer":
        out = _run(["journalctl", "-u", unit, "--since", "%d hours ago" % hours,
                    "--no-pager", "-q"], timeout=10)
        lines = [l for l in out.splitlines() if l.strip()] if out else []
        err = sum(1 for l in lines if "[error]" in l)
        warn = sum(1 for l in lines if "[warning]" in l)
    else:
        def _count(prio):
            out = _run(["journalctl", "-u", unit, "-p", prio,
                        "--since", "%d hours ago" % hours, "--no-pager", "-q"], timeout=8)
            return len([l for l in out.splitlines() if l.strip()]) if out else 0
        err = _count("err")
        warn = max(0, _count("warning") - err)  # -p warning 含 err，减去得纯 warning
    _log_err = ({"err": err, "warn": warn, "hours": hours}, now)
    return _log_err[0]

_props_cache, _props_ts = None, 0

def llama_props():
    if detect_service()[0] == "ninfer":
        return ninfer_props()
    return _llama_props()

def _llama_props():
    global _props_cache, _props_ts
    if _props_cache is not None and time.time() - _props_ts < 30:
        return _props_cache
    text = _get(llama_base() + "/props")
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

# ---------------- ninfer-serve 数据（无 /metrics：日志解析 + /v1/models） ----------------

# 请求完成行，例：
#   [req 3] done finish=stop_token prompt=19833 gen=72 cache=19774 reuse=append_frontier
#     ttft=436ms prefill=158.5tok/s decode=76.3tok/s wall=1.37s speculative=mtp 2.73tok/round (57.7%)
_NINFER_DONE_RE = re.compile(
    r"\[req (\d+)\] done finish=(\S+)(?: tool_calls=\d+)? prompt=(\d+) gen=(\d+) cache=(\d+) "
    r"reuse=\S+ ttft=(\d+)ms prefill=(?:(\d+(?:\.\d+)?)tok/s|n/a) decode=(?:(\d+(?:\.\d+)?)tok/s|n/a) "
    r"wall=(\d+(?:\.\d+)?)s(?: speculative=off| speculative=(\S+) (\d+(?:\.\d+)?|n/a)tok/round"
    r"(?: \((\d+(?:\.\d+)?)%\))?)?")
_NINFER_START_RE = re.compile(r"\[req (\d+)\] [\w-]+ (?:stream|non-stream).*→ submitted")
_NINFER_ERR_RE = re.compile(r"\[req (\d+)\] error")
_NINFER_REJECT_RE = re.compile(r"\[req (\d+)\] rejected")
_NINFER_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.\d{3}\]")
# 服务端每 5s 一次的调度吞吐行（生成中/prefill 中实时速度）
_NINFER_TPUT_RE = re.compile(
    r"throughput interval=[\d.]+s prefill=(?:(\d+(?:\.\d+)?)tok/s|n/a) decode=(?:(\d+(?:\.\d+)?)tok/s|n/a) "
    r"running=(\d+) prefilling=(\d+) decode_ready=(\d+) waiting=(\d+)")

def _line_ts(line, year):
    """日志行时间戳（秒）。优先 ninfer 行内嵌的 [YYYY-mm-dd HH:MM:SS.mmm]，
    否则 journal 前缀（英文月份名），都无返回 None。"""
    ts = None
    m = _NINFER_TS_RE.search(line)
    if m:
        y, mo, dd = (int(x) for x in m.group(1).split("-"))
        hh, mi, ss = (int(x) for x in m.group(2).split(":"))
        ts = time.mktime((y, mo, dd, hh, mi, ss, 0, 0, -1))
    else:
        m = re.match(r"(\w{3}) +(\d{1,2}) (\d{2}:\d{2}:\d{2})", line)
        if m:
            mm = _MONTHS.get(m.group(1))
            if mm:
                hh, mi, ss = (int(x) for x in m.group(3).split(":"))
                ts = time.mktime((year, mm, int(m.group(2)), hh, mi, ss, 0, 0, -1))
    # journal 月份名无年份：跨年时旧日志会解析成“未来”，回拨一年避免新鲜度判断失效
    if ts is not None and ts > time.time() + 3600:
        ts -= 365 * 86400
    return ts

_ninfer_stats = None  # (data, ts, unit)

_ninfer_mprev = None  # (monotonic, counters) 上一窗口，用于实际间隔累计
_ninfer_mtp = []     # [(monotonic, draft_tokens, accepted_tokens)] 近 15s 采样，实时接受率窗口
_ninfer_mtp_hold = None  # decode 进行中且窗口内无完成事件时，保持上一次接受率
_ninfer_boot = None    # (monotonic, counters) 基准点，用于平均速度
_ninfer_acc = None     # [基准 decode counter, decode 秒数] 平均速度只累计实际在 decode 的时间
_ninfer_cnt_cache = None  # (data, monotonic) 0.4s 缓存：sampler 快路径里 metrics 与 log_speeds
                          # 都会调用，共享同一份差值（否则第二次 dt<0.15 被防抖丢成 None）

def _ninfer_counters():
    """ninfer-serve /metrics（Prometheus 格式，无前缀 counter）。
    返回服务启动以来累计量（tokens/MTP）、实时速度（直接读 decode/prefill_tokens_per_second
    gauge，服务端 5s 窗口平滑、空闲回零）、dashboard 启动以来 decode 均值（只计实际
    decode 时间，服务重启 counter 归零时自动重置基准）。服务未响应返回 None。"""
    global _ninfer_mprev, _ninfer_boot, _ninfer_cnt_cache, _ninfer_acc, _ninfer_mtp, _ninfer_mtp_hold
    c = _ninfer_cnt_cache
    if c is not None and time.monotonic() - c[1] < 0.4:
        return c[0]
    text = _get(llama_base() + "/metrics")
    if text is None:
        _ninfer_mprev = None
        return None
    s, _ = parse_prom(text)
    now = time.monotonic()
    prev = _ninfer_mprev
    dt = (now - prev[0]) if prev is not None else 0.0
    _ninfer_mprev = (now, s)
    b = _ninfer_boot
    if b is None or b[1].get("decode_tokens_total", 0) > s.get("decode_tokens_total", 0):
        b = _ninfer_boot = (now, s)  # 首次基准 / 服务重启 counter 倒退
        _ninfer_acc = [s.get("decode_tokens_total", 0), 0.0]
    acc = _ninfer_acc
    accept_inst = None
    # 实时速度直接读服务端 gauge（5s 窗口平滑，空闲回零），不再用 counter 差值
    gen_tps = s.get("decode_tokens_per_second") or None
    prompt_tps = s.get("prefill_tokens_per_second") or None
    if gen_tps and dt > 0:
        acc[1] += dt  # 只计实际 decode 时间，空闲不算（与 llama 的 decode-only 均值口径一致）
    d = s.get("spec_decode_draft_tokens_total", 0)
    a = s.get("spec_decode_accepted_tokens_total", 0)
    # 实时接受率：ninfer 的 spec_decode_*_total 只在请求结束时更新，逐秒差值恒为 0，
    # 改用近 15s 窗口差值（跨请求边界）；服务重启 counter 倒退时清窗
    d_cur = d
    a_cur = a
    if _ninfer_mtp and d_cur < _ninfer_mtp[-1][1]:
        _ninfer_mtp.clear()
        _ninfer_mtp_hold = None
    _ninfer_mtp.append((now, d_cur, a_cur))
    while _ninfer_mtp and now - _ninfer_mtp[0][0] > 15:
        _ninfer_mtp.pop(0)
    accept_inst = None
    if len(_ninfer_mtp) >= 2:
        dd = _ninfer_mtp[-1][1] - _ninfer_mtp[0][1]
        da = _ninfer_mtp[-1][2] - _ninfer_mtp[0][2]
        if dd > 0:
            accept_inst = 100.0 * da / dd
    if accept_inst is not None:
        _ninfer_mtp_hold = accept_inst
    elif gen_tps:
        # 长请求中途窗口内无完成事件：沿用上一次值（接受率由模型/配置决定，基本稳定）；
        # dashboard 刚启动冷启动时用累计值兜底
        accept_inst = _ninfer_mtp_hold or ((100.0 * a / d) if d else None)
    avg = pred_sec = None
    delta = s.get("decode_tokens_total", 0) - acc[0]
    if acc[1] >= 30 and delta > 0:  # decode 样本太小时均值无意义
        avg = delta / acc[1]
        pred_sec = delta / avg
    res = {
        "prompt": s.get("prefill_tokens_total", 0),
        "predicted": s.get("decode_tokens_total", 0),
        "cached": s.get("prompt_tokens_cached_total", 0),
        "rounds": s.get("spec_decode_rounds_total", 0),
        "gen_tps": gen_tps, "prompt_tps": prompt_tps, "gen_tps_avg": avg,
        "predicted_seconds": pred_sec,
        "accept": (100.0 * a / d) if d else None,
        "accept_inst": accept_inst,
        "draft_tokens": d, "accepted": a,
    }
    _ninfer_cnt_cache = (res, time.monotonic())
    return res

def ninfer_stats(force=False):
    """解析 ninfer-serve 请求日志（最近 2000 行）：吞吐行（在途/排队）与
    done 行（逐请求详情）。5s 缓存（吞吐行 5s 一行，更高频率无意义）。
    累计 tokens / MTP / 速度已改走 /metrics（_ninfer_counters）。"""
    global _ninfer_stats
    unit = log_unit()
    now = time.monotonic()
    if not force and _ninfer_stats is not None and now - _ninfer_stats[1] < 5.0 \
            and _ninfer_stats[2] == unit:
        return _ninfer_stats[0]
    data = {"last": None, "processing": 0, "tput": None}
    submitted, finished = {}, set()
    year = time.localtime().tm_year
    out = _run(["journalctl", "-u", unit, "-n", "2000", "--no-pager", "-q"], timeout=6)
    for line in out.splitlines():
        m = _NINFER_TPUT_RE.search(line)
        if m:
            data["tput"] = {"prefill_tps": float(m.group(1)) if m.group(1) else 0.0,
                             "decode_tps": float(m.group(2)) if m.group(2) else 0.0,
                             "running": int(m.group(3)),
                             "prefilling": int(m.group(4)),
                             "waiting": int(m.group(6)),
                             "ts": _line_ts(line, year)}
            continue
        m = _NINFER_DONE_RE.search(line)
        if m:
            data["last"] = {
                "finish": m.group(2), "prompt": int(m.group(3)), "gen": int(m.group(4)),
                "cache": int(m.group(5)), "ttft_ms": int(m.group(6)),
                "prefill_tps": float(m.group(7)) if m.group(7) else None,
                "decode_tps": float(m.group(8)) if m.group(8) else None,
                "wall_s": float(m.group(9)),
                "mtp_backend": m.group(10),
                "tok_per_round": float(m.group(11)) if m.group(11) not in (None, "n/a") else None,
                "accept_pct": float(m.group(12)) if m.group(12) else None,
                "ts": _line_ts(line, year),
            }
            finished.add(m.group(1))
            continue
        m = _NINFER_START_RE.search(line)
        if m:
            submitted[m.group(1)] = _line_ts(line, year)
            continue
        m = _NINFER_ERR_RE.search(line)
        if m:
            finished.add(m.group(1))
            continue
        m = _NINFER_REJECT_RE.search(line)
        if m:
            finished.add(m.group(1))
    # 在途请求：服务未运行时日志全是历史（窗口法会把滚出窗口的旧请求永久算在途），直接归零；
    # 运行中优先用吞吐行里服务端自报的 running（总活跃数 = prefilling + decode_ready，15 秒内权威值）；
    # 无近期吞吐行时回退窗口推算，但只认 60s 内提交的：有请求在途时服务端每 5s 出吞吐行，
    # 回退只应发生在服务刚启动的头几秒；更早提交且无 done 行的视为已取消/中断，不再误算在途
    tput = data["tput"]
    now_wall = time.time()
    if detect_service()[0] is None:
        data["processing"], data["tput"] = 0, None
    elif tput and tput["ts"] is not None and 0 <= now_wall - tput["ts"] <= 15:
        data["processing"] = tput["running"]
    else:
        data["processing"] = sum(
            1 for r, ts in submitted.items()
            if r not in finished and ts is not None and 0 <= now_wall - ts <= 60)
    _ninfer_stats = (data, now, unit)
    return data

def ninfer_log_speeds():
    """与 llama_log_speeds 同形：gen_live / prefill_live = /metrics 实时速度 gauge
    （decode/prefill_tokens_per_second，服务端 5s 窗口平滑）；gen = 最近完成请求（done 行）。"""
    c = _ninfer_counters()
    d = ninfer_stats()
    out = {"prompt": None, "gen": None, "gen_live": None, "prefill_live": None}
    tput = d.get("tput")
    fresh_tput = tput and tput["ts"] is not None and 0 <= time.time() - tput["ts"] <= 15
    if c:
        if c["gen_tps"]:
            out["gen_live"] = {"n_gen": None, "tg": c["gen_tps"], "tg_3s": c["gen_tps"]}
        if c.get("prompt_tps"):
            out["prefill_live"] = {"tps": c["prompt_tps"], "ts": time.time(),
                                    "prefilling": tput["prefilling"] if tput else 0,
                                    "waiting": tput["waiting"] if tput else 0}
    elif fresh_tput and tput["prefill_tps"] > 0:
        # /metrics 未响应时退回吞吐行服务端自测值
        out["prefill_live"] = {"tps": tput["prefill_tps"], "ts": tput["ts"],
                                "prefilling": tput["prefilling"], "waiting": tput["waiting"]}
    last = d["last"]
    if last and last["ts"] is not None:
        out["gen"] = {"ms": last["wall_s"] * 1000.0, "tokens": last["gen"],
                      "tps": last["decode_tps"], "prefill_tps": last["prefill_tps"],
                      "ts": last["ts"]}
        # decode=n/a（如 gen<=1）时无生成速度，不出实时点；counter 已给出
        # 实时速度时不覆盖（连续请求间隔 <10s 时 done 行是上一请求的旧值）
        if last["decode_tps"] is not None and out["gen_live"] is None \
                and 0 <= time.time() - last["ts"] <= 10:
            out["gen_live"] = {"n_gen": last["gen"], "tg": last["decode_tps"],
                               "tg_3s": last["decode_tps"]}
    return out

def ninfer_metrics():
    """按 llama metrics 同形组装：累计 tokens / MTP 接受率 / 实时速度来自 /metrics counter
    （服务启动以来累计）；在途/排队来自日志吞吐行；逐请求细节来自 done 行。
    /metrics 未响应返回 None。"""
    c = _ninfer_counters()
    if c is None:
        return None
    d = ninfer_stats()
    last = d["last"] or {}
    tput = d["tput"]
    return {
        "gen_tps": round(c["gen_tps"], 1) if c["gen_tps"] else None,
        "gen_hold": None,
        "prompt_tps": round(c["prompt_tps"], 1) if c["prompt_tps"] else None,
        "gen_tps_avg": round(c["gen_tps_avg"], 1) if c["gen_tps_avg"] else None,
        "mtp": {
            "accept": round(c["accept"], 1) if c["accept"] is not None else None,
            "accept_inst": round(c["accept_inst"], 1) if c["accept_inst"] is not None else None,
            "pos": {},
            "drafts": c["rounds"],
            "draft_tokens": c["draft_tokens"],
            "accepted": c["accepted"],
            "tok_per_round": last.get("tok_per_round"),
        },
        "tokens": {"prompt": c["prompt"], "cached": c["cached"], "predicted": c["predicted"],
                    "predicted_seconds": c["predicted_seconds"],
                    "max_seq": None},
        "requests": {"processing": d["processing"],
                      # 无近期吞吐行（全部请求已结束或无日志）视为无排队，避免页面显示 “—”
                      "deferred": (tput["waiting"]
                                   if tput and tput["ts"] is not None
                                   and 0 <= time.time() - tput["ts"] <= 15 else 0)},
    }

_ninfer_props_cache, _ninfer_props_ts = None, 0.0

_health_cache = (None, 0.0)  # (result, ts) 10s 缓存：推理服务 hang 时避免 /health 阻塞采样线程

def api_health():
    """探测推理服务 /health（进程在但模型加载/API 未就绪时 ok=False）。"""
    global _health_cache
    now = time.time()
    if _health_cache[0] is not None and now - _health_cache[1] < 10:
        return _health_cache[0]
    t0 = time.time()
    try:
        with urllib.request.urlopen(llama_base() + "/health", timeout=2) as r:
            res = {"ok": r.status == 200, "ms": int((time.time() - t0) * 1000)}
    except Exception:
        res = {"ok": False, "ms": None}
    _health_cache = (res, now)
    return res

def ninfer_props():
    """ninfer-serve 模型信息：model ID 从 /v1/models；参数从进程 cmdline 解析（30s 缓存）。"""
    global _ninfer_props_cache, _ninfer_props_ts
    if _ninfer_props_cache is not None and time.time() - _ninfer_props_ts < 30:
        return _ninfer_props_cache
    props = {"alias": None, "ftype": None, "path": None, "n_ctx": None, "slots": None,
             "modalities": {"text": True, "vision": False}, "sleeping": False}
    try:
        j = json.loads(_get(llama_base() + "/v1/models") or "")
        data = j.get("data") or []
        if data:
            props["alias"] = data[0].get("id")
    except Exception:
        pass
    backend, pid, _ = detect_service()
    if backend == "ninfer" and pid is not None:
        cmd = _proc_cmdline(pid) or ""
        m = re.search(r"(\S+\.ninfer)", cmd)
        if m:
            props["path"] = m.group(1)
            base = os.path.basename(m.group(1)).lower()
            if "nvfp4" in base:
                props["ftype"] = "NVFP4"
            elif "groupwise" in base:
                props["ftype"] = "groupwise-int"
        ctx = _last_flag(cmd, "--max-context")
        if ctx and ctx.isdigit():
            props["n_ctx"] = int(ctx)
        conc = _last_flag(cmd, "--max-concurrency")
        if conc and conc.isdigit():
            props["slots"] = int(conc)
        props["modalities"]["vision"] = re.search(r"(?:^|\s)--vision(?=\s|$)", cmd) is not None
    _ninfer_props_cache = props
    _ninfer_props_ts = time.time()
    return props

def loadavg():
    try:
        with open("/proc/loadavg") as f:
            return [round(float(x), 2) for x in f.read().split()[:3]]
    except Exception:
        return None

# ---------------- 快照 + 历史 ----------------

_history = {k: [] for k in (
    "prefill_tps", "decode_tps", "cpu", "ram",
    "gpu_util0", "gpu_util1", "gpu_temp0", "gpu_temp1", "vram0", "vram1",
    "pcie_rx0", "pcie_rx1", "pcie_tx0", "pcie_tx1",
    "disk_r", "disk_w",
)}

def _push(d):
    for k, v in d.items():
        h = _history[k]
        h.append(v)
        if len(h) > HISTORY_LEN:
            del h[:len(h) - HISTORY_LEN]

def _ensure_hist(*keys):
    """GPU 数动态化：超过预置的 2 卡时按需建历史 key。"""
    for k in keys:
        if k not in _history:
            _history[k] = []

def snapshot(m=None):
    backend = detect_service()[0]
    props = dict(llama_props() or {})  # 拷贝，避免把派生的 n_ctx 写回共享缓存
    if m is None:
        m = llama_metrics()
    proc = llama_proc()
    # /props 没有 n_ctx，从启动命令 -c 解析（ninfer 的 props 已含 --max-context）
    if backend == "llama" and props.get("n_ctx") is None and proc and proc.get("cmdline"):
        cm = re.search(r"(?:^|\s)-c\s+(\d+)", proc["cmdline"])
        if cm:
            props["n_ctx"] = int(cm.group(1))
    g = gpus()
    pc = pcie()
    c = cpu_usage()
    memd = mem()
    d = disks()
    e = edac()
    log = llama_log_speeds()
    # prefill 速度：ninfer 取 /metrics prefill_tokens_per_second gauge（服务端 5s 窗口平滑）；
    # llama 无实时速度，取最近结束请求的 prompt eval 整段均值
    pf_live = log.get("prefill_live")
    pf_prompt = log.get("prompt")
    prefill_tps = (pf_live["tps"] if pf_live and pf_live["tps"] > 0
                   else (pf_prompt["tps"] if pf_prompt and pf_prompt.get("ts") is not None
                         and 0 <= time.time() - pf_prompt["ts"] <= 15 else None))
    h = {
        "prefill_tps": prefill_tps,
        # metrics 的 tokens_predicted_total 只在请求结束时跳变，
        # 生成中的速度曲线改用日志里服务端自测的 tg_3s
        "decode_tps": (log["gen_live"]["tg_3s"] if log.get("gen_live") else None),
        "cpu": c,
        "ram": memd["pct"],
        "disk_r": d["read_mbps"],
        "disk_w": d["write_mbps"],
    }
    # 动态支持 >2 卡
    _ensure_hist(*[s + str(i) for i in range(len(g))
                  for s in ("gpu_util", "gpu_temp", "vram", "pcie_rx", "pcie_tx")])
    pcmap = {x["index"]: x for x in pc}
    for i in range(len(g)):
        h["gpu_util%d" % i] = g[i]["util"]
        h["gpu_temp%d" % i] = g[i]["temp"]
        h["vram%d" % i] = g[i]["vram_used"]
        h["pcie_rx%d" % i] = (pcmap.get(i) or {}).get("rx")
        h["pcie_tx%d" % i] = (pcmap.get(i) or {}).get("tx")
    _push(h)

    return {
        "t": time.time(),
        "llama": {"backend": backend or "none",
                  "up": (m is not None) if backend == "llama" else (backend == "ninfer"),
                  "health": api_health() if backend else {"ok": False, "ms": None},
                  "props": props, "metrics": m, "proc": proc,
                  "log": log, "log_err": log_error_counts()},
        "cpu": {
            "usage": c,
            "cores": NPROC,
            "load": loadavg(),
            "temp": _cpu_temp,
        },
        "mem": memd,
        "gpus": g,
        "pcie": pc,
        "pcie_link": pcie_link(),
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
            # 约定：快路径只替换已有 key 的值，不增删 key。
            # GIL 下 dict 单 key 赋值是原子的，HTTP 线程并发 json.dumps 安全；
            # 若要给 _latest 加新 key，必须改为整体替换快照。
            # up 仅 llama 后端由 /metrics 可达性决定；ninfer 的 up 语义是“进程在”
            # （模型加载中也算加载中），不能每 0.5s 被 metrics 未就绪刷成 False
            if _latest["llama"]["backend"] == "llama":
                _latest["llama"]["up"] = m is not None
            _latest["llama"]["metrics"] = m
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
    # 顺手清理过期 token，避免残留
    for t in [t for t, exp in _tokens.items() if now > exp]:
        _tokens.pop(t, None)
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

def _ctrl_unit():
    """服务控制目标单元：优先运行进程所属单元，未运行时用 ai-server（默认入口）。"""
    _, _, unit = detect_service()
    return unit or "ai-server"

def service_info():
    # 不用 --value：多属性时输出顺序不保证，按 Key=Value 解析
    unit = _ctrl_unit()
    out = _run(["systemctl", "show", unit,
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
        "unit": unit,
    }

def service_action(action):
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": "未知操作"}
    r = subprocess.run(["systemctl", action, _ctrl_unit()],
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
            with urllib.request.urlopen(llama_base() + path, timeout=3) as r:
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

def logs_tail(n, prio, since_hours=None):
    if prio not in ("info", "warning", "err"):
        prio = "info"
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 200
    n = max(1, min(5000, n))
    unit = log_unit()
    if detect_service()[0] == "ninfer":
        # ninfer 日志级别写在正文（[error]/[warning]），journalctl -p 无法过滤，
        # 取足量后按正文筛选
        factor = 4 if prio != "info" else 1
        cmd = ["journalctl", "-u", unit, "-n", str(n * factor), "--no-pager", "-q"]
        if since_hours:
            try:
                h = int(since_hours)
            except (TypeError, ValueError):
                h = 0
            if h > 0:
                cmd += ["--since", "%d hours ago" % h]
        lines = _run(cmd, timeout=10).splitlines()
        if prio == "err":
            lines = [l for l in lines if "[error]" in l]
        elif prio == "warning":
            lines = [l for l in lines if "[error]" in l or "[warning]" in l]
        # journalctl -n 返回最近 N 行（时间升序），过滤后取尾部 = 最新的 n 行
        return "\n".join(lines[-n:])
    cmd = ["journalctl", "-u", unit, "-n", str(n), "-p", prio, "--no-pager", "-q"]
    if since_hours:
        try:
            h = int(since_hours)
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            cmd += ["--since", "%d hours ago" % h]
    return _run(cmd, timeout=10)

def _spec_counters():
    """读投机解码累计计数器（快速测试前后差值用）。"""
    text = _get(llama_base() + "/metrics")
    if text is None:
        return None
    s, _ = parse_prom(text)
    return {"drafts": s.get("llamacpp:spec_decode_num_drafts_total", 0),
            "draft_tokens": s.get("llamacpp:spec_decode_num_draft_tokens_total", 0),
            "accepted": s.get("llamacpp:spec_decode_num_accepted_tokens_total", 0)}

def test_generation(prompt, n_predict):
    backend, _, _ = detect_service()
    if backend == "ninfer":
        return _ninfer_test_generation(prompt, n_predict)
    try:
        n_predict = int(n_predict)
    except (TypeError, ValueError):
        n_predict = 64
    n_predict = max(1, min(1024, n_predict))
    before = _spec_counters()
    body = json.dumps({"prompt": prompt, "n_predict": n_predict,
                       "stream": False}).encode()
    req = urllib.request.Request(llama_base() + "/completion", data=body,
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

def _ninfer_test_generation(prompt, n_predict):
    """ninfer 快速测试：/v1/chat/completions + 从日志最近 done 行取回服务端计时
    （按生成 token 数匹配，max-concurrency=1 时基本准确）。"""
    try:
        n_predict = int(n_predict)
    except (TypeError, ValueError):
        n_predict = 64
    n_predict = max(1, min(1024, n_predict))
    model = ninfer_props().get("alias") or ""
    if not model:
        return {"ok": False, "error": "无法获取模型 ID（/v1/models 未响应）"}
    body = json.dumps({"model": model, "stream": False, "max_tokens": n_predict,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(llama_base() + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            j = json.loads(r.read().decode())
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    try:
        content = j["choices"][0]["message"].get("content", "")
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "error": "响应格式异常"}
    usage = j.get("usage") or {}
    # done 行要经 journald 落盘，轮询等待（最多 ~3s）
    last = None
    for _ in range(6):
        time.sleep(0.5)
        last = ninfer_stats(force=True).get("last")
        if last and last.get("gen") == usage.get("completion_tokens") \
                and last.get("ts") is not None and time.time() - last["ts"] < 10:
            break
    timing = {"prompt_n": usage.get("prompt_tokens"), "prompt_ms": None,
              "predicted_n": usage.get("completion_tokens"), "predicted_ms": None,
              "predicted_per_second": None, "draft_n": None}
    mtp = None
    if (last and last.get("gen") == usage.get("completion_tokens")
            and last.get("ts") is not None and time.time() - last["ts"] < 10):
        timing["predicted_per_second"] = last["decode_tps"]
        timing["predicted_ms"] = max(0.0, last["wall_s"] - last["ttft_ms"] / 1000.0) * 1000.0
        if last.get("accept_pct") is not None:
            mtp = {"accept": last["accept_pct"], "tok_per_round": last.get("tok_per_round")}
    return {"ok": True, "content": content, "timing": timing, "mtp": mtp}

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
    os.chmod(tmp, 0o600)  # 单元文件含 --api-key，不给同机其他用户读
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
    ("-ngld", "draft_ngl"),
    ("--cache-ram", "cache_ram"), ("--temp", "temp"), ("--top-k", "top_k"),
    ("--top-p", "top_p"), ("--min-p", "min_p"),
    ("--reasoning-budget", "reasoning_budget"),
    ("--repeat-penalty", "repeat_penalty"), ("--presence-penalty", "presence_penalty"),
    ("--frequency-penalty", "frequency_penalty"), ("--repeat-last-n", "repeat_last_n"),
    ("--seed", "seed"),
    ("--cache-reuse", "cache_reuse"), ("--threads-batch", "threads_batch"),
    ("--image-min-tokens", "image_min_tokens"), ("--image-max-tokens", "image_max_tokens"),
    ("--main-gpu", "main_gpu"), ("--n-cpu-moe", "n_cpu_moe"),
    ("--rope-scale", "rope_scale"),
    ("--threads-http", "threads_http"), ("--timeout", "timeout"),
    ("--sleep-idle-seconds", "sleep_idle_seconds"),
]
# 下拉菜单：值必须是固定选项之一（选项列表见 admin.html）
SEL_FIELDS = [
    ("-fa", "fa"), ("--split-mode", "split"), ("-ctk", "ctk"), ("-ctv", "ctv"),
    ("-ctkd", "draft_ctk"), ("-ctvd", "draft_ctv"),
    ("--load-mode", "load_mode"), ("--spec-type", "spec_type"), ("--reasoning", "reasoning"),
    ("--reasoning-effort", "reasoning_effort"), ("--reasoning-format", "reasoning_format"),
    ("--fit", "fit"),
    ("--numa", "numa"), ("--rope-scaling", "rope_scaling"),
]
TEXT_FIELDS = [
    ("--alias", "alias"), ("--device", "device"), ("--tensor-split", "tensor_split"),
    ("--host", "host"), ("--chat-template-kwargs", "chat_kwargs"),
    ("--fit-target", "fit_target"),
    ("--api-key", "api_key"),
]
FILE_FIELDS = [("-m", "model"), ("--mmproj", "mmproj"), ("--model-draft", "draft"),
               ("--chat-template-file", "chat_template_file")]
# 草稿模型文件的三种等价写法（--spec-draft-model / --model-draft / -md），解析时都识别
DRAFT_FLAG = r"(?:--spec-draft-model|--model-draft|-md)"
BOOL_FIELDS = [("--metrics", "metrics"), ("--no-host", "no_host"),
               ("--no-mmproj-offload", "no_mmproj_offload"),
               ("--no-kv-offload", "no_kv_offload"),
               ("--context-shift", "context_shift"),
               ("--no-webui", "no_webui")]

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
        m = re.search(r"(?:^|\s)" + pat + r"\s+('(?:[^']*)'|\"(?:[^\"]*)\"|\S+)", rest)
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

def auto_name(cmd):
    """根据启动命令自动生成可读名称：llama+版本 · 主模型 · VISION · MTP。"""
    if not cmd or not cmd.strip():
        return "（空）"
    d = parse_cmd(cmd)
    parts = []
    binary = d.get("binary") or ""
    m = re.search(r"llama[.\-]?cpp[-_]?v?(\d+(?:\.\d+)*)", binary)
    if m:
        vpre = "v" if re.search(r"v\d", binary[m.start():m.end()]) else ""
        parts.append("llama-" + vpre + m.group(1))
    else:
        m2 = re.search(r"/([^/]+?)/build", binary)
        parts.append("llama" + ("-" + m2.group(1) if m2 else ""))
    model = d.get("model") or ""
    if model:
        name = os.path.basename(model)
        name = re.sub(r"\.gguf$", "", name, flags=re.I)
        name = re.sub(r"-(UD-)?[QFB]\d+[A-Z0-9_]*$", "", name)
        if name:
            parts.append(name)
    if d.get("mmproj"):
        parts.append("VISION")
    if d.get("draft") or "draft-mtp" in (d.get("spec_type") or ""):
        parts.append("MTP")
    return " · ".join(parts)

def diff_brief(cmd, cur):
    """返回 cmd 相对当前命令 cur 的差异参数简写（最小 flag + 值），无差异返回空串。
    历史里缺失/关闭的参数用 ! 前缀标注（如 !--cache-reuse）。"""
    pc = parse_cmd(cur) if cur else parse_cmd("")
    pn = parse_cmd(cmd) if cmd else parse_cmd("")
    parts = []
    if (pc.get("binary") or "") != (pn.get("binary") or ""):
        parts.append(os.path.basename(pn.get("binary") or "") or "（无主程序）")
    for p, k in NUM_FIELDS + SEL_FIELDS + TEXT_FIELDS:
        cv, nv = pc.get(k), pn.get(k)
        if cv != nv:
            if not nv:
                parts.append("!" + p)  # 历史里没有该参数
            elif k == "api_key":
                parts.append(p)  # 敏感值不显示
            else:
                parts.append(p + " " + nv)
    for p, k in FILE_FIELDS:
        cv, nv = pc.get(k), pn.get(k)
        if cv != nv:
            parts.append(("!" + p) if not nv else (p + " " + os.path.basename(nv)))
    for p, k in BOOL_FIELDS:
        cv, nv = pc.get(k), pn.get(k)
        if cv != nv:
            parts.append(p if nv else "!" + p)
    if (pc.get("custom") or "") != (pn.get("custom") or "") and pn.get("custom"):
        parts.append(pn["custom"])
    return " · ".join(parts)

def cmd_info():
    backend, pid, _ = detect_service()
    if backend == "ninfer":
        # Q2-a：NInfer 启动参数由 launcher 脚本管理，管理页只读展示
        return {"backend": "ninfer", "readonly": True,
                "current": _proc_cmdline(pid)
                           or "（服务未运行；启动后此处显示实际启动命令）",
                "parsed": None, "history": []}
    cur = _read_unit_cmd()
    h = _load_hist()
    if cur and (not h or h[0].get("cmd") != cur):
        # 当前命令不在历史顶部时置顶显示（不落盘）
        h = [{"ts": time.time(), "cmd": cur}] + [x for x in h if x.get("cmd") != cur]
    h = h[:HIST_LEN]
    for x in h:
        if not x.get("name"):
            x["name"] = auto_name(x.get("cmd", ""))
        x["diff"] = "" if x.get("cmd") == cur else diff_brief(x.get("cmd", ""), cur)
    return {"current": cur,
            "parsed": parse_cmd(cur) if cur else None,
            "history": h}

def hist_rename(cmd, name):
    """按命令内容定位历史记录并改名（当前置顶项不落盘，不可改名）。"""
    name = (name or "").strip()
    if not cmd or not name:
        return {"ok": False, "error": "命令或名称为空"}
    h = _load_hist()
    for x in h:
        if x.get("cmd") == cmd:
            x["name"] = name
            _save_hist(h)
            return {"ok": True, "history": cmd_info()["history"]}
    return {"ok": False, "error": "未找到该历史记录"}

def hist_delete(cmd):
    """按命令内容定位历史记录并删除（当前置顶项不落盘，不可删除）。"""
    if not cmd:
        return {"ok": False, "error": "命令为空"}
    h = _load_hist()
    for i, x in enumerate(h):
        if x.get("cmd") == cmd:
            h.pop(i)
            _save_hist(h)
            return {"ok": True, "history": cmd_info()["history"]}
    return {"ok": False, "error": "未找到该历史记录"}

def cmd_diff(cmd):
    """对比当前命令与给定命令，返回逐字段差异（选择历史命令时展示用）。"""
    cur = _read_unit_cmd()
    pc = parse_cmd(cur) if cur else parse_cmd("")
    pn = parse_cmd(cmd) if cmd else parse_cmd("")
    diffs = []
    if pc.get("binary") != pn.get("binary"):
        diffs.append({"key": "binary", "flag": "主程序",
                      "cur": pc.get("binary") or "", "new": pn.get("binary") or ""})
    for p, k in NUM_FIELDS + SEL_FIELDS + TEXT_FIELDS + FILE_FIELDS + BOOL_FIELDS:
        if pc.get(k) != pn.get(k):
            diffs.append({"key": k, "flag": p, "cur": pc.get(k), "new": pn.get(k)})
    if pc.get("custom") != pn.get("custom"):
        diffs.append({"key": "custom", "flag": "其他参数",
                      "cur": pc.get("custom") or "", "new": pn.get("custom") or ""})
    return {"diffs": diffs, "same": not diffs, "cur": cur, "new": cmd}

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
    if parent == p or not (parent == FILES_ROOT or parent.startswith(FILES_ROOT + os.sep)):
        parent = None
    return {"path": p, "parent": parent, "dirs": dirs, "files": files}

SPEC_TYPES = {"none", "draft-mtp", "draft-dflash"}

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
    if detect_service()[0] == "ninfer":
        return {"ok": False,
                "error": "NInfer 后端的启动参数由 /mnt/models/ai-launchers/ninfer-v100-duo.sh 管理，不可在管理页编辑"}
    new_cmd = " ".join(new_cmd.split())
    if not new_cmd or "llama-server" not in new_cmd:
        return {"ok": False, "error": "命令为空或不含 llama-server"}
    # 单引号是 systemd ExecStart 唯一的引用字符：整体校验引用必须成对闭合，
    # 值内裸单引号会破坏引用结构、可注入额外参数
    if " ".join(re.findall(r"'[^']*'|[^'\s]+", new_cmd)) != new_cmd:
        return {"ok": False, "error": "命令含非法引号（只允许整个值用单引号包裹）"}
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
        h = [{"ts": time.time(), "cmd": cur, "name": auto_name(cur)}] + [x for x in h if x.get("cmd") != cur]
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
        """SSE 实时推送 journalctl -f 输出（ninfer 的级别写在正文，需按正文过滤）。"""
        if prio not in ("info", "warning", "err"):  # 与 logs_tail 同一白名单
            prio = "info"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        ninfer = detect_service()[0] == "ninfer"
        cmd = ["journalctl", "-u", log_unit(), "-f", "--no-pager", "-q"]
        if not ninfer:
            cmd += ["-p", prio]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            while True:
                r, _, _ = select.select([p.stdout], [], [], 10)
                if r:
                    line = p.stdout.readline()
                    if not line:  # journalctl 退出
                        break
                    if ninfer:
                        if prio == "err" and "[error]" not in line:
                            continue
                        if prio == "warning" and "[error]" not in line and "[warning]" not in line:
                            continue
                    self.wfile.write(("data: " + line.rstrip("\n") + "\n\n").encode())
                    self.wfile.flush()
                else:
                    # 心跳：客户端断连时写失败抛异常，进 finally 回收线程和 journalctl
                    self.wfile.write(b": hb\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.wait(timeout=1)
                except Exception:
                    pass

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
        if path == "/api/admin/cmd/diff":
            if not self._admin_token():
                self._json(401, {"error": "未登录"})
                return
            self._json(200, cmd_diff(self._query().get("cmd", "")))
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
            self._json(200, {"lines": logs_tail(q.get("n", "200"), q.get("prio", "info"), q.get("hours")).splitlines()})
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
        self.send_header("Cache-Control", "no-store")  # 页面迭代快，避免浏览器缓存旧版 JS
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            n = int(self.headers.get("Content-Length", 0))
        except Exception:
            n = 0
        if n > 1048576:  # 业务 body 都很小，设上限防内存打爆
            self._json(413, {"error": "请求体过大"})
            return
        try:
            self.connection.settimeout(30)  # Content-Length 大于实际 body 时不至于无限阻塞
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
        try:
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
            if path == "/api/admin/cmd/hist/rename":
                self._json(200, hist_rename(data.get("cmd", ""), data.get("name", "")))
                return
            if path == "/api/admin/cmd/hist/delete":
                self._json(200, hist_delete(data.get("cmd", "")))
                return
            if path == "/api/admin/password":
                err = change_password(data.get("old", ""), data.get("new", ""))
                if err:
                    self._json(400, {"error": err})
                else:
                    self._json(200, {"ok": True})
                return
            self._json(404, {"error": "未找到"})
        except Exception:
            self._json(500, {"error": "内部错误"})

def main():
    ThreadingHTTPServer.daemon_threads = True
    threading.Thread(target=_disk_temp_worker, daemon=True).start()
    threading.Thread(target=_cpu_temp_worker, daemon=True).start()
    threading.Thread(target=_pcie_worker, daemon=True).start()
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
