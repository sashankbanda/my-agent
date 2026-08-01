# Phase 2 — Requirements Analysis (rev 2: cloud-first reasoning, Drive vault)

Requirement IDs are stable and referenced by milestones (Phase 6) and tests.
Priority: **P0** = core identity, **P1** = expected soon, **P2** = roadmap.
Rev 2 adds the FR-LLM and FR-SYNC families, SEC-12/13, and revises NFR-PERF and TC-02/05.

## 1. Functional requirements

### Conversation (CONV)
| ID | Requirement | Pri |
|---|---|---|
| FR-CONV-01 | Text chat with streamed responses | P0 |
| FR-CONV-02 | Full-duplex voice conversation (streamed STT in, streamed TTS out) | P0 |
| FR-CONV-03 | Wake word, push-to-talk, and continuous modes, switchable | P0 |
| FR-CONV-04 | Barge-in: user speech interrupts assistant playback within 300 ms | P0 |
| FR-CONV-05 | Persona: consistent, configurable personality; emotion-aware phrasing | P1 |
| FR-CONV-06 | Multiple selectable voices | P1 |
| FR-CONV-07 | Conversation continuity across restarts and across text↔voice | P0 |
| FR-CONV-08 | Clarifying questions only when ambiguity is action-blocking | P0 |

### Memory (MEM)
| ID | Requirement | Pri |
|---|---|---|
| FR-MEM-01 | Episodic memory: all conversations persisted and searchable (keyword + semantic) | P0 |
| FR-MEM-02 | Semantic memory: extracted facts/preferences with provenance and confidence | P0 |
| FR-MEM-03 | Procedural memory: successful task recipes stored and retrieved as guidance | P1 |
| FR-MEM-04 | Working memory: relevance-based context assembly per turn within token budget | P0 |
| FR-MEM-05 | Consolidation: background job distills episodes → facts, decays stale items | P1 |
| FR-MEM-06 | Corrections are first-class: user correction supersedes and links to the mistake | P1 |
| FR-MEM-07 | Memory viewer UI: browse, edit, delete, export any memory ("right to forget") | P1 |
| FR-MEM-08 | Tracks projects, coding style, frequent folders/apps, goals, calendar, key files | P1 |

### Task engine (TASK)
| ID | Requirement | Pri |
|---|---|---|
| FR-TASK-01 | Understand → plan → execute → observe → retry loop for every non-trivial request | P0 |
| FR-TASK-02 | Plans decompose into typed steps with chosen tools, inspectable before/during run | P0 |
| FR-TASK-03 | Bounded execution: per-task step limit, token budget, wall-clock timeout | P0 |
| FR-TASK-04 | Failure handling: analyze error, replan or retry (max N), then report honestly | P0 |
| FR-TASK-05 | Long-running tasks run in background; user can query status, pause, cancel | P1 |
| FR-TASK-06 | Scheduled and recurring tasks (cron-like) surviving restarts | P1 |
| FR-TASK-07 | Multi-agent delegation for parallelizable or specialist work | P2 |

### Desktop & system control (DESK)
| ID | Requirement | Pri |
|---|---|---|
| FR-DESK-01 | File management: search/read/move/rename/organize within permitted roots | P0 |
| FR-DESK-02 | App control: launch/focus/close; window management | P0 |
| FR-DESK-03 | UIA-based control of app UIs (click/type/read by accessibility tree) | P1 |
| FR-DESK-04 | Vision fallback: screenshot + OCR + element grounding when no UIA/API exists | P1 |
| FR-DESK-05 | Clipboard read/write (permissioned) | P1 |
| FR-DESK-06 | Process & hardware monitoring: CPU/RAM/GPU/disk, process list, startup apps | P1 |
| FR-DESK-07 | Terminal execution in sandboxed shell with output capture | P0 |
| FR-DESK-08 | App-specific skill packs: VS Code (CLI/extension), Adobe apps (UXP/ExtendScript scripting — never pixel-clicking Premiere timelines) | P2 |
| FR-DESK-09 | Screen understanding: "what am I looking at" from current screen | P1 |

### Browser (BROW)
| ID | Requirement | Pri |
|---|---|---|
| FR-BROW-01 | Full browser automation: navigate, read, fill, click, download via Playwright | P1 |
| FR-BROW-02 | Attach to the user's real browser profile (logged-in sessions) with consent | P1 |
| FR-BROW-03 | Research agent: multi-page reading → synthesized, cited answer | P1 |
| FR-BROW-04 | Web content is always treated as untrusted input (see SEC-07) | P0 |

### Platform (PLAT)
| ID | Requirement | Pri |
|---|---|---|
| FR-PLAT-01 | All capabilities exposed as MCP tools; third-party MCP servers mountable | P0 |
| FR-PLAT-02 | Plugin lifecycle: install/enable/disable/configure without kernel restart | P1 |
| FR-PLAT-03 | Notifications: Windows toasts + phone push; assistant-initiated when relevant | P1 |
| FR-PLAT-04 | Remote access: full text/voice control from phone browser (PWA) over Tailscale | P1 |
| FR-PLAT-05 | OpenAI-compatible API endpoint exposing the assistant to other clients | P2 |
| FR-PLAT-06 | Self-learning: preference extraction from corrections and repeated behavior | P2 |

### Model gateway (LLM) — new in rev 2
| ID | Requirement | Pri |
|---|---|---|
| FR-LLM-01 | Single provider-agnostic inference interface; adding/switching providers is a config (registry) change only — zero application-code change | P0 |
| FR-LLM-02 | Quota-aware routing: local persisted accounting of RPM/RPD/TPM per provider×model; exhausted models are never attempted (preemptive, not reactive) | P0 |
| FR-LLM-03 | Automatic failover cascade with circuit breakers; an interactive turn survives any single provider outage with ≤ 2 s added latency | P0 |
| FR-LLM-04 | Task-class routing (triage / conversation / planning / long-context / vision / background) driven by registry data | P0 |
| FR-LLM-05 | Privacy classes P0/P1/P2 enforced at the gateway; P2 content never leaves the device; per-provider informed consent for P1 | P0 |
| FR-LLM-06 | Optional local model as emergency fallback and P2 handler; full offline degraded mode (chat + local tools + queued cloud work) | P1 |
| FR-LLM-07 | Response caching for deterministic/background prompts to conserve quota | P1 |
| FR-LLM-08 | Quota, provider-health, and egress ("what left the device, to whom, at what class") visible in UI | P1 |
| FR-LLM-09 | Background/batch work draws quota only above a reserved interactive headroom (default 30 %) | P1 |

### Vault: backup, sync, archive (SYNC) — new in rev 2
| ID | Requirement | Pri |
|---|---|---|
| FR-SYNC-01 | Scheduled + on-demand encrypted snapshots of the hot store to Google Drive; GFS retention (14 daily / 8 weekly / 12 monthly) | P0 |
| FR-SYNC-02 | Client-side encryption (AES-256-GCM) for every vault blob; key in Windows Credential Manager + one-time recovery phrase; Drive never sees plaintext or keys | P0 |
| FR-SYNC-03 | Verified restore: fresh machine + recovery phrase → identical assistant; restore drill automated and part of release gates | P0 |
| FR-SYNC-04 | Multi-device sync via per-device append-only journal segments (never raw DB file sync); conflict-free by construction; user-visible resolution for concurrent edits of the same memory item | P1 |
| FR-SYNC-05 | Cold archive tiering: episodes/audit beyond hot window move to Drive with local search stubs; on-demand rehydration | P1 |
| FR-SYNC-06 | Least-privilege Drive access (`drive.file` scope); offline-tolerant outbox (accumulate, drain on reconnect); vendor-portable blob formats behind a `RemoteVault` port | P0 |

### UI (UI)
| ID | Requirement | Pri |
|---|---|---|
| FR-UI-01 | Main window: chat, task dashboard, memory viewer, plugin manager, logs, settings | P0/P1 |
| FR-UI-02 | Floating orb/overlay: always-on-top mini state (idle/listening/thinking/speaking) | P1 |
| FR-UI-03 | System tray: quick actions, kill switch, mode toggles | P0 |
| FR-UI-04 | Developer mode: raw event stream, prompt inspector, token/cost meters | P1 |

## 2. Non-functional requirements

### Performance (latency budgets — the product *is* these numbers)
Targets assume a normal broadband connection; cloud TTFT (Groq/Gemini Flash) typically
beats consumer-GPU local inference, so rev 2 keeps or tightens rev 1 targets while
dropping the GPU requirement entirely.

| ID | Metric | Target | Ceiling |
|---|---|---|---|
| NFR-PERF-01 | Voice round-trip (end of user speech → first audio out) | ≤ 1.2 s | 2.5 s |
| NFR-PERF-02 | Text first-token (interactive, cloud path) | ≤ 800 ms p50 | 2 s p95 (then hedge/failover fires) |
| NFR-PERF-03 | Wake-word detection | ≤ 200 ms | 500 ms |
| NFR-PERF-04 | Barge-in stop-speaking | ≤ 300 ms | 500 ms |
| NFR-PERF-05 | Memory retrieval (hybrid search, local) | ≤ 150 ms | 400 ms |
| NFR-PERF-06 | Idle footprint (kernel, no voice) | **≤ 300 MB RAM, ~0 % CPU, no GPU** | 500 MB |
| NFR-PERF-07 | Simple tool action (open app, file op) | ≤ 3 s end-to-end | 8 s |
| NFR-PERF-08 | Voice satellite idle (VAD + wake only, CPU) | ≤ 150 MB RAM, ≤ 2 % CPU | 300 MB |

### Availability — new in rev 2
| ID | Requirement |
|---|---|
| NFR-AVAIL-01 | ≥ 99.5 % of interactive turns get a first token within the NFR-PERF-02 ceiling across the provider portfolio (measured over the eval suite + telemetry) |
| NFR-AVAIL-02 | Full offline mode: memory, files, desktop tools, voice edge, and (if installed) local-model chat all function with zero internet; cloud-dependent work queues durably |

### Reliability & operability
| ID | Requirement |
|---|---|
| NFR-REL-01 | Kernel survives crash of any tool/plugin/voice process; supervised restarts |
| NFR-REL-02 | All state transitions journaled; recovery replays from event log + checkpoints |
| NFR-REL-03 | Graceful degradation ladder: no GPU → smaller models; no mic → text; no net → local-only |
| NFR-REL-04 | Every subsystem independently disableable via config |
| NFR-OPS-01 | Structured JSON logs, rotating; log level switchable at runtime |
| NFR-OPS-02 | One-command dev setup (`uv sync`); one-command run |

### Scalability (personal-scale, not web-scale)
| ID | Requirement |
|---|---|
| NFR-SCAL-01 | 5+ years of history: ≥ 100k conversations, ≥ 1M memory items, search stays < 400 ms |
| NFR-SCAL-02 | ≥ 20 concurrently mounted MCP servers without kernel degradation |
| NFR-SCAL-03 | ≥ 10 concurrent background tasks |
| NFR-SCAL-04 | Storage layer swappable to Postgres/server deployment without schema redesign |

## 3. Security requirements

| ID | Requirement |
|---|---|
| SEC-01 | **Permission tiers** per tool: T0 read-only → T1 reversible write → T2 destructive/external-visible → T3 credentials/money/system-config. T2+ require explicit confirmation unless durably granted per-tool |
| SEC-02 | **Confirmation workflow**: shows *concretely* what will happen (paths, recipients, commands); grants scoped: once / session / always-for-this-tool |
| SEC-03 | **Audit log**: append-only, hash-chained record of every tool call (args, result, initiator, permission decision). Tamper-evident |
| SEC-04 | **Emergency stop**: global hotkey + tray + voice ("stop everything") halts all execution ≤ 500 ms; independent watchdog process can kill the kernel |
| SEC-05 | **Sandboxing**: shell/code execution in restricted subprocess (job objects, low-privilege token, filesystem allowlist); no silent access outside permitted roots |
| SEC-06 | **Secrets**: only in Windows Credential Manager (DPAPI); never in config, logs, prompts, or memory; plugins receive scoped handles, not raw secrets |
| SEC-07 | **Taint tracking / prompt-injection defense**: content from web, screen, OCR, files, emails is UNTRUSTED. Untrusted content can never escalate: any T2+ action in a turn that consumed untrusted content forces confirmation regardless of standing grants |
| SEC-08 | **Encrypted memory at rest** (SQLCipher or OS-level) — P1, opt-in initially |
| SEC-09 | **Role system**: remote/mobile sessions get reduced default capabilities vs local console; per-channel policy |
| SEC-10 | Camera/mic access is indicator-visible and per-session revocable |
| SEC-11 | Network egress allowlist for plugins (default: none) |
| SEC-12 | **Egress choke points**: only the Model Gateway (classified prompts) and the Vault (ciphertext) may transmit user data off-device; all egress is audited and user-inspectable |
| SEC-13 | **Privacy classification**: every prompt section carries a P0/P1/P2 class (memory items classed at write time, tool outputs inherit source class, secret-pattern scan as backstop); enforcement at the gateway, below cognition |

## 4. Technical constraints

| ID | Constraint |
|---|---|
| TC-01 | $0 mandatory spend: every P0/P1 feature runs on free/open components or free cloud tiers (Gemini/Groq/OpenRouter free, Drive 15 GB, Tailscale personal) |
| TC-02 | Primary target: Windows 11, single user, **any x64 machine with 8 GB RAM — no GPU required**. Optional extras (local fallback model, local VLM) scale with available hardware but nothing P0/P1 depends on them |
| TC-03 | No vendor lock-in: model layer is a config-driven provider registry behind one interface; tools speak MCP; storage is SQLite (portable file); vault blobs are provider-agnostic versioned formats behind a `RemoteVault` port |
| TC-04 | Paid services only as optional, clearly-flagged upgrades (e.g., a paid LLM tier through the same gateway — one registry entry). Justification required in ADR |
| TC-05 | Data leaving the device is limited to (a) classified, audited inference prompts per SEC-12/13 and (b) client-side-encrypted vault blobs. Free-tier training-data implications are disclosed per provider at consent time; local-only routing mode is one settings toggle |
