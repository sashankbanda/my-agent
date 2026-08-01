# MyAgent — Runbook (how to run it)

Quick reference for daily use. Every command runs from the project root:
`C:\02_DEV\01_ACTIVE\my-agent`

---

## TL;DR — one command, one terminal

**Double-click `MyAgent.bat`**, or in a terminal:

```
uv run python -m myagent.start
```

That starts everything and prints what it started:

```
starting MyAgent...
  kernel   http://127.0.0.1:8765
  voice    starting (models load on first run)
  overlay  orb on screen (drag it; click opens the HUD)
  HUD      opened in your browser

ready. press Ctrl+C here to stop everything.
```

- **The HUD** (browser) shows the conversation, a live activity feed of every
  action, provider health and quota, memory counts, and an emergency stop.
- **The overlay orb** floats above other windows: grey = voice off, blue =
  ready, green pulse = hearing you, amber spin = thinking, blue pulse =
  speaking. Drag to move, click to open the HUD, right-click for a menu.
- **Ctrl+C** in that terminal stops everything. Even if the window is killed,
  Windows terminates the child processes (they run in a job object).

Options: `--no-voice` (text only), `--no-overlay`, `--no-browser`,
`--corner top-left|top-right|bottom-left|bottom-right`.

You no longer need to read terminal output — everything shows up in the HUD.

---

## Running parts by hand (debugging)

| What | Command | Wait for |
|---|---|---|
| kernel only | `uv run python -m myagent` | `Uvicorn running on http://127.0.0.1:8765` |
| voice only | `uv run python -m myagent.voice` | `voice_ready`, `kernel_connected` |
| overlay only | `uv run python -m myagent.overlay` | orb appears |

The kernel must be running before voice or overlay.

---

## Using voice

Say your **wake word** (check `wake.model` in
[config/voice.yaml](config/voice.yaml)), pause half a beat, then speak.

After it replies you have **30 seconds** to keep talking with no wake word —
and every exchange refreshes that window, so a real back-and-forth continues
indefinitely. Talking over it interrupts it (best with a headset).

Watch the orb to know whether it heard you.

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
| Start everything | `uv run python -m myagent.start` |
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
| HUD + overlay + launcher | done (brought forward from M8 polish) |
| M5 Web + scheduling | next |

---

## Three tiers: what runs where

Every turn takes the cheapest route that can do the job well:

| Tier | Handles | Cost | Speed |
|---|---|---|---|
| **Pattern** (no model) | "open chrome", "what's my battery", "remember that..." | free | 50–350 ms |
| **Local model** (Ollama, on this laptop) | easy questions, chit-chat, short facts | free | ~1.5–3 s |
| **Cloud** (Groq/Gemini/OpenRouter) | reasoning, code, planning, multi-step, tool chains | tokens | ~0.5–3 s |

The local model is `qwen2.5:3b` (1.9 GB, CPU-only). It also covers two cases
the cloud can't:

- **Secrets never leave.** A prompt containing an API key or password is
  routed to the local model *only* — previously it was refused outright.
- **When cloud quotas run out** (or you're offline), the local model keeps the
  assistant working instead of failing.

If the local model gives a weak answer ("I'm not sure", empty, repetitive),
the turn is automatically retried on the cloud — you never see the bad one.

Set it up (one time): `uv run python scripts/setup_local_model.py --bench`
Turn it off: `tools.local_tier: false` in [config/default.yaml](config/default.yaml)

The HUD's status bar shows the split: "7 free · 5 cloud".

---

## Free commands (no tokens spent)

Simple requests are answered **locally** — no model call, no tokens, ~50–350 ms.
The HUD's status bar shows the tally ("7 free · 0 model") and the activity feed
marks them "handled locally · 0 tokens".

Handled for free:

| Say | What happens |
|---|---|
| "hi", "thanks", "ok" | canned reply |
| "what time is it", "what's the date" | answered from the clock |
| "open chrome", "open Premiere Pro" | launches the app |
| "open youtube.com" | opens the browser |
| "what's in my Downloads", "list Documents" | lists the folder |
| "what's my battery", "check cpu", "disk space" | real system readings |
| "what's running" | top processes |
| "what apps can you open" | installed apps |
| "remember that ..." | stores a fact |
| "what do you remember about me" | lists your facts |

Anything with reasoning, multiple steps, or conjunctions ("open chrome **and**
search for X", "**why** is my battery draining") goes to the model as usual.
Destructive things (delete, run a command) always go to the model **and** ask
for confirmation — they are deliberately not fast-pathed.

Turn it off with `tools.fast_path: false` in [config/default.yaml](config/default.yaml).

---

## A note on free-tier quotas

The HUD's provider panel shows today's usage per model. Those counters live in
your database, so they reflect *this machine's* usage — the providers keep
their own counts. If every provider shows 429 errors in the activity feed,
you have genuinely used up today's free allowance; it resets on their
schedule (Groq per-minute, Gemini and OpenRouter daily).
