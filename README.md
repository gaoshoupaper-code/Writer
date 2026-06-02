# Writer

Minimal runnable skeleton for a screenplay generation agent.

## Architecture

- `backend/`: FastAPI API with a DeepAgents-powered screenplay service
- `frontend/`: Next.js writing workspace for collecting inputs and displaying output

## Why this shape

This first version optimizes for a fast feedback loop.
The backend already uses the Agent boundary we will keep long term, while the frontend is a small but real workspace instead of a disposable demo page.

The main trade-off is that the first API only supports one workflow: generate a logline, synopsis, and five beats from a premise.
That is intentional because it keeps the data contract stable while we validate the product loop.

## Run the backend

1. Create a virtual environment in `backend/`.
2. Install dependencies from `backend/pyproject.toml`.
3. Copy `backend/.env.example` to `backend/.env`.
4. Start with `WRITER_AGENT_MODE=mock` so the app runs before you add model keys.
5. Run:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 7788
```

Backend URL: `http://127.0.0.1:7788`

## Run the frontend

1. Install dependencies in `frontend/`.
2. Copy `frontend/.env.local.example` to `frontend/.env.local`.
3. Run:

```powershell
npm.cmd run dev
```

Frontend URL: `http://127.0.0.1:3000`

## Start both together

From the repo root, run:

```powershell
.\start-dev.ps1
```

This opens two terminal windows:
- backend: `uvicorn` on `http://127.0.0.1:7788`
- frontend: `next dev` on `http://127.0.0.1:3000`

## Switch to a live model

Set these values in `backend/.env`:

```env
OPENAI_API_KEY=your_key
WRITER_MODEL=openai:gpt-4o-mini
WRITER_AGENT_MODE=live
```
