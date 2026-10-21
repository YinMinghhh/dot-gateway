// Pick the Auto review permission preset for dsh --profile acp-auto sessions.
//
// DSH_AUTO_REVIEW=1  -> select dsh's experimental Auto preset (full access + ask).
// otherwise          -> keep the sandbox mode from DSH_PERMISSION_MODE and repair any
//                       persisted Auto selection after a resume.
//
// If Auto is requested but the auto-review integration is not live, fall back to the
// confined default and log; never silently run with Auto reported but off.
export const inject = ["permissionPresets"];

const SANDBOX_PRESETS = new Set(["read-only", "workspace-write", "danger-full-access"]);

function wantedPreset() {
  if (process.env.DSH_AUTO_REVIEW === "1") return "auto";
  const sandbox = process.env.DSH_PERMISSION_MODE;
  return SANDBOX_PRESETS.has(sandbox) ? sandbox : "workspace-write";
}

export function apply(ctx) {
  ctx.on("session/created", (session) => {
    const want = wantedPreset();
    let current;
    try {
      current = ctx.permissionPresets.current(session);
    } catch {
      return;
    }
    if (current === want) return;
    try {
      ctx.permissionPresets.set(session, want);
    } catch (error) {
      if (want === "auto") {
        const fallback = SANDBOX_PRESETS.has(process.env.DSH_PERMISSION_MODE)
          ? process.env.DSH_PERMISSION_MODE
          : "workspace-write";
        try {
          if (ctx.permissionPresets.current(session) !== fallback) ctx.permissionPresets.set(session, fallback);
        } catch {
          // Leave the session at its persisted preset; the ACP caller still sees its requested mode.
        }
        ctx.logger?.warn?.("auto-select: Auto review is unavailable, staying on " + fallback);
      }
    }
  });
}
