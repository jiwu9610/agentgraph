// Login page tests — form rendering, credential submission, error display.
//
// We mock the api module so no network happens, and wrap the component in a real
// AuthProvider so login() can store the token. Then we drive the form like a
// user would (type, click) and assert the right things happen.

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Login } from "./Login";
import { AuthProvider, TOKEN_STORAGE_KEY } from "../auth";

// Replace the real api with a mock. We control what login() returns/throws.
vi.mock("../api", () => ({
  login: vi.fn(),
}));
import { login as apiLogin } from "../api";

function renderLogin() {
  return render(
    <AuthProvider>
      <Login />
    </AuthProvider>,
  );
}

afterEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

describe("Login", () => {
  it("renders username and password inputs and a submit button", () => {
    renderLogin();
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /log ?in/i })).toBeInTheDocument();
  });

  it("submitting calls api.login with the typed credentials and stores the token", async () => {
    (apiLogin as ReturnType<typeof vi.fn>).mockResolvedValueOnce("tok-abc");
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText(/username/i), "ada");
    await user.type(screen.getByLabelText(/password/i), "hunter2");
    await user.click(screen.getByRole("button", { name: /log ?in/i }));

    await waitFor(() => expect(apiLogin).toHaveBeenCalledWith("ada", "hunter2"));
    // a successful login should persist the token via the auth context
    await waitFor(() =>
      expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("tok-abc"),
    );
  });

  it("shows an error message when login fails (401)", async () => {
    (apiLogin as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("401"),
    );
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText(/username/i), "ada");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /log ?in/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
