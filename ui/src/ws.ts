// Kernel chat client over WebSocket.
//
// Wire protocol (one JSON object per message; see server/chat.py):
//   {session_id} | {delta} | {reset:true} | {done:true, model} | {error}

export type TurnEvent =
  | { kind: "session"; sessionId: string }
  | { kind: "delta"; text: string }
  | { kind: "reset" }
  | { kind: "done"; model: string }
  | { kind: "error"; message: string };

type Payload = {
  session_id?: string;
  delta?: string;
  reset?: boolean;
  done?: boolean;
  model?: string;
  error?: string;
};

function toEvent(payload: Payload): TurnEvent | null {
  if (payload.error !== undefined) return { kind: "error", message: payload.error };
  if (payload.reset) return { kind: "reset" };
  if (payload.delta !== undefined) return { kind: "delta", text: payload.delta };
  if (payload.done) return { kind: "done", model: payload.model ?? "" };
  if (payload.session_id !== undefined) return { kind: "session", sessionId: payload.session_id };
  return null;
}

export class KernelClient {
  private socket: WebSocket | null = null;

  private async connect(): Promise<WebSocket> {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) return this.socket;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}/ws`);
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve();
      socket.onerror = () => reject(new Error("cannot reach the kernel"));
    });
    this.socket = socket;
    return socket;
  }

  /** Send one message; invoke onEvent for each streamed event until done/error. */
  async send(
    message: string,
    sessionId: string | null,
    onEvent: (event: TurnEvent) => void,
  ): Promise<void> {
    const socket = await this.connect();
    await new Promise<void>((resolve) => {
      socket.onmessage = (raw: MessageEvent<string>) => {
        const event = toEvent(JSON.parse(raw.data) as Payload);
        if (!event) return;
        onEvent(event);
        if (event.kind === "done" || event.kind === "error") resolve();
      };
      socket.onclose = () => {
        onEvent({ kind: "error", message: "connection to the kernel was lost" });
        resolve();
      };
      socket.send(JSON.stringify({ message, session_id: sessionId }));
    });
  }
}
