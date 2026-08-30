// API client tests — token attachment, SSE stream parsing, error mapping.
//
// We mock the global `fetch` so no real server is needed and the tests are
// deterministic. The point is to test the client logic (headers, error handling, SSE
// buffering/parsing), not the network.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ask, askStream, health, login, parseSSEFrame } from "./api";
import type { Citation } from "./types";

/** Build a minimal fake Response for the non-streaming JSON endpoints. */
function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  } as unknown as Response;
}

/**
 * Build a fake streaming Response whose body yields the given byte chunks via
 * getReader(). We split SSE frames across chunks ON PURPOSE so the test proves
 * the buffering handles frames that don't align with network reads.
 */
function streamResponse(chunks: string[]) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          read: async () => {
            if (i < chunks.length) {
              return { value: encoder.encode(chunks[i++]), done: false };
            }
            return { value: undefined, done: true };
          },
        };
      },
    },
  } as unknown as Response;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("login", () => {
  it("POSTs credentials as JSON and returns the access_token", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse({ access_token: "tok-123", token_type: "bearer" }),
    );

    const token = await login("ada", "hunter2");
    expect(token).toBe("tok-123");

    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toMatch(/\/auth\/login$/);
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ username: "ada", password: "hunter2" });
    expect(opts.headers["Content-Type"]).toBe("application/json");
  });

  it("throws on 401 (bad credentials)", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse({ detail: "bad creds" }, { ok: false, status: 401 }),
    );
    await expect(login("ada", "wrong")).rejects.toThrow();
  });
});

describe("health", () => {
  it("GETs /health with no auth and returns the body", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse({ status: "ok", chunks: 42 }),
    );
    const h = await health();
    expect(h).toEqual({ status: "ok", chunks: 42 });
    const [url] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toMatch(/\/health$/);
  });
});

describe("ask", () => {
  it("sends the bearer token and returns answer + citations", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse({
        answer: "Use RETRIEVAL_DOCUMENT.",
        citations: [{ source: "embeddings.md", index: 1, score: 0.9 }],
      }),
    );

    const res = await ask("which task type?", "tok-123");
    expect(res.answer).toBe("Use RETRIEVAL_DOCUMENT.");
    expect(res.citations).toHaveLength(1);

    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toMatch(/\/ask$/);
    expect(opts.headers["Authorization"]).toBe("Bearer tok-123");
    expect(JSON.parse(opts.body)).toEqual({ question: "which task type?" });
  });

  it("throws on 401 (missing/expired token)", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      jsonResponse({ detail: "unauthorized" }, { ok: false, status: 401 }),
    );
    await expect(ask("q", "expired")).rejects.toThrow();
  });
});

describe("parseSSEFrame", () => {
  it("parses a data: frame into a typed event", () => {
    expect(parseSSEFrame('data: {"type":"token","text":"hi"}')).toEqual({
      type: "token",
      text: "hi",
    });
  });
});

describe("askStream", () => {
  it("calls onToken per token, then onCitations, sending the auth header", async () => {
    // Note the deliberately awkward chunking: the first token frame is split
    // across two network reads. The buffer must stitch it back together.
    const frames = [
      'data: {"type":"to', // <-- partial frame
      'ken","text":"Hello "}\n\n',
      'data: {"type":"token","text":"world"}\n\n',
      'data: {"type":"citations","citations":[{"source":"a.md","index":0,"score":0.5}]}\n\n',
      'data: {"type":"done"}\n\n',
    ];
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(streamResponse(frames));

    const tokens: string[] = [];
    let gotCitations: Citation[] = [];

    await askStream(
      "hi",
      "tok-123",
      (t) => tokens.push(t),
      (c) => {
        gotCitations = c;
      },
    );

    expect(tokens.join("")).toBe("Hello world");
    expect(gotCitations).toEqual([{ source: "a.md", index: 0, score: 0.5 }]);

    const [url, opts] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toMatch(/\/ask\/stream$/);
    expect(opts.method).toBe("POST");
    expect(opts.headers["Authorization"]).toBe("Bearer tok-123");
  });
});
