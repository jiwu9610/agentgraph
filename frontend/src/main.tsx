// Infrastructure (filled in). This is the app's entry point — Vite loads it
// from index.html. It does the standard React 18 mount and, crucially, wraps
// the whole app in <AuthProvider> so that `useAuth()` works anywhere inside.
//
// You normally won't edit this file. It's complete so you can run `npm run dev`
// so the app renders the moment components mount.
import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { AuthProvider } from "./auth";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </React.StrictMode>,
);
