# Phase 6 — Implementation Plan

Thirteen milestones. **Every milestone ends with something you use every day** — that
usage is the test bed and the memory-training data for the next milestone. Sizes are
relative (S ≈ days, M ≈ 1–2 weeks, L ≈ 2–4 weeks of part-time work).

```mermaid
flowchart LR
    M0[M0 Skeleton] --> M1[M1 Talkable brain] --> M2[M2 Memory] --> M3[M3 Hands + permissions]
    M3 --> M4[M4 Voice] --> M5[M5 Desktop control] --> M6[M6 Browser + research]
    M6 --> M7[M7 Time: scheduler + notify] --> M8[M8 Remote + PWA]
    M8 --> M9[M9 Plugins at scale] --> M10[M10 Eyes: vision] --> M11[M11 Coder + media] --> M12[M12 Self-learning]
```

| # | Name | Size | You can now… | Key requirement IDs |
|---|---|---|---|---|
| M0 | Skeleton | S | run `just dev`: kernel boots, event journal writes, CI green | NFR-OPS-* |
| M1 | Talkable brain | M | chat (streaming) via the cloud portfolio (Groq+Gemini+OpenRouter) with quota-aware routing and live failover; optional local fallback | FR-CONV-01/07, FR-LLM-01..04/06 |
| M2 | Memory + vault backup | M | it remembers you across restarts; memory viewer; hybrid search; nightly encrypted snapshot to Drive with a proven restore path | FR-MEM-01/02/04/07, FR-SYNC-01..03/06 |
| M3 | Hands + permissions | L | file ops, sandboxed shell, app launch — with tiers, confirmation UI, audit log, kill switch. **Security ships before capability grows** | FR-DESK-01/02/07, SEC-01..06, FR-TASK-01..04 |
| M4 | Voice | L | full voice loop: wake word/PTT, streaming STT, Kokoro TTS, barge-in | FR-CONV-02..04, NFR-PERF-01/03/04 |
| M5 | Desktop control | L | UIA control of real apps, clipboard, window mgmt, process/hardware monitor; overlay orb | FR-DESK-03/05/06, FR-UI-02 |
| M6 | Browser + research | M | web tasks in your real browser; cited multi-page research; taint rules live | FR-BROW-*, SEC-07 |
| M7 | Time | M | reminders, recurring jobs, morning briefing; toasts + phone push via ntfy | FR-TASK-05/06, FR-PLAT-03 |
| M8 | Remote + sync | M | full assistant from your phone (PWA over Tailscale), reduced-privilege role; multi-device journal sync via the Drive vault | FR-PLAT-04, SEC-09, FR-SYNC-04 |
| M9 | Plugins at scale | M | mount external MCP servers (Spotify, GitHub, Home Assistant…); plugin manager UI; egress allowlist | FR-PLAT-01/02, SEC-11 |
| M10 | Eyes | L | "what's on my screen?", OCR, vision-grounded fallback control (rung 3) | FR-DESK-04/09 |
| M11 | Coder + media | L | diff-based coding help in your repos; Premiere/Photoshop scripting skill packs | FR-DESK-08 |
| M12 | Self-learning | L | procedural memory guides plans; corrections update behavior; consolidation nightly; cold-tier archiving; multi-agent parallel research | FR-MEM-03/05/06, FR-SYNC-05, FR-PLAT-06, FR-TASK-07 |

## Milestone detail & exit criteria

### M0 — Skeleton
Workspace (`uv`, `pnpm`), `contracts` package, event bus + SQLite journal, config
loader, structlog, FastAPI gateway with WS echo, CI. **Exit:** event round-trip test
green; `just dev` boots kernel in < 3 s.

### M1 — Talkable brain  ← now the gateway milestone
Model Gateway v2: provider registry (`providers.yaml`), LiteLLM adapter behind the
`InferenceProvider` port, quota governor (persisted buckets), health tracker + circuit
breakers, ranked-cascade router, privacy filter skeleton (P2 blocking from day one).
Orchestrator port + minimal LangGraph adapter (understand→respond), session
persistence, streaming to terminal + minimal Tauri chat window with tray. Optional
Ollama fallback wired if present. **Exit:** sustained multi-turn chat; restarts keep
history and RPD counts; first-token ≤ 800 ms p50; **provider-kill test**: block the
primary provider's DNS mid-conversation → next turn succeeds via failover with ≤ 2 s
added latency and a visible `ProviderDegraded` event; quota exhaustion (simulated)
routes around the empty bucket without a single 429 sent.

### M2 — Memory + vault backup
Store (SQLite+sqlite-vec+FTS5, migrations), episodic auto-write, semantic
`remember/forget` tools, fastembed local embeddings, context assembler with budgets
and privacy-class tagging, memory viewer UI. Vault v1: Drive OAuth (`drive.file`),
crypto envelope + recovery-phrase ceremony, scheduled `VACUUM INTO` snapshots with
GFS retention. **Exit:** "what did we discuss last Tuesday?" works; a stated
preference changes later behavior; retrieval < 400 ms at 50k items (synthetic);
**restore drill**: wipe a scratch environment, restore from Drive with only the
recovery phrase, byte-identical memory state. Backup ships *before* the assistant
gets hands (M3) — never risk data you can't restore.

### M3 — Hands + permissions  ← the keystone milestone
Permission broker + policy file + confirmation UX (UI dialog), audit chain, sandbox
executor, watchdog + kill switch, MCP tool router, `tools/files`, `tools/shell`,
`tools/windows(launch)`; plan→execute→observe loop with retries and bounds.
**Exit:** red-team checklist passes (path escape blocked, T2 without grant blocked,
kill switch < 500 ms mid-task, audit chain verifies); "organize my Downloads" works
end-to-end with one confirmation.

### M4 — Voice
Voice satellite process: sounddevice, Silero VAD, openWakeWord, faster-whisper
streaming, Kokoro+Piper, barge-in, mode switching; sentence-streamed TTS; decision
point: keep custom pipeline vs adopt Pipecat. All CPU-only (NFR-PERF-08). **Exit:**
NFR-PERF-01/03/04 measured and met over the cloud path on real broadband; interruption
feels natural in daily use.

### M5–M12
Scoped per the table; each adds one tool family or cognitive capability plus its UI
surface, never both a new capability *and* a new infrastructure layer in one milestone.

## Sequencing rationale

- **Failover before features (M1):** the provider portfolio and quota governor are the
  foundation everything sits on — retrofitting failover into a single-provider codebase
  is a rewrite; building it first is a config file and two small modules.
- **Backup before hands (M2 < M3):** the vault must prove restore *before* the
  assistant can modify files. Never grant write capability over data you can't restore.
- **Memory before hands (M2 < M3):** an assistant that remembers you but can't act is
  a companion; one that acts but forgets is a liability. Memory also improves every
  later milestone's usefulness data.
- **Permissions before capability (M3 before M5/M6/M9):** the broker must exist before
  the tool surface grows — retrofitting security is how projects become dangerous.
- **Voice after hands (M4 > M3):** voice with nothing to do is a demo; hands make voice
  compelling. (Swappable if motivation says otherwise — M4 only depends on M1.)
- **Vision late (M10):** rung-3 control is the least reliable, most GPU-hungry path;
  rungs 1–2 cover most real tasks first.
