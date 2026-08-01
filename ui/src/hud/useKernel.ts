import { useEffect, useRef, useState } from "react";

// One hook for everything the HUD renders: the live event stream plus the
// periodic status snapshot. Both auto-recover, so the HUD survives a kernel
// restart without a page reload.

export type KernelEvent = {
  id: number;
  ts?: string;
  type: string;
  session_id?: string | null;
  data: Record<string, unknown>;
  replay?: boolean;
};

export type ProviderStatus = {
  key: string;
  provider: string;
  available: boolean;
  healthy: boolean;
  usage: Record<string, { used: number; limit: number }>;
  trains_on_data: boolean;
};

export type Status = {
  version: string;
  uptime_seconds: number;
  kill_switch: boolean;
  voice: {
    connected: boolean;
    state: string;
    mode: string;
    wake_word: string;
    stt_engine: string;
    tts_engine: string;
  };
  providers: ProviderStatus[];
  savings: {
    fast_path_today: number;
    local_model_today: number;
    free_today: number;
    cloud_today: number;
  };
  memory: { sessions: number; messages: number; facts: number };
  vault: { enabled: boolean; backend: string; last_snapshot: { created_at: string } | null };
  tools: { roots: string[] };
  ui_clients: number;
};

const MAX_EVENTS = 300;

export function useKernel() {
  const [events, setEvents] = useState<KernelEvent[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [online, setOnline] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);

  // Live events
  useEffect(() => {
    let closed = false;
    let retry: number | undefined;

    function connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${scheme}://${location.host}/events`);
      socketRef.current = socket;
      socket.onopen = () => setOnline(true);
      socket.onmessage = (message: MessageEvent<string>) => {
        const event = JSON.parse(message.data) as KernelEvent;
        setEvents((all) => [...all, event].slice(-MAX_EVENTS));
      };
      socket.onclose = () => {
        setOnline(false);
        if (!closed) retry = window.setTimeout(connect, 1500);
      };
      socket.onerror = () => socket.close();
    }
    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socketRef.current?.close();
    };
  }, []);

  // Status snapshot: cheap, and some things are states rather than events.
  useEffect(() => {
    let stop = false;
    async function poll() {
      try {
        const response = await fetch("/status");
        if (response.ok) setStatus(await response.json());
      } catch {
        /* kernel down; the events socket reports it */
      }
      if (!stop) window.setTimeout(poll, 3000);
    }
    void poll();
    return () => {
      stop = true;
    };
  }, []);

  return { events, status, online };
}

/** Human-readable one-liner for an event, used by the activity feed. */
export function describeEvent(event: KernelEvent): { text: string; kind: string } {
  const data = event.data as Record<string, never>;
  const tool = data.tool as unknown as string | undefined;
  switch (event.type) {
    case "UserSaid":
      return { text: `You: ${data.text}`, kind: "user" };
    case "AssistantSaid":
      return { text: `Assistant: ${data.text}`, kind: "assistant" };
    case "ToolCallRequested":
      return { text: `${tool} requested`, kind: "tool" };
    case "ToolCallCompleted":
      return {
        text: data.ok ? `${tool} done (${data.ms}ms)` : `${tool} failed: ${data.error}`,
        kind: data.ok ? "tool-ok" : "tool-fail",
      };
    case "PermissionDecided":
      return { text: `${tool} [${data.tier}] → ${data.decision}: ${data.reason}`, kind: "perm" };
    case "ConfirmationResolved":
      return { text: `${tool}: you ${data.allowed ? "allowed" : "denied"} it`, kind: "perm" };
    case "InferenceRouted":
      return { text: `thinking via ${data.model}`, kind: "infer" };
    case "FastPathHandled":
      return {
        text: `handled locally: ${data.intent}${data.tool ? ` → ${data.tool}` : ""} · 0 tokens`,
        kind: "local",
      };
    case "ProviderDegraded":
      return { text: `${data.provider} failed over: ${data.error}`, kind: "warn" };
    case "QuotaExhausted":
      return { text: `quota exhausted for ${data.task}`, kind: "warn" };
    case "MemoryWritten":
      return { text: `remembered something (#${data.id})`, kind: "memory" };
    case "MemoryForgotten":
      return { text: `forgot #${data.id}`, kind: "memory" };
    case "VaultSnapshotCreated":
      return { text: `backup uploaded (${data.size} bytes)`, kind: "vault" };
    case "TurnInterrupted":
      return { text: "you interrupted", kind: "warn" };
    case "BudgetExceeded":
      return { text: `step budget reached (${data.steps})`, kind: "warn" };
    case "KillSwitchEngaged":
      return { text: "EMERGENCY STOP engaged", kind: "danger" };
    case "KillSwitchReleased":
      return { text: "actions re-enabled", kind: "ok" };
    case "VoiceConnected":
      return { text: "voice connected", kind: "ok" };
    case "VoiceDisconnected":
      return { text: "voice disconnected", kind: "warn" };
    case "AppStarted":
      return { text: `kernel started (v${data.version})`, kind: "ok" };
    case "GrantAdded":
      return { text: `granted ${tool} (${data.scope})`, kind: "perm" };
    default:
      return { text: event.type, kind: "misc" };
  }
}
