# 部署、迁移与排查

## 部署到另一台客户端机器

1. 安装 Python 3.10+、Git 和该机器的 MCP 客户端。
2. 克隆仓库，使用 `uv sync --locked` 或虚拟环境安装。
3. 运行 `terminal-control-mcp init`，编辑地址和 SSH 用户。
4. 在该机器上验证并登记 SSH 主机密钥，重新录入密码或安装专用私钥。
5. 运行 `validate --config-only` 和 `validate --target HOST_ID`。
6. 将该机器的可执行文件绝对路径注册到 MCP 客户端，启动服务。

安装服务不会改变网络路由、SSH 服务或防火墙。
目标可以通过办公内网、公网、VPN 或 Tailscale 到达；配置填入该机器实际可达的地址。
Windows 原生客户端不在支持范围内；可在 WSL Linux 内安装和运行。

## 迁移已有配置

现有 `targets.yaml` 和旧版 `vehicles` 字段均可继续使用。
修改其中包含旧用户名的绝对路径，替换上传源目录、私钥文件和 known_hosts 文件位置。
源机器和新机器应分别保存这些私密文件；不要放入 Git 仓库。
如需复制私密配置，使用可信的加密传输，然后检查：

```bash
chmod 700 ~/.config/terminal-control-mcp
chmod 600 ~/.config/terminal-control-mcp/targets.yaml
chmod 700 ~/.config/terminal-control-mcp/credentials
chmod 600 ~/.config/terminal-control-mcp/credentials/*
```

使用自定义配置路径时，显式设置 `TERMINAL_CONTROL_MCP_CREDENTIALS_DIR`，
或保留默认凭据目录。`init` 不覆盖已有清单，也不会覆盖已登记的主机密钥。

## 更新或回滚

```bash
git pull --ff-only
uv sync --locked
terminal-control-mcp --version
```

将最后一行替换为实际虚拟环境路径（如 `.venv/bin/terminal-control-mcp`）。
更新后重启 MCP 服务，已运行的进程不会自动加载新代码或清单。
若需回滚，可切换到已验证的 Git 提交或 tag，再执行 `uv sync --locked`。

## 排查连接慢或失败

先检查目标地址实际走哪条路：

```bash
ip route get SERVER_IP
ping -c 5 SERVER_IP
```

内网连接应走预期的网卡和网关。Docker 网段和公司网段重叠时，
目标可能被错误路由到 `docker0`；代理 TUN 也可能接管目标地址。
按该机器的网络拓扑配置路由，不复制其他机器的网关或网卡名称。

然后分别检查 SSH 和 MCP：

```bash
ssh -v -o UserKnownHostsFile="$HOME/.config/terminal-control-mcp/known_hosts" operator@SERVER_IP
terminal-control-mcp validate --target my-server
terminal-control-mcp run my-server 'whoami; hostname; printf "%s\n" "$SSH_CONNECTION"'
```

新的 CLI 进程每次独立建连；长期运行的 MCP 进程复用连接。
首次请求包含 SSH 握手、认证和 SFTP HOME 查询，通常比后续请求耗时更长。
比较网络延迟请使用 ping，比较命令执行请看复用连接后的 `duration_ms`。

| 错误或现象 | 检查方向 |
| --- | --- |
| `CONFIG_ERROR` | 文件权限、绝对路径、未知字段、私钥和 known_hosts 是否存在 |
| `CREDENTIAL_UNAVAILABLE` | 引用名是否一致；密码是否在当前用户/当前环境目录中配置 |
| `HOST_KEY_ERROR` | 用配置相同地址或 alias 核对主机密钥；更换 IP 后也需要登记 |
| `AUTH_FAILED` | 用户名、密码、私钥和远端 SSH 认证策略 |
| `UNREACHABLE` / `TIMEOUT` | 路由、DNS、端口、防火墙或跳板连接 |
| SSH 命令成功，文件工具失败 | 远端 SFTP subsystem、目录权限、符号链接和允许路径 |
| 连接成功但长时间没有响应 | shell 启动脚本、远程程序状态、命令超时或输出缓冲 |
| 清单修改后仍连接旧地址 | 重启对应 MCP 服务，清理旧配置和连接池 |

后台任务输出位于远端 `~/.cache/terminal-control/tasks/`。
MCP 重启后任务仍可能运行，但新进程不会恢复旧任务注册；可以通过该目录和系统进程检查。
