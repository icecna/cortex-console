# service/

- `ai_conf.py` — **conf 解析与 argv 构建的唯一实现**：`parse_conf()`（include/section/引号/
  覆盖语义）、`build_argv()`、`env_pairs()`。ai-server 启动入口与管理页（`app.py`）共用，
  不存在两份解析漂移。也可单独跑：
  - `ai_conf.py launch <conf>` — 解析 + 校验 + exec
  - `ai_conf.py check <conf>` — 只校验 default section 能否启动（`install.sh` 预检用）
  - `ai_conf.py dump <conf>` — 打印解析结果（排障）
- `dispatch.sh` — ai-server 的启动入口（薄壳）：`exec python3 ai_conf.py launch <conf>`。
  systemd 托管它，它读 `/etc/cortex-console/ai-server.conf` 中 `default` 指向的 section，
  设置 `env` 后 `exec` 该 section 的 `execute`（推理服务前台运行，崩溃时 systemd
  `Restart=on-failure` 拉起）。`CORTEX_DRY_RUN=1` 只打印命令不启动；`AI_SERVER_CONF=...`
  可指定其他 conf（测试用）。
- `cortex-console.service.in` / `ai-server.service.in` — systemd unit 模板，
  `install.sh` 用 sed 替换 `__DASH_DIR__` / `__LAUNCHERS_DIR__` / `__WORK_DIR__`
  后安装到 `/etc/systemd/system/`。

## 新增一个后端

在 `/etc/cortex-console/models.d/` 下新建一个文件（或在管理页「启动参数 → 编辑配置 → 新建模型」）：

```ini
# models.d/my-backend.conf
[my-backend]
server-type = "ninfer"    # 或 "llama.cpp"
execute = "/path/to/server"
model = "/path/to/model"
cmdline = "--port 8080"
```

然后在管理页「服务控制 → 后端切换」激活（或命令行 `sed -i 's/^default = .*/default = my-backend/' /etc/cortex-console/ai-server.conf && systemctl restart ai-server`）。

`dispatch.sh` 和 cortex-console 代码不需要任何改动。
