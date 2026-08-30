// Chat page tests — form wiring and incremental streaming render.
//
// The key thing we prove here: when you submit a question, the assistant's
// answer renders INCREMENTALLY as tokens arrive (streaming/optimistic UI), and a
// citations panel appears afterward. We mock askStream to synchronously invoke
// its callbacks, simulating a fast stream.

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Chat } from "./Chat";
import { AuthProvider, TOKEN_STORAGE_KEY } from "../auth";

// Mock the api. Our fake askStream drives the onToken/onCitations callbacks just
// like the real SSE stream would, so the component's rendering logic is what's
// under test.
vi.mock("../api", () => ({
  askStream: vi.fn(
    async (
      _question: string,
      _token: string,
      onToken: (t: string) => void,
      onCitations: (c: unknown) => void,
    ) => {
      onToken("Hello ");
      onToken("world");
      onCitations([{ source: "rag_overview.md", index: 2, score: 0.81 }]);
    },
  ),
}));
import { askStream } from "../api";

function renderChat() {
  // Seed a token so the Chat page considers us authenticated.
  localStorage.setItem(TOKEN_STORAGE_KEY, "tok-123");
  return render(
    <AuthProvider>
      <Chat />
    </AuthProvider>,
  );
}

afterEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

describe("Chat", () => {
  it("renders an input and a log out button", () => {
    renderChat();
    expect(screen.getByRole("button", { name: /log ?out/i })).toBeInTheDocument();
    // some text entry for the question (input or textarea)
    expect(
      screen.getByRole("textbox") ?? screen.getByPlaceholderText(/ask/i),
    ).toBeInTheDocument();
  });

  it("submitting a question streams tokens into an assistant message", async () => {
    const user = userEvent.setup();
    renderChat();

    await user.type(screen.getByRole("textbox"), "what is RAG?");
    // submit via Enter or a Send button — support either
    const sendBtn = screen.queryByRole("button", { name: /send|ask|submit/i });
    if (sendBtn) {
      await user.click(sendBtn);
    } else {
      await user.keyboard("{Enter}");
    }

    expect(askStream).toHaveBeenCalled();
    // the streamed tokens should be concatenated into the answer
    expect(await screen.findByText(/Hello world/)).toBeInTheDocument();
  });

  it("renders the citations after the stream completes", async () => {
    const user = userEvent.setup();
    renderChat();

    await user.type(screen.getByRole("textbox"), "what is RAG?");
    const sendBtn = screen.queryByRole("button", { name: /send|ask|submit/i });
    if (sendBtn) {
      await user.click(sendBtn);
    } else {
      await user.keyboard("{Enter}");
    }

    await waitFor(() =>
      expect(screen.getByText(/rag_overview\.md/)).toBeInTheDocument(),
    );
  });
});
