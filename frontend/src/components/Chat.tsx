// Chat screen. Submitting a question appends the user's message plus an empty
// assistant message, then streams the answer into that assistant message token
// by token; assistant messages render as markdown with clickable citations,
// their retrieval sources, and the request's cost.
//
// The streaming callbacks fire many times per request, so every state update
// they make uses the updater form of setState — building on a captured
// `messages` value would be a stale closure and drop tokens.
//
// Failure handling: a 401 means the token is expired or revoked, so the user
// is logged out; an abort is a user action, not an error; a network-level
// failure (no HTTP status) additionally flips into a "disconnected" state
// that polls /health until the backend answers again.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { askStream, health } from "../api";
import { useAuth } from "../auth";
import type { Citation } from "../types";
import { AnswerText } from "./AnswerText";

/** localStorage key the conversation session id is persisted under. */
export const SESSION_STORAGE_KEY = "docsbot.session";

/** How often the disconnected banner re-checks /health. */
export const HEALTH_POLL_INTERVAL_MS = 5000;

/** One chat message. */
export interface Message {
  role: "user" | "assistant";
  text: string;
  /** Retrieval citations for an assistant message, kept with the message. */
  citations?: Citation[];
  /** Model cost of the request that produced this answer, in dollars. */
  cost?: number;
}

/** HTTP status carried by an ApiError-shaped error, if any. */
function errorStatus(err: unknown): number | undefined {
  if (typeof err === "object" && err !== null && "status" in err) {
    const status = (err as { status: unknown }).status;
    if (typeof status === "number") {
      return status;
    }
  }
  return undefined;
}

/** True for the AbortError a cancelled fetch/read rejects with. */
function isAbort(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    "name" in err &&
    (err as { name: unknown }).name === "AbortError"
  );
}

export function Chat({
  healthPollIntervalMs = HEALTH_POLL_INTERVAL_MS,
}: {
  /** Interval between /health probes while disconnected. */
  healthPollIntervalMs?: number;
} = {}) {
  const { token, logout } = useAuth();
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [disconnected, setDisconnected] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(SESSION_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const abortRef = useRef<AbortController | null>(null);

  // Patch the trailing assistant message; a no-op if the transcript was
  // cleared while a stream was still delivering events.
  const patchLastAssistant = useCallback(
    (patch: (msg: Message) => Message) => {
      setMessages((prev) => {
        const last = prev.length - 1;
        if (last < 0 || prev[last].role !== "assistant") {
          return prev;
        }
        const next = [...prev];
        next[last] = patch(next[last]);
        return next;
      });
    },
    [],
  );

  const resetConversation = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setError(null);
    setSessionId(null);
    try {
      localStorage.removeItem(SESSION_STORAGE_KEY);
    } catch {
      // localStorage unavailable; nothing was persisted.
    }
  }, []);

  function handleLogout() {
    resetConversation();
    logout();
  }

  // While disconnected, poll /health until the backend answers, then clear
  // the banner. The interval is torn down on reconnect and on unmount.
  useEffect(() => {
    if (!disconnected) {
      return;
    }
    const id = setInterval(async () => {
      try {
        await health();
        setDisconnected(false);
      } catch {
        // Still unreachable; keep polling.
      }
    }, healthPollIntervalMs);
    return () => clearInterval(id);
  }, [disconnected, healthPollIntervalMs]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || !token || streaming) {
      return;
    }
    setQuestion("");
    setError(null);
    setMessages((prev) => [
      ...prev,
      { role: "user", text: q },
      { role: "assistant", text: "" },
    ]);
    setStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await askStream(
        q,
        token,
        (chunk) =>
          patchLastAssistant((m) => ({ ...m, text: m.text + chunk })),
        (cites) => patchLastAssistant((m) => ({ ...m, citations: cites })),
        {
          signal: controller.signal,
          sessionId,
          onCost: (cost) => patchLastAssistant((m) => ({ ...m, cost })),
          onSession: (id) => {
            setSessionId(id);
            try {
              localStorage.setItem(SESSION_STORAGE_KEY, id);
            } catch {
              // localStorage unavailable; the session lives only in memory.
            }
          },
        },
      );
      setDisconnected(false);
    } catch (err) {
      if (isAbort(err)) {
        // Stopped by the user: keep the partial answer, show no banner.
      } else if (errorStatus(err) === 401) {
        resetConversation();
        logout();
      } else {
        setError("The request failed. Please try again.");
        if (errorStatus(err) === undefined) {
          // No HTTP status means the backend never answered.
          setDisconnected(true);
        }
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  return (
    <div className="card">
      <header className="chat-header">
        <h1>DocsBot</h1>
        <div className="chat-actions">
          <button type="button" onClick={resetConversation}>
            Clear chat
          </button>
          <button type="button" onClick={handleLogout}>
            Log out
          </button>
        </div>
      </header>

      {error && (
        <div role="alert" className="banner banner-error">
          {error}
        </div>
      )}
      {disconnected && (
        <div role="status" className="banner banner-disconnected">
          Backend unreachable — retrying…
        </div>
      )}

      <ul className="messages">
        {messages.map((m, i) => (
          <li key={i} className={`message message-${m.role}`}>
            <span className="message-role">
              {m.role === "user" ? "You" : "DocsBot"}
            </span>
            {m.role === "assistant" ? (
              <div className="message-text">
                <AnswerText text={m.text} citations={m.citations ?? []} />
              </div>
            ) : (
              <span className="message-text">{m.text}</span>
            )}
            {m.cost !== undefined && (
              <span className="message-cost">${m.cost.toFixed(6)}</span>
            )}
            {m.role === "assistant" &&
              m.citations &&
              m.citations.length > 0 && (
                <section className="citations">
                  <h2>Sources</h2>
                  <ul>
                    {m.citations.map((c, j) => (
                      <li key={j}>
                        {c.source} #{c.index} (score {c.score.toFixed(2)})
                      </li>
                    ))}
                  </ul>
                </section>
              )}
          </li>
        ))}
      </ul>

      <form onSubmit={handleSubmit}>
        <input
          aria-label="Question"
          placeholder="Ask a question about the docs"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        {streaming && (
          <button type="button" onClick={() => abortRef.current?.abort()}>
            Stop
          </button>
        )}
        <button type="submit" disabled={streaming}>
          Send
        </button>
      </form>
    </div>
  );
}
