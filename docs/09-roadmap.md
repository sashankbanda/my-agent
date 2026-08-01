# Phase 9 — Future Roadmap: From Assistant to AI Operating System

The architecture was shaped so that each future form is an *extension along an existing
seam*, not a rewrite. This doc names the seams and the order of evolution.

## The load-bearing seams

| Seam (exists from M0) | What it unlocks later |
|---|---|
| `contracts` thin waist (typed events/tools/memory) | Any new frontend, backend, or language can join by speaking the contracts |
| Event journal as source of truth | Sync, replication, time-travel debugging, learning corpora |
| MCP for 100 % of capabilities | Ecosystem growth without kernel changes; capabilities usable by *other* AI clients |
| Ports-and-adapters around frameworks | Swap orchestrator/models/stores as the field evolves |
| Gateway as the only entry point | New channels (app, watch, car, API consumers) are gateway clients |
| Repository-pattern storage | SQLite → Postgres/servers when multi-device demands it |

## Stage A — AI Operating Layer on Windows (year 1–2)

The M0–M12 plan. End state: the assistant is the primary interface to the machine —
apps become backends the assistant drives. Additions after M12:
- **Semantic desktop index**: background indexer embeds documents/projects → "find that
  invoice from March" beats Explorer search.
- **Workflow compiler**: repeated procedures (procedural memory) get promoted into
  deterministic scripts that run *without* LLM inference — learned automation that is
  fast, free, and reliable. This is the quiet superpower: the system gets cheaper and
  faster with use, not just smarter.
- **App intents registry**: skills declare capabilities the planner can browse — an
  "API of your computer."

## Stage B — Cross-platform desktop (year 2)

- Kernel is already OS-agnostic except `tools/windows`, `tools/uia`, sandbox launcher,
  and secrets backend — each an adapter. Ship `tools/macos` (AppleScript/AX API) and
  `tools/linux` (AT-SPI, D-Bus) equivalents; Tauri UI is already cross-platform.
- Deliverable: same companion, same memory file, any desk.

## Stage C — Mobile companion (year 2–3)

- **C1 (exists at M8):** PWA over Tailscale — full remote control.
- **C2:** Native Android app (Kotlin or Tauri Mobile): native push, share-sheet
  ("send to assistant"), background voice, on-device wake word streaming audio to the
  laptop kernel. iOS after (TestFlight constraints noted).
- **C3:** On-device micro-kernel: the phone runs the same cloud-first Model Gateway
  (it's just HTTP — a phone reaches Groq/Gemini as easily as the laptop) plus a tiny
  local fallback; state reconciles through the **existing Drive journal-segment sync
  (built at M8)** — the phone is simply another device stream in the vault.

## Stage D — Personal cloud node (year 3)

Not "move to the cloud" — **add a node**. A cheap VPS (or a spare mini-PC at home —
still $0) runs the same kernel image in headless mode, joined via Tailscale:
- Always-on scheduler, email/feed watching, long research jobs while the laptop sleeps.
- Laptop remains the *hands* (desktop control stays local by definition); the node is
  extra *time and availability*. Task placement becomes a scheduler property
  (`requires: desktop` vs `anywhere`).
- The node is cheap by design now: it needs no GPU (reasoning is already cloud-rented)
  and no new sync mechanism (it joins as another device stream in the Drive vault).
  Postgres becomes justified only here, if ever (the repository seam pays off).

## Stage E — A team of agents (year 3+)

Multi-agent was deliberately *not* over-built early (research shows single-graph
beats agent swarms for reliability). It grows along these lines when justified:
- **Specialist sub-runs** (exists at M12): parallel researcher/coder/vision runs under
  one plan, budget-bounded.
- **Cross-node delegation:** planner on the laptop dispatches a research task to the
  cloud node's agent via MCP-over-Tailscale — agents *are* MCP servers to each other
  (the protocol was chosen partly because it makes this symmetric).
- **Third-party agents:** because every capability is MCP and the gateway speaks
  OpenAI-compatible, external agents (a coding agent, a home-automation brain) can be
  granted *scoped, audited* access to your assistant's tools — the permission broker
  and role system already model "less-trusted principals."

## Stage F — Commercial hardening (if/when)

The "commercial AI OS" ambition needs, in rough order: multi-user tenancy in the data
model (single-user assumptions are currently allowed everywhere — the one acknowledged
rewrite-debt); installer/updater (Tauri updater + MSIX); crash telemetry (opt-in);
license-clean audit (already enforced: every pick in Phase 4 is Apache/MIT/BSD —
the XTTS rejection was this rule firing early); a plugin store with signing.

## Revision triggers (when to reopen this architecture)

| Signal | Action |
|---|---|
| Local models reach frontier-quality tool use at laptop-CPU sizes | Flip the routing tables local-first (privacy + availability win) — a `providers.yaml` edit, which is the whole point of the gateway |
| Free tiers collectively tighten below daily-driver viability | Promote the flagged paid exception (one cheap paid tier via the same gateway) or re-expand the local fallback — both are registry entries |
| OS vendors ship deep native agent APIs (MS Copilot runtime opens up) | Rung-1 ladder absorbs them as preferred bindings |
| MCP superseded by a better protocol | Tool Router adapter swap; contracts unchanged |
| A voice-native local model (speech-in/speech-out) matures | Voice pipeline collapses STT/TTS into the model gateway — huge latency win |
| You get a desktop GPU / home server | Tier C configs + Stage D node, zero redesign |

---

*End of architecture phase. Next step when you're ready: review these docs, then begin
M0 per [06-implementation-plan.md](06-implementation-plan.md). The first code written
should be `packages/contracts` — the thin waist everything else hangs on.*
