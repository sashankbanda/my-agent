# MyAgent — Personal AI Operating Layer

> Working codename: **MyAgent**. A cloud-lean, local-sovereign, voice-native,
> autonomous personal assistant for Windows — designed to grow into a personal AI
> operating system over several years.

**Status:** Implementation in progress (M0–M4 complete). The architecture is frozen at
**v3** ([docs/10-review-v3.md](docs/10-review-v3.md)) and all implementation follows the
**[Implementation Playbook](docs/11-playbook.md)** — the single source of truth.

**▶ To run it, see the [RUNBOOK](RUNBOOK.md)** — every command, which terminal it goes
in, and how to fix the usual problems. Short version: `uv run python -m myagent`, then
open http://127.0.0.1:8765.

> **Rev 2:** reasoning layer redesigned around free cloud APIs (Groq, Gemini,
> OpenRouter) with local LLMs as optional emergency fallback; storage redesigned as
> hot SQLite + encrypted Google Drive vault (backup / sync / archive).

---

## Vision

A digital companion that talks like a friend, remembers everything that matters,
plans and executes real work on this machine (desktop, browser, files, code, media
tools), and is reachable from anywhere — while remaining **safe, auditable, free to
run, and owned entirely by you**.

## Design principles (the constitution)

These override any convenience shortcut. Every future decision is tested against them.

1. **Cloud-lean, local-sovereign.** State, memory, and capability live on this machine;
   reasoning rents free cloud capacity through one auditable gateway. No internet ⇒
   degraded brain, never lost data or a dead system. Zero mandatory spend.
2. **Thin waist.** The kernel exposes a small, stable set of internal contracts
   (events, tool calls, memory ops, model calls). Providers, frameworks, and UIs plug
   into the waist and are replaceable. Providers and frameworks churn; contracts don't.
3. **MCP-native.** Every capability — built-in or plugin — is a tool behind the
   Model Context Protocol. The plugin system is not a subsystem; it is the tool system.
4. **Capability security, not vibes.** No tool executes without the permission broker.
   Only two modules may send user data off-device: the Model Gateway (classified,
   audited prompts) and the Vault (ciphertext only). Untrusted content can never
   silently trigger privileged actions.
5. **Every milestone ships something you use daily.** No six-month dark periods.
6. **The brain is a commodity behind a registry.** Adding or switching an LLM provider
   is a config entry, never a code change. Free tiers today, anything tomorrow.
7. **Boring storage, durable data.** One SQLite file (WAL) is operational truth; the
   encrypted Drive vault makes it survivable and portable. Data outlives code — and
   outlives providers.

## Document map (read in order)

| Phase | Document | Contents |
|---|---|---|
| 1 | [docs/01-research.md](docs/01-research.md) | Survey of 19 comparable projects — strengths, weaknesses, lessons |
| 2 | [docs/02-requirements.md](docs/02-requirements.md) | Functional, non-functional, security, performance requirements |
| 3 | [docs/03-architecture.md](docs/03-architecture.md) | Full system architecture, all subsystems, Mermaid diagrams |
| 4 | [docs/04-technology.md](docs/04-technology.md) | Every stack decision with objective comparisons |
| 5 | [docs/05-folder-structure.md](docs/05-folder-structure.md) | Production monorepo layout |
| 6 | [docs/06-implementation-plan.md](docs/06-implementation-plan.md) | 13 milestones, each independently usable |
| 7 | [docs/07-standards.md](docs/07-standards.md) | Coding, testing, logging, docs, versioning standards |
| 8 | [docs/08-risks.md](docs/08-risks.md) | Risk register: bottlenecks, failure modes, mitigations |
| 9 | [docs/09-roadmap.md](docs/09-roadmap.md) | Evolution path: AI OS, cross-platform, mobile, cloud, agent teams |

Architecture decisions are recorded as ADRs in `docs/adr/` once implementation starts.

## Decision summary (one screen)

| Concern | Decision | Runner-up |
|---|---|---|
| Core language | Python 3.12+ (kernel), TypeScript (UI) | Rust kernel (later, selectively) |
| Process model | Modular monolith kernel + voice, UI, sandbox as separate processes | Microservices (rejected) |
| Orchestration | LangGraph behind an internal `Orchestrator` port | Custom asyncio loop |
| **Reasoning providers** | **Groq (speed) + Gemini free (quotas/tools/vision) + OpenRouter free pool, via config registry** | Single provider (rejected — availability) |
| **Provider abstraction** | **LiteLLM normalization + our quota-aware preemptive router** | OpenRouter as sole gateway (rejected — lock-in) |
| **Local LLM** | **Optional emergency fallback + sensitive-data (P2) handler (Ollama, small CPU model)** | Local-primary (retired in rev 2) |
| **Privacy** | **P0/P1/P2 prompt classes enforced at the gateway; P2 never leaves device** | Trust-the-provider (rejected) |
| Embeddings | fastembed (ONNX, CPU, local — deliberately not cloud) | Gemini embeddings (rejected — streams memory out) |
| Memory store | SQLite + sqlite-vec, layered memory model | LanceDB, Qdrant |
| **Backup / sync / archive** | **Encrypted Google Drive vault: `VACUUM INTO` snapshots (GFS), journal-segment sync, cold-tier archive — behind a `RemoteVault` port** | Syncing the SQLite file (rejected hard — corruption) |
| **Vault crypto** | **zstd + AES-256-GCM client-side; key in Credential Manager + recovery phrase; `drive.file` scope** | SQLCipher-to-Drive (wrong tool) |
| STT | faster-whisper CPU int8 (streaming) + optional Groq Whisper accuracy pass | Vosk |
| Wake word / VAD | openWakeWord / Silero — always local | Porcupine |
| TTS | Kokoro-82M (CPU) + Piper fallback | Cloud TTS (no viable free streaming tier) |
| Desktop automation | Layered: native APIs → Windows UIA → vision+input fallback | PyAutoGUI-only (rejected) |
| Browser automation | Playwright over CDP, attach to real profile | Selenium (rejected) |
| OCR | Windows.Media.Ocr + RapidOCR — local | Tesseract |
| Screen/vision understanding | Gemini Flash multimodal (consent-gated); moondream2 optional offline extra | Local 7B VLM (no longer required) |
| API layer | FastAPI + WebSocket | Flask (rejected) |
| Scheduler | APScheduler + SQLite job store | Celery/Redis (rejected — overkill) |
| Desktop UI shell | Tauri 2 + React | Electron (rejected — weight) |
| Remote access | Tailscale (free tier) + PWA | Port forwarding (rejected — unsafe) |
| Phone push | ntfy | Pushover (paid) |
| Secrets | Windows Credential Manager via `keyring` + DPAPI | Encrypted file vault |
| Package management | `uv` workspaces | pip/poetry |

## Resource budget (rev 2 headline)

No GPU required. Kernel idle ≤ 300 MB RAM; voice satellite idle ≤ 150 MB (VAD + wake
word only); heavy lifting (LLM reasoning, vision) is rented from free cloud tiers
through one audited gateway.

## The two honest caveats

1. **Free tiers are paid in data and volatility.** Free-tier prompts may be used for
   training, and quotas/models change monthly. The design answers with privacy classes
   (sensitive content never leaves the device), per-provider consent, an egress audit
   you can inspect, a three-provider portfolio with preemptive quota routing, and a
   one-toggle local-only mode. See risks R1 and R3.
2. **No internet ⇒ degraded brain.** Memory, files, desktop tools, and the voice edge
   keep working offline; chat falls back to a small local model if installed; cloud
   work queues durably. The assistant says so honestly rather than failing silently.
