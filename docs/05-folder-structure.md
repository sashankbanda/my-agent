# Phase 5 — Repository Structure

Monorepo, `uv` workspace for Python + `pnpm` workspace for the UI. Structure mirrors
the architecture: one folder per kernel layer/subsystem, satellites under `apps/`,
capabilities under `tools/`, everything shared under `packages/`.

```
my-agent/
├── README.md
├── pyproject.toml              # uv workspace root
├── uv.lock
├── .env.example                # non-secret config template (secrets live in Credential Manager)
├── Justfile                    # task runner: just dev / test / lint / run
│
├── docs/
│   ├── 01-research.md … 09-roadmap.md
│   └── adr/                    # ADR-0001-orchestrator.md, ... one per irreversible decision
│
├── packages/                   # shared Python libraries (no side effects on import)
│   ├── contracts/              # THE THIN WAIST — Pydantic models for events, tool calls,
│   │   └── src/contracts/      #   memory items, plans, permissions. Zero heavy deps.
│   │                           #   Everything imports this; this imports nothing of ours.
│   └── common/                 # logging setup, config loader, errors, ids, time utils
│
├── apps/
│   ├── kernel/                 # the modular monolith
│   │   ├── src/kernel/
│   │   │   ├── main.py         # composition root: wire modules, start supervisors
│   │   │   ├── bus/            # event bus + journal writer/reader
│   │   │   ├── gateway/        # FastAPI app: WS, HTTP, OpenAI-compat endpoint, sessions
│   │   │   ├── cognition/
│   │   │   │   ├── orchestrator.py     # Orchestrator PORT (interface)
│   │   │   │   ├── langgraph_adapter/  # the ONLY place LangGraph is imported
│   │   │   │   ├── roles/              # conversation, planner, executor, researcher,
│   │   │   │   │                       #   coder, vision — prompts + tool bindings
│   │   │   │   └── context/            # context assembler + token budgeting
│   │   │   ├── capability/
│   │   │   │   ├── tool_router.py      # MCP client, in-proc fast path, taint tagging
│   │   │   │   ├── permissions/        # broker, policy store, grant rules
│   │   │   │   └── scheduler/          # APScheduler wiring, durable jobs
│   │   │   ├── memory/
│   │   │   │   ├── service.py          # memory PORT
│   │   │   │   ├── store/              # SQLite repos, sqlite-vec, FTS5, migrations/
│   │   │   │   ├── retrieval.py        # hybrid search + fusion
│   │   │   │   └── consolidation.py    # nightly distill/decay/promote job
│   │   │   ├── models/                 # Model Gateway v2 (sole LLM egress point)
│   │   │   │   ├── provider.py         # InferenceProvider PORT
│   │   │   │   ├── litellm_adapter.py  # the ONLY place LiteLLM is imported
│   │   │   │   ├── registry.py         # loads config/providers.yaml (models, quotas,
│   │   │   │   │                       #   capabilities, trains_on_data flags)
│   │   │   │   ├── router.py           # task-class × privacy-class ranked cascade
│   │   │   │   ├── quota.py            # persisted token buckets, interactive headroom
│   │   │   │   ├── health.py           # latency EWMA, circuit breakers, hedging
│   │   │   │   ├── privacy.py          # P0/P1/P2 filter, redaction, secret prescan
│   │   │   │   └── cache.py            # exact-match response cache
│   │   │   ├── vault/                  # Drive vault engine (sole non-LLM egress, ciphertext only)
│   │   │   │   ├── remote.py           # RemoteVault PORT
│   │   │   │   ├── drive_adapter.py    # Google Drive v3, drive.file scope, changes cursor
│   │   │   │   ├── crypto.py           # zstd + AES-256-GCM envelope, recovery phrase
│   │   │   │   ├── snapshot.py         # VACUUM INTO → encrypt → upload, GFS retention
│   │   │   │   ├── sync.py             # journal-segment shipper + folder/ingest
│   │   │   │   ├── archive.py          # cold-tier bundles + local stubs + rehydrate
│   │   │   │   └── restore.py          # full-restore path (also used by CI drill)
│   │   │   ├── security/               # audit chain, secrets facade, sandbox launcher
│   │   │   └── notify/                 # toasts + ntfy push
│   │   └── tests/
│   │
│   ├── voice/                  # real-time satellite process
│   │   ├── src/voice/
│   │   │   ├── pipeline.py     # frames: mic → vad → wake/stt → ws;  ws → tts → speaker
│   │   │   ├── stt/  tts/  wake/  vad/
│   │   │   └── barge_in.py
│   │   └── tests/
│   │
│   ├── watchdog/               # tiny independent kill-switch process
│   │
│   └── ui/                     # Tauri 2 + React + TS (pnpm workspace)
│       ├── src-tauri/          # rust shell: tray, overlay window, hotkeys, autostart
│       └── src/                # React app (shared by desktop window, overlay, PWA)
│           ├── views/          # chat, tasks, memory, plugins, logs, settings, dev
│           └── lib/ws.ts       # kernel client
│
├── tools/                      # built-in MCP servers (each independently runnable)
│   ├── files/                  # FR-DESK-01: scoped filesystem ops
│   ├── shell/                  # sandboxed terminal (via kernel sandbox launcher)
│   ├── windows/                # apps, windows, clipboard, processes, hardware (psutil)
│   ├── uia/                    # rung-2 UI Automation control
│   ├── vision/                 # screenshot, OCR, VLM screen understanding, grounding
│   ├── browser/                # Playwright + DOM distillation
│   ├── coder/                  # repo map, diff-based edits, VS Code integration
│   └── media/                  # Premiere/AE/Photoshop via UXP/ExtendScript bridges
│
├── plugins/                    # third-party & user MCP servers + manifests
│   └── registry.json           # source, transport, version, granted tiers per plugin
│
├── skills/                     # declarative skill packs: per-app knowledge, workflows,
│   └── vscode/ adobe/ office/  #   prompt fragments (markdown + yaml, hot-reloadable)
│
├── config/
│   ├── default.yaml            # feature flags, latency budgets, vault schedule
│   ├── providers.yaml          # LLM provider registry: models, quotas (RPM/RPD/TPM),
│   │                           #   capabilities, speed class, trains_on_data, routing tables
│   ├── policies.yaml           # permission tiers per tool, taint rules, channel roles,
│   │                           #   privacy-class provider allowances
│   └── personas/               # assistant personality definitions
│
├── scripts/                    # doctor.py (env/network/quota check), restore.py
│                               #   (disaster recovery from Drive), install_autostart.py,
│                               #   setup_fallback_model.py (optional Ollama pull)
└── .github/workflows/ci.yml    # lint, types, tests on push
```

Rules that keep this healthy:

1. **`packages/contracts` is sacred** — the thin waist. Changing it requires an ADR.
   It has no heavy imports, so every process (kernel, voice, tools) shares it cheaply.
2. **Import direction is enforced** (ruff/import-linter): `tools/*` and `apps/voice`
   never import `kernel`; kernel layers only import downward; only
   `cognition/langgraph_adapter` imports LangGraph.
3. **Each `tools/*` server runs standalone** (`uv run tools/files`) — testable without
   the kernel, usable from any MCP client (including Claude Desktop — free dogfooding).
4. **Migrations are append-only** (`memory/store/migrations/`) — data outlives code.
5. `skills/` and `config/` are data, not code — editable without redeploying, and the
   long-term home of self-learned workflows.
