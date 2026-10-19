# 配置参考

配置默认路径：`~/.config/terminal-control-mcp/targets.yaml`。
优先级是 `--config PATH` > `TERMINAL_CONTROL_MCP_CONFIG` 环境变量 > 默认路径。
`--config` 是全局参数，放在子命令之前：

```bash
terminal-control-mcp --config ~/.config/lab-terminal/targets.yaml init
terminal-control-mcp --config ~/.config/lab-terminal/targets.yaml validate --config-only
```

配置文件必须为当前用户所有、权限 0600，直接父目录权限 0700，不允许配置文件或其直接父目录为符号链接。
字段严格校验；未知字段、重复 YAML 键和同名主机/组都会报错。

## 字段

| 位置 | 字段 | 说明 |
| --- | --- | --- |
| 根 | `version` | 整数 `1` |
| 根 | `defaults` | 各主机和节点继承的 SSH 参数 |
| 根 | `hosts` | 独立主机字典；主机 ID 不能与组 ID 重复 |
| 根 | `groups` | 每组包含 `media` 和 `main`；旧字段 `vehicles` 仍兼容，两者不能并用 |
| 根 | `allowed_local_read_roots` | 本地上传源目录，支持绝对路径和 `~` 展开，目录必须存在 |
| 节点 | `host` | IP 或域名，必填，不放在 defaults 中 |
| 默认/节点 | `username` | SSH 用户 |
| 默认/节点 | `port` | 1–65535 |
| 默认/节点 | `credential_ref` | 密码文件引用，与 `client_key_file` 二选一 |
| 默认/节点 | `client_key_file` | SSH 私钥路径，要求当前用户所有、0600、普通文件，不能是符号链接 |
| 默认/节点 | `known_hosts_file` | 已验证的 SSH 主机密钥文件，必须存在 |
| 默认/节点 | `connect_timeout` | 建连超时，1–120 秒 |
| 默认/节点 | `allowed_write_roots` | 远端文件操作目录；使用 `~` 表示远端 HOME，其他条目使用绝对路径 |
| 节点 | `via` | 仅组的 `main` 节点使用，值为 `media` |
| 节点 | `host_key_alias` | SSH 密钥验证时的主机别名；组的 `main` 节点必填 |

主机 ID 和组 ID 使用小写字母、数字、连字符或下划线，并以字母或数字开头。
节点字段覆盖默认值。节点指定 `client_key_file` 会覆盖默认密码认证；反之亦然。
路径请使用绝对路径或 `~`，不要依赖 MCP 客户端启动时的工作目录。
省略 `allowed_local_read_roots` 的旧配置默认允许从配置目录上传；新初始化配置明确使用 `uploads/`。

## 密码认证

```bash
terminal-control-mcp credentials set server-default
terminal-control-mcp credentials check server-default
terminal-control-mcp credentials delete server-default
```

交互输入密码并确认，不在命令参数或 YAML 中存放密码。
默认文件路径是 `~/.config/terminal-control-mcp/credentials/REFERENCE`。
目录和其直接父目录必须 0700，凭据文件必须 0600，全部属于当前用户。
覆盖密码会提示确认，也可用 `credentials set REFERENCE --force` 跳过覆盖确认。
`--force` 仍会交互读取和确认新密码。

使用 `TERMINAL_CONTROL_MCP_CREDENTIALS_DIR` 设置独立凭据目录。
`--config` 只切换清单文件，不切换凭据目录；多个独立环境可分别设置这个环境变量。

## 私钥认证

先生成并安装专用密钥：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/terminal-control-gpu
ssh-copy-id -i ~/.ssh/terminal-control-gpu.pub operator@SERVER_IP
chmod 600 ~/.ssh/terminal-control-gpu
```

节点配置示例：

```yaml
hosts:
  gpu-server:
    host: 192.0.2.20
    username: operator
    client_key_file: ~/.ssh/terminal-control-gpu
```

当前版本直接读取配置中的私钥文件，不使用 SSH agent，也不支持交互解锁加密私钥。
使用无需交互的专用密钥，按需要在远端 `authorized_keys` 设置访问限制。

## 双节点和跳板连接

```yaml
groups:
  lab:
    media:
      host: 192.0.2.30
    main:
      host: 192.0.2.40
      via: media
      host_key_alias: lab-main
```

客户端直接连接 `media`，再通过该 SSH 连接访问 `main`；`media` 的 SSH 服务需要允许 TCP forwarding。
两个节点可分别覆盖用户名、密码引用、私钥、端口和 known_hosts 文件。
目前组结构固定为这两个节点；独立主机不支持 `via`，不读取 OpenSSH `ProxyJump`。

`main` 的 known_hosts 条目使用配置的别名，例如：

```text
lab-main ssh-ed25519 BASE64_PUBLIC_KEY
```

通过可信渠道核对主机密钥后写入。非 22 端口的记录使用 `[lab-main]:PORT`。
为每组分配唯一别名，避免不同内网中重复 IP 的主机密钥混淆。

## MCP 环境变量示例

```json
{
  "mcpServers": {
    "terminal-control": {
      "command": "/absolute/path/to/.venv/bin/terminal-control-mcp",
      "args": ["serve"],
      "env": {
        "TERMINAL_CONTROL_MCP_CONFIG": "/home/operator/.config/lab-terminal/targets.yaml",
        "TERMINAL_CONTROL_MCP_CREDENTIALS_DIR": "/home/operator/.config/lab-terminal/credentials"
      }
    }
  }
}
```

上传文件路径在 MCP 服务所在机器上解析；不会从浏览器或对话客户端自动取文件。
