import { useCallback, useEffect, useState } from "react";

// The task dashboard: what runs on its own, when it last ran, and whether it
// worked. A scheduled task you cannot inspect is a task you cannot trust.

export type Schedule = {
  id: number;
  name: string;
  cron: string;
  task: string;
  enabled: boolean;
  next_run: string;
  last_run: string | null;
  last_status: string | null;
  last_error: string | null;
  running?: boolean;
};

// Cron is precise but nobody remembers the field order, so the common cases
// are offered by name and the raw expression stays available underneath.
const PRESETS: { label: string; cron: string }[] = [
  { label: "Every morning at 8", cron: "0 8 * * *" },
  { label: "Weekdays at 9", cron: "0 9 * * 1-5" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every Monday at 9", cron: "0 9 * * 1" },
  { label: "Nightly at 3am", cron: "0 3 * * *" },
];

function when(value: string | null): string {
  if (!value) return "never";
  return value.replace("T", " ").slice(0, 16);
}

function StatusDot({ item }: { item: Schedule }) {
  if (item.running) return <span className="dot warn" title="running now" />;
  if (!item.enabled) return <span className="dot" title="paused" />;
  if (item.last_status === "failed") return <span className="dot bad" title={item.last_error ?? "failed"} />;
  return <span className="dot ok" title={item.last_status ?? "never run"} />;
}

export default function Tasks({ onClose }: { onClose: () => void }) {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [name, setName] = useState("");
  const [task, setTask] = useState("");
  const [cron, setCron] = useState(PRESETS[0].cron);
  const [error, setError] = useState<string | null>(null);
  const [pushConfigured, setPushConfigured] = useState<boolean | null>(null);
  const [topic, setTopic] = useState("");

  const refresh = useCallback(async () => {
    const response = await fetch("/schedules");
    if (response.ok) setSchedules(await response.json());
  }, []);

  useEffect(() => {
    void refresh();
    void fetch("/notify/topic")
      .then((r) => r.json())
      .then((d) => setPushConfigured(d.configured))
      .catch(() => setPushConfigured(null));
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function create() {
    setError(null);
    const response = await fetch("/schedules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), cron: cron.trim(), task: task.trim() }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({ detail: "could not create that" }));
      setError(typeof detail.detail === "string" ? detail.detail : "could not create that");
      return;
    }
    setName("");
    setTask("");
    await refresh();
  }

  async function toggle(item: Schedule) {
    await fetch(`/schedules/${item.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !item.enabled }),
    });
    await refresh();
  }

  async function remove(item: Schedule) {
    await fetch(`/schedules/${item.id}`, { method: "DELETE" });
    await refresh();
  }

  async function runNow(item: Schedule) {
    setError(null);
    const response = await fetch(`/schedules/${item.id}/run`, { method: "POST" });
    if (!response.ok) setError("that task is already running");
    await refresh();
  }

  async function savePush() {
    if (!topic.trim()) return;
    await fetch("/notify/topic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic: topic.trim() }),
    });
    setTopic("");
    setPushConfigured(true);
  }

  async function testNotification() {
    await fetch("/notify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "MyAgent", body: "Notifications are working." }),
    });
  }

  return (
    <div className="panel-backdrop" onClick={onClose}>
      <div className="panel wide" onClick={(event) => event.stopPropagation()}>
        <header className="panel-head">
          <h2>Scheduled tasks</h2>
          <button onClick={onClose}>Close</button>
        </header>

        <section className="card">
          <h3>New task</h3>
          <div className="schedule-form">
            <input
              placeholder="Name, e.g. Morning briefing"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
            <select value={cron} onChange={(event) => setCron(event.target.value)}>
              {PRESETS.map((preset) => (
                <option key={preset.cron} value={preset.cron}>
                  {preset.label}
                </option>
              ))}
              {!PRESETS.some((preset) => preset.cron === cron) && (
                <option value={cron}>{cron}</option>
              )}
            </select>
            <input
              className="cron"
              placeholder="cron"
              value={cron}
              onChange={(event) => setCron(event.target.value)}
              title="Minute hour day month weekday"
            />
            <textarea
              rows={2}
              placeholder="What should it do? e.g. Summarise today's AI news with sources."
              value={task}
              onChange={(event) => setTask(event.target.value)}
            />
            <button disabled={!name.trim() || !task.trim()} onClick={() => void create()}>
              Add
            </button>
          </div>
          {error && <p className="error small">{error}</p>}
          <p className="muted small">
            Tasks run as if you had typed them. Anything needing your approval will stop and
            wait, so unattended tasks should be read-only work like briefings and research.
          </p>
        </section>

        <section className="card">
          <h3>Tasks</h3>
          {schedules.length === 0 && <p className="muted">Nothing scheduled yet.</p>}
          <ul className="schedule-list">
            {schedules.map((item) => (
              <li key={item.id} className={item.enabled ? "" : "paused"}>
                <div className="schedule-head">
                  <StatusDot item={item} />
                  <strong>{item.name}</strong>
                  <code>{item.cron}</code>
                  <span className="muted small">next {when(item.next_run)}</span>
                </div>
                <div className="muted small schedule-task">{item.task}</div>
                {item.last_run && (
                  <div className="muted small">
                    last {when(item.last_run)} · {item.last_status}
                    {item.last_error ? ` · ${item.last_error}` : ""}
                  </div>
                )}
                <div className="schedule-actions">
                  <button onClick={() => void runNow(item)} disabled={item.running}>
                    {item.running ? "Running…" : "Run now"}
                  </button>
                  <button onClick={() => void toggle(item)}>
                    {item.enabled ? "Pause" : "Resume"}
                  </button>
                  <button className="danger" onClick={() => void remove(item)}>
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>

        <section className="card">
          <h3>Phone notifications</h3>
          {pushConfigured ? (
            <p className="muted small">
              Phone push is on. Notifications also appear as Windows toasts.
            </p>
          ) : (
            <p className="muted small">
              Install the <strong>ntfy</strong> app, subscribe to a topic nobody else would
              guess, and enter it here. Anyone who knows the topic can read your
              notifications, so treat it like a password.
            </p>
          )}
          <div className="schedule-form">
            <input
              placeholder={pushConfigured ? "Change topic" : "ntfy topic"}
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
            />
            <button disabled={!topic.trim()} onClick={() => void savePush()}>
              Save
            </button>
            <button onClick={() => void testNotification()}>Send a test</button>
          </div>
        </section>
      </div>
    </div>
  );
}
