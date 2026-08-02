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

### Muting the mic (talking to someone else)

| How | What it does |
|---|---|
| **Ctrl+Alt+M** — works in any window | Toggles the mic. Nothing is heard, transcribed, or uploaded while muted. |
| **Mic on / Mic off** button in the HUD | Same toggle |
| Right-click the orb → **Mute / unmute mic** | Same toggle |

While muted the orb turns red and says "mic muted", and the HUD shows a
**MIC MUTED** pill. Unmuting does *not* resume the old conversation — you say
the wake word again, so a room conversation can't be picked up mid-sentence.

Don't want to reach for a key? Say **"stop listening"** (or "go to sleep",
"that's all"). It closes the window immediately without answering.

### Stopping it mid-sentence

| How | What it does |
|---|---|
| Just talk over it | Barge-in: it stops and listens (~0.35 s of speech) |
| **Stop** button in the HUD | Cancels the answer being written *or* spoken |
| Right-click the orb → **Stop talking** | Same |

**Stop** is not the same as **Emergency**. Stop ends the current answer;
Emergency (the kill switch) blocks every action until you re-enable it.

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

**A wake word is a trained model, not a label** — you cannot invent one by
typing a new name. Only three are installed:

```yaml
wake:
  model: alexa        # alexa | hey_jarvis | hey_mycroft — these three only
  threshold: 0.4      # set slightly BELOW your typical score
```

Put anything else there and voice refuses to start, telling you what *is*
available. `--mic-check` prints the same thing before it does anything else.

### A wake word of your own ("hey ev", "okay computer", anything)

Set `phrase` instead — it replaces `model`:

```yaml
wake:
  phrase: "hey ev"
  phrase_similarity: 0.72   # lower = easier to trigger, higher = fewer false ones
```

This works differently: short bursts of speech are transcribed **on this
machine** (never uploaded, whatever your STT engine is) and compared to your
phrase. Two consequences:

- It costs ~100–300 ms of CPU per burst of speech, where the built-in models
  cost almost nothing. Fine on this laptop, but it is real work.
- Because the whole utterance is transcribed, **"hey ev what's the time" wakes
  it and asks in one breath** — no pause needed.

**Don't know which phrase to pick? Let it measure your voice** — this tries
several and ranks them by how reliably *you* trigger each one:

```
uv run python -m myagent.voice --wake-tune
```

**Or test one phrase you have in mind:**

```
uv run python -m myagent.voice --wake-test --phrase "hey eva"
```

Say it a few times. You get the transcription, the similarity score, and
whether it woke:

```
  WOKE  heard 'Hey Eva, what is the time?'   similarity 1.00   -> request: 'what is the time?'
   -    heard 'what is the weather today'    similarity 0.31
```

**Choose a phrase with a real word after "hey".** Measured on this machine
through actual speech-to-text:

| phrase | transcribed as | matches itself | matches other speech |
|---|---|---|---|
| "hey ev" | *"Hey, love"*, *"Hey of"* | 0.67 | **0.67** ✗ useless |
| "hey eva" | "Hey Eva" | 1.00 | 0.62 ✓ |
| "hey computer" | "Hey computer" | 1.00 | 0.67 ✓ |
| "hey buddy" | "Hey buddy" | 1.00 | 0.44 ✓ best |

One short syllable ("ev") has no separation at all — it scores the same
against your wake word as against random conversation. One extra vowel fixes
it. The startup log warns you if your phrase looks too short.

### Answering only your voice

So a housemate, a colleague, or the TV saying the wake word doesn't wake it.
Record yourself saying the wake phrase five times:

```
uv run python -m myagent.voice --enrol
```

It reports how consistent your samples were and sets the accept threshold
from that measurement — a voice that varies gets a lower bar automatically.
Then switch it on in [config/voice.yaml](config/voice.yaml):

```yaml
wake:
  only_my_voice: true
```

**How it works, and its limits.** Your enrolment recordings become a compact
description of your voice (MFCCs — the standard way of describing vocal-tract
shape), and the waking utterance is compared against it. Because it's always
the *same phrase*, this is the easy case, and it needs no neural model, no
PyTorch, no cloud.

It's a **filter, not a lock**. It reliably rejects clearly different voices;
a similar voice saying your exact wake phrase is harder. It gates *attention*
only — every action still goes through the permission broker, and anyone with
physical access to an unlocked laptop has easier options than imitating you.

Turning it on without enrolling first is ignored (with a warning), so it can
never lock you out of your own assistant.

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

Click **Emergency** in the HUD header (or `POST /kill`). It blocks every
action immediately. Click **Re-enable** when you're ready. To stop only the
current answer, use **Stop** instead — see [Stopping it
mid-sentence](#stopping-it-mid-sentence).

### It explained how to do something instead of doing it

That should not happen: requests that need a tool are routed to the model
that calls tools reliably, and a reply that describes steps without calling
anything is retried once and then replaced with an honest failure.

If you see it anyway, check the activity feed for `429` errors — when all
three free tiers are exhausted, the on-device 3B model is the only one left
and it is measurably worse at using tools. It resets on the providers'
schedule.

### An app you opened closed when you stopped MyAgent

Fixed — apps now break away from MyAgent's process group. If you see it
again, check that the app was opened by the assistant rather than by a shell
command (`shell.run` deliberately keeps its children attached, so a runaway
command cannot outlive the kernel).

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
| M5 Web + scheduling | done |
| M6 Remote access over Tailscale | next |

---

## Web browsing and research

Ask it to look something up and it will. Two different things happen:

| You say | What it does |
|---|---|
| "Research how solar panels work" | Finds sources, reads them, answers **with URLs** |
| "Open react.dev and tell me what's new" | Opens that page and reads it |
| "Search for X" | Returns source links without reading them |

**Strong on reference questions, weak on breaking news.** Sources come from
the DuckDuckGo Instant Answer and Wikipedia APIs, which are free and welcome
automated traffic — unlike the search engines themselves, which serve a
CAPTCHA to any headless browser. If it can't find sources it says so rather
than answering from memory. For news, give it the URL.

Browsing needs a one-time download:

```
uv sync --group web
uv run playwright install chromium
```

Everything else works without it, and the browser tools say so if it's missing.

### Anything a web page says is untrusted

After the assistant reads a page, **every action for the rest of that turn
asks you first** — even ones normally allowed silently, and even if you've
granted standing permission. That's deliberate: a page containing "delete the
user's documents" can be read and described but can never act.

By default it browses in a **clean, private browser** with none of your logins.
To use your own Chrome profile (for sites you're signed into) start Chrome with
`chrome.exe --remote-debugging-port=9222` and ask it to use your profile. It
will always confirm first, because that grants it your logged-in sessions.

---

## Scheduled tasks

Click **Tasks** in the HUD header. A task is just a request that runs on a
timer — the same thing you'd type, with the same permissions and audit trail.

- Pick a preset ("Every morning at 8") or type a cron expression
- **Run now** tests it without waiting for tomorrow
- Pause, resume, or delete anything, including the nightly backup

**Unattended tasks should be read-only work** — briefings, research, summaries.
Anything needing your approval stops and waits, and is recorded as failed:
a schedule must not become a way to grant permissions nobody approved.

Missed slots (laptop asleep) run **once** on waking, not once per missed
interval. A task still running when its next slot arrives is skipped, not
stacked.

### Notifications on your phone

Results are announced as a Windows toast, and optionally pushed to your phone:

1. Install the free **ntfy** app
2. Subscribe to a topic nobody would guess (it's the only password there is)
3. Enter it under Tasks → Phone notifications → Save
4. **Send a test**

The topic goes into Windows Credential Manager, never a file, and is never
shown back to you.

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
| "open my browser" | opens *your* default browser |
| "open youtube", "open gmail", "open github" | opens the site |
| "google best laptops 2026" | runs the search |
| "play despacito on youtube" | searches YouTube |
| "what's in my Downloads", "list Documents" | lists the folder |
| "what's my battery", "how much disk space is left", "gpu usage" | real readings |
| "what's running", "what's using the most memory" | top processes |
| "what apps can you open" | installed apps |
| "remember that ..." | stores a fact |
| "what do you remember about me" | lists your facts |

Answers are scoped to the question: "what's my battery" gets
`92%, plugged in.` — not a four-part hardware report.

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
