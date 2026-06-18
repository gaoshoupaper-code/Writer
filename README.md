# Writer

Minimal runnable skeleton for a screenplay generation agent.

## Architecture

- `backend/`: FastAPI API with a DeepAgents-powered screenplay service
- `frontend/`: Next.js writing workspace for collecting inputs and displaying output

### 分层依赖铁律（重构护栏）

后端正按"平台层 + 领域层 + 基础设施层"三分层重构。6 条铁律由 `scripts/check_layering.py` 机器校验：

1. `platform/` 不得 import `domains/` 或 `infrastructure/`
2. `domains/X/` 不得 import `domains/Y/`（domain 间禁止互依）
3. `domains/` 只能 import `platform/` + `infrastructure/`（过渡期含 `db`/`schemas`/`core`/`auth`/`admin`）
4. `infrastructure/` 不得 import `platform/` 或 `domains/`
5. `core/` 不得 import `writer/`（PR-03 切断）
6. `domains/image` 不得 import `writer/`（PR-02 切断）

```powershell
python scripts/check_layering.py          # baseline 模式：只拦新增违规
python scripts/check_layering.py --strict # 严格模式：存量违规也 fail（重构完成后用）
```

存量违规登记在 `backend/layering_baseline.txt`（当前 6 条：1 条 core→writer + 5 条 image→writer）。每消除一条就从 baseline 删一行，或重跑 `python scripts/check_layering.py --update`。CI（`.github/workflows/layering.yml`）在 PR 时自动跑此检查。

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
