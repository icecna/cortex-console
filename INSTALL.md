# 安装说明

`install.sh` 部署 Cortex Console（监控面板）+ AI Server（推理服务管理器）。
纯 bash + python3 标准库，无第三方依赖，幂等可重复执行。

## 快速开始

```bash
# 完整安装（cortex-console + ai-server），使用默认路径
sudo ./install.sh

# 只装 Cortex Console（推理服务已存在的机器）
sudo ./install.sh --no-ai-server

# 指定初始管理员密码
sudo ./install.sh --admin-password 'MyStr0ngPass!'

# 指定初始默认后端
sudo ./install.sh --launcher quasar

# 自定义安装目录（默认 /mnt/models/cortex-console 和 /mnt/models/ai-launchers）
sudo ./install.sh --dash-dir /opt/dash --launchers-dir /opt/launchers
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--dash-dir <path>` | `cortex-console 代码目录| cortex-console 代码目录 |
| `--launchers-dir <path>` | `/mnt/models/ai-launchers` | launcher 脚本目录 |
| `--launcher <name>` | 取现有 conf / 首个脚本 | 初始默认后端（仅首次生成 conf 时生效） |
| `--admin-password <pw>` | 随机生成 | 初始管理员密码（仅 `admin.json` 不存在时生效） |
| `--force-conf` | 关 | 覆盖已存在的 `/etc/ai-server.conf`（覆盖前备份 `.bak`） |
| `--no-ai-server` | 关 | 跳过 ai-server 相关（conf、launcher、unit） |
| `--uninstall` | 关 | 卸载：停服务、删 unit；**保留** conf、`admin.json`、日志 |
| `--purge` | 关 | 与 `--uninstall` 连用，连安装目录一起删 |
| `-h / --help` | | 帮助 |

## 安装内容

| 组件 | 目标 | 已存在时 |
|---|---|---|
| cortex-console 代码（`app.py` + 前端） | `$DASH_DIR` | 内容相同跳过，不同则覆盖 |
| `cortex-console.service` | `/etc/systemd/system/` | 不覆盖，diff 提示 |
| launcher 脚本 + `dispatch.sh` | `$LAUNCHERS_DIR` | 同上 |
| `ai-server.service` | `/etc/systemd/system/` | 不覆盖，diff 提示 |
| `/etc/ai-server.conf` | 同路径 | **绝不覆盖**（`--force-conf` 除外） |
| `admin.json` | `$DASH_DIR` | **绝不覆盖** |

## 管理员账号

- 无内置固定默认密码。`admin.json` 不存在时：
  - 给了 `--admin-password` → 用它生成
  - 没给 → 生成 12 位随机密码，**打印到终端**，并写入 `$DASH_DIR/admin-password.txt`（权限 600）
- 密码以 scrypt 加盐哈希存储（`n=2^14, r=8, p=1`），文件权限 600，不明文落盘
- 首次登录后请在 admin 页"密码修改"tab 改为自己的密码，并删除 `admin-password.txt`

## 环境要求与自适应

| 依赖 | 要求 | 缺失时行为 |
|---|---|---|
| python3 | ≥ 3.8（仅标准库） | 报错退出 |
| systemd | 可选 | **降级**：代码照常部署，跳过 unit 安装，打印 `nohup python3 app.py &` 手动启动指引 |
| root 权限 | 写 `/etc` 必需 | 提示 `sudo` 后退出 |
| 端口 80/8090 | 可选 | 被占用时警告（cortex-console 运行时自动跳过占用端口） |

- 全程 `set -euo pipefail`，任一步失败立即退出并打印已完成步骤清单
- 自动 `mkdir -p` 缺失目录；launcher 脚本安装后做 `bash -n` 语法校验
- **不碰**：模型文件、NInfer 编译产物、`admin.json`、`cmd_history.json`
- launcher 脚本内的机器相关路径（模型文件、二进制、host IP）原样复制，**装完按需修改**，改完在 admin 页"后端切换"或 `systemctl restart ai-server` 生效

## 验证

```bash
systemctl status cortex-console ai-server     # 均 active
curl -s http://127.0.0.1:8090/api/status # 200 + JSON
# 浏览器打开 http://<ip>:80/admin 登录
journalctl -u ai-server -f               # 推理服务启动日志
```

## 卸载

```bash
sudo ./install.sh --uninstall        # 停服务 + 删 unit，保留配置和日志
sudo ./install.sh --uninstall --purge  # 连安装目录一起删
```

卸载保留清单：`/etc/ai-server.conf`、`$DASH_DIR/admin.json`、`$DASH_DIR/cmd_history.json`、journal 日志。

## 测试方法

### 单元级（临时目录，不碰真实服务）

```bash
./install.sh --dash-dir /tmp/t/dash --launchers-dir /tmp/t/launchers --no-ai-server
```

- [ ] 首次安装：文件齐全，`bash -n` 通过，`admin.json` 生成且权限 600
- [ ] 二次安装：全部跳过，零写入（`find /tmp/t -newer <标记文件>` 验证幂等）
- [ ] 手工修改 conf 后重装：conf 不被覆盖
- [ ] 非 root 运行：清晰报错提示 sudo
- [ ] `--uninstall`：unit 删除、目录保留；加 `--purge` 目录删除

### 集成级（真实环境）

- [ ] 完整安装后两个 unit 均 active，`:8090/api/status` 返回 200
- [ ] admin 页可登录，后端切换一次再切回，conf/服务/参数表单联动正常

### 环境适配级

- [ ] 无 systemd（容器）：部署成功、打印手动启动指引、正常退出码
- [ ] python3.8/3.9 容器：`python3 -c "import app"` 冒烟通过
- [ ] `--no-ai-server`：ai-server 相关零写入
- [ ] `/etc` 只读：清晰报错，无半安装状态