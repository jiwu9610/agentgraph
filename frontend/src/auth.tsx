// Authentication state for the app: a React context holding the bearer token,
// mirrored into localStorage so a login survives page reloads.
//
// Note: localStorage is readable by any script on the page; httpOnly cookies
// are the harder-to-steal alternative when XSS is a concern.

import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";

/** localStorage key the token is persisted under. */
export const TOKEN_STORAGE_KEY = "docsbot.token";

/** What `useAuth()` hands back to components. */
export interface AuthContextValue {
  /** The current bearer token, or null when logged out. */
  token: string | null;
  /** Store a token (in state and localStorage) — call after a successful login. */
  login: (token: string) => void;
  /** Clear the token (from state and localStorage). */
  logout: () => void;
}

// Default is undefined so useAuth can detect a missing <AuthProvider> and
// throw a clear error instead of silently handing back nulls.
const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/**
 * Holds the token in state, restoring any previously persisted token on mount,
 * and keeps localStorage in sync on login/logout.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() =>
    localStorage.getItem(TOKEN_STORAGE_KEY),
  );

  const login = useCallback((newToken: string) => {
    localStorage.setItem(TOKEN_STORAGE_KEY, newToken);
    setToken(newToken);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    setToken(null);
  }, []);

  return (
    <AuthContext.Provider value={{ token, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

/**
 * Read the auth context. Throws when called outside an <AuthProvider>, which
 * always indicates a wiring mistake rather than a legitimate logged-out state.
 */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error("useAuth must be used within an <AuthProvider>");
  }
  return ctx;
}
