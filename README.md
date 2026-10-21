# dot-gateway

让 ChatGPT Dot 通过 OpenAI Secure MCP Tunnel，跟本机的编码 agent（默认是 DeepSeek Harness `dsh --profile acp`）进行**多轮会话**。Dot 可以一轮接一轮地追问，agent 会记得上下文；网关重启、机器重启以后，用同一个 `sessionId` 还能接着聊。

## 架构

```mermaid
flowchart LR
    Dot["ChatGPT Dot"] -- "MCP 工具调用" --> T["Secure MCP Tunnel"]
    T --> GW["本机上的 dot-gateway"]
    GW -- "ACP" --> DSH["dsh"]
    GW -- "ACP" --> MORE["……"]
```

Dot 只会 MCP，网关负责翻译：对上用 MCP 和 Dot 说话，对下用 ACP（Agent Client Protocol）和各个 agent 说话。工具照 ACP 会话动作命名，任何走 stdio 的 ACP agent 写进配置就能接入。

## 快速开始

```sh
cd ~/Workspace/dot-gateway
uv sync
scripts/start.sh          # 前台运行，监听 http://127.0.0.1:8765/mcp（Streamable HTTP）
uv run pytest -q          # 单元测试，用假 agent，不花钱
```

tunnel-client 用 `--mcp-server-url http://127.0.0.1:8765/mcp` 连过来。部署、开机自启、配置项和技术细节见 [AGENTS.md](AGENTS.md)。

## 工具

| 工具 | 作用 |
|---|---|
| `start_session(cwd, prompt?)` | 开一个会话；带 prompt 时发出去就返回，不等跑完 |
| `prompt(sessionId, text)` | 同一个会话的下一轮；一轮没结束再调会返回 `SESSION_BUSY`。响应回显这一轮实际生效的 `permissionMode`/`permissionPolicy`，`operator` 下另带 `permissionNote` |
| `get_events(sessionId, view?, includeToolOutput?)` | 轮询结果。默认 `view=final`：只给状态，这一轮结束时给一次最终答复，该视图不分页事件；`messages` 给合并后的整条消息和工具调用，`full` 给原始事件，这两个视图才按 `afterSequence` 翻页；`includeToolOutput=true` 附带有界的工具输出摘录；等审批时带 `pendingPermissions`（含 `toolCallId`、回填的一行工具摘要、完整命令，以及 `sandboxPermissions`/justification（如有）） |
| `get_tool_call(sessionId, toolCallId)` | 某次工具调用的完整输入和输出 |
| `get_session` / `list_sessions` | 会话概况；`detail=full` 带每一轮的完整答复 |
| `load_session` | 接回会话（dsh 用 `session/resume`） |
| `cancel_prompt` / `close_session` | 中断这一轮 / 结束会话 |
| `set_config_option`、`resolve_permission`、`list_agents` | 切换模型等配置、回答权限请求、列出 agent |

返回值默认是一段紧凑的 JSON（不额外返回 `structuredContent`；部署设置 `DOT_GATEWAY_STRUCTURED_OUTPUT=1` 后会同时返回 structuredContent）。最终答复只投递一次，如果没收到，传 `includeText=true` 或调 `get_session(detail="full")` 再取一次；`messages`/`full` 也可以用交付时相同的 `afterSequence` 重试那一页。

网关不加安全限制，但权限有两层：`permissionMode` 是 agent 自己的沙箱（默认 `workspace-write`）；`permissionPolicy` 只决定网关怎么回答 agent **自己发起**的权限请求，不是每次写入/命令的审批开关。沙箱允许的操作直接执行，所以 `operator` 下 `pendingPermissions` 为空、`permissionRequests` 为 0 是正常的；策略每一轮都可以调整。

某个调用被沙箱拒绝时，dsh 可以对同一调用重试一次、申请严格更宽一档的沙箱（带 `justification`），执行前由 dsh-acp 桥接成 ACP 权限请求。网关策略：`read_only`（默认）自动拒绝这类请求；`allow_all` 自动允许；`deny_all` 自动拒绝；`operator` 挂起在 `pendingPermissions` 里等 `resolve_permission`（条目带 `toolCallId` 和一行回填的工具摘要，回填不到是 `tool`）。选 allow-once 只放宽该次调用，不改变会话的 `permissionMode`；要整会话换档用 `prompt(permissionMode=...)`。

限制：dsh-acp 的 ACP 请求只带 `toolCallId`，申请的模式/理由只能从工具输入回填（`command`、`sandboxPermissions`、`justification`）；Auto review 的审查理由没有 ACP 字段，网关看不到。

部署可以选择启用 dsh 的实验性 **Auto review**（`dsh --profile acp-auto` + `permissionMode=auto` + `permissionPolicy=operator`）：执行前由模型逐调用审查，低风险直接以 full access 运行，medium/high 被拒时挂起问人。安装与配置见 [docs/auto-review.md](docs/auto-review.md)；这是实验功能，会多花模型调用，也可能误放行/误拒绝。

## 许可证

[MIT](LICENSE)
