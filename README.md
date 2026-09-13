# dbt-healer

dbt-healer reviews dbt changes and analyzes failed CI runs, finds the failing dbt file from the logs, asks an LLM for a fix, and opens a GitHub pull request or GitLab merge request with the proposed patch.

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
GitHub Actions / GitLab CI
  -> POST /create/ -> clone commit, prepare dbt metadata, return run_id
  -> POST /review/ with run_id -> review.py -> Telegram findings
  -> dbt build
  -> on failure: POST /analyze/ with run_id and dbt.log
     -> run.py -> AI provider -> GitHub PR / GitLab MR -> Telegram
```

Core modules:

- `cli.py` - interactive setup and Docker Compose launcher.
- `service/failure_ingest.py` - FastAPI webhook receiver.
- `review.py` and `app/review.py` - separate business logic review process.
- `app/utils.py` - dbt log parsing, repo clone, and dbt metadata setup.
- `app/context.py` - source, diff, and lineage context extraction.
- `app/rag.py` - focused lineage snippets using deterministic SQL attributes and relevant windows.
- `app/providers.py` - Ollama, Google AI Studio, and DeepSeek providers.
- `app/push_repo.py` - solution parsing and GitHub PR updates.
- `app/ci_generator.py` - GitHub Actions / GitLab CI and dbt `profiles.yml` CI profile generation.

Each generated `run_id` gets a workspace under `~/.failedrepo/<repo>/<run_id>`. Analyzer subprocesses select it through `HEALER_RUN_ID`. CI sends `branch_name` and `diff_base` so context can include changes from the previous push. Uploaded failure logs are kept separately from local dbt parse logs.

## Supported AI Providers

- Ollama API
- Local Ollama
- Google AI Studio
- DeepSeek API

For local Ollama, dbt-healer can truncate large prompts using `AI_MAX_INPUT_CHARS` and pass `OLLAMA_NUM_CTX` to the local model.

## Setup

Use Python 3.12 (the API Docker image uses the same version). Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`. Running the analyzer outside Docker also requires `dbt-core`, the adapter for your database (for example `dbt-postgres`), and Git.

Run interactive setup:

```bash
python cli.py setup
```

The setup writes `.env`, configures AI/GitHub settings, and can generate:

- `.github/workflows/ci.yml`
- `.gitlab-ci.yml` when GitLab is selected
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
GIT_PLATFORM=Github
BASE_BRANCH=main
DBT_PROJECT_NAME=my_dbt_project
HEALER_REVIEW_ENABLED=true
HEALER_ANALYZE_ON_FAILURE_ENABLED=true

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

The generated GitHub Actions / GitLab CI workflow:

1. Installs project dependencies.
2. Runs `dbt deps`.
3. Creates an isolated healer workspace if either healer feature is enabled.
4. Requests business logic review when `HEALER_REVIEW_ENABLED=true`.
5. Builds changed dbt models, or falls back to full build when needed. Macro, seed, snapshot, and project/package configuration changes trigger a full build.
6. Uploads `dbt.log` when CI fails on a `feature/*` branch and `HEALER_ANALYZE_ON_FAILURE_ENABLED=true`.

The service then creates a fix PR/MR against `BASE_BRANCH`. Both flags default to `true`; disabling both removes all healer calls from generated CI. Regenerate the workflow after changing these settings.

## Development

Run syntax checks:

```bash
python -m compileall app common service notifier run.py review.py cli.py
```

Run tests:

```bash
python -m unittest discover -s tests
```

Tests cover log parsing, context, providers, patch handling, reviews, and CI generation. The GitHub full-build regression test executes Bash with stubbed Git/dbt commands and checks both success and failure; it requires Bash (Git for Windows on Windows). Unit tests do not require live AI, GitHub/GitLab, Telegram, or database credentials.

## Current Limitations

This project is still a prototype-quality automation tool. Before exposing it publicly or using it in production, add:

- authentication or signed webhooks for `/analyze/`
- repository allowlisting
- workspace cleanup and limits on concurrent analyses
- validation of generated patches with `dbt parse` or `dbt build`
- stronger GitHub path safety checks before writing files
- persistent job status and failure reporting

The AI output is treated defensively: malformed responses become `NO_FIX`, but generated SQL should still be validated by dbt before trusting the PR.
