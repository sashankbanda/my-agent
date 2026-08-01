import { useEffect, useRef, useState } from "react";

// Confirmation requests and the audit trail. The socket stays open for the
// app's lifetime so a permission prompt can arrive at any moment.

export type ConfirmRequest = {
  id: string;
  tool: string;
  tier: string;
  summary: string;
  reason: string;
};

type AuditEntry = {
  id: number;
  ts: string;
  type: string;
  data: Record<string, unknown>;
};

/** Live confirmation channel; returns the pending request and a decide fn. */
export function useSecuritySocket() {
  const [pending, setPending] = useState<ConfirmRequest[]>([]);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}/security`);
    socketRef.current = socket;
    socket.onmessage = (event: MessageEvent<string>) => {
      const frame = JSON.parse(event.data);
      if (frame.type === "confirm_request") {
        setPending((all) => [...all.filter((r) => r.id !== frame.id), frame as ConfirmRequest]);
      } else if (frame.type === "confirm_closed") {
        setPending((all) => all.filter((r) => r.id !== frame.id));
      }
    };
    return () => socket.close();
  }, []);

  function decide(id: string, allowed: boolean, scope: "once" | "session" | "always") {
    socketRef.current?.send(JSON.stringify({ id, allowed, scope }));
    setPending((all) => all.filter((r) => r.id !== id));
  }

  return { pending, decide };
}

/** Modal shown while the assistant waits for permission. */
export function ConfirmDialog({
  request,
  onDecide,
}: {
  request: ConfirmRequest;
  onDecide: (id: string, allowed: boolean, scope: "once" | "session" | "always") => void;
}) {
  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h3>
          Permission needed <span className={`tier tier-${request.tier}`}>{request.tier}</span>
        </h3>
        <p className="summary">{request.summary}</p>
        <p className="muted">{request.reason}</p>
        <div className="modal-actions">
          <button onClick={() => onDecide(request.id, true, "once")}>Allow once</button>
          <button onClick={() => onDecide(request.id, true, "session")}>
            Allow for this chat
          </button>
          <button onClick={() => onDecide(request.id, true, "always")}>Always allow</button>
          <button className="danger" onClick={() => onDecide(request.id, false, "once")}>
            Deny
          </button>
        </div>
      </div>
    </div>
  );
}

/** Audit log + emergency stop. */
export function SecurityPanel({ onClose }: { onClose: () => void }) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [killed, setKilled] = useState(false);

  async function refresh() {
    setEntries(await (await fetch("/audit?limit=60")).json());
    setKilled((await (await fetch("/kill")).json()).engaged);
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function toggleKill() {
    await fetch(killed ? "/kill/release" : "/kill", { method: "POST" });
    void refresh();
  }

  function describe(entry: AuditEntry): string {
    const data = entry.data as Record<string, string | boolean | number>;
    switch (entry.type) {
      case "ToolCallRequested":
        return `requested ${data.tool}`;
      case "ToolCallCompleted":
        return `${data.tool} ${data.ok ? "ok" : `failed: ${data.error}`} (${data.ms}ms)`;
      case "PermissionDecided":
        return `${data.tool} [${data.tier}] -> ${data.decision} (${data.reason})`;
      case "ConfirmationResolved":
        return `${data.tool}: user ${data.allowed ? "allowed" : "denied"} (${data.scope})`;
      case "GrantAdded":
        return `granted ${data.tool} (${data.scope})`;
      case "GrantRevoked":
        return `revoked grant ${data.grant_id}`;
      case "BudgetExceeded":
        return `turn budget reached after ${data.steps} steps`;
      default:
        return entry.type;
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Activity &amp; permissions</h2>
        <div>
          <button className={killed ? "" : "danger"} onClick={() => void toggleKill()}>
            {killed ? "Re-enable actions" : "Emergency stop"}
          </button>
          <button onClick={onClose}>Close</button>
        </div>
      </div>
      {killed && <p className="muted">Emergency stop is engaged: all tool actions are denied.</p>}
      <ul className="audit">
        {entries.length === 0 && <li className="muted">No actions yet.</li>}
        {entries.map((entry) => (
          <li key={entry.id}>
            <code>{entry.ts.slice(11, 19)}</code> {describe(entry)}
          </li>
        ))}
      </ul>
    </div>
  );
}
