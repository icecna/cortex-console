# Cortex Console

> **声明：本项目 100% 由 AI 完成** —— 全部代码（后端 `app.py`、前端页面、样式、安装脚本、文档）
> 均由 AI 编程助手编写，人类仅负责提出需求、确认方案与验收测试。

推理服务 + 硬件监控面板。纯 Python 标准库实现（无第三方依赖），单文件后端 `app.py` + 静态前端。

- 监控页 `http://<ip>:80` 或 `:8090`（`index.html`）：GPU / PCIe / 内存 / 磁盘 / 推理指标
- 管理页 `/admin`（`admin.html`）：服务控制、后端切换、启动参数编辑、实时日志、生成测试

安装见 [INSTALL.md](INSTALL.md)（`sudo ./install.sh`，幂等，支持 `--uninstall`）。

## 架构

```
┌────────────────────────────┐     ┌─────────────────────────────────────────┐
│  cortex-console.service    │     │  ai-server.service                       │
│  python3 app.py :80/8090   │     │  dispatch.sh                             │
│                            │     │    └─ 读 /etc/cortex-console/ai-server.conf │
│  只读监控 + 管理 API        │◄────│       的 default 指向的 section          │
│                            │     │    └─ exec section 里的 execute 二进制    │
└────────────────────────────┘     └─────────────────────────────────────────┘
```

- **cortex-console** 不承载推理流量，只做监控/管理，挂了不影响推理服务
- **ai-server** 由 systemd 拉起，`Restart=on-failure` 兜底崩溃自愈
- 两者解耦：切换后端 = 改 conf 的 `default` 行 + 重启 ai-server，cortex-console 无需重启

## 后端配置

配置分两层（INI 风格，值可用引号包裹）：

**主文件** `/etc/cortex-console/ai-server.conf` —— 只放 default 行：

```ini
default = quasar          # 当前激活的后端（只有一个生效）
include /etc/cortex-console/models.d/*.conf   # 自动加载各后端配置
```

**每个后端一个文件** `/etc/cortex-console/models.d/<name>.conf`：

```ini
[quasar]
server-type = "ninfer"    # 必填：推理框架（ninfer / llama.cpp）
execute = "/mnt/models/xxx/apps/ninfer-serve"   # 必填，绝对路径
model   = "/mnt/models/ninfer-models/model.ninfer"   # 可选
model_flag = "-m"              # 可选：缺失=位置参数 / "-m"=标志 / "model="=键值融合
env     = "K=V K2=V2"        # 可选：exec 前设置的环境变量（空格分隔）
cmdline = "--tp 2 --port 8080"   # 可选，不含 model
```

- 每个 section = 一个后端（llama.cpp / NInfer / 任意推理服务），独立成文件，互不干扰
- `server-type` 声明推理框架（`ninfer` / `llama.cpp`），决定管理页显示哪种参数表单；缺失时回退按 `execute` 文件名推断
- `dispatch.sh` 每次启动读 `default` 指向的 section 并 exec；主文件里的同名 section **整体覆盖** include 里的（不是按字段合并）
- 管理页「新建 section」和「启动参数」表单直接读写各自的 `models.d/<name>.conf`（原子写 + `.bak`），
  「后端切换」只改主文件的 `default` 行；**定义在主文件里的 section 管理页不写回**（写回会被主文件覆盖、不生效），只能手工编辑

### 通过管理页切换

admin 页 → 服务控制 tab → **🛠️ 服务控制** 卡片内的后端切换下拉框：

1. 下拉框自动枚举 conf（含 include）中所有 section
2. 点击"切换并重启"后（校验 section 存在且 execute 文件存在）：
   - 备份 conf 到 `/etc/cortex-console/ai-server.conf.bak`
   - 临时文件 + `os.replace` 原子改写 `default =` 行（只改该行，其余逐字保留）
   - `systemctl restart ai-server`
   - 前端轮询服务状态最多 3 分钟（等模型加载），期间禁用启停按钮
   - 切换成功后自动刷新"启动参数"表单（后端变了，参数跟着变）

### 新建 section / 编辑参数（管理页）

- **新建 section**：admin 页 → 启动参数 tab → **📝 编辑配置**：
  输入名字 + 选 server-type（ninfer / llama.cpp）→ 创建骨架 section（不改 `default`，不影响当前服务）；
  下拉框自动选中它填参数，保存后用「服务控制 → 后端切换」激活
- **编辑参数**：admin 页 → 启动参数 tab，按 server-type 显示对应结构化表单
  （NInfer / llama），可改主程序、模型、全部参数，"保存并重启"会重启 ai-server 并轮询等待就绪
- 每个后端的配置单独在 `/etc/cortex-console/models.d/<name>.conf`，可手工编辑（改完 `systemctl restart ai-server`）；主文件只放 `default` 行
- 新机器从零到可用：安装 → 管理页新建 section → 启动参数 tab 填参数 → 保存并重启，全程不用 SSH

### 命令行切换

```bash
sed -i 's/^default = .*/default = <section名>/' /etc/cortex-console/ai-server.conf
systemctl restart ai-server
journalctl -u ai-server -f   # 看启动日志

# 只预检不启动（看 default section 会跑出什么命令、配置是否有效）
python3 service/ai_conf.py check /etc/cortex-console/ai-server.conf
CORTEX_DRY_RUN=1 service/dispatch.sh
```

### 新增一个后端

在 `/etc/cortex-console/models.d/` 下新建一个文件（或在管理页「启动参数 → 编辑配置 → 新建模型」）：

```ini
# models.d/my-backend.conf
[my-backend]
server-type = "ninfer"    # 或 "llama.cpp"
execute = "/path/to/server"
model = "/path/to/model"
cmdline = "--port 8080"
```

完成——下拉框自动出现新选项，`dispatch.sh` 和 cortex-console 代码零改动。

## API

### 公开（无需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 全量监控快照（GPU/PCIe/内存/磁盘/推理指标），1s 轮询 |

### 管理（需 admin 登录，`X-Admin-Token` 请求头）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/admin/login` | `{password}` → `{token}` |
| GET | `/api/admin/service` | 服务状态（active/pid/启动时间/控制单元） |
| POST | `/api/admin/service` | `{action: start/stop/restart}` |
| GET | `/api/admin/launcher` | 当前 default section + 全部 section 列表 |
| POST | `/api/admin/launcher` | `{launcher: <section名>}` 切换 default 并重启（原子改 conf + 备份） |
| POST | `/api/admin/ai-conf/new` | 新建 section 骨架（`{name, server_type}`），不改 default（创建 ≠ 激活） |
| POST | `/api/admin/ai-conf/delete` | 删除非激活 section（`{name}`，仅 models.d/ 下定义的） |
| GET | `/api/admin/health` | 进程 CPU/内存/运行时长 + API 探测 |
| GET | `/api/admin/cmd` | 当前启动参数（NInfer：conf section + 运行进程实际 cmdline；llama：渲染命令 + 历史） |
| POST | `/api/admin/cmd` | 保存 llama 启动命令（`{cmd, restart}`，校验模型文件存在等） |
| POST | `/api/admin/ninfer` | 保存 NInfer 结构化参数（`{fields, restart}`，跨字段互斥/范围校验） |
| GET | `/api/admin/cmd/diff` | 对比命令差异（`?cmd=`） |
| POST | `/api/admin/cmd/hist/rename`、`/api/admin/cmd/hist/delete` | 启动命令历史改名/删除 |
| GET | `/api/admin/logs` | 日志尾部（`?n=200&prio=info&hours=`） |
| GET | `/api/admin/logs/stream` | SSE 实时日志流 |
| GET | `/api/admin/logs/download` | 下载日志（原始行） |
| POST | `/api/admin/test` | 快速生成测试（`{prompt, n_predict}`） |
| GET | `/api/admin/files`、`/api/admin/devices` | 文件浏览（限 `/mnt/models`）/ GPU 设备列表 |
| POST | `/api/admin/password` | 修改管理员密码 |

> SSE 流和日志下载无法自定义请求头，token 也可经 `?token=` 查询参数传递。

## 运维

```bash
systemctl status ai-server cortex-console
journalctl -u ai-server -f          # 推理服务日志
journalctl -u cortex-console -f   # cortex-console 日志
```

- 管理员账号存 `admin.json`（权限 600，scrypt 加盐哈希），密码在 admin 页"密码修改"tab 修改
- 启动命令历史存 `cmd_history.json`（权限 600）
- 修改 `app.py` 后需 `systemctl restart cortex-console`；前端/CSS 改动刷新页面即可

## 文件

```
app.py        后端（HTTP server + 采样线程 + 管理 API）
index.html    监控页
admin.html    管理页
themes.html   主题选择页
style.css     主样式
themes.css    主题皮肤
install.sh    安装/卸载脚本（含旧版 LAUNCHER 配置自动迁移）
conf.sample   ai-server.conf 主文件示例（首次安装时生成 /etc/cortex-console/ai-server.conf）
conf.sample.d/  示例 section 文件（首次安装时部署到 /etc/cortex-console/models.d/）
service/ai_conf.py    conf 解析 + argv 构建的唯一实现（dispatch 与管理页共用；带 check/dump/launch CLI）
service/dispatch.sh   ai-server 启动入口（薄壳，调 ai_conf.py launch）
cortex-console.service.in   systemd unit 模板（路径占位符由 install.sh 替换）
ai-server.service.in        同上
README.md     本文档
INSTALL.md    安装说明
```