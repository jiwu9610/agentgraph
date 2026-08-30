// Tests for the chat extensions: automatic logout on 401, stream cancellation
// via the Stop button, clickable inline citations with expandable snippets,
// per-message cost display, markdown answers, error/disconnected banners, and
// clear-chat / logout localStorage hygiene.
//
// The api module is mocked so no network is involved; the mock's askStream
// drives the same callbacks the real SSE client would.

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { AuthProvider, TOKEN_STORAGE_KEY } from "./auth";
import { Chat, SESSION_STORAGE_KEY } from "./components/Chat";
import type { Citation } from "./types";

vi.mock("./api", () => ({
  API_BASE: "http://test",
  login: vi.fn(),
  health: vi.fn(),
  ask: vi.fn(),
  askStream: vi.fn(),
}));
import { askStream, health, login as apiLogin } from "./api";

const mockAskStream = vi.mocked(askStream);
const mockHealth = vi.mocked(health);
const mockLogin = vi.mocked(apiLogin);

/** Render the chat screen with a seeded token so it considers us logged in. */
function renderChat() {
  localStorage.setItem(TOKEN_STORAGE_KEY, "tok-123");
  return render(
    <AuthProvider>
      <Chat />
    </AuthProvider>,
  );
}

/** Type a question and submit it. */
async function send(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(screen.getByRole("textbox"), text);
  await user.click(screen.getByRole("button", { name: /send/i }));
}

afterEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
});

describe("401 auto-logout", () => {
  it("logs the user out when a protected request returns 401", async () => {
    const user = userEvent.setup();
    localStorage.setItem(TOKEN_STORAGE_KEY, "tok-expired");
    mockAskStream.mockRejectedValue(
      Object.assign(new Error("Stream request failed (401)"), { status: 401 }),
    );
    render(
      <AuthProvider>
        <App />
      </AuthProvider>,
    );

    await send(user, "hello?");

    // Back on the login form, token gone from storage.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /log in/i }),
      ).toBeInTheDocument(),
    );
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("does not log out on a non-auth server error", async () => {
    const user = userEvent.setup();
    mockAskStream.mockRejectedValue(
      Object.assign(new Error("Stream request failed (500)"), { status: 500 }),
    );
    renderChat();

    await send(user, "boom");

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("tok-123");
  });

  it("shows an error on the login form itself for bad credentials", async () => {
    const user = userEvent.setup();
    mockLogin.mockRejectedValue(
      Object.assign(new Error("Login failed (401)"), { status: 401 }),
    );
    render(
      <AuthProvider>
        <App />
      </AuthProvider>,
    );

    await user.type(screen.getByLabelText(/username/i), "ada");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /log in/i }),
    ).toBeInTheDocument();
  });
});

describe("stop button", () => {
  it("aborts the stream and shows no error banner", async () => {
    const user = userEvent.setup();
    mockAskStream.mockImplementation((_q, _t, onToken, _onCitations, opts) => {
      onToken("partial ");
      return new Promise<void>((_resolve, reject) => {
        opts?.signal?.addEventListener("abort", () =>
          reject(new DOMException("The operation was aborted.", "AbortError")),
        );
      });
    });
    renderChat();

    await send(user, "long question");

    const stop = await screen.findByRole("button", { name: /stop/i });
    await user.click(stop);

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /stop/i }),
      ).not.toBeInTheDocument(),
    );
    // An abort is a user action, not a failure.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // The partial answer stays in the transcript.
    expect(screen.getByText(/partial/)).toBeInTheDocument();
  });

  it("is not shown when nothing is streaming", () => {
    renderChat();
    expect(
      screen.queryByRole("button", { name: /stop/i }),
    ).not.toBeInTheDocument();
  });
});

describe("clickable citations", () => {
  it("toggles the snippet per occurrence, independently", async () => {
    const user = userEvent.setup();
    const citations: Citation[] = [
      { source: "a.md", index: 0, score: 0.9, snippet: "Alpha snippet text." },
    ];
    mockAskStream.mockImplementation(async (_q, _t, onToken, onCitations) => {
      onToken("See (a.md #0) and again (a.md #0).");
      onCitations(citations);
    });
    renderChat();

    await send(user, "cite?");

    const links = await screen.findAllByRole("button", {
      name: /\(a\.md #0\)/,
    });
    expect(links).toHaveLength(2);
    expect(screen.queryByText("Alpha snippet text.")).not.toBeInTheDocument();

    // Expand the first occurrence only.
    await user.click(links[0]);
    expect(screen.getAllByText("Alpha snippet text.")).toHaveLength(1);

    // The second occurrence toggles independently.
    await user.click(links[1]);
    expect(screen.getAllByText("Alpha snippet text.")).toHaveLength(2);

    // Clicking again collapses each one on its own.
    await user.click(links[0]);
    expect(screen.getAllByText("Alpha snippet text.")).toHaveLength(1);
    await user.click(links[1]);
    expect(screen.queryByText("Alpha snippet text.")).not.toBeInTheDocument();
  });

  it("keeps citations attached to their own message", async () => {
    const user = userEvent.setup();
    mockAskStream
      .mockImplementationOnce(async (_q, _t, onToken, onCitations) => {
        onToken("First (a.md #0).");
        onCitations([
          { source: "a.md", index: 0, score: 0.9, snippet: "Alpha." },
        ]);
      })
      .mockImplementationOnce(async (_q, _t, onToken, onCitations) => {
        onToken("Second (b.md #1).");
        onCitations([
          { source: "b.md", index: 1, score: 0.8, snippet: "Beta." },
        ]);
      });
    renderChat();

    await send(user, "one");
    await screen.findByRole("button", { name: /\(a\.md #0\)/ });
    await send(user, "two");
    await screen.findByRole("button", { name: /\(b\.md #1\)/ });

    // The first message's citation is still there and still expandable.
    await user.click(screen.getByRole("button", { name: /\(a\.md #0\)/ }));
    expect(screen.getByText("Alpha.")).toBeInTheDocument();
  });
});

describe("markdown rendering", () => {
  it("renders assistant answers as markdown", async () => {
    const user = userEvent.setup();
    mockAskStream.mockImplementation(async (_q, _t, onToken) => {
      onToken("This is **important** advice.");
    });
    renderChat();

    await send(user, "md?");

    const emphasized = await screen.findByText("important");
    expect(emphasized.tagName).toBe("STRONG");
  });
});

describe("per-message cost", () => {
  it("shows the request cost next to the assistant message", async () => {
    const user = userEvent.setup();
    mockAskStream.mockImplementation(async (_q, _t, onToken, _c, opts) => {
      onToken("Answer.");
      opts?.onCost?.(0.00123);
    });
    renderChat();

    await send(user, "cost?");

    expect(await screen.findByText(/\$0\.001230/)).toBeInTheDocument();
  });
});

describe("disconnected banner", () => {
  it("appears after a network failure, polls /health, and clears on recovery", async () => {
    const user = userEvent.setup();
    mockAskStream.mockRejectedValue(new TypeError("fetch failed"));
    let healthy = false;
    mockHealth.mockImplementation(async () => {
      if (!healthy) {
        throw new TypeError("fetch failed");
      }
      return { status: "ok", chunks: 3 };
    });
    localStorage.setItem(TOKEN_STORAGE_KEY, "tok-123");
    render(
      <AuthProvider>
        <Chat healthPollIntervalMs={20} />
      </AuthProvider>,
    );

    await send(user, "down?");

    // The failed send raises both banners; the disconnected one polls /health.
    expect(await screen.findByRole("status")).toBeInTheDocument();
    await waitFor(() => expect(mockHealth).toHaveBeenCalled());
    // Still down: the banner stays.
    expect(screen.getByRole("status")).toBeInTheDocument();

    // Recovery: the next poll clears the banner.
    healthy = true;
    await waitFor(() =>
      expect(screen.queryByRole("status")).not.toBeInTheDocument(),
    );
  });
});

describe("clear chat / logout hygiene", () => {
  it("clear chat resets messages, session id, and persisted session", async () => {
    const user = userEvent.setup();
    mockAskStream.mockImplementation(async (_q, _t, onToken, _c, opts) => {
      opts?.onSession?.("sess-1");
      onToken("Remembered answer.");
    });
    renderChat();

    await send(user, "hello");
    await screen.findByText(/Remembered answer/);
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBe("sess-1");

    await user.click(screen.getByRole("button", { name: /clear/i }));

    expect(screen.queryByText(/Remembered answer/)).not.toBeInTheDocument();
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    // Clearing the chat does not log the user out.
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("tok-123");

    // The next question starts a fresh session.
    mockAskStream.mockClear();
    await send(user, "fresh");
    const opts = mockAskStream.mock.calls[0][4];
    expect(opts?.sessionId ?? null).toBeNull();
  });

  it("logout clears the persisted session along with the token", async () => {
    const user = userEvent.setup();
    mockAskStream.mockImplementation(async (_q, _t, onToken, _c, opts) => {
      opts?.onSession?.("sess-9");
      onToken("hi");
    });
    localStorage.setItem(TOKEN_STORAGE_KEY, "tok-123");
    render(
      <AuthProvider>
        <App />
      </AuthProvider>,
    );

    await send(user, "hello");
    await waitFor(() =>
      expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBe("sess-9"),
    );

    await user.click(screen.getByRole("button", { name: /log out/i }));

    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    expect(
      screen.getByRole("button", { name: /log in/i }),
    ).toBeInTheDocument();
  });
});
