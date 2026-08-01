# Phase 4 — Technology Selection (rev 2: cloud-first reasoning, Drive vault)

Method unchanged: compare against *our* constraints (free, minimal local footprint,
Windows, years-long maintenance), pick one, name the trigger that would change it.
Free-tier quotas cited are mid-2026 ballparks — they change monthly; the design keeps
them in config (`providers.yaml`), never in code.

## 1. API framework — FastAPI vs Flask

Unchanged from rev 1: **FastAPI** (async end-to-end, native WebSockets, Pydantic).

## 2. Database — SQLite vs PostgreSQL

Unchanged: **SQLite WAL + sqlite-vec + FTS5** as the hot store — and the cloud pivot
*strengthens* it: with a Drive vault providing durability and sync, the last argument
for a heavier local DB disappears. Repository pattern still guards the Postgres exit.

## 3. Orchestration — LangGraph (unchanged)

**LangGraph behind our `Orchestrator` port**, as rev 1. Cloud-first adds one point in
its favor: checkpoint/interrupt durability matters more when a provider mid-plan
outage must pause-and-resume a task instead of losing it.

## 4. Reasoning providers — the free-tier portfolio

The core question is no longer "which model fits my GPU" but "which *portfolio* of
free tiers maximizes quality × quota × availability." Single-provider is rejected
outright: any one free tier can throttle, change terms, or remove models overnight.

| Candidate | Strengths (free tier) | Weaknesses | Role |
|---|---|---|---|
| **Groq** | Extreme speed (LPU: lowest TTFT, 300–800+ tok/s) — the "feels instant" experience voice needs; solid OSS models (Llama-class 70B); free Whisper STT endpoint | Modest daily quotas; model list changes; no vision on free tier historically | **Primary: interactive conversation & triage** |
| **Google Gemini (AI Studio free)** | Biggest free quotas (Flash/Flash-Lite RPD in the hundreds–1500 range); best free function calling; 1M context; multimodal (vision); free embeddings exist | Data used for training on free tier (the privacy price); RPM modest; regional ToS variance | **Primary: planning/tool-calls, long context, vision; quota workhorse** |
| **OpenRouter (`:free` models)** | One key → dozens of models/hosts; instant breadth for failover; good for A/B-ing new OSS releases | Low per-model rate limits; variable latency/reliability; free pool composition churns; most free routes train on data | **Failover pool + experimentation** |
| Cerebras / SambaNova / Mistral (free tiers) | Very fast inference, decent quotas | Smaller model menus; tiers appear/vanish | Registry candidates — add when stable |
| Local Ollama (small model, CPU) | Always available, private, free forever | Quality floor; slow on CPU | **Emergency fallback + P2-sensitive handling** |

**Pick: Groq + Gemini + OpenRouter as the launch portfolio** (three independent
infrastructures), local Qwen-class 4B via Ollama as the optional-but-recommended
emergency layer. ToS note: quota multiplication via multiple accounts per provider is
a ToS violation — we don't design for it; multiplication comes from multiple *providers*.

## 5. Provider abstraction & routing — LiteLLM vs OpenRouter-as-router vs Portkey vs custom

| | LiteLLM (lib) | OpenRouter as sole gateway | Portkey/Kong-style gateway | Raw httpx per provider |
|---|---|---|---|---|
| Wire normalization (100+ providers) | ✓ mature | ✓ but one vendor | ✓ | build it |
| Quota-aware *preemptive* routing | ✗ (reactive fallbacks/cooldowns only) | ✗ (their policy, not ours) | partial | build it |
| Privacy-class routing | ✗ | ✗ | ✗ | build it |
| Runs in-proc, zero services | ✓ | ✓ (nothing local) | ✗ extra service | ✓ |
| Lock-in | low (lib behind our port) | **high — single point of failure & policy** | medium | none |

**Pick: LiteLLM as the normalization layer only, wrapped by our own thin router**
(Quota Governor + Health Tracker + privacy filter, ~small modules — see architecture
§6). LiteLLM's built-in router is *reactive* (retry on failure); our quota buckets
make routing *preemptive* (never call an exhausted provider). OpenRouter is demoted
to *a provider in the pool*, not the abstraction — making one aggregator the only path
to every model recreates vendor lock-in with extra steps. Trigger to revisit: LiteLLM
ships preemptive quota budgets + pluggable routing policy natively.

## 6. Embeddings — local stays local

| | fastembed (ONNX, CPU) | Ollama embed models | Gemini embedding API |
|---|---|---|---|
| Footprint | ~200 MB lazy-loaded, no service | needs Ollama service resident | zero local |
| Quota/privacy | free, private | free, private | free tier is small; ships **all memory content** to Google continuously |
| Latency | ms, in-proc | ms–10s ms | network RTT per memory op |

**Pick: fastembed with a bge-small / nomic-class ONNX model.** This is the deliberate
exception to cloud-first: embedding calls are high-frequency, low-compute, and carry
your entire memory verbatim — the worst possible thing to stream to a free tier.
Changed from rev 1 (was Ollama-served) to drop the Ollama residency requirement.

## 7. Speech — CPU-only local edge, cloud assist

- **STT: faster-whisper** `distil-small.en`/`base` int8 on CPU for live streaming
  (unchanged pick, smaller default models). **Groq Whisper (free)** as optional
  background re-transcriber for long dictations (accuracy pass, P1-gated).
- **Wake word: openWakeWord; VAD: Silero** — unchanged, always local (privacy).
- **TTS: Kokoro-82M (CPU) + Piper fallback** — unchanged. Cloud TTS rejected: no free
  tier offers streaming at conversational latency with stable quotas.

## 8. Vision & OCR — cloud takes the heavy lift

- **Screen/image understanding: Gemini Flash multimodal** (free tier) — replaces the
  local 7B VLM requirement entirely; biggest single footprint win of this revision.
  **moondream2 (CPU)** optional extra for offline/P2 screens. Consent + secret-prescan
  rules per architecture §9.
- **OCR: unchanged** — Windows.Media.Ocr fast path + RapidOCR robust path. OCR stays
  local: it's instant, free, and screenshots shouldn't leave the device just to read a
  button label.

## 9. Browser automation — Playwright (unchanged)

Playwright + CDP profile attach + DOM distillation, as rev 1.

## 10. Desktop automation — layered ladder (unchanged)

Native API/CLI → pywinauto (UIA) → vision grounding + input synthesis, as rev 1.

## 11. Vector store — sqlite-vec (unchanged)

Atomic memory-row + vector + FTS transactions still beat external vector DBs at
personal scale. Archive tiering (architecture §7.4) further caps growth.

## 12. Task queue / scheduler — asyncio + APScheduler (unchanged)

The Vault engine's upload workers are ordinary asyncio tasks fed by the
`sync_outbox` table — no broker, no new infrastructure.

## 13. Google Drive vault stack (new)

### 13.1 Drive client — official SDK vs raw REST vs rclone
| | google-api-python-client + google-auth | raw httpx + google-auth | rclone (external binary) |
|---|---|---|---|
| Fit for programmatic sync (changes API, resumable uploads) | ✓ complete | ✓ but hand-rolled paging/backoff | ✗ file-tree mirroring, not app logic |
| Dependency weight | moderate (pure Python) | minimal | external process to ship/manage |
| Maintenance risk | Google-maintained | ours | third-party binary |

**Pick: `google-auth` + `google-api-python-client`** (Drive v3: resumable uploads,
`changes.list` cursors, `drive.file` scope). rclone remains a documented *manual*
disaster-recovery path (it can list/pull the blob folder), not a dependency.

### 13.2 Encryption — cryptography (AES-256-GCM) vs age/pyrage vs SQLCipher
| | AES-256-GCM via `cryptography` | age (pyrage) | SQLCipher whole-DB |
|---|---|---|---|
| Blob encryption fit | ✓ standard, streamable, AEAD | ✓ nice UX, extra dep | ✗ encrypts the *local* DB, not blobs |
| Key custody | ours (DPAPI + recovery phrase) | ours | passphrase juggling |

**Pick: zstd (`zstandard`) + AES-256-GCM via `cryptography`**, envelope format
versioned in the blob header. SQLCipher stays the separate, optional local-at-rest
answer (SEC-08) — different threat, different tool.

### 13.3 Snapshot & archive formats
- Snapshot: `VACUUM INTO` (online, consistent) → zstd → AES-GCM. One file, restorable
  with SQLite alone after decrypt — no custom reader needed for disaster recovery.
- Journal segments & archive bundles: zstd JSONL (schema-versioned via `contracts`),
  manifest with hash chain. Human-recoverable formats on purpose: your data must
  outlive this codebase (TC-03).

## 14. UI shell — Tauri 2 (unchanged); new UI surfaces
Quota/provider health panel and vault status (last snapshot, outbox depth, "what left
the device" egress view) join the dev-mode and settings views.

## 15. Remote access & notifications — Tailscale + ntfy + toasts (unchanged)

## 16. Cross-cutting dev stack (delta only)

| Slot | Pick | Note |
|---|---|---|
| Provider normalization | **litellm** | behind `InferenceProvider` port |
| Embeddings | **fastembed** | ONNX, CPU, lazy-loaded |
| Drive | **google-api-python-client + google-auth** | `drive.file` scope |
| Crypto | **cryptography** (AES-256-GCM), **zstandard** | vault envelope |
| Everything else | unchanged from rev 1 | uv, ruff, pyright, pytest, Pydantic v2, structlog, keyring, pywin32/winrt, sounddevice |

## 17. Rejected / conditional (rev 2 status)

| Item | Status | Why |
|---|---|---|
| Local LLM as primary | **Demoted** to emergency fallback + P2 handler | Free cloud beats consumer-GPU local on quality *and* TTFT; footprint goal |
| Paid LLM APIs | Still optional upgrade, off by default | Now a smaller leap: same gateway, flip a registry entry (TC-04) |
| OpenRouter as sole gateway | Rejected | Aggregator lock-in; it's one provider in our pool |
| Syncing the SQLite file via Drive desktop client | **Rejected hard** | Concurrent binary-file sync corrupts databases; journal segments exist precisely for this |
| Google Drive full scope | Rejected | `drive.file` least privilege; Drive-as-user-tool is a separate MCP plugin + grant |
| Plaintext anything on Drive | Rejected | Client-side AES-GCM always; Google sees ciphertext only |
| Redis/Celery, Docker-mandatory, LangSmith | Rejected | unchanged from rev 1 |
