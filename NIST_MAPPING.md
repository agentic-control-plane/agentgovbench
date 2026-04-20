# NIST AI RMF 1.0 mapping

AgentGovBench categories mapped to NIST AI Risk Management Framework (1.0) controls. Full RMF text: <https://doi.org/10.6028/NIST.AI.100-1>.

## Why this mapping exists

Compliance buyers want NIST citations in vendor claims. This table provides the receipt. Each scenario in `scenarios/` carries a `nist:` list referencing these controls; results can therefore be reported as *"passes MEASURE-2.3 across all provenance scenarios"* rather than *"achieves X% on AgentGovBench"*.

## Mapping table

| Category | Primary control(s) | Secondary | Why this mapping |
|---|---|---|---|
| **1. Identity Propagation** | `MAP-2.1` (Context characterized) | `GOVERN-1.4` (Accountability), `MEASURE-2.6` (Safety) | Every tool call must be attributable to a human principal. This is foundational for risk mapping (MAP), accountability structures (GOVERN-1.4), and safe multi-actor operation (MEASURE-2.6). |
| **2. Per-User Policy Enforcement** | `GOVERN-1.2` (Responsibility for AI risks) | `MANAGE-2.1` (Risk response) | User-scoped policies define who bears which risks. A system that lets user X perform actions X is not authorized for violates the GOVERN responsibility model. |
| **3. Delegation Provenance** | `MEASURE-2.3` (Functionality and behavior) | `GOVERN-1.4` (Accountability), `MANAGE-4.1` (Post-deployment monitoring) | Behavior in a multi-agent system is only meaningful if you can reconstruct the call chain. MEASURE-2.3 demands documented, reproducible behavior; provenance is the forensic substrate. |
| **4. Scope Inheritance / Privilege Escalation** | `MAP-4.1` (Risks and benefits identified) | `MEASURE-2.7` (Security resilience) | Privilege escalation is a fundamental risk in delegating systems. MAP-4.1 requires it to be identified; MEASURE-2.7 requires the system to resist it. |
| **5. Rate Limit Cascade** | `MANAGE-2.1` (Risk response strategies) | `GOVERN-1.5` (Monitoring) | Rate limits are a risk-response strategy for abuse and cost. If they can be bypassed by fan-out, the response is defective under MANAGE-2.1. |
| **6. Audit Completeness** | `MEASURE-2.3` (Functionality and behavior documented) | `MANAGE-4.1` (Post-deployment monitoring), `GOVERN-1.5` (Mechanisms in place) | Audit logs *are* the functionality documentation during operation. MEASURE-2.3 requires behavior to be inspectable; MANAGE-4.1 requires ongoing monitoring capability. |
| **7. Fail-Mode Discipline** | `GOVERN-1.1` (Legal and regulatory requirements understood) | `MANAGE-2.2` (Mechanisms to sustain risk management) | Under governance failure, the declared behavior must occur. If policy is fail-closed but the system fails open, GOVERN-1.1 (understanding of requirements) is violated. |
| **8. Cross-Tenant Isolation** | `GOVERN-1.2` (Responsibility for AI risks) | `MEASURE-2.7` (Security resilience) | Multi-tenant deployments distribute risk responsibility per-tenant. Leakage across tenants merges risk responsibilities in ways not declared by GOVERN-1.2. |

## Notes on mapping philosophy

**We cite the minimum set of controls that clearly apply.** A more expansive mapping is possible — identity propagation arguably touches every GOVERN subcategory — but citing everything dilutes the signal. Each scenario claims one primary control and one or two secondaries; anything beyond that would be noise.

**We do not claim NIST endorses this benchmark.** The mapping is our interpretation. NIST AI RMF is a framework, not a certification. A scenario "passing MEASURE-2.3" means *the governance layer demonstrated the documented-behavior property required by MEASURE-2.3 under this scenario's conditions*, not *NIST has blessed this test as definitive for MEASURE-2.3*.

**Mapping is reviewable.** If a practitioner disagrees with a mapping, that's a contribution. Submit a PR; we'll discuss. The mapping is part of the benchmark's versioned spec.

## Controls the benchmark does NOT exercise

Not all NIST controls are within scope. Notably:

- `GOVERN-2.*` (Workforce competency) — organizational, not technical
- `GOVERN-3.*` (Diverse perspectives) — process concern
- `MAP-1.*` (System context establishment) — documented outside the running system
- `MAP-5.*` (Impacts identified) — pre-deployment analysis
- `MEASURE-1.*` (Appropriate methods identified) — methodology, not runtime
- `MEASURE-4.*` (Feedback mechanisms) — post-deployment process
- `MANAGE-1.*` (Risk priorities) — business decision
- `MANAGE-3.*` (Third-party risks) — supply chain, orthogonal

These matter for AI governance programs but are not testable by a runtime benchmark. AgentGovBench focuses on the technical, runtime controls.

## References

- NIST AI Risk Management Framework (AI RMF 1.0). <https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf>
- AI RMF Playbook. <https://airc.nist.gov/AI_RMF_Knowledge_Base/Playbook>
