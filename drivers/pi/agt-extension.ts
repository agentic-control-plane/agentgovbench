/**
 * Microsoft Agent Governance Toolkit as a pi extension.
 *
 * AGT attaches in-process — a host calls its PolicyEngine before running a
 * tool — while ACP attaches at a harness hook. To compare them we put both
 * at the SAME interception point: pi's `tool_call` event. This extension is
 * the adapter that lets AGT decide there.
 *
 * That is not a contrivance. `pi-dcg` (unaffiliated author) does exactly
 * this for dcg, sending it a Claude-compatible PreToolUse payload and
 * mapping the verdict onto pi's outcomes. One hook shape, several
 * governance products.
 *
 * FAIRNESS NOTES — this adapter is written by ACP's team, so read it with
 * that in mind and check it against Microsoft's own docs:
 *
 *   * It uses `evaluatePolicy(agentDid, context)`, the RICH path with the
 *     full policy-document model, conflict resolution, rate limits and
 *     approvals — not the legacy flat `evaluate(action)`. Using the weaker
 *     path would understate the product.
 *   * Policy comes from a document the runner generates from the scenario,
 *     in AGT's own schema. Nothing is hand-tuned per scenario.
 *   * The whole verdict vocabulary is honoured: allow / deny / warn /
 *     require_approval / log.
 *
 * Verdict mapping onto pi's ToolCallEventResult:
 *   allow, warn, log  -> undefined (proceeds; warn and log are advisory
 *                        by AGT's own definition, not gating)
 *   deny              -> { block, reason }
 *   require_approval  -> blocked here, because the benchmark runs headless.
 *                        An unattended agent cannot self-approve; this is
 *                        the same posture ACP takes, so neither product is
 *                        advantaged by the substrate being unattended.
 */
import { readFileSync, writeFileSync } from "node:fs";

export default function agt(pi: any): void {
  const policyPath = process.env.AGB_AGT_POLICY;
  const auditPath = process.env.AGB_AGT_AUDIT;
  let engine: any = null;
  let audit: any = null;
  let loadError: string | null = null;

  const ready = (async () => {
    try {
      const mod: any = await import("@microsoft/agent-governance-sdk");
      engine = new mod.PolicyEngine();
      // AGT ships a hash-chained AuditLogger. Wire it, rather than
      // declining the audit categories — declining a capability the
      // product HAS would understate it.
      audit = new mod.AuditLogger();
      if (policyPath) {
        const doc = JSON.parse(readFileSync(policyPath, "utf8"));
        for (const p of Array.isArray(doc) ? doc : [doc]) engine.loadPolicy(p);
      }
    } catch (e: any) {
      // Loud, never silent: a governance layer that failed to load must not
      // look like a governance layer that allowed everything.
      loadError = e?.message ?? String(e);
      console.error(`[AGT] load failed: ${loadError}`);
    }
  })();

  pi.on("tool_call", async (event: any, ctx: any) => {
    await ready;
    if (!engine) {
      return { block: true, reason: `AGT unavailable: ${loadError ?? "not loaded"}` };
    }

    const agentDid = process.env.AGB_AGT_AGENT ?? "agb-agent";
    const context = {
      action: event.toolName,
      tool: event.toolName,
      tier: process.env.AGB_AGT_TIER ?? "interactive",
      user: process.env.AGB_AGT_USER ?? "",
      tenant: process.env.AGB_AGT_TENANT ?? "",
      surface: "cli",
      input: event.input,
    };

    let result: any;
    try {
      result = engine.evaluatePolicy(agentDid, context);
    } catch (e: any) {
      return { block: true, reason: `AGT evaluation error: ${e?.message ?? e}` };
    }

    const action = result?.action ?? (result?.allowed ? "allow" : "deny");

    if (audit) {
      try {
        // AGT's entry model is {agentId, action, decision} plus the hash
        // chain. It carries no reason, trace id, tenant or tier — that is
        // the product's shape, not an omission by this adapter, and the
        // scorecard should reflect it honestly either way.
        audit.log({
          agentId: context.user || agentDid,
          action: event.toolName,
          decision: action === "deny" || action === "require_approval" ? "deny" : "allow",
        });
        if (auditPath) writeFileSync(auditPath, audit.exportJSON());
      } catch { /* audit must never change the decision */ }
    }
    if (action === "deny") {
      return { block: true, reason: result?.reason ?? `denied by ${result?.matchedRule ?? "policy"}` };
    }
    if (action === "require_approval") {
      // Headless: nobody can approve, so the safe outcome is to block.
      return {
        block: true,
        reason: `requires approval (${(result?.approvers ?? []).join(", ") || "unspecified"}) — unattended session cannot self-approve`,
      };
    }
    if (result?.rateLimited) {
      return { block: true, reason: result?.reason ?? "rate limited" };
    }
    // allow / warn / log all proceed. warn and log are advisory in AGT's
    // own vocabulary; treating them as gating would overstate the product.
    return undefined;
  });
}
