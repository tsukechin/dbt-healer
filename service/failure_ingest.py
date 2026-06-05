from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form
import hashlib
import logging
import os
import secrets
import sys
from pathlib import Path
import subprocess

from app.utils import clone_repo_from_ci

PATH = str(Path(__file__).resolve().parents[1])

sys.path.append(PATH)

app = FastAPI()
status_logger = logging.getLogger("uvicorn.error")


def _status(message: str, run_id: str | None = None) -> None:
    """Log a user-facing status line for CLI log streaming."""
    if run_id:
        status_logger.info("(%s) STATUS: %s", run_id, message)
    else:
        status_logger.info("STATUS: %s", message)


def _created_env_status(run_id: str) -> None:
    """Log the isolated environment id once it is allocated."""
    status_logger.info("STATUS: created env %s", run_id)


def _new_run_id(repo: str, commit_hash: str, dbt_path: str) -> str:
    """Return unique run id for an isolated CI analysis."""
    seed = f"{repo}|{commit_hash}|{dbt_path}|{secrets.token_hex(8)}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _script_status_label(script: str) -> str:
    """Return user-facing process label for an analyzer script."""
    return {
        "run.py": "Failure analysis process",
        "review.py": "Review process",
    }.get(script, f"{script} process")


def _run_script(script: str, run_id: str) -> None:
    """Run an analyzer script with per-run environment."""
    env = {**os.environ, "HEALER_RUN_ID": run_id}
    label = _script_status_label(script)
    _status(f"{label} started", run_id)
    try:
        subprocess.run(["python3", script], cwd=PATH, env=env, check=True)
    except subprocess.CalledProcessError:
        _status(f"{label} failed", run_id)
        raise
    _status(f"{label} finished", run_id)


def upload_failure(
    repo: str,
    commit_hash: str,
    dbt_path: str,
    log_file: UploadFile,
    run_id: str,
    branch_name: str | None,
    diff_base: str | None,
):
    """Process uploaded CI failure in background."""
    logging.info(
        "dbt failure received: repo=%s commit=%s path=%s run_id=%s branch=%s",
        repo, commit_hash, dbt_path, run_id, branch_name
    )
    _status("Failure analysis started", run_id)

    clone_repo_from_ci(repo, commit_hash, dbt_path, log_file, run_id, branch_name, diff_base)
    _status("Failure payload prepared", run_id)

    _run_script("run.py", run_id)

@app.get("/health/")
def health():
    """Return service health status."""
    return {"status": "healer is healthy"}

@app.post("/analyze/")
def analyze(
    background_tasks: BackgroundTasks,
    repo: str = Form(...),
    commit_hash: str = Form(...),
    dbt_path: str = Form(...),
    log_file: UploadFile = File(...),
    run_id: str | None = Form(None),
    branch_name: str | None = Form(None),
    diff_base: str | None = Form(None),
):
    """Accept CI failure payload for async analysis."""
    try:
        if not run_id:
            run_id = _new_run_id(repo, commit_hash, dbt_path)
            _created_env_status(run_id)
        _status("Failure accepted for analysis", run_id)
        background_tasks.add_task(
            upload_failure,
            repo,
            commit_hash,
            dbt_path,
            log_file,
            run_id,
            branch_name,
            diff_base,
        )
        
        return {"status": "accepted", "run_id": run_id}
    except Exception as e:
        logging.error(f"Error processing failure: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/create/")
def create(
    repo: str = Form(...),
    commit_hash: str = Form(...),
    dbt_path: str = Form(...),
    branch_name: str | None = Form(None),
    diff_base: str | None = Form(None),
):
    """Create isolated CI analysis workspace."""
    try:
        run_id = _new_run_id(repo, commit_hash, dbt_path)
        _created_env_status(run_id)
        _status("Creating isolated check workspace and running dbt parse", run_id)
        clone_repo_from_ci(repo, commit_hash, dbt_path, run_id=run_id, branch_name=branch_name, diff_base=diff_base)
        _status("Analysis workspace created", run_id)
        return {"status": "created", "run_id": run_id}
    except Exception as e:
        logging.error(f"Error creating analysis instance: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/review/")
def review(
    background_tasks: BackgroundTasks,
    run_id: str = Form(...),
):
    """Accept async review request for an existing analysis workspace."""
    try:
        _status("Review accepted for analysis", run_id)
        background_tasks.add_task(_run_script, "review.py", run_id)
        return {"status": "accepted", "run_id": run_id}
    except Exception as e:
        logging.error(f"Error processing review: {e}")
        return {"status": "error", "message": str(e)}
