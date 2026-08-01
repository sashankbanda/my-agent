# MyAgent — Runbook (how to run it)

Quick reference for daily use. Every command runs from the project root:
`C:\02_DEV\01_ACTIVE\my-agent`

---

## TL;DR — the two commands you need

**Terminal 1 — the brain (always start this first):**
```
uv run python -m myagent
```
Wait for: `Uvicorn running on http://127.0.0.1:8765`

**Terminal 2 — the voice (optional):**
```
uv run python -m myagent.voice
```
Wait for: `voice_ready` and `kernel_connected`

Then either **talk** (say your wake word — check `wake.model` in
[config/voice.yaml](config/voice.yaml)) or **type** at http://127.0.0.1:8765

Stop anything with `Ctrl+C` in its terminal.

---

## Text chat (no voice)

Only Terminal 1 is needed.

1. `uv run python -m myagent`
2. Open http://127.0.0.1:8765 in your browser.
3. Buttons in the top right:
   - **Memory** — what it remembers about you; add/delete facts; "Back up now"
   - **Activity** — live audit log of every action + the **Emergency stop** button

---

## Voice chat

| Terminal | Command | Wait for |
|---|---|---|
| 1 | `uv run python -m myagent` | `Uvicorn running on http://127.0.0.1:8765` |
| 2 | `uv run python -m myagent.voice` | `voice_ready`, `kernel_connected` |

Then: say your **wake word**, pause half a beat, then speak your request.
After each reply you have ~8 seconds to keep talking without the wake word.

Talking over it stops it (barge-in) — works best with a headset.

---

## When voice doesn't respond

**Terminal 3** (leave 1 and 2 running):

```
uv run python -m myagent.voice --mic-check 30
```

Speak during the countdown. You get one line per second:

```
 8s  peak 0.703 |##########| vad 1.00  alexa 0.72  hey_jarvis 0.05  hey_mycroft 0.31
```

| What you see | What it means | Fix |
|---|---|---|
| `peak 0.000` always | mic is dead or wrong device | it should self-heal in ~6s; else raise mic volume in Windows Sound settings |
| peak moves, `vad` under 0.5 | input too quiet | Settings → System → Sound → your mic → raise volume to 80–100 |
| `vad 1.00` but wake scores low | wake word doesn't match your voice | pick the highest-scoring phrase and set it as `wake.model` (below) |

### Changing the wake word

Edit [config/voice.yaml](config/voice.yaml):

```yaml
wake:
  model: alexa        # alexa | hey_jarvis | hey_mycroft
  threshold: 0.4      # set slightly BELOW your typical score
```

Then restart Terminal 2 (`Ctrl+C`, then `uv run python -m myagent.voice`).

### No wake word working at all?

Use push-to-talk instead — edit `config/voice.yaml`:
```yaml
mode: ptt             # hold Ctrl+Space to talk
```

---

## Common problems

### "It says it has no tools" or a page/endpoint behaves oddly

Almost always an **old kernel still running** on port 8765 (your new one
couldn't take the port, so you're talking to stale code). Kill it:

```
$listener = (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($listener) { Stop-Process -Id $listener -Force }
```

Then start Terminal 1 again.

### Nuclear option — stop absolutely everything

```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match "myagent" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

### `Ctrl+C` in Terminal 1 didn't free the port

`uv` sometimes leaves the Python child running. Use the kill command above.

### The assistant is doing something you want stopped NOW

Click **Emergency stop** in the Activity panel (or `POST /kill`). It blocks
every action immediately. Click **Re-enable actions** when you're ready.

---

## Checking providers (Groq / Gemini / OpenRouter)

```
uv run python scripts/doctor.py --ping
```

Shows each key's presence, live provider pings, and today's quota usage.
Expect all three `[ok]`. If a model 404s, the free tier retired it — edit
[config/providers.yaml](config/providers.yaml) (data only, no code change).

Re-add or rotate a key:
```
uv run python scripts/doctor.py --set-key groq
uv run python scripts/doctor.py --set-key gemini
uv run python scripts/doctor.py --set-key openrouter
```
(Input is hidden; keys go into Windows Credential Manager, never a file.)

---

## Backups (encrypted Google Drive vault)

Manual backup right now:
```
uv run python scripts/restore.py --backup
```

List what's in the vault:
```
uv run python scripts/restore.py --list
```

Restore (**stop Terminal 1 first**):
```
uv run python scripts/restore.py
```

A nightly backup runs automatically at 3am while the kernel is running.
Your **recovery string** (shown once, at the first backup) is the only way to
decrypt the vault on a new machine — keep it off this laptop.

---

## What it's allowed to touch

Files: only your **Desktop, Documents, Downloads, Pictures, Music, Videos**.
Change that in [config/default.yaml](config/default.yaml) under `tools.roots`.

Permission behavior:
- **Reading** anything in those folders — no prompt
- **Creating / moving / copying** — no prompt (reversible)
- **Deleting, running commands, closing apps** — always asks you first
- After it reads a file or command output, **even safe writes ask again**
  (that's the injection defense: a document can't grant itself permissions)

---

## Development commands

| Purpose | Command |
|---|---|
| Run tests | `uv run pytest` |
| Fast subset | `uv run pytest tests/test_redteam.py -q` |
| Lint + format | `uv run ruff format src tests scripts` then `uv run ruff check src tests scripts` |
| Type check | `uv run pyright` |
| Everything CI runs | `uv run ruff check src tests scripts; uv run pyright; uv run pytest` |
| Rebuild the web UI (after UI edits) | `cd ui; npm run build` |
| Install/refresh dependencies | `uv sync` |
| Download voice models (one-time) | `uv run python scripts/setup_voice.py` |

---

## Where things live

| Thing | Path |
|---|---|
| Your data (SQLite) | `%LOCALAPPDATA%\MyAgent\myagent.db` |
| Voice models | `%LOCALAPPDATA%\MyAgent\models\` |
| API keys, vault key, OAuth token | Windows Credential Manager, service `myagent` |
| Kernel settings | [config/default.yaml](config/default.yaml) |
| Voice settings | [config/voice.yaml](config/voice.yaml) |
| LLM providers/models | [config/providers.yaml](config/providers.yaml) |
| The build plan | [docs/11-playbook.md](docs/11-playbook.md) |

---

## Progress

| Milestone | Status |
|---|---|
| M0 Skeleton | done |
| M1 Cloud brain (3 providers, failover) | done |
| M2 Memory + encrypted Drive backup | done |
| M3 Voice (wake word, barge-in) | done |
| M4 Hands + permission broker | done |
| M5 Web + scheduling | next |
