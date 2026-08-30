// HTTP client for the DocsBot backend. All network access goes through this
// module so request URLs, headers, error handling, and the streaming protocol
// live in exactly one place.
//
// The streaming endpoints are consumed with `fetch` + `response.body.getReader()`
// instead of `EventSource`: EventSource cannot attach custom headers, and the
// endpoints require an `Authorization` header.

import type {
  AskResponse,
  Citation,
  HealthResponse,
  StreamEvent,
} from "./types";

/**
 * Base URL of the backend, from the Vite env var `VITE_API_BASE` with a
 * localhost fallback for development.
 */
export const API_BASE: string =
  import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

/**
 * Error thrown for non-2xx responses, carrying the HTTP status so callers can
 * distinguish auth failures (401) from other errors.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Optional extras for a streaming request. */
export interface StreamOptions {
  /** Abort signal forwarded to fetch; aborting rejects with an AbortError. */
  signal?: AbortSignal;
  /**
   * Conversation session id. When this property is present (even as null) the
   * request goes to the session-aware `/chat/stream` endpoint: null starts a
   * new session and the server's `session` event supplies the id to send back
   * on later turns. When absent, the stateless `/ask/stream` endpoint is used.
   */
  sessionId?: string | null;
  /** Invoked when the `cost` event reports the request's model spend. */
  onCost?: (cost: number) => void;
  /** Invoked when the `session` event identifies the conversation. */
  onSession?: (sessionId: string) => void;
}

/**
 * Exchange a username + password for an access token via `POST /auth/login`.
 *
 * `fetch` only rejects on network failure, so non-2xx statuses (401 on bad
 * credentials) are turned into thrown ApiErrors here.
 *
 * @returns the access token string
 */
export async function login(
  username: string,
  password: string,
): Promise<string> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw new ApiError(`Login failed (${response.status})`, response.status);
  }
  const data = (await response.json()) as { access_token: string };
  return data.access_token;
}

/** Unauthenticated health check: `GET /health`. */
export async function health(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE}/health`);
  if (!response.ok) {
    throw new ApiError(
      `Health check failed (${response.status})`,
      response.status,
    );
  }
  return (await response.json()) as HealthResponse;
}

/**
 * Ask a question and receive the complete answer at once via `POST /ask`.
 *
 * Requires a bearer token; throws ApiError on any non-2xx response (401 means
 * the token is missing or expired).
 */
export async function ask(
  question: string,
  token: string,
): Promise<AskResponse> {
  const response = await fetch(`${API_BASE}/ask`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ question }),
  });
  if (!response.ok) {
    throw new ApiError(`Ask failed (${response.status})`, response.status);
  }
  return (await response.json()) as AskResponse;
}

/**
 * Ask a question and stream the answer (`text/event-stream`).
 *
 * Without `opts.sessionId` the request is a stateless `POST /ask/stream`; when
 * `opts.sessionId` is set (null starts a new session) it becomes a session-aware
 * `POST /chat/stream` carrying the id so the server can keep history.
 *
 * The server emits SSE frames (`data: <json>\n\n`): any number of `token`
 * events, one `citations` event, optionally `session` and `cost` events, then
 * `done`. Network reads do not align with frame boundaries, so decoded bytes
 * accumulate in a buffer that is split on the `"\n\n"` delimiter; only complete
 * frames are parsed and the trailing partial frame is kept for the next read.
 *
 * @param onToken     invoked once per `token` event with the token text
 * @param onCitations invoked once when the `citations` event arrives
 * @param opts        abort signal, session id, and cost/session callbacks
 */
export async function askStream(
  question: string,
  token: string,
  onToken: (text: string) => void,
  onCitations: (citations: Citation[]) => void,
  opts: StreamOptions = {},
): Promise<void> {
  const withSession = opts.sessionId !== undefined;
  const url = withSession
    ? `${API_BASE}/chat/stream`
    : `${API_BASE}/ask/stream`;
  const body = withSession
    ? { message: question, session_id: opts.sessionId }
    : { question };
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  if (!response.ok) {
    throw new ApiError(
      `Stream request failed (${response.status})`,
      response.status,
    );
  }

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const event = parseSSEFrame(frame);
      if (!event) {
        continue;
      }
      switch (event.type) {
        case "token":
          onToken(event.text);
          break;
        case "citations":
          onCitations(event.citations);
          break;
        case "session":
          opts.onSession?.(event.session_id);
          break;
        case "cost":
          opts.onCost?.(event.cost);
          break;
        case "done":
          return;
      }
    }
  }
}

/**
 * Parse one raw SSE frame such as `data: {"type":"token","text":"hi"}` into a
 * typed StreamEvent. Returns null for frames without a `data:` payload
 * (e.g. comment/keep-alive lines).
 */
export function parseSSEFrame(frame: string): StreamEvent | null {
  const line = frame.trim();
  if (!line.startsWith("data:")) {
    return null;
  }
  return JSON.parse(line.slice("data:".length).trim()) as StreamEvent;
}
