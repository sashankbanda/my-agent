import { useEffect, useRef, useState } from "react";
import MemoryPanel from "./MemoryPanel";
import { KernelClient, TurnEvent } from "./ws";

type Message = { role: "user" | "assistant"; text: string; pending?: boolean };

const client = new KernelClient();

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showMemory, setShowMemory] = useState(false);
  const sessionRef = useRef<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function applyEvent(event: TurnEvent) {
    switch (event.kind) {
      case "session":
        sessionRef.current = event.sessionId;
        break;
      case "delta":
        setMessages((all) => {
          const last = all[all.length - 1];
          const updated = { ...last, text: last.text + event.text };
          return [...all.slice(0, -1), updated];
        });
        break;
      case "reset":
        // A provider failed mid-answer; the kernel restarted on another one.
        setMessages((all) => [...all.slice(0, -1), { role: "assistant", text: "", pending: true }]);
        break;
      case "done":
        setMessages((all) => {
          const last = all[all.length - 1];
          return [...all.slice(0, -1), { ...last, pending: false }];
        });
        break;
      case "error":
        setError(event.message);
        setMessages((all) => (all[all.length - 1]?.pending ? all.slice(0, -1) : all));
        break;
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setError(null);
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

  return (
    <div className="app">
      <header>
        <h1>MyAgent</h1>
        <button className="ghost" onClick={() => setShowMemory((open) => !open)}>
          Memory
        </button>
      </header>
      {showMemory && <MemoryPanel onClose={() => setShowMemory(false)} />}
      <main>
        {messages.length === 0 && <p className="empty">Say something to get started.</p>}
        {messages.map((message, index) => (
          <div key={index} className={`bubble ${message.role}`}>
            {message.text || (message.pending ? "…" : "")}
          </div>
        ))}
        {error && <div className="error">{error}</div>}
        <div ref={bottomRef} />
      </main>
      <footer>
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
    </div>
  );
}
