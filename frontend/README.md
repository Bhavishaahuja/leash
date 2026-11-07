# Leash frontend

Vite + React + TypeScript. Three screens:

- `/dashboard`: the merchant's view of agent orders (Fernwood Supply Co).
- `/place-order`: the demo's stand-in for the shopping agent. Pick a mandate and a scenario, and see where the order lands.
- `/confirm/:token`: the human's mobile page for a flagged order, designed for a 390px wide phone. It opens from a one-time review link: click a flagged order on the dashboard (or the result on `/place-order`) and use **Get review link**. Approve captures the money, reject releases the hold.

## Run it

```bash
cd frontend
npm install
cp .env.example .env.local   # optional, the defaults use mock data
npm run dev
```

Then open http://localhost:5173/dashboard.

## Mock data vs the real API

`src/api.ts` has typed functions for `GET /orders`, `GET /orders/{id}`, `GET /mandates`, `POST /demo/orders`, `POST /orders/{id}/review-link`, and the confirm page's `GET /review/{token}`, `POST /review/{token}/opened`, `/approve` and `/reject`. Each one returns fake data from `src/mocks.ts` while `VITE_USE_MOCKS` is anything but `false`.

Mock review links only live in the tab that made them, so in mock mode the **Open** button stays in the same tab. A reload works for the seeded orders but not for ones placed on `/place-order`.

Against the real API, every merchant page (`/dashboard`, `/place-order`) asks for the merchant password first. The session token lives in `sessionStorage`, so it's gone when the tab closes, and an expired session sends you back to the login form. The confirm page never asks: its link is the only credential. In mock mode there's no login.

To hit a real backend, set this in `.env.local`:

```
VITE_API_URL=http://127.0.0.1:8000
VITE_USE_MOCKS=false
```

The types in `src/types.ts` mirror the Pydantic models in `backend/app/orders.py`. Money and times come back as strings (FastAPI serializes `Decimal` and `datetime` that way), so they're formatted in `src/lib/format.ts`.

## Checks CI runs

```bash
npm run lint
npm run build
```
