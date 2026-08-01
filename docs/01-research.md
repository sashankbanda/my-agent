# Phase 1 — Research: Comparable Projects

Survey of the landscape as of mid-2026. Goal: steal every good idea, avoid every
documented mistake. Verify current versions before implementation — this space moves fast.

## 1. The requested projects

### Open Interpreter
- **What it is:** LLM executes code (Python/shell/JS) locally to accomplish tasks; "code as the universal tool."
- **Does well:** Radical simplicity — one loop: LLM writes code → run → feed output back. Astonishing capability-per-line-of-code. OS mode drives GUIs via screenshots. Proved that *code execution is the most general tool*.
- **Weaknesses:** No real memory. No planning layer — greedy single-loop, fails on multi-step tasks. Safety model is basically "YOLO or confirm every block," which trains users to spam-approve. Unstructured tool surface makes permissions impossible to reason about.
- **Lessons:** Include a code-execution tool — it covers the long tail no dedicated tool covers. But wrap it in a *tiered* permission model, and never make raw code the *primary* tool when a structured tool exists (structured = auditable, permissible, retryable).

### OpenHands (ex-OpenDevin)
- **What it is:** Autonomous software-engineering agent platform (sandboxed runtime, browser, editor).
- **Does well:** Event-stream architecture — every observation/action is an immutable event; state is a fold over the stream. Excellent sandboxing (Docker runtime). Strong eval discipline (SWE-bench).
- **Weaknesses:** Heavy (Docker mandatory), dev-task-centric, no voice/desktop/personal-memory story. Agent loop is expensive in tokens.
- **Lessons:** **Adopt the event-stream as the kernel's spine.** Immutable, append-only event log = audit log = replay/debug = state reconstruction, all one mechanism. This is the single best architectural idea in the field.

### Microsoft UFO / UFO² (highly relevant, not on your list)
- **What it is:** Microsoft Research's "Desktop AgentOS" for Windows — multi-agent (HostAgent + per-app AppAgents), hybrid GUI+API actions, Windows UI Automation grounding.
- **Does well:** Exactly our desktop-control problem. Key insights: (1) *hybrid control* — call an app's API/COM when available, click pixels only as fallback; (2) per-application agents with app-specific knowledge; (3) UIA tree + visual grounding fused; (4) a "puppeteer" execution layer separate from reasoning.
- **Weaknesses:** Research code, GPT-4V-tethered (paid), no memory/voice/companionship, brittle outside eval scenarios.
- **Lessons:** Copy the **hybrid action layer** (API-first, UIA-second, vision-last) and per-app "skill packs." This is the correct desktop-control architecture and validates our layered approach.

### AutoGen → Microsoft Agent Framework
- **What it is:** Multi-agent conversation framework; in late 2025 AutoGen and Semantic Kernel converged into Microsoft Agent Framework.
- **Does well:** Pioneered agent-to-agent conversation patterns, group chat, human-in-the-loop turns. Good async core (v0.4 rewrite).
- **Weaknesses:** **Framework churn is the lesson** — three incompatible rewrites in three years (v0.2 → v0.4 → Agent Framework). Building a long-lived personal system directly on it means forced migrations. Conversation-as-control-flow is hard to make deterministic and hard to permission.
- **Lessons:** Never let a framework's types leak past an adapter boundary. Multi-agent-as-chat is the wrong default; explicit graphs beat emergent conversation for reliability.

### LangGraph
- **What it is:** Graph-based stateful agent orchestration (nodes = steps, edges = control flow), checkpointing, interrupts.
- **Does well:** Explicit state machine → deterministic, resumable, debuggable. **Checkpointing to SQLite** and **interrupt/resume** (pause graph, ask human, continue) map perfectly onto our permission-confirmation flow. Streaming-first. Can be used standalone without the LangChain kitchen sink.
- **Weaknesses:** Tied to LangChain org's pace and telemetry-driven roadmap; abstraction tax when debugging; some churn risk (less than AutoGen).
- **Lessons:** Best-in-class orchestration primitives today. Use it — but behind our own `Orchestrator` port (see Phase 3) so it's an implementation detail, not a foundation.

### Claude Desktop / Claude Code (Anthropic)
- **Does well:** **MCP** — the open standard that solved the plugin problem for the whole industry: a tool server speaks JSON-RPC, declares tools/resources/prompts, any client can use it. Thousands of MCP servers now exist (Spotify, GitHub, Google Drive, Home Assistant…). Claude Code's permission UX (allowlist, per-tool prompt, session grants) is the best-executed confirmation workflow shipped anywhere.
- **Weaknesses:** Closed clients, cloud models, no persistent-companion memory or voice.
- **Lessons:** **Adopt MCP as the plugin architecture wholesale** — our roadmap's plugin list (Spotify, WhatsApp, GitHub, Docker, OBS, Home Assistant…) largely already exists as MCP servers we get for free. Copy the permission UX patterns.

### Continue / Cursor / Windsurf
- **What they are:** IDE-embedded coding assistants (Continue = OSS; Cursor/Windsurf = commercial VS Code forks).
- **Do well:** Context engineering (repo maps, embeddings over code, diff-based edits), tight feedback loops, streaming UX. Cursor proved *speculative, reviewable edits* (apply → user eyeballs diff) beats autonomous file writing for trust.
- **Weaknesses:** Single-domain; closed (Cursor/Windsurf); no life outside the editor.
- **Lessons:** For our coding-assistant agent: work through diffs the user can review, keep a repo map in memory, and *integrate with* VS Code (CLI/extension) rather than reimplementing an editor.

### Open WebUI
- **What it is:** Self-hosted chat UI over Ollama/OpenAI-compatible backends.
- **Does well:** Model-agnostic via OpenAI-compatible API (the de-facto standard we should also speak), polished self-hosted UX, simple RAG, plugin "pipelines."
- **Weaknesses:** Chat-first, not agent-first; no OS control, weak memory.
- **Lessons:** Expose our kernel over an OpenAI-compatible endpoint too — instantly usable from dozens of existing clients. UI polish drives daily use; daily use drives memory value.

### SuperAGI / AutoGPT-era platforms
- **Do well:** Early vision of autonomous agents with tool marketplaces, telemetry dashboards.
- **Weaknesses:** The cautionary tale: unbounded autonomous loops burn tokens, drift off-goal, and were abandoned. GUI-configured "agents" without solid ground truth = demos, not tools.
- **Lessons:** Bound every loop (budget, step count, timeout). Plans must be inspectable *before* execution. Reliability > autonomy theater.

## 2. Adjacent projects we should also learn from

| Project | Domain | Key takeaway for us |
|---|---|---|
| **Leon** | OSS personal assistant | Skill-based structure is nice; but NLU-intent architecture (pre-LLM) is obsolete — don't build intents, let the LLM route |
| **OVOS / Neon (Mycroft's heirs)** | Voice assistant OS | Wake-word + STT + TTS pipeline plumbing on-device is a solved problem; message-bus architecture between voice components works |
| **Home Assistant** | Home automation | The gold standard for a *years-long* OSS project: entity/service registry, automations DSL, integration model, and its Assist pipeline (wake word → STT → intent → TTS) is exactly our voice pipeline shape. Also: MCP server exists → our IoT plugin is nearly free |
| **browser-use** | Browser agents | DOM-distillation (interactive elements → indexed list for the LLM) massively outperforms screenshot-only browsing; reuse the technique or the library |
| **OmniParser (MS)** | Screen parsing | Turns raw screenshots into structured, LLM-readable element lists — the vision fallback for apps with no UIA tree |
| **Pipecat / LiveKit Agents** | Realtime voice pipelines | Frame-based streaming pipeline (VAD/STT/LLM/TTS as composable processors) with barge-in; Pipecat is OSS and local-friendly — strong candidate to adopt rather than rebuild |
| **Anthropic Computer Use / OpenAI Operator** | Computer-use models | Validated screenshot→action agents; also validated that *pure vision* control is slow and expensive → reinforces API-first hybrid control |
| **Agent-S / Agent-S2 (Simular)** | OS agents | Experience-augmented hierarchical planning: store successful task trajectories, retrieve them as few-shot guidance later — this is our "procedural memory" concretely |
| **MemGPT / Letta** | Agent memory | Paged memory (core/archival) with self-editing memory tools; validates memory-as-tools the LLM manages explicitly |
| **whisper.cpp / faster-whisper** | Local STT | Local Whisper is production-grade; faster-whisper large-v3-turbo ≈ real-time on modest GPUs, distil variants on CPU |

## 3. Synthesis — the ten commandments extracted

1. **Event-stream kernel** (OpenHands): append-only event log as spine = audit + replay + state.
2. **MCP for all tools/plugins** (Claude): don't invent a plugin API; adopt the ecosystem.
3. **Hybrid desktop control** (UFO²): API → UIA → vision, in that order, never vision-first.
4. **Explicit graphs over emergent chat** (LangGraph vs AutoGen): deterministic, resumable orchestration.
5. **Framework firewall** (AutoGen churn): adapters around every third-party framework.
6. **Bounded autonomy** (AutoGPT collapse): budgets, step limits, inspectable plans.
7. **Diff-style reviewable actions** (Cursor): show what *will* happen; make approval cheap and informed.
8. **DOM/UIA distillation over pixels** (browser-use, OmniParser): structured context beats screenshots.
9. **Memory as explicit, layered, self-editable state** (MemGPT, Agent-S): not just a vector dump.
10. **Ship a usable product every milestone** (Home Assistant's longevity): daily use is what makes a companion learn.

## 4. Gap analysis — why this project needs to exist

No surveyed project combines: voice-native companionship + deep Windows control +
persistent layered memory + local-first free stack + real security model. Closest
composites: UFO² (control, no companion) + Open WebUI (UI, no agency) + OVOS (voice,
no brain) + MemGPT (memory, no body). Our architecture is deliberately the union of
their best parts behind one kernel.
