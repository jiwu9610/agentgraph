// Login form. Controlled inputs; on submit it exchanges the credentials for a
// token via the API and stores it through the auth context (which flips the
// app over to the chat screen). A failed login surfaces a visible error
// instead of being swallowed.

import { useState, type FormEvent } from "react";
import { login as apiLogin } from "../api";
import { useAuth } from "../auth";

export function Login() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const token = await apiLogin(username, password);
      login(token);
    } catch {
      setError("Login failed. Check your username and password.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="card">
      <h1>DocsBot</h1>
      <form onSubmit={handleSubmit}>
        <div className="field">
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <button type="submit" disabled={submitting}>
          Log in
        </button>
        {error && <p role="alert">{error}</p>}
      </form>
    </div>
  );
}
