// App shell. The auth state is the only routing decision: no token shows the
// login form, a token shows the chat screen.

import { useAuth } from "./auth";
import { Login } from "./components/Login";
import { Chat } from "./components/Chat";

export function App() {
  const { token } = useAuth();
  return token ? <Chat /> : <Login />;
}
