// Types mirroring the backend's JSON contract (docsbot/api/schemas.py). Keep
// the two in sync: a field renamed on the server must be renamed here too.

/** One source citation attached to an answer. */
export interface Citation {
  /** The document the chunk came from, e.g. "embeddings.md". */
  source: string;
  /** Which chunk within that document (0-based). */
  index: number;
  /** Relevance score; higher means more relevant. */
  score: number;
  /** Text of the cited chunk; shown when a citation is expanded in the UI. */
  snippet?: string;
}

/** Response from POST /ask (the non-streaming endpoint). */
export interface AskResponse {
  answer: string;
  citations: Citation[];
}

/** Response from POST /auth/login. */
export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
}

/** Response from GET /health. */
export interface HealthResponse {
  status: string;
  chunks: number;
}

/**
 * Events the server emits over the SSE streaming endpoints, discriminated on
 * `type`. `token` events carry answer text as it is generated; `citations`
 * arrives once when retrieval results are final; `session` identifies the
 * conversation on the session-aware endpoint; `cost` reports the request's
 * model spend in dollars after the answer completes; `done` closes the stream.
 */
export type StreamEvent =
  | { type: "token"; text: string }
  | { type: "citations"; citations: Citation[] }
  | { type: "session"; session_id: string }
  | { type: "cost"; cost: number }
  | { type: "done" };
