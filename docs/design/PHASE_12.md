# Phase 12 — The Web Frontend  ·  `frontend/`

You've built a real backend API (Phase 11): login, health, `/ask`, and a
streaming `/ask/stream`. Now you'll build the **face** of DocsBot — a React +
TypeScript single-page app that logs in, asks questions, and renders the answer
as it streams in, with citations.

Same rules as the rest of the repo: **the tests are the spec.** Everything in
`frontend/` is scaffolded for you EXCEPT the parts you're meant to learn. Those
ship as stubs that throw `Not implemented` (or render a red `TODO`) with a thick
comment above each explaining *what* and *why*. Replace the stub bodies until
`npm test` goes green.

---

## What you're learning

1. **Reading an SSE stream by hand** with `fetch()` + `response.body.getReader()`.
   The browser's built-in `EventSource` is the "normal" way to consume
   Server-Sent Events — but `EventSource` **cannot send an `Authorization`
   header**, and our stream endpoint is protected. So we use `fetch` (which can
   send any header) and parse the `data: …` frames ourselves. This is the
   headline skill of the phase.

2. **Auth tokens in the browser.** Login returns a bearer token; every protected
   request must carry `Authorization: Bearer <token>`. You'll store it in React
   Context (so any component can read it) and mirror it to `localStorage` (so a
   refresh doesn't log you out). You'll also learn the tradeoff: `localStorage`
   is readable by any script on the page — fine here, but production apps often
   prefer httpOnly cookies.

3. **Optimistic / incremental UI.** Don't block on the full answer. Append an
   empty assistant message immediately and grow it token-by-token as the stream
   arrives — the ChatGPT typewriter effect, built from raw bytes.

---

## Setup

```bash
cd frontend
npm install                 # one time
cp .env.example .env        # optional; defaults to http://localhost:8000
npm run dev                 # Vite dev server at http://localhost:5173
npm test                    # Vitest — RED now, GREEN when you're done
```

Run the backend (Phase 11) on port 8000 in another terminal so the app has
something to talk to. `npm test` needs **no** backend — every test mocks the
network.

> Expected on a fresh checkout: `npm test` **fails**. That is correct. The
> failing tests are your to-do list.

---

## What to implement (in recommended order)

Work bottom-up: the API client first (pure logic, easiest to test), then the
auth context, then the screens.

### 1. `src/api.ts` — the API client  ·  tested by `src/api.test.ts`
- `health()` — `GET /health`, no auth. **Do this first** to confirm your plumbing.
- `login(username, password)` — `POST /auth/login`; return `access_token`; throw on 401.
- `ask(question, token)` — `POST /ask` with the bearer header; return `{answer, citations}`.
- `askStream(question, token, onToken, onCitations)` — `POST /ask/stream`; read
  the body with `getReader()`, buffer bytes, split on `"\n\n"`, parse each
  `data:` frame, and fire the callbacks. **The hard one.**
- `parseSSEFrame(frame)` — helper: turn one `data: {…}` line into a typed event.

Gotchas the tests enforce:
- `fetch` does **not** throw on 4xx/5xx. Check `response.ok` and throw yourself.
- Network reads don't align with SSE frames. One read may hold half a frame or
  several frames. **Keep a string buffer**, split on `"\n\n"`, and carry the
  trailing partial frame to the next read. The streaming test deliberately
  splits a frame mid-word to catch this.

### 2. `src/auth.tsx` — auth context  ·  tested by `src/auth.test.tsx`
- `AuthProvider` holds the token in state, **initialized from `localStorage`**.
- `login(token)` writes state + `localStorage`; `logout()` clears both.
- `useAuth()` reads the context and throws a clear error if used outside the provider.

### 3. `src/components/Login.tsx`  ·  tested by `Login.test.tsx`
- Controlled username/password inputs (give them `<label>`s) + submit button.
- On submit: `preventDefault`, call `api.login`, on success `useAuth().login(token)`.
- On 401: catch and render an error with `role="alert"`.

### 4. `src/components/Chat.tsx`  ·  tested by `Chat.test.tsx`
- Message list + controlled input + submit (Enter or a Send button).
- On submit: push the user message and an empty assistant message, call
  `askStream`, and **append each token to the last message using the updater
  form** `setMessages(prev => …)` (a stale closure will drop tokens — see the
  comment in the file).
- Render a citations panel when citations arrive. A "Log out" button calls `logout()`.

### 5. `src/App.tsx`
- One line: `token ? <Chat /> : <Login />`.

---

## Acceptance checklist ("done" looks like)

- [ ] `npm test` is **all green** (api, auth, Login, Chat suites).
- [ ] `npm run typecheck` passes (no `any`-leaks, no unused stubs left behind).
- [ ] `npm run dev`: you can log in, the screen switches to Chat, and asking a
      question **types the answer out live** with citations below it.
- [ ] Refresh the page while logged in — you stay logged in (localStorage).
- [ ] Click **Log out** — you return to the Login screen and the token is gone
      from `localStorage`.
- [ ] An expired/invalid token surfaces as an error rather than a silent failure.

---

## Files in this phase

| File | Yours to build? | What it is |
|------|-----------------|------------|
| `package.json`, `tsconfig.json`, `vite.config.ts`, `index.html` | no (infra) | project scaffold |
| `src/main.tsx`, `src/index.css`, `src/setupTests.ts`, `src/vite-env.d.ts` | no (infra) | entry point, styles, test setup |
| `src/types.ts` | no (given) | the backend JSON contract as TypeScript types |
| `src/api.ts` | **yes** | the four API functions + SSE parser |
| `src/auth.tsx` | **yes** | token context + localStorage persistence |
| `src/components/Login.tsx` | **yes** | login form |
| `src/components/Chat.tsx` | **yes** | streaming chat + citations |
| `src/App.tsx` | **yes** (tiny) | which screen to show |
| `src/*.test.tsx?` | no — read them | the spec, in executable form |

---

## Stretch goals (once it's green)

- Show a "Disconnected" banner when `health()` fails or `chunks === 0`.
- Add a stop button that aborts an in-flight stream (`AbortController` passed to
  `fetch`, plumbed into `askStream`).
- Auto-logout on any `401` from a protected call (centralize it in `api.ts`).
- Render markdown in answers; make citations clickable to scroll to the source.
- Persist chat history to `localStorage` too, and restore it on load.
