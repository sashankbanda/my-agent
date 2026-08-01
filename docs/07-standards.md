# Phase 7 — Engineering Standards

Standards exist to keep a multi-year solo/small-team project maintainable. Everything
here is enforced by tooling where possible, convention where not.

## 1. Naming

| Thing | Convention | Example |
|---|---|---|
| Python modules/functions/vars | `snake_case` | `tool_router.py`, `assemble_context()` |
| Python classes | `PascalCase` | `PermissionBroker` |
| Constants | `UPPER_SNAKE` | `MAX_PLAN_STEPS` |
| Event types | `PascalCase` past/declarative | `ToolCallCompleted`, `PlanCreated` |
| MCP tools | `verb_object`, namespaced by server | `files.move_file`, `browser.read_page` |
| TS/React | `camelCase` / `PascalCase` components | `useKernelSocket`, `TaskDashboard` |
| Branches | `feat/…`, `fix/…`, `adr/…` | `feat/m3-permission-broker` |
| DB tables/columns | `snake_case`, singular columns, plural tables | `memory_items.created_at` |

Requirement IDs (FR-*, SEC-*, NFR-*) appear in docstrings/tests that implement them —
greppable traceability.

## 2. Testing

| Layer | Tooling | Policy |
|---|---|---|
| Unit | pytest (+ pytest-asyncio) | Pure logic: permissions, context budgeting, retrieval fusion, planners. Fast (< 30 s suite) |
| Contract | pytest + Pydantic schemas | Every event/tool schema has round-trip + backward-compat tests; contracts never break silently |
| Integration | pytest, real SQLite tmp file, fake model | Kernel loop with a **scripted FakeLLM** (deterministic tool-call sequences) — agent logic tested without model flakiness |
| Tool e2e | pytest, real OS in CI-optional lane | files/shell/uia tools against a scratch dir & Notepad-class targets |
| Security regression | pytest, red-team suite | Every SEC-* has attack tests: path traversal, taint escalation, injection strings in web/OCR content, grant bypass. **Gate for every release** |
| Eval (model-in-loop) | promptfoo or pytest-eval lane, local model | Scenario suite ("organize downloads", "research X") scored on success/steps/tokens; run nightly, tracked over time — this is how prompt/model changes are judged |
| UI | Vitest + Playwright | Critical flows: confirmation dialog, kill switch, chat streaming |

Coverage target: 85 % on `kernel/capability`, `kernel/security`, `kernel/memory`
(the parts that must not fail); no numeric target elsewhere — tests must earn their
maintenance cost.

## 3. Documentation

- Every module: a top docstring saying *why it exists and what invariants it holds*.
- **ADRs** (`docs/adr/NNNN-title.md`) for every decision that is expensive to reverse:
  context → options → decision → consequences. The Phase 4 doc seeds ADRs 1–10.
- `CLAUDE.md` at repo root for AI-assisted development context (build cmds, conventions).
- Public docstrings on ports (`Orchestrator`, `MemoryService`, `ToolRouter`) are the
  API reference; generated docs later via mkdocs if ever needed.

## 4. Logging & observability

- **structlog**, JSON lines, one file per process, rotated daily, 14-day retention.
- Levels: DEBUG (dev only) / INFO (state changes) / WARNING (degraded) / ERROR (failed
  user-visible action). `trace_id` on every line, propagated across processes via WS
  metadata — one grep reconstructs a request end-to-end.
- **Never logged:** secrets, full prompts at INFO (DEBUG only, dev mode), raw audio.
- The **audit log is not the app log**: separate hash-chained store, append-only,
  never rotated away (compact/archive instead).
- Dev mode UI = live journal tail + prompt inspector + token meters (FR-UI-04).

## 5. Error handling

- Exception taxonomy in `contracts.errors`: `ToolError` (retryable?), `PermissionDenied`,
  `ModelError`, `BudgetExceeded`, `UserCancelled`. Orchestrator maps taxonomy → retry /
  replan / ask / abort. Unknown exceptions never cross a process boundary raw.
- Every tool result is `Result[ok|error]` in-band (LLM sees errors as observations —
  that's how retry/replan works), *plus* an event for the journal.
- User-facing failure reports state: what was attempted, what succeeded, what failed,
  why, and what wasn't attempted. No silent partial success (honesty requirement).

## 6. Dependency management

- `uv` workspace, committed `uv.lock`; `pnpm` + lockfile for UI.
- Heavy optional deps (torch, VLM, OCR) live in **extras** (`uv sync --extra vision`)
  so the kernel stays light (NFR-PERF-06).
- Monthly dependency-review chore; Dependabot/Renovate on. Any new dependency in
  `kernel/` core needs a one-line justification in the PR; `contracts/` allows none.
- Model weights are **not** dependencies: `scripts/setup_models.py` pulls pinned
  versions/hashes into a local cache.

## 7. Versioning & releases

- **SemVer** for the product (`0.x` until M8). `contracts` versioned independently —
  its major version is the compatibility line for plugins and satellites.
- **Conventional Commits** (`feat:`, `fix:`, `adr:`…) → changelog generated.
- `main` always boots; milestone tags (`m3-hands`) mark daily-drivable states.
- DB migrations append-only, tested against a copy of *real* data before release.

## 8. AI-assisted development rules

Because this repo will be co-developed with AI agents: keep files < ~400 lines,
one responsibility per module, ports documented — context-window-friendly code is
also human-friendly code. Generated code passes the same review, lint, and test gates
as hand-written code; no unreviewed generated code lands on `main`.
