# service/

- `dispatch.sh` — ai-server 的启动入口。systemd 托管它，它读取
  `/etc/cortex-console/ai-server.conf` 中 `default` 指向的 section，
  设置 `env` 后 `exec` 该 section 的 `execute`（推理服务前台运行，
  崩溃时 systemd `Restart=on-failure` 拉起）。
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
