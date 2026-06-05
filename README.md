# dbt-healer

dbt-healer watches failed dbt CI runs, finds the failing dbt file from the logs, asks an LLM for a fix, and opens a GitHub pull request with the proposed patch.

The project is aimed at data teams that have recurring dbt failures in feature branches and want a first-pass repair PR instead of a manual debugging loop every time.

## What It Does

- Receives failed dbt CI logs through a FastAPI endpoint.
- Clones the failed repository commit into a local workspace.
- Parses dbt logs to detect failing models, snapshots, seeds, or macros.
- Builds source context from the failing file, git diff, and dbt manifest lineage.
- Reviews changed dbt business logic and sends Telegram findings when risky changes are detected.
- Sends the context to an AI provider.
- Validates that the model returns strict `<solution>` and `<file>` blocks.
- Creates a GitHub branch and pull request with the suggested fix.
- Optionally sends Telegram notifications about created PRs.

## Architecture

```text
GitHub Actions
  -> POST /analyze/ with dbt.log
  -> service/failure_ingest.py
  -> app/utils.py clones repo and prepares dbt metadata
  -> run.py orchestrates repair
  -> app/providers.py calls AI provider
  -> app/push_repo.py opens GitHub PR
  -> notifier/ sends Telegram message
```

Core modules:

- `cli.py` - interactive setup and Docker Compose launcher.
- `service/failure_ingest.py` - FastAPI webhook receiver.
- `app/utils.py` - dbt log parsing, repo clone, and dbt metadata setup.
- `app/context.py` - source, diff, and lineage context extraction.
- `app/rag.py` - focused lineage snippets using deterministic SQL attributes and relevant windows.
- `app/providers.py` - Ollama, Google AI Studio, and DeepSeek providers.
- `app/push_repo.py` - solution parsing and GitHub PR updates.
- `app/ci_generator.py` - GitHub Actions workflow and dbt `profiles.yml` CI profile generation.

## Supported AI Providers

- Ollama API
- Local Ollama
- Google AI Studio
- DeepSeek API

For local Ollama, dbt-healer can truncate large prompts using `AI_MAX_INPUT_CHARS` and pass `OLLAMA_NUM_CTX` to the local model.

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run interactive setup:

```bash
python cli.py setup
```

The setup writes `.env`, configures AI/GitHub settings, and can generate:

- `.github/workflows/ci.yml`
- a `ci` target in the dbt project's `profiles.yml`

Start the service:

```bash
python cli.py serve --port 8888
```

Or directly:

```bash
docker compose up -d
```

Health check:

```bash
curl http://localhost:8888/health/
```

## Configuration

The main configuration lives in `.env`.

Important values:

```env
SERVICE_ENDPOINT=https://your-host/analyze/
GITHUB_REPO_LINK=https://github.com/org/repo.git
GITHUB_TOKEN=...
BASE_BRANCH=main
DBT_PROJECT_NAME=my_dbt_project

AI_PROVIDER=Ollama
AI_PROVIDER_TYPE=Ollama (API)
AI_API_KEY=...
AI_MODEL=...

OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_NUM_CTX=8192
AI_MAX_INPUT_CHARS=24000

TELEGRAM_BOT_TOKEN=...
```

## CI Flow

The generated GitHub Actions workflow:

1. Installs project dependencies.
2. Runs `dbt deps`.
3. Builds changed dbt models, or falls back to full build when needed.
4. Uploads `dbt.log` to dbt-healer when CI fails on a `feature/*` branch.

The service then creates a fix PR against `BASE_BRANCH`.

## Development

Run syntax checks:

```bash
python3 -m compileall app common service notifier run.py cli.py
```

Run tests:

```bash
python3 -m unittest discover -s tests
```

## Current Limitations

This project is still a prototype-quality automation tool. Before exposing it publicly or using it in production, add:

- authentication or signed webhooks for `/analyze/`
- repository allowlisting
- isolated workspaces per CI failure
- validation of generated patches with `dbt parse` or `dbt build`
- stronger GitHub path safety checks before writing files
- persistent job status and failure reporting

The AI output is treated defensively: malformed responses become `NO_FIX`, but generated SQL should still be validated by dbt before trusting the PR.
