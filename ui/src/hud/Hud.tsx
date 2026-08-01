import { useEffect, useMemo, useRef, useState } from "react";
import { ConfirmDialog, useSecuritySocket } from "../SecurityPanel";
import { KernelClient, TurnEvent } from "../ws";
import { KernelEvent, Status, describeEvent, useKernel } from "./useKernel";

// The HUD: everything the terminals used to tell you, in one screen.
// Left: conversation. Right: live state (orb, activity, providers, memory).

type Message = { role: "user" | "assistant"; text: string; pending?: boolean };

const client = new KernelClient();

const STATE_LABEL: Record<string, string> = {
  offline: "voice off",
  idle: "ready",
  waiting: "listening",
  listening: "hearing you",
  thinking: "thinking",
  speaking: "speaking",
};

function Orb({ state, online }: { state: string; online: boolean }) {
  const effective = online ? state : "offline";
  return (
    <div className={`orb orb-${effective}`}>
      <div className="orb-core" />
      <span className="orb-label">{STATE_LABEL[effective] ?? effective}</span>
    </div>
  );
}

function StatusBar({ status, online }: { status: Status | null; online: boolean }) {
  const uptime = status ? Math.floor(status.uptime_seconds / 60) : 0;
  return (
    <div className="status-bar">
      <span className={online ? "pill ok" : "pill bad"}>{online ? "kernel up" : "kernel down"}</span>
      {status && (
        <>
          <span className={status.voice.connected ? "pill ok" : "pill dim"}>
            voice {status.voice.connected ? status.voice.mode : "off"}
          </span>
          <span className="pill dim">wake: {status.voice.wake_word}</span>
          <span className="pill dim">stt: {status.voice.stt_engine}</span>
          <span className="pill dim">up {uptime}m</span>
          {status.kill_switch && <span className="pill bad">STOPPED</span>}
        </>
      )}
    </div>
  );
}

function Providers({ status }: { status: Status | null }) {
  if (!status) return null;
  return (
    <section className="card">
      <h3>Providers</h3>
      {status.providers.map((provider) => {
        const rpd = provider.usage.rpd;
        const percent = rpd ? Math.min(100, (rpd.used / rpd.limit) * 100) : 0;
        return (
          <div key={provider.key} className="provider">
            <div className="provider-head">
              <span className={provider.available ? "dot ok" : "dot bad"} />
              <span className="provider-name" title={provider.key}>
                {provider.key.split("/").slice(-1)[0]}
              </span>
              {rpd && (
                <span className="muted small">
                  {rpd.used}/{rpd.limit} today
                </span>
              )}
            </div>
            <div className="meter">
              <div className="meter-fill" style={{ width: `${percent}%` }} />
            </div>
          </div>
        );
      })}
    </section>
  );
}

function Activity({ events }: { events: KernelEvent[] }) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [events]);
  const rows = useMemo(
    () =>
      events
        .filter((event) => event.type !== "VoiceState")
        .slice(-80)
        .map((event) => ({ event, ...describeEvent(event) })),
    [events],
  );
  return (
    <section className="card grow">
      <h3>Activity</h3>
      <ul className="feed">
        {rows.length === 0 && <li className="muted">Nothing yet.</li>}
        {rows.map(({ event, text, kind }) => (
          <li key={event.id} className={`feed-${kind}`}>
            <code>{(event.ts ?? "").slice(11, 19)}</code> {text}
          </li>
        ))}
        <div ref={bottomRef} />
      </ul>
    </section>
  );
}

export default function Hud() {
  const { events, status, online } = useKernel();
  const { pending, decide } = useSecuritySocket();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const sessionRef = useRef<string | null>(null);
  const chatBottom = useRef<HTMLDivElement>(null);

  const voiceState = useMemo(() => {
    const last = [...events].reverse().find((event) => event.type === "VoiceState");
    return (last?.data.state as string) ?? status?.voice.state ?? "offline";
  }, [events, status]);

  useEffect(() => {
    chatBottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Spoken turns should appear in the transcript too.
  useEffect(() => {
    const latest = events[events.length - 1];
    if (!latest || latest.replay) return;
    if (latest.type === "UserSaid") {
      setMessages((all) => [...all, { role: "user", text: String(latest.data.text) }]);
    } else if (latest.type === "AssistantSaid") {
      setMessages((all) => [...all, { role: "assistant", text: String(latest.data.text) }]);
    }
  }, [events]);

  function applyEvent(event: TurnEvent) {
    switch (event.kind) {
      case "session":
        sessionRef.current = event.sessionId;
        break;
      case "delta":
        setMessages((all) => {
          const last = all[all.length - 1];
          return [...all.slice(0, -1), { ...last, text: last.text + event.text }];
        });
        break;
      case "reset":
        setMessages((all) => [...all.slice(0, -1), { role: "assistant", text: "", pending: true }]);
        break;
      case "done":
        setMessages((all) => {
          const last = all[all.length - 1];
          return [...all.slice(0, -1), { ...last, pending: false }];
        });
        break;
      case "error":
        setMessages((all) => [
          ...all.filter((message) => !message.pending),
          { role: "assistant", text: `⚠ ${event.message}` },
        ]);
        break;
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setMessages((all) => [
      ...all,
      { role: "user", text },
      { role: "assistant", text: "", pending: true },
    ]);
    await client.send(text, sessionRef.current, applyEvent);
    setBusy(false);
  }

  async function toggleKill() {
    await fetch(status?.kill_switch ? "/kill/release" : "/kill", { method: "POST" });
  }

  return (
    <div className="hud">
      {pending.length > 0 && <ConfirmDialog request={pending[0]} onDecide={decide} />}

      <header className="hud-head">
        <h1>MyAgent</h1>
        <StatusBar status={status} online={online} />
        <button className={status?.kill_switch ? "" : "danger"} onClick={() => void toggleKill()}>
          {status?.kill_switch ? "Re-enable" : "Stop"}
        </button>
      </header>

      <div className="hud-body">
        <main className="chat">
          <div className="messages">
            {messages.length === 0 && (
              <p className="empty">
                Talk to it, or type below. Everything it does appears on the right.
              </p>
            )}
            {messages.map((message, index) => (
              <div key={index} className={`bubble ${message.role}`}>
                {message.text || (message.pending ? "…" : "")}
              </div>
            ))}
            <div ref={chatBottom} />
          </div>
          <footer className="composer">
            <textarea
              value={input}
              placeholder="Message MyAgent"
              rows={1}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void send();
                }
              }}
            />
            <button onClick={() => void send()} disabled={busy || !input.trim()}>
              Send
            </button>
          </footer>
        </main>

        <aside className="side">
          <section className="card orb-card">
            <Orb state={voiceState} online={online} />
          </section>
          <Activity events={events} />
          <Providers status={status} />
          {status && (
            <section className="card">
              <h3>Memory &amp; backup</h3>
              <p className="muted small">
                {status.memory.facts} facts · {status.memory.messages} messages ·{" "}
                {status.memory.sessions} chats
              </p>
              <p className="muted small">
                backup: {status.vault.enabled ? status.vault.backend : "off"}
                {status.vault.last_snapshot
                  ? ` · last ${status.vault.last_snapshot.created_at.slice(0, 16).replace("T", " ")}`
                  : " · never"}
              </p>
              <p className="muted small">folders: {status.tools.roots.join(", ")}</p>
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}
