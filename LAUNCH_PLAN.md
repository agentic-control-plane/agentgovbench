# Launch plan — AgentGovBench

*Internal strategic doc. Not shipped publicly. Keep out of the README.*

## Objective

Establish AgentGovBench as the standard third-party benchmark for AI agent governance. Primary success metric: citations in vendor procurement collateral and academic publications within 12 months. Secondary: at least 3 external vendor runners contributed within 6 months.

## Positioning (what we say; what we don't)

### What we say
- *"The first open, NIST-aligned benchmark for agent governance infrastructure."*
- *"Complements AgentLeak (privacy), InjecAgent (injection), HarmBench (alignment)."*
- *"Reference implementation is ACP — scorecard published, including partial failures."*
- *"Vendor-neutral by design. Runners welcome; dispute process transparent."*

### What we don't say
- *"The best governance product scores highest"* — not the frame
- *"NIST endorses this benchmark"* — they don't; we map to their framework
- *"ACP is proven secure"* — no benchmark proves security; we pass the test conditions

## Sequencing

### Pre-launch (weeks -2 to 0)

- **Socialize with 5–8 friendly contacts.** Target: 1 compliance buyer, 1 security academic, 2 governance vendors (competitors), 1 AI-RMF practitioner, 1 Gartner/Forrester analyst. Pitch: *"We'd like your feedback on this before we publish it publicly."*
- **Register the GitHub organization.** Suggested: `openagentgov` (neutral, descriptive). Avoid `agentic-*` or `acp-*` naming.
- **Register the domain.** `agentgovbench.org` or similar. Static site can come later; initial home is the GitHub repo.
- **Legal review.** Check that scenario contents don't inadvertently disclose customer data or internal policies. Confirm MIT license appropriate for the org's goals.
- **Self-score honestly.** Current ACP scorecard is already in `results/acp-v0.2.json`. Do not polish the fails away — they are credibility.

### Launch day (week 0)

- **Public repo open.** Under the neutral org, not under the personal account.
- **Blog post on agenticcontrolplane.com** — *"Why we built AgentGovBench (and why we're publishing our own failures)."*
- **arXiv preprint** — methodology paper. 4–6 pages. Abstract + sections for each of METHODOLOGY.md, THREAT_MODEL.md, NIST_MAPPING.md, SCORING.md. Categories cs.CR, cs.AI.
- **Twitter/X thread** — 10 posts, starts with the credibility hook (*"we published our own scores including partial failures"*), ends with the call for vendors to submit runners.
- **HN / Reddit r/MachineLearning / r/cybersecurity** — seed, don't spam. One thoughtful post per venue, tied to the arXiv preprint.
- **Direct outreach** to the contacts from pre-launch. *"It's public now; happy to answer any questions for a write-up you're working on."*

### Week 1

- **First external runner proof-of-life.** Even a stub runner from one friendly vendor validates the contribution path.
- **Address feedback quickly.** If reviewers find legitimate spec bugs in the first week, fix them openly. Public revision in week 1 is fine; public denial is not.
- **Quantified post** — *"Initial results: ACP 41/48, vanilla 25/48. Here's what the gap measures and how to reproduce."*

### Month 1

- **NIST AI RMF Playbook submission.** The Playbook accepts community contributions. Submit a reference to AgentGovBench as a practitioner tool for measuring specific controls.
- **One vendor runner per week target** for 4 weeks. Direct outreach: Guardrails AI, Arthur AI, Credo AI, Galileo, Protect AI, Robust Intelligence, Lakera, Invariant Labs. *"We want your product represented."*
- **Post #2** — *"Three runners, three scorecards. What the category matrix tells us about the current state of agent governance."*

### Quarter 1 (through v0.3)

- **Held-out scenario set** — 15% of scenarios moved to encrypted private set. Publish the public set, run held-out privately, publish both numbers.
- **Quarterly scenario rotation** — retire 20% / add 20%. Maintains freshness; decays vendor-specific tuning.
- **Post #3** — *"Gaming decay: how the quarterly rotation has affected runner scores."*
- **Conference submission** — target: IEEE S&P workshops, USENIX Security AI track, NeurIPS safety workshop, CSA AI Safety Initiative. Abstract as "open infrastructure for evaluating AI governance."

### Year 1

- **v1.0 spec freeze** — after 3 quarterly rotations of learning. Commit to stability for 1 year at v1.0.
- **Sibling benchmarks** — partner with AgentLeak, InjecAgent, AgentDAM authors to create a combined "agent security test suite" bundle. Individual benchmarks stay independent; suite is a convenience package.
- **Certification path** (maybe) — *"AgentGovBench compliance level 1"* (pass categories 1–4) / *"level 2"* (all 8) as an informal designation vendors can claim. No formal certification body yet.

## Outreach targets, by category

### Academics (papers, credibility)

- AgentLeak authors (El Yagoubi, Badu-Marfo, Al Mallah — Polytechnique Montreal)
- Stanford HAI
- MIT CSAIL AI Safety
- Mozilla.ai (open, independent)
- CMU CyLab
- UIUC InformaticsSecurity (InjecAgent authors)

### Governance vendors (runners)

- Guardrails AI
- Arthur AI
- Credo AI
- Protect AI
- Robust Intelligence
- Lakera
- Invariant Labs
- NVIDIA NeMo Guardrails
- Meta PurpleLlama (partial overlap)

### Framework vendors (built-in governance)

- Anthropic (Claude Agent SDK team)
- OpenAI (Agents SDK + evals team)
- LangChain (middleware)
- CrewAI
- Microsoft Semantic Kernel

### Standards / regulatory

- NIST AI RMF Playbook maintainers
- OWASP Agent Security Initiative
- Cloud Security Alliance AI Safety Initiative
- ISO/IEC JTC 1/SC 42 (AI standards)
- MLCommons AILuminate

### Buyers / signals

- Gartner AI security analysts
- Forrester TEI / Wave team for AI governance
- McKinsey / Deloitte AI risk practices
- SaaS procurement influencers

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Vendor protest ("we reject your assertions") | Medium | Medium | Dispute process public; revision policy clear |
| Academic critique ("methodology is vendor-biased") | Medium | High | Pre-launch socialization, published raw results, visible own failures |
| Maintenance decay (repo abandoned) | High w/o discipline | Fatal | Quarterly release cadence committed; second maintainer identified |
| Benchmark gaming | High once adopted | Medium | Held-out set; rotation; public runner code |
| Naming clash / trademark | Low | Low | Name chosen neutral; check before domain registration |
| Customer data inadvertently in scenarios | Low now | High | Legal review pre-launch; all scenarios synthetic |

## Success metrics (measure at 6 months)

- [ ] ≥3 external vendor runners
- [ ] ≥1 academic paper cites the benchmark
- [ ] ≥1 compliance buyer document references it
- [ ] ≥100 GitHub stars
- [ ] ≥1 NIST Playbook reference

Miss these and the play didn't land — regroup or retire.

## Budget

- Domain: ~$20/year
- GitHub org: free
- Hosting (static site, if built): ~$0–$100/year
- Author time: weekly maintenance, ~1 day/month. Quarterly release: ~1 week.
- Outreach / content: one blog post per month, ~4 hours each.

Total ongoing: 1–2 days/month author time + negligible infra.

## Decision log

- **Neutral org name chosen over `acp-benchmark`** to pre-empt vendor-bias dismissal.
- **NIST 1.0 mapping chosen over ISO 42001** because AI RMF is US-focused, widely adopted in target market, and free/open-access. ISO 42001 mapping can follow in v0.3+.
- **Deterministic scenarios chosen over LLM-in-the-loop** for reproducibility. `llm_required: true` scenarios available but optional.
- **Binary per scenario chosen over partial credit** to prevent score-gaming and keep the scorecard interpretable.
- **No single overall "score"** — category matrix only. Single number invites bad-faith marketing.
