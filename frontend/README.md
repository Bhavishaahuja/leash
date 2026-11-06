# Leash frontend

Vite + React + TypeScript. Three screens:

- `/dashboard`: the merchant's view of agent orders (Fernwood Supply Co).
- `/place-order`: the demo's stand-in for the shopping agent. Pick a mandate and a scenario, and see where the order lands. Against the real API it asks for the merchant password first.
- `/confirm/:orderId`: the human's mobile page for a flagged order, designed for a 390px wide phone.

## Run it

```bash
cd frontend
npm install
cp .env.example .env.local   # optional, the defaults use mock data
npm run dev
```

Then open http://localhost:5173/dashboard.

## Mock data vs the real API

`src/api.ts` has typed functions for `GET /orders`, `GET /orders/{id}`, `GET /mandates`, `POST /demo/orders`, `POST /orders/{id}/capture` and `POST /orders/{id}/void`. Each one returns fake data from `src/mocks.ts` while `VITE_USE_MOCKS` is anything but `false`.

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
