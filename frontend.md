# Frontend (folder `Frontend/`)

This repo contains a separate Node/TypeScript “fullstack” app under `Frontend/` (capital `F` on disk). It is currently a polished UI prototype for a transcription product (“Scriber”), with an Express server scaffold and shared DB schema/types.

## What’s inside

- `Frontend/client/`: React 19 + Vite 7 app (Tailwind CSS v4 + shadcn/ui + Radix).
- `Frontend/server/`: Express server that (a) mounts API routes (currently stubbed) and (b) serves the Vite app in dev / static build in prod.
- `Frontend/shared/`: Shared TypeScript types and Drizzle schema (`users` table) for server/client.
- `Frontend/script/build.ts`: Build pipeline (Vite build for client + esbuild bundle for server).
- `Frontend/attached_assets/`: Images + a design prompt used to shape the UI; aliased as `@assets` in Vite.
- `Frontend/.replit`, `Frontend/.local/`: Replit runtime/deployment config + local agent state (not needed for local dev).
- `Frontend/.git/`: The folder contains its own nested git repo metadata.

## Running locally

Prereqs: Node.js 20+ (matches `.replit`), npm.

From repo root:

1. `cd Frontend`
2. `npm install`
3. Dev (recommended): `npm run dev`
   - Starts the Express server and mounts the Vite dev middleware.
   - Opens on `http://localhost:5000` (uses `PORT`, default `5000`).

Other useful commands:

- Client-only dev server: `npm run dev:client` (also uses port `5000`, so don’t run alongside `npm run dev`).
- Typecheck: `npm run check`
- Production build: `npm run build` (outputs `Frontend/dist/public` + `Frontend/dist/index.cjs`)
- Start production server: `npm start`
- Drizzle schema push: `npm run db:push` (requires `DATABASE_URL`; see below)

## App behavior (current state)

The UI is functional and can talk to the Python backend (HTTP + WebSocket). It also degrades gracefully when the backend is unavailable:

- Most screens fetch data from the backend via `/api/...` and listen to `/ws` for updates.
- `TranscriptDetail` falls back to mock content from `Frontend/client/src/lib/mockData.ts` when needed.
- `Settings` loads/saves via `/api/settings` (API keys, hotkey/mode, mic device, language, etc.).

## Related docs

- `docs/UI-UX-Improvement-Proposals.md`: UI/UX roadmap ideas (prioritized).
- `docs/Performance-Optimization-Proposals.md`: performance-focused improvements (incl. UI performance).

## Client structure

- Entry: `Frontend/client/src/main.tsx` -> `Frontend/client/src/App.tsx`
- Routing: `wouter`
  - Tab routes inside layout: `/`, `/youtube`, `/file`, `/settings`
  - Detail route: `/transcript/:id`
- Layout: `Frontend/client/src/components/layout/AppLayout.tsx`
  - Left sidebar navigation (Live Mic / YouTube / File / Settings)
  - Global search input (`SidebarSearch`) + theme toggle
  - Page transitions via `framer-motion`
- Data fetching scaffold: TanStack Query in `Frontend/client/src/lib/queryClient.ts`
  - `apiRequest()` + `getQueryFn()` expect JSON endpoints and `credentials: "include"`.

## Server structure

- Entry: `Frontend/server/index.ts`
  - Adds JSON/body parsing (captures `req.rawBody` for webhook-style verification)
  - Logs `/api` requests with timing + captured JSON responses
  - In dev: mounts Vite middleware (`Frontend/server/vite.ts`)
  - In prod: serves built static assets (`Frontend/server/static.ts`)
- Routes: `Frontend/server/routes.ts`
  - Placeholder `registerRoutes()`; intended to add `/api/...` endpoints.
- Storage abstraction: `Frontend/server/storage.ts`
  - `IStorage` interface and `MemStorage` in-memory implementation for users.

## Shared schema / DB

- Schema: `Frontend/shared/schema.ts`
  - Drizzle `users` table + `insertUserSchema` (Zod) + `User`/`InsertUser` types.
- Drizzle config: `Frontend/drizzle.config.ts`
  - Requires `DATABASE_URL` and uses `postgresql` dialect.

## Configuration / environment variables

- `PORT`: server listen port (defaults to `5000`).
- `NODE_ENV`: controls dev vs prod behavior (`production` uses `serveStatic`, otherwise uses Vite middleware).
- `DATABASE_URL`: required for Drizzle commands (`npm run db:push`).
- Replit-specific (used by Vite config and meta image plugin):
  - `REPL_ID`: enables Replit dev plugins in `Frontend/vite.config.ts`
  - `REPLIT_INTERNAL_APP_DOMAIN`, `REPLIT_DEV_DOMAIN`: used by `Frontend/vite-plugin-meta-images.ts` to rewrite `og:image`/`twitter:image` to the deployment domain (when an `opengraph.(png|jpg|jpeg)` exists in `client/public/`).

## Notable implementation details / gotchas

- `Frontend` is a separate Node project with its own `package.json`; it is launched/managed by the Python tray app and talks to the Python backend via HTTP/WS.
- `Frontend/server/vite.ts` imports `nanoid`, but `nanoid` is not listed in `Frontend/package.json` (it may work transitively, but should be an explicit dependency if used).
- `Frontend/.local/state/...` looks like Replit agent state; it’s not needed for the app at runtime.
- `Frontend/components.json` references `tailwind.config.ts`, but this project uses Tailwind v4’s CSS-first setup via `client/src/index.css` + `@tailwindcss/vite`.
