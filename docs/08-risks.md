# Phase 8 — Risk Register & Bottleneck Analysis (rev 2: cloud-first)

Scored L(ikelihood) × I(mpact), 1–5, sorted by score. Rev 2 re-ranks the register:
local-model-quality risk is retired; free-tier dependency and privacy take its place.

## Tier 1 — existential risks (score ≥ 16)

### R1. Free-tier dependency: rate limits, quota exhaustion, model removal, ToS changes (L5 × I4)
The reasoning layer now rents capacity that providers can throttle, reprice, or
withdraw with zero notice — and historically do, monthly.
- **Mitigations (in design):** three independent providers minimum (Groq + Gemini +
  OpenRouter pool); Quota Governor with *preemptive* routing (never hit the 429);
  interactive headroom reservation (background work can't starve conversation);
  response cache for repeated background prompts; quotas live in `providers.yaml`
  (config change tracks a limit change, no release); local emergency fallback;
  graceful queue-and-notify when truly exhausted.
- **Contingency ladder:** add new free providers to the registry (Cerebras, Mistral,
  whoever's next) → lean harder on local fallback → the single flagged paid exception
  (TC-04): a cheap paid tier through the *same gateway* — one registry entry, zero code.

### R2. Prompt injection → real-world actions (L4 × I5) — unchanged from rev 1
Untrusted web/screen/file text meeting real capabilities remains our nastiest risk.
- **Mitigations:** taint tracking + escalation suspension (SEC-07) enforced in the
  broker below cognition; tiered permissions; red-team regression suite; audit chain.
- **Contingency:** panic-tighten policy (all T1+ confirm) is one config change.

### R3. Privacy: free tiers train on prompts (L5 × I4) — new at this rank
A personal assistant's prompts contain your life. "Free" is paid in training data.
This is now a *structural* property of the primary reasoning path, not an edge case.
- **Mitigations:** privacy classes P0/P1/P2 enforced in the gateway (P2 never leaves
  the device); informed one-time consent per provider with `trains_on_data` shown from
  the registry; optional redaction pass for P1; embeddings and OCR deliberately local
  so memory content never streams out wholesale; egress audit ("what has each provider
  seen") user-visible; Drive vault is ciphertext-only.
- **Contingency:** one settings toggle flips the routing table to local-only mode
  (degraded quality, full privacy) — the architecture treats that as a routing policy,
  not a rebuild.

### R4. Scope: still 6+ products in one repo (L5 × I4) — unchanged
- **Mitigations:** daily-driver milestones; integrate-don't-reinvent; one new
  capability *or* one new layer per milestone. The cloud pivot actually shrinks scope:
  no local-inference tuning, no GPU tier matrix, no VLM serving.
- **Contingency:** M1–M3 core is a complete product on its own.

## Tier 2 — serious (score 9–15)

### R5. Network dependency: no internet ⇒ no primary brain (L3 × I4) — new
- **Mitigations:** local fallback model keeps chat + P2 + basic tool use alive;
  the entire action layer (files, UIA, browser-less tools), memory, scheduler, and
  voice edge are local and unaffected; offline turns queue durable tasks
  (`sync_outbox` pattern); assistant states its degraded mode honestly.
- **Contingency:** phone-hotspot is the practical human fallback; "offline pack"
  (bigger local model, prefetched) for planned travel — a config profile.

### R6. Latency variance & tail latency on shared free infrastructure (L4 × I3)
Free tiers have noisy neighbors: p50 may be 400 ms, p99 can be 8 s or a hang.
- **Mitigations:** Health Tracker latency EWMA feeds routing (slow provider loses
  rank automatically); hard first-token deadline with hedged second request
  (quota-permitting); Groq as interactive primary (fastest, most consistent TTFT);
  streaming everywhere; small-talk acknowledgments from the fast path mask tails.
- **Contingency:** voice UX inserts natural filler ("hmm, let me think") tied to
  deadline misses — psychology where engineering runs out.

### R7. Windows UIA flakiness / apps without accessibility trees (L4 × I3) — unchanged
- Hybrid ladder, skill packs, verify-by-reading-state, vision fallback (now Gemini),
  per-app manual mode.

### R8. Framework/protocol churn: LangGraph, MCP, LiteLLM, provider APIs (L4 × I3)
Rev 2 adds provider-API churn (Gemini/Groq/OpenRouter versioning) to the list.
- **Mitigations:** ports-and-adapters everywhere (only adapters import frameworks;
  only the gateway knows providers exist); `contracts` owns our types; locked deps;
  provider wire quirks absorbed by LiteLLM (their full-time job, not ours).
- **Contingency:** adapters are rewrite-bounded by design (~days).

### R9. Memory rot (L3 × I4) — unchanged
- Provenance + confidence, supersede-links for corrections, decay, viewer, rebuildable
  semantic layer from immutable episodes.

### R10. Vault integrity: backup/sync corruption or key loss (L2 × I5) — new
Losing years of memory to a bad restore or a lost key is the disaster scenario.
- **Mitigations:** snapshots via `VACUUM INTO` (consistent by construction);
  hash-chained manifest detects corruption *at backup time*, not restore time;
  restore drill automated in CI and required in M2 exit criteria; append-only journal
  segments mean sync can never destroy local state (fold is additive); GFS retention
  keeps 12 months of independent restore points; recovery phrase shown at setup with
  an explicit "store this outside this machine" ceremony.
- **Contingency:** local hot store is itself the primary copy — the vault failing
  loses redundancy, not data; rclone manual pull path documented for DR.

### R11. Remote access attack surface (L2 × I5) — unchanged
- Tailscale-only, reduced remote role, remote T3 denial, audit, node revocation.

### R12. OAuth/API credential theft from the machine (L2 × I4) — new emphasis
Gateway keys + Drive refresh token are now valuable loot.
- **Mitigations:** Credential Manager (DPAPI, user-scoped); never logged/prompted;
  `drive.file` scope caps blast radius (attacker sees ciphertext blobs it can't
  decrypt — the AES key never leaves DPAPI); free-tier LLM keys have no billing
  attached; key rotation runbook.

## Tier 3 — manageable (score ≤ 8)

| Risk | L×I | Mitigation |
|---|---|---|
| R13. Audio device hell | 4×2 | unchanged: WASAPI abstraction, hot-swap, doctor.py, text always works |
| R14. Drive API deprecations / 15 GB quota pressure | 2×3 | `RemoteVault` port (OneDrive/S3/WebDAV adapters); archive compaction; quota telemetry in vault panel |
| R15. Adobe scripting arcana | 3×2 | unchanged: skill-pack isolation, read-only first, late milestone |
| R16. SQLite write contention (journal+checkpoints+quota buckets) | 2×3 | WAL, single-writer queue, busy_timeout; split DB files if measured |
| R17. Context assembly token-budget bugs | 3×2 | hard per-source caps, budget tests in CI |
| R18. Wake-word false positives | 3×2 | threshold tuning, chime, mic indicator, hardware-mute respect |
| R19. Battery drain | 2×2 | improved in rev 2: no local inference; idle = VAD-only; power-aware profiles |
| R20. Solo bus-factor / motivation | 3×3 | daily-driver milestones, standards, ADRs preserve *why* |
| R21. Free-tier consent misconfiguration (P1 data to a trains-on-data provider the user didn't intend) | 2×3 | consent recorded per provider; registry `trains_on_data` surfaced in UI; egress audit makes it inspectable after the fact |

## Retired from rev 1

| Was | Why retired |
|---|---|
| R1 (old): local model quality ceiling | Local models no longer primary; quality now tracks the free-tier frontier |
| R5 (old): GPU contention between STT/LLM/VLM/TTS | No GPU in the required stack; voice edge is CPU-budgeted |

## Standing review — unchanged
Register reviewed at every milestone exit; new capabilities add red-team entries
before merge. Rev 2 adds: provider registry reviewed monthly (quotas/ToS drift).
