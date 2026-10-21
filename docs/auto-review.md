# Auto review（dsh 实验功能）接入说明

dsh 自带一个实验性的 **Auto review** 层：挂载后权限预设多一个 `auto`（full access + ask）。每个受支持的工具调用在执行前会先让当前模型审一遍：

- 低风险（普通的项目内读写、测试、构建、非破坏性 git 操作等）：直接以 full access 执行；
- medium 风险（删除既有对象、force push、生产操作、权限/系统变更等）：只有在当前人类/直接父级明确授权时才允许，否则拒绝并走 approval `ask`；
- high 风险（把密钥/私有数据外发等）：始终拒绝；
- 审查失败（网络、协议错误）时 **fail closed**：调用不执行。

它是 dsh 的插件（`@deepseek-ai/dsh-experimental-auto-review`），不是 dot-gateway 的 `permissionPolicy` 取值。dsh 的 ACP 接口不暴露 modes/commands，所以 Auto 这个 preset 不能由 Dot 在 ACP 里选择；要让网关的 dsh agent 用上它，需要：

1. 在 dsh 的 profile 里挂上 auto-review，并让每个 ACP session 创建/恢复时选中 `auto` preset；
2. 网关的 dsh agent 用这个 profile 启动，并把 `auto` 作为一个 `permissionMode` 暴露；
3. 建议把 `permissionPolicy` 设为 `operator`，否则 Auto review 的拒绝→ask 会被默认的 `read_only` 自动拒绝，Dot 看不到问题。

本仓库的 `deploy/dsh-acp-auto/` 提供了这套 profile 和安装脚本。

## 安装

```sh
deploy/dsh-acp-auto/install.sh
```

它把 profile 复制到 `${DSH_HOME:-$HOME/.dsh}/profiles/acp-auto`，不改原来的 `acp` profile，也不改网关配置。profile 包含：

- `package.json`：bundles = `dsh-base`、`dsh-acp-app`；
- `cordis.patch.yml`：插入 vendored auto-review 插件和 `auto-select.mjs`；
- `vendor/auto-review/lib/index.js`：从 dsh 0.2.1-rc.2 的 `@deepseek-ai/dsh-experimental-auto-review` 复制并 patch（audit、测试模式、显式约束优先级）；
- `auto-select.mjs`：在 `session/created` 时根据 `DSH_AUTO_REVIEW` 选择/修复权限预设（新建和 resume 都走这个事件）。

## 网关配置

把网关的 dsh agent 指到 `--profile acp-auto`，并加一个 `auto` 模式。示例（`<state_dir>/config.json`，默认 `~/Library/Application Support/dot-gateway/config.json`）：

```json
{
  "defaults": {"permission_policy": "operator"},
  "agents": {
    "dsh": {
      "args": ["--profile", "acp-auto"],
      "permission_modes": {
        "read-only": {"env": {"DSH_PERMISSION_MODE": "read-only"}},
        "workspace-write": {"env": {"DSH_PERMISSION_MODE": "workspace-write"}},
        "danger-full-access": {"env": {"DSH_PERMISSION_MODE": "danger-full-access"}},
        "auto": {"env": {"DSH_PERMISSION_MODE": "workspace-write", "DSH_AUTO_REVIEW": "1"}}
      },
      "default_permission_mode": "auto"
    }
  }
}
```

说明：

- `auto` 模式启动时先按 `workspace-write` 起 dsh，Auto preset 选中后切到 full access + ask；如果 auto-review 插件没装好，`auto-select.mjs` 会 fail closed 回落到 `workspace-write` 并记录 warning，不会出现“名义 auto、实际无审查”。
- `default_permission_mode: "auto"` 让新会话默认 Auto review；想保守一点可以改成 `workspace-write`，由调用方显式传 `permissionMode: "auto"`。
- `defaults.permission_policy: "operator"` 让 Auto review 的 ask 挂在 `pendingPermissions` 里等 Dot 用 `resolve_permission` 回答；Dot 不回答会一直等（`cancel_prompt` 会取消）。
- 改配置后重启网关：`launchctl kickstart -k gui/$(id -u)/<标签>`。

## Dot 侧用法

- `list_agents` 会显示 dsh 的 `permission_modes`（包含 `auto`）和 `default_permission_mode`；
- `start_session(cwd, permissionMode="auto", permissionPolicy="operator")`，或不传模式使用默认值；
- Auto review 拒绝时，`get_events` / `get_session` 的 `pendingPermissions` 会出现条目（`toolCallId` + 回填的 `tool` + `options`）；用 `resolve_permission(..., optionId="allow-once"|"reject-once")` 回答：
  - `allow-once`：该次调用按 Auto preset 的 full access 执行（Auto 的默认行为）；
  - `reject-once`：调用不执行，dsh 收到拒绝并应停止/解释；
- 想对某一轮关掉 Auto review：`prompt(permissionMode="workspace-write")`；网关会重启 dsh 并 `session/resume`，`auto-select.mjs` 会把 persisted `auto` 修回 requested mode。再切回 `auto` 同理。

## 注意与限制

- **实验功能**：会为每个受支持的工具调用额外调用一次当前模型（更多 token/延迟），可能误放行或误拒绝；dsh 升级可能改变策略或接口。
- Auto preset 是 **full access + ask**：被 Auto review 放行的调用不受 workspace 边界限制。这正是它的用途，但也意味着不要把它当成比 workspace-write 更小的沙箱。
- allow 决策按 dsh reviewer 协议没有文本 reason，只有 risk/policy；deny/ask 的 reason 来自本地 audit 旁路。audit 不是 ACP 标准，dsh 升级后需要重新 patch vendored 插件。
- `pendingPermissions` 会带工具记录里能回填的完整命令（上限 16 KiB）、`sandboxPermissions`/justification（如有）、`approvalSource` 和 Auto review 的 `review`。
- 只对 dsh 的 ACP profile 生效；`pi` 等其他 agent 不受影响。
- dsh 的 `danger-full-access` preset 仍是 `approval: never`；Auto 不是它的别名。

## 审查可观测性（audit）

vendored 插件在每个 reviewer 决定后追加一行 JSONL 到
`${DOT_GATEWAY_AUTO_REVIEW_AUDIT_DIR:-$DSH_HOME/auto-review-audit}/<acpSessionId>.jsonl`；
网关启动 dsh 时会自动把 `DOT_GATEWAY_AUTO_REVIEW_AUDIT_DIR` 指向网关状态目录下的
`auto-review-audit/`，所以和网关状态一起持久化、重启不丢。记录字段：

```json
{"toolCallId":"call_…","tool":"bash","decision":"allow","risk":"low","reason":null,
 "source":"auto-review","executedSandbox":"danger-full-access","policy":"dsh-auto-review/…","reviewedAt":"…"}
```

decision 取值 allow / ask / deny / error；allow 按协议没有 reason，deny/ask 有。网关把它合并为：

- `get_events(final)` 的 `prompt.reviewMode` 和 `prompt.toolReviews`；
- `get_tool_call` 的 `review`；
- `get_session(detail="full")` 每个工具行的 `review`；
- `pendingPermissions` 条目的 `review` 和 `approvalSource`（auto-review / sandbox-escalation / agent）。

## 确定性测试入口

真实 dsh 的一次性验收（会花少量模型调用，只跑 printf）：

```sh
uv run python scripts/test_auto_review.py
```

脚本用 vendored 插件的 `DSH_AUTO_REVIEW_TEST=by-command`（只允许测试环境设置）：

- 命令参数含 `AUTO_TEST_ASK`：mock reviewer 返回 medium deny，走 approval ask，验证 `pendingPermissions` 的 review/source 和 reject-once 后不执行；
- 含 `AUTO_TEST_DENY`：mock reviewer 返回最终 deny，验证工具被拒绝；
- 其它：allow，验证 executedSandbox=danger-full-access 和 `toolReviews`。

生产不要设置 `DSH_AUTO_REVIEW_TEST`。

## 显式人类约束

vendored `REVIEW_POLICY` 明确：当前人类指令里的“不许删除 / 只允许写到某个目录 / 不许改权限或联网”等限制是硬约束，优先级高于“session 内创建对象的低风险 cleanup”例外；违反即 deny。注意这仍是模型判断，不是执行层沙箱：Auto preset 本身是 full access，真正的硬保证需要容器/VM、只读挂载或自定义 guard。

## 回退

- 想恢复普通 ACP 行为：把网关配置的 `agents.dsh.args` 改回 `["--profile", "acp"]`（或删掉这个 override），并去掉 `auto` 模式；重启网关。
- 想保留 profile 但让新会话默认不用 Auto：把 `default_permission_mode` 改回 `workspace-write`，调用方仍可显式选择 `auto`。
- 删除 `$HOME/.dsh/profiles/acp-auto` 可移除 profile；不影响原 `acp` profile。
