# AI Server Dashboard

推理服务 + 硬件监控面板。纯 Python 标准库实现（无第三方依赖），单文件后端 `app.py` + 静态前端。

- 监控页 `http://<ip>:80` 或 `:8090`（`index.html`）：GPU / PCIe / 内存 / 磁盘 / 推理指标
- 管理页 `/admin`（`admin.html`）：服务控制、后端切换、启动参数编辑、实时日志、生成测试

安装见 [INSTALL.md](INSTALL.md)（`sudo ./install.sh`，幂等，支持 `--uninstall`）。

## 架构

```
┌──────────────────────────┐     ┌─────────────────────────────────────────┐
│  dashboard.service       │     │  ai-server.service                       │
│  python3 app.py :80/8090 │     │  dispatch.sh                             │
│                          │     │    └─ 读 /etc/ai-server.conf 的 LAUNCHER │
│  只读监控 + 管理 API      │◄────┤    └─ exec /mnt/models/ai-launchers/     │
│                          │     │         <launcher>.sh                    │
└──────────────────────────┘     └─────────────────────────────────────────┘
```

- **dashboard** 不承载推理流量，只做监控/管理，挂了不影响推理服务
- **ai-server** 由 systemd 拉起，`Restart=on-failure` 兜底崩溃自愈
- 两者解耦：切换后端 = 改一行配置 + 重启 ai-server，dashboard 无需重启

## 后端切换

`/etc/ai-server.conf` 只有一个有效变量：

```bash
#LAUNCHER=ninfer-v100-duo
#LAUNCHER=ninfer-v100x2
#LAUNCHER=ninfer-v100x2-dflash
LAUNCHER=quasar
#LAUNCHER=llama-server
```

`dispatch.sh` 每次启动读取**第一个**未注释的 `LAUNCHER=` 行，exec 对应脚本。

### 通过管理页切换

admin 页 → 服务控制 tab → **🔀 后端切换**：

1. 下拉框自动枚举 `/mnt/models/ai-launchers/*.sh`（排除 `dispatch.sh`），说明文字取自 conf 顶部注释行
2. 点击"切换并重启"后：
   - 备份 conf 到 `/etc/ai-server.conf.bak`
   - 临时文件 + `os.replace` 原子改写 LAUNCHER 行（只改 LAUNCHER 行，其余逐字保留）
   - `systemctl restart ai-server`
   - 前端轮询服务状态最多 3 分钟（等模型加载），期间禁用启停按钮
   - 切换成功后自动刷新"启动参数"表单（后端变了，参数跟着变）

### 命令行切换

```bash
sed -i 's/^LAUNCHER=.*/LAUNCHER=<name>/' /etc/ai-server.conf
systemctl restart ai-server
journalctl -u ai-server -f   # 看启动日志
```

### 新增一个后端

1. 写 `/mnt/models/ai-launchers/<name>.sh`（可执行，`exec` 起服务进程，监听 8080）
2. 在 `/etc/ai-server.conf` 注释区加一行说明（可选，下拉框会显示）：
   ```
   #   <name>   说明文字
   ```
3. 完成——下拉框自动出现新选项，`dispatch.sh` 和 dashboard 代码零改动

## API

### 公开（无需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 全量监控快照（GPU/PCIe/内存/磁盘/推理指标），1s 轮询 |

### 管理（需 admin 登录，`X-Admin-Token` 请求头）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/admin/login` | `{password}` → `{token}` |
| GET | `/api/admin/service` | 服务状态（active/pid/启动时间） |
| POST | `/api/admin/service` | `{action: start/stop/restart}` |
| GET | `/api/admin/launcher` | 当前 LAUNCHER + 可选后端列表 |
| POST | `/api/admin/launcher` | `{launcher: <name>}` 切换并重启（原子改 conf + 备份） |
| GET | `/api/admin/health` | 进程 CPU/内存/运行时长 + API 探测 |
| GET | `/api/admin/cmd` | 当前启动参数（ninfer 后端读 launcher 脚本/进程 cmdline） |
| POST | `/api/admin/cmd` | 保存启动参数（`{cmd, restart}`） |
| POST | `/api/admin/ninfer` | 保存 NInfer 结构化参数（`{fields, restart}`） |
| GET/POST | `/api/admin/cmd/diff`、`/api/admin/cmd/hist/rename`、`/api/admin/cmd/hist/delete` | 历史配置管理 |
| GET | `/api/admin/logs` | 日志尾部（`?n=200&prio=info&hours=`） |
| GET | `/api/admin/logs/stream` | SSE 实时日志流 |
| GET | `/api/admin/logs/download` | 下载日志 |
| POST | `/api/admin/test` | 快速生成测试（`{prompt, n_predict}`） |
| GET | `/api/admin/files`、`/api/admin/devices` | 文件浏览 / 模型设备列表 |
| POST | `/api/admin/password` | 修改管理员密码 |

## 运维

```bash
systemctl status ai-server dashboard
journalctl -u ai-server -f          # 推理服务日志
journalctl -u dashboard -f          # dashboard 日志
```

- 管理员账号存 `admin.json`（git 忽略），密码 scrypt 加盐
- 启动参数历史存 `cmd_history.json`（git 忽略）
- 修改 `app.py` 后需 `systemctl restart dashboard`；前端/CSS 改动刷新页面即可

## 文件

```
app.py        后端（HTTP server + 采样线程 + 管理 API）
index.html    监控页
admin.html    管理页
themes.html   主题选择页
style.css     主样式
themes.css    主题皮肤
```