# AGENTS.md

这份文档写给维护本仓库的编码 agent，也写给负责部署和运维的人。面向用户的简介见 [README.md](README.md)。

## 项目概况

- 网关是一个 MCP 服务（Python，`uv` 管理）。它把 MCP 工具调用翻译成 ACP（Agent Client Protocol），通过 stdio 驱动本机的编码 agent。一个 MCP 会话对应一个 ACP 会话，支持多轮对话。
- 内置的 agent 是 DeepSeek Harness：`dsh --profile acp`。以后接 pi 走 `pi-acp`，在配置文件里加一项就行。
- 工具名照 [kxress/acp-mcp](https://github.com/kxress/acp-mcp) 起。没有直接用 acp-mcp，原因有两个：它要求 Node ≥ 24.12，而本机是 v22；它的 `load_session` 走 `session/load`，dsh 不支持这个方法。
- 按所有者的决定，网关**不加安全限制**：没有确认环节，没有仓库白名单，不隔离工作区，不限制推送和合并，不设任务超时，不留审计日志，也没有停用开关。权限策略只决定网关怎么回答 agent 的权限请求。

## 代码结构

| 路径 | 内容 |
|---|---|
| `src/dot_gateway/server.py` | MCP 工具定义、工具描述和 `INSTRUCTIONS`、紧凑 JSON 输出、未知路径返回 JSON 404、命令行入口 |
| `src/dot_gateway/sessions.py` | `SessionManager`：会话生命周期、各种视图（final / messages / full）、最终答复只投递一次、工具调用记录、权限策略、重启恢复 |
| `src/dot_gateway/acp.py` | ACP stdio 客户端（换行分隔的 JSON-RPC），容忍 stdout 上的非 JSON 行 |
| `src/dot_gateway/events.py` | 每个会话一份只追加的事件日志 `events.jsonl`，以及截断和原子写入 |
| `src/dot_gateway/agents.py`、`config.py` | agent 注册表、配置项（环境变量 / 命令行参数 / 配置文件） |
| `src/dot_gateway/gitinfo.py` | 每轮开始时记下 git 快照，结束时生成改动摘要 |
| `tests/` | 单元测试和 MCP 测试，用 `tests/fake_acp_agent.py` 当假 agent |
| `scripts/start.sh` | 前台启动（LaunchAgent 也用它） |
| `scripts/smoke_dsh.py` | 真实 dsh 的验收测试（会花一点 DeepSeek 费用） |
| `docs/local.dot-gateway.plist.example` | LaunchAgent 示例 |

## 开发

```sh
uv sync
uv run pytest -q                       # 全部单元测试和 MCP 测试，不花钱
uv run python scripts/smoke_dsh.py     # 真实 dsh：多轮对话、网关完全停止后续接、忙碌、取消、崩溃
scripts/start.sh --port 18765          # 本地试跑时换一个端口，不要占用正式服务的 8765
```

- 别用 heredoc 往本机 shell 里传补丁，可能会卡住。补丁写成文件再执行，执行时加上 `< /dev/null`。
- 测试和试跑的临时文件用完要清掉，包括 `~/.dsh/sessions/<编码后的 cwd>/<会话 id>/` 和 `~/.dsh/storages/session_projcache/sessions/<会话 id>.json`。
- 工作区里只能放通用内容：不写机器名、用户名、家目录绝对路径、邮箱、隧道 id。路径一律写成 `~` 或 `$HOME`。

## 工具语义（简洁输出）

每个工具返回**一个紧凑的 JSON 文本块**（不缩进，也不再附带一份重复的 `structuredContent`），字段统一用 camelCase。

- `start_session` / `load_session` 只返回 `sessionId`、`agentId`、`cwd`、`state`、`live`、`permissionMode`、`permissionPolicy`，再加上 `promptId` 或 `note`（如果有）。
- `prompt` 返回 `{sessionId, promptId, prompt}`。设了 `waitMs` 并且这一轮在时限内跑完，`prompt.text` 就是最终答复，算作已经投递。
- `get_events(view=...)` 的三种视图翻页方式相同，都是 `afterSequence` → `nextSequence` / `hasMore`，`limit` 最大 500，`maxBytes` 最大 1 MiB：
  - `final`（默认）：不返回事件列表，只返回 `prompt`。
    - 运行中时，`prompt` 里有 `progress`：工具调用次数、最近一次工具调用的一行摘要、消息数。
    - 结束后，`prompt` 里有 `text`（这一轮**最后一条** agent 消息，中间的过渡话不算）、`toolCalls`（每次工具调用一行，例如 `bash: ls -la → completed`）和 `gitSummary`。
    - `waitMs`（最大 30000）会一直等到这一轮结束或出现权限请求。
  - `messages`：同一条消息的多个分片合并成一整条；每次工具调用一行，带 `toolCallId`；不含 `usage_update` 这类噪声，也不含思考内容（除非 `includeThoughts`）。如果这一页的 `prompt.text` 已经带了最终答复，对应那条消息只标 `textInPrompt: true`，不重复正文。
  - `full`：原始 ACP `session/update` 事件，外加网关自己的事件。思考内容只有 `includeThoughts=true` 时才给。
- **最终答复只投递一次。** 投递以后再查，只返回 `textBytes` 和 `deliveredAtSequence`。下面三种情况会再给一次：
  - 从这一轮结束之前的游标重新轮询（应对响应丢失）；
  - 传 `includeText=true`；
  - `get_session(detail="full")`，它总是带全部答复。
  
  投递状态保存在 `session.json` 里，网关重启后也不会丢。
- `get_session(detail="summary")` 不带答复正文。`detail="full"` 会带上每一轮的答复、工具调用行和 `toolCallId`、git 摘要、配置项（可选模型）、agent 能力和进程信息。
- `get_tool_call(sessionId, toolCallId, offset, maxBytes)` 返回某次工具调用的完整输入和输出，输出按字节分页（每页最大 256 KiB）。这份完整记录存在 `tool-calls/<id>.json`，每个字符串最多 1 MiB。事件日志里的字符串只保留到 8 KiB。
- `list_sessions` 每个会话只给状态和最近一轮的预览（200 字），不带答复。`close_session` 只返回 `{sessionId, state, note}`。`cancel_prompt` 只返回这一轮的状态。

### 时间约束

隧道每个请求最多等 120 秒，网关保证每个工具调用都在 60 秒内返回：

- `start_session` 带首轮 prompt 时，把 prompt 发给 agent 就返回，实测约 1 秒。
- `prompt` 的 `waitMs` 最大 60000，如果这次调用里重启了 agent，花掉的时间也算在里面。
- `get_events` 的 `waitMs` 最大 30000。
- 启动 agent 时，`initialize`、`session/new`、`session/resume` 每一步最多等 25 秒。
- `cancel_prompt` 最多等 5 秒，还没停下来就用 `force=true` 再调一次。

同一个会话里，一轮还没结束就再调 `prompt`，会返回 `SESSION_BUSY`。不同的会话可以同时跑。

### 事件

- 事件按 `sequence` 从 1 开始递增。单个事件超过 32 KiB 时会被截断，并标记 `truncated`。
- 事件分两类：
  - ACP 的 update：`agent_message_chunk`、`agent_thought_chunk`、`tool_call`、`tool_call_update`、`usage_update`、`plan` 等，`update.sessionUpdate` 也保留着；
  - 网关自己的事件：`session_started`、`prompt_started`（只存 200 字预览和总字数）、`prompt_finished`、`cancel_requested`、`permission_request`、`permission_resolved`、`session_attached`、`agent_exited`、`agent_restart`、`config_set`、`gateway_restarted`、`session_closed`。

## 权限

1. **`permissionMode`：agent 自己的沙箱。** dsh 的取值是 `read-only`、`workspace-write`（默认）、`danger-full-access`，通过 `DSH_PERMISSION_MODE` 生效。`start_session` 里设的是会话默认值，`prompt` 里设的只对这一轮生效。模式改了，网关会重启 agent，再用 `session/resume` 接回会话。
2. **`permissionPolicy`：网关怎么回答 agent 的 `session/request_permission`。** 命名照 acp-mcp：
   - `read_only`（默认）：`read`、`search`、`fetch`、`think` 类的请求选 allow，其他都选 reject；
   - `allow_all`：全部选 allow；
   - `deny_all`：全部选 reject；
   - `operator`：请求挂起，出现在 `pendingPermissions` 里，等 `resolve_permission` 回答。没人回答就一直等着；`cancel_prompt` 会把挂起的请求全部回答为 cancelled。

实测 dsh 0.2.1-rc.2 在 `workspace-write` 模式下，往 cwd 里写文件、跑 shell 命令都不会发权限请求，所以默认的 `read_only` 挡不住它。如果 dsh 发出写入或执行类请求，就要对那一轮传 `permissionPolicy: "allow_all"`。

## 状态、续接与恢复

- 状态目录默认在 `~/Library/Application Support/dot-gateway/`。每个会话一个子目录 `sessions/<sessionId>/`，里面有：
  - `session.json`：元数据、每一轮的状态、答复、投递状态、工具调用摘要。先写临时文件、fsync，再改名替换；
  - `events.jsonl`：完整的事件日志；
  - `tool-calls/<toolCallId>.json`：工具调用的完整记录；
  - `agent-stderr.log`：agent 的 stderr，以及它打在 stdout 上的非 JSON 行。
- `session.json` 记着续接需要的一切：网关 `session_id` 到 `acp_session_id` 的映射（目前两者相同，但网关调 ACP 时一律用后者），以及 `agent_id`、`cwd`、两层权限和配置项。dsh 自己的对话历史存在 `~/.dsh/sessions/` 里。这些都不依赖存活的进程，也不依赖 `/tmp`，所以网关重启、机器重启以后都能续接，前提是 `cwd` 还在。
- **dsh 支持 `session/resume`，不支持 `session/load`。** `load_session` 会看 agent 声明的能力：有 resume 就用 resume，否则退回 load；load 回放的历史 update 在事件日志里标记 `replay: true`。
- 网关重启或崩溃以后：
  - 会话还在；
  - 当时在跑的那一轮标记为 `failed`，并写明原因；
  - 如果残留的 agent 进程命令行对得上，就杀掉它的进程组；
  - 事件日志里记一条 `gateway_restarted`。
  
  下一次 `prompt` 或 `load_session` 会自动用 resume 接回会话。网关收到 SIGTERM 时会停掉所有 agent。
- `load_session` 也能接管 agent 自己保存的会话，这时要传 `agentId` 和 `cwd`。可以先用 `list_sessions(source="agent")` 列出来。
- 旧会话不会自动清理，要清就删 `sessions/` 下对应的目录。
- 机器睡眠或断网时，调用会在隧道层失败。醒来以后会话都还在。睡眠中被打断的那一轮，以 agent 实际给出的结果为准，或者被标记为 `failed`。

## 配置

优先级：命令行参数 > 环境变量 > 配置文件（`<state_dir>/config.json`，也可以用 `DOT_GATEWAY_CONFIG` 指定）里的 `defaults`。

| 环境变量 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| `DOT_GATEWAY_HOST` | `--host` | `127.0.0.1` | 监听地址 |
| `DOT_GATEWAY_PORT` | `--port` | `8765` | 端口 |
| `DOT_GATEWAY_PATH` | `--path` | `/mcp` | MCP 路径（Streamable HTTP，无状态模式） |
| `DOT_GATEWAY_STATE_DIR` | `--state-dir` | `~/Library/Application Support/dot-gateway` | 状态目录 |
| `DOT_GATEWAY_CONFIG` | `--config` | `<state_dir>/config.json` | agent 配置和默认值 |
| `DOT_GATEWAY_DSH_BIN` | — | `/opt/homebrew/bin/dsh` | dsh 路径 |
| `DOT_GATEWAY_DSH_PERMISSION_MODE` | — | `workspace-write` | dsh 默认 permissionMode |
| `DOT_GATEWAY_PERMISSION_POLICY` | — | `read_only` | 默认 permissionPolicy |
| `DOT_GATEWAY_EXTRA_PATH` | — | `/opt/homebrew/bin:/usr/local/bin` | 加到 agent 的 PATH 前面（launchd 下的 PATH 很短） |
| `DOT_GATEWAY_STARTUP_TIMEOUT` | — | `25` | 启动 agent 时每一步最多等几秒 |
| `DOT_GATEWAY_LOG_LEVEL` | `--log-level` | `INFO` | 日志级别，日志写到 stderr |

dsh 的 DeepSeek 密钥在 dsh 自己的配置里，网关不读也不传。切换模型用 `start_session(model=...)` 或 `set_config_option`，可选值看 `get_session(detail="full")` 里的 `configOptions`。设过的值重新接回会话时会再应用一遍。

配置文件示例（加一个 agent）：

```json
{
  "defaults": {"permission_policy": "read_only"},
  "agents": {
    "pi": {"command": "/opt/homebrew/bin/pi-acp", "args": [], "description": "pi via pi-acp"}
  }
}
```

还可以加 `env`、`permission_modes`（`{名字: {"env": {...}, "args": [...]}}`）和 `default_permission_mode`。同样的写法也能覆盖内置的 `dsh`。`DOT_GATEWAY_AGENTS_JSON` 环境变量的格式和这里的 `agents` 相同。

## 容错

- dsh 会往 stdout 打印一行不是 JSON 的日志（模型列表）。ACP 读取端会跳过它，写进 `agent-stderr.log`。
- agent 中途崩溃时，这一轮标记为 `failed`，并记一条 `agent_exited`。下一次 `prompt` 会重新启动 agent，再 resume 会话。
- 网关只认 `/mcp`。其他路径（包括 tunnel-client 探测的 `/.well-known/*` OAuth 发现地址）返回 JSON 404：`{"error":"not_found"}`。网关不声明任何鉴权方式。

## 部署

1. 在 OpenAI Platform 的 Tunnels 设置页创建隧道，关联到用 Dot 的那个 ChatGPT 工作区，记下 tunnel id。再建一个有 Tunnels Read + Use 权限的运行密钥。密钥只在本机输入，不要写进仓库或 plist。
2. 安装 openai/tunnel-client，然后运行：
   ```sh
   tunnel-client init --sample sample_mcp_remote_no_auth --profile dot-gateway \
     --tunnel-id <tunnel-id> --mcp-server-url http://127.0.0.1:8765/mcp
   tunnel-client doctor --profile dot-gateway --explain
   tunnel-client run --profile dot-gateway
   ```
3. 在 ChatGPT 里依次进入 Plugins → + → Add custom MCP server，Connection 选 Tunnel，鉴权选不需要鉴权。创建后应该能看到 12 个工具。

## 运维

- 开机自启：把 `docs/local.dot-gateway.plist.example` 复制到 `~/Library/LaunchAgents/local.dot-gateway.plist`，把里面的占位路径换成本机的实际路径，然后执行 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.dot-gateway.plist`。LaunchAgent 的标签是 **`local.dot-gateway`**。已经用别的标签部署的，可以继续用原来的标签。
- 网关可以设 `KeepAlive`：重启后会话都在，正在跑的那一轮标记为 `failed`，之后可以接着 prompt。
- **代码更新以后要重启网关**才会生效，比如 `launchctl kickstart -k gui/$(id -u)/<标签>`。重启的那一刻，正在跑的那一轮会被标记为 `failed`。
- tunnel-client 也配一个 LaunchAgent，并设置 KeepAlive。机器睡眠怎么处理（比如 `caffeinate`、`pmset`）由运维决定。

## 验收测试（scripts/smoke_dsh.py）

脚本在随机端口上自己启动网关，状态目录和 git 仓库都放在 `~/Library/Application Support/dot-gateway-smoke-*` 这个临时目录里，每次运行都会随机生成代号和暗号。步骤：

1. 第 1 轮：写 notes.md；
2. 第 2 轮：不许用工具，答出代号，并记住一个只在聊天里出现的暗号；
3. 用 SIGTERM 停掉网关，杀掉残留的 dsh；
4. 启动新网关；
5. 第 3 轮：用同一个 sessionId，不许用工具，答出文件名、代号和暗号。要求换了新的 dsh 进程，并且通过 `session/resume` 接回；
6. 另外再测忙碌、取消，以及 SIGKILL 崩溃后的第 4 轮。

最后会清理临时目录和 dsh 的会话记录。
