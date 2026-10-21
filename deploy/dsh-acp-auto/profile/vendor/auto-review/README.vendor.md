# Vendored @deepseek-ai/dsh-experimental-auto-review

Source: dsh 0.2.1-rc.2 `@deepseek-ai/dsh-experimental-auto-review` (MIT, see LICENSE).
Only `lib/index.js` is vendored and patched. Local changes:

- append one JSONL audit record per reviewer decision to
  `$DOT_GATEWAY_AUTO_REVIEW_AUDIT_DIR/<sessionId>.jsonl` (or `$DSH_HOME/auto-review-audit`):
  `toolCallId`, `tool`, `decision` (allow/ask/deny/error), `risk`, `reason`,
  `executedSandbox`, `source`, `policy`, `reviewedAt`;
- `DSH_AUTO_REVIEW_TEST=allow|ask|deny|by-command` gives deterministic decisions for
  transport tests without an LLM reviewer (test-only; production does not set it);
- `REVIEW_POLICY` now treats explicit human restrictions (no deletion, allowed
  directories, no network/permission changes) as hard constraints overriding the
  low-risk session-created cleanup exception.

When upgrading dsh, re-copy the upstream `lib/index.js` and reapply these patches;
`docs/auto-review.md` covers the contract.
