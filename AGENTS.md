# AGENTS.md

## Project Overview

This project is a Writer application with a FastAPI backend and a frontend app.

## Collaboration Style

### When Writing New Code

- Briefly explain the architectural choice or logic before writing code.
- Deliver code in small, digestible units: one function, component, or logical unit at a time.
- Add comments only for complex, clever, or unintuitive logic, and explain why the code is written that way.
- When teaching or walking through code, pause after each segment to confirm understanding.
- When implementing a requested change, continue through implementation and verification unless clarification is required.

### When Fixing Bugs or Refactoring

- Start with root cause analysis: explain why the bug happened or why the current approach is suboptimal.
- Briefly outline the fix strategy before editing.
- Keep changes focused on the specific lines, functions, or files needed for the fix.
- Do not rewrite unrelated code or change unrelated behavior.

### When Answering Questions or Explaining Concepts

- Start with a direct answer or definition.
- Use simple mental models or analogies for abstract, complex, or low-level concepts.
- Check whether the explanation was clear and offer to break down a specific part further.

## Backend Rules

- Backend code lives in `backend/app`.
- API routes are in `backend/app/main.py`.
- Agent logic lives in `backend/app/agents`.
- Schemas live in `backend/app/schemas`.
- The backend must use the DeepAgents framework for agent behavior by default.
- Use other frameworks, custom orchestration, or non-DeepAgents agent logic only when DeepAgents does not provide the needed functionality.
- When using a non-DeepAgents approach, explain why DeepAgents is insufficient for that specific case before implementing it.
- Prefer existing backend patterns and dependencies over introducing new abstractions.

## Frontend Rules

- Match the existing frontend structure, style, and component conventions.
- Keep user-facing changes focused on the requested workflow.
- Avoid broad redesigns unless explicitly requested.

## Commands

- Start development: `.\start-dev.ps1`
- Windows command wrapper: `start-dev.cmd`
- Backend environment file: `backend/.env`
- Backend package config: `backend/pyproject.toml`

## Safety

- Do not modify `.env` files unless explicitly requested.
- Do not delete files or generated metadata without confirmation.
- Do not revert user changes unless explicitly requested.
