# Terminal Control MCP

通过 SSH 管理远程 Linux 终端的本地 STDIO MCP 服务。支持多主机、跳板连接、
前台命令、后台任务，以及 SFTP 文件写入、编辑和校验上传。

服务安装在运行 MCP 客户端的机器上；远程机器只需 SSH 服务和 SFTP，运行命令时使用远端账号的 shell。
后台任务额外需要 Bash、`setsid`、`nohup`、`pgrep` 和 `pkill`（Ubuntu 通常已安装）。

- 运行环境：Linux，Python 3.10+。
- 连接方式：内网 IP、公网 IP、DNS 或 Tailscale 地址；不绑定任何特定网络。
- 认证方式：独立的本地密码文件，或专用 SSH 私钥。
- 部署配置：保存在用户配置目录，和源代码分离；仓库不包含生产主机或凭据。

## 快速部署

### 1. 安装

```bash
git clone https://github.com/AhernResearch/terminal-control-mcp.git
cd terminal-control-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
```

也可使用 [uv](https://docs.astral.sh/uv/) 从锁文件安装：

```bash
uv sync --locked
```

下面命令均假设位于仓库根目录。私有仓库需要先配置 GitHub 访问权限。

### 2. 初始化并编辑主机清单

```bash
.venv/bin/terminal-control-mcp init
```

命令创建：

```text
~/.config/terminal-control-mcp/       # 0700
├── targets.yaml                    # 0600：主机、用户名、认证引用
├── known_hosts                     # 初始为空，后续录入并验证 SSH 主机密钥
└── uploads/                        # 允许通过 file_upload 读取的本地目录
```

编辑 `targets.yaml`，将 `my-server.host` 改成目标地址，将 `defaults.username` 改成 SSH 用户。
生成的 `192.0.2.10` 是文档示例地址，需要替换。
密码不会写入 YAML；使用 `credential_ref` 引用独立凭据文件。

### 3. 登记 SSH 主机密钥

使用与配置一致的地址、端口和用户名首次 SSH 登录，核对服务器指纹后接受：

```bash
ssh -o UserKnownHostsFile="$HOME/.config/terminal-control-mcp/known_hosts" operator@SERVER_IP
```

非默认端口增加 `-p PORT`。密码模式用下面命令保存密码；私钥模式见[配置文档](docs/configuration.md)。

```bash
.venv/bin/terminal-control-mcp credentials set server-default
```

### 4. 验证

```bash
# 离线检查：配置格式、路径和权限，不建立 SSH 连接
.venv/bin/terminal-control-mcp validate --config-only
# 只检查指定目标
.venv/bin/terminal-control-mcp validate --target my-server
# 直接执行，便于在接入 MCP 前诊断
.venv/bin/terminal-control-mcp run my-server 'whoami; hostname'
# 检查所有配置目标
.venv/bin/terminal-control-mcp validate
```

`list` 列出目标 ID；`--version` 显示版本；`--help` 查看全部命令。
`run` 在远程命令失败时返回非零退出码，JSON 内保留原始远程退出码。

### 5. 接入 MCP 客户端

通用 STDIO 配置；将 `command` 替换为本机安装后的绝对路径：

```json
{
  "mcpServers": {
    "terminal-control": {
      "command": "/absolute/path/to/terminal-control-mcp/.venv/bin/terminal-control-mcp",
      "args": ["serve"]
    }
  }
}
```

原有 Codex CLI 注册方式仍然可用：

```bash
codex mcp add terminal-control -- "$PWD/.venv/bin/terminal-control-mcp" serve
```

每台客户端机器分别安装和配置。修改清单后重启客户端中的 MCP 服务，重新加载配置和连接池。
通过 STDIO 启动，无需开放额外 HTTP 端口或配置 systemd 常驻服务。

## MCP 工具

| 工具 | 功能 |
| --- | --- |
| `health_check` | 检查本地服务状态，不连接远程 |
| `list_targets` | 返回目标 ID，不返回地址和凭据 |
| `get_target_status` | 检查 SSH 连通性、用户名和主机名 |
| `run_command` | 前台执行，或 `background=true` 启动后台任务 |
| `task_control` | `list` / `status` / `cancel` 后台任务 |
| `file_write` | 原子写入 UTF-8 文件，最大 10 MiB |
| `file_edit` | 原子替换一处唯一匹配的文本 |
| `file_upload` | 上传二进制文件，最大 512 MiB，远端 SHA256 校验后原子替换 |

目标选择器支持独立主机 `my-server`，组默认节点 `lab`（即 `lab:media`），
指定节点 `lab:main`，以及组广播 `lab:*`。后台任务管理只支持单个具体目标。

前台命令默认超时 30 秒，最大 300 秒；长任务使用后台模式。
同一服务进程复用 SSH 连接，空闲 300 秒后在下一次操作时清理；不会对已提交的命令自动重试。
`duration_ms` / `latency_ms` 包含建连、认证和命令执行开销，不等于网络 ping 延迟。

## 配置与迁移

- [配置字段、密码/私钥、跳板连接](docs/configuration.md)
- [多机部署、迁移、网络排查](docs/deployment.md)
- [完整示例](configs/targets.yaml.example)

密码文件使用 0600 权限，未加密；私钥同样要求 0600。
`allowed_write_roots` 约束文件工具，`allowed_local_read_roots` 约束本地上传来源。
`run_command` 按 SSH 账号权限执行任意 shell 命令，不受这两个路径列表限制。
后台任务注册保存在 MCP 进程内存中：重启后远端任务继续运行，但不再出现在新进程的任务列表中。

## 开发与验证

```bash
uv sync --locked --extra dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
uv build
```

测试使用临时配置和本地 SSH 测试服务，不连接生产主机。
GitHub Actions 在 Python 3.10–3.13 上执行检查、测试和 wheel 构建。

```text
src/terminal_control_mcp/
├── cli.py / setup.py       # 初始化、命令行与连接诊断
├── config.py / models.py   # 配置校验与数据模型
├── credentials.py          # 本地凭据文件
├── registry.py             # 主机和组选择器
├── transport.py            # SSH 连接池、执行、超时和输出限制
├── tasks.py                # 远程后台任务
├── filesystem.py           # SFTP 文件操作
└── server.py / service.py  # MCP 接口与业务协调
```

MIT License.
