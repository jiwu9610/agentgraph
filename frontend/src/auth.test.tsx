// Auth context tests — login/logout state and localStorage persistence.
//
// We render a tiny throwaway component that uses `useAuth()` so we can drive the
// context from a test, then assert on both the rendered token AND what landed in
// localStorage. jsdom gives us a real localStorage, so persistence is testable.

import { afterEach, describe, expect, it } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { AuthProvider, TOKEN_STORAGE_KEY, useAuth } from "./auth";

// A probe component: shows the current token and exposes buttons to drive
// login/logout. Lets the test interact with the context without a real UI.
function Probe() {
  const { token, login, logout } = useAuth();
  return (
    <div>
      <span data-testid="token">{token ?? "none"}</span>
      <button onClick={() => login("tok-xyz")}>login</button>
      <button onClick={logout}>logout</button>
    </div>
  );
}

afterEach(() => {
  localStorage.clear();
});

describe("AuthProvider / useAuth", () => {
  it("starts logged out when localStorage is empty", () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(screen.getByTestId("token")).toHaveTextContent("none");
  });

  it("restores a token saved in localStorage on mount (persistence)", () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "saved-token");
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(screen.getByTestId("token")).toHaveTextContent("saved-token");
  });

  it("login() stores the token in state and localStorage", () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    act(() => {
      screen.getByText("login").click();
    });
    expect(screen.getByTestId("token")).toHaveTextContent("tok-xyz");
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("tok-xyz");
  });

  it("logout() clears the token from state and localStorage", () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "saved-token");
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    act(() => {
      screen.getByText("logout").click();
    });
    expect(screen.getByTestId("token")).toHaveTextContent("none");
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });
});
