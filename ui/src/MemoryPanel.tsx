import { useEffect, useState } from "react";

type Fact = {
  id: number;
  type: string;
  content: string;
  privacy_class: string;
  created_at: string;
};

type VaultStatus = {
  enabled: boolean;
  backend: string;
  last_snapshot: { blob_name: string; created_at: string } | null;
  manifest_chain_ok: boolean;
};

export default function MemoryPanel({ onClose }: { onClose: () => void }) {
  const [facts, setFacts] = useState<Fact[]>([]);
  const [draft, setDraft] = useState("");
  const [vault, setVault] = useState<VaultStatus | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<string | null>(null);

  async function refresh() {
    setFacts(await (await fetch("/memory")).json());
    setVault(await (await fetch("/vault/status")).json());
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function addFact() {
    const content = draft.trim();
    if (!content) return;
    setDraft("");
    await fetch("/memory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    void refresh();
  }

  async function forget(id: number) {
    await fetch("/memory/forget", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    void refresh();
  }

  async function backupNow() {
    setNotice("backing up…");
    const response = await fetch("/vault/backup", { method: "POST" });
    if (!response.ok) {
      const body = await response.json();
      setNotice(`backup unavailable: ${body.detail}`);
      return;
    }
    const entry = await response.json();
    setNotice(`backed up: ${entry.blob_name}`);
    if (entry.recovery_string) setRecovery(entry.recovery_string);
    void refresh();
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Memory</h2>
        <button onClick={onClose}>Close</button>
      </div>

      <div className="panel-add">
        <input
          value={draft}
          placeholder="Something to remember about you…"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && void addFact()}
        />
        <button onClick={() => void addFact()} disabled={!draft.trim()}>
          Remember
        </button>
      </div>

      <ul className="facts">
        {facts.length === 0 && <li className="muted">Nothing remembered yet.</li>}
        {facts.map((fact) => (
          <li key={fact.id}>
            <span>
              {fact.content}
              {fact.privacy_class === "local_only" && <em className="tag">local-only</em>}
            </span>
            <button title="Forget permanently" onClick={() => void forget(fact.id)}>
              ✕
            </button>
          </li>
        ))}
      </ul>

      <div className="vault">
        <h3>Backup</h3>
        {vault && (
          <p className="muted">
            {vault.enabled
              ? `${vault.backend} · last: ${vault.last_snapshot?.blob_name ?? "never"} · chain ${
                  vault.manifest_chain_ok ? "ok" : "BROKEN"
                }`
              : "vault disabled (see config/default.yaml)"}
          </p>
        )}
        <button onClick={() => void backupNow()}>Back up now</button>
        {notice && <p className="muted">{notice}</p>}
        {recovery && (
          <div className="recovery">
            <strong>Your recovery string — shown once. Store it OFF this machine:</strong>
            <code>{recovery}</code>
          </div>
        )}
      </div>
    </div>
  );
}
