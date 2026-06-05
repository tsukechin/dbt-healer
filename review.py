import asyncio
import logging

from app.provider_builder import build_provider
from app.review import build_review_context, review_finding
from common.config import get_config
from notifier.utils import notify_about_review

config = get_config()


def _status(message: str) -> None:
    """Log a user-facing status line for CLI log streaming."""
    if config.healer_run_id:
        logging.info("(%s) STATUS: %s", config.healer_run_id, message)
    else:
        logging.info("STATUS: %s", message)


async def main() -> None:
    """Review changed code and notify only on business-logic findings."""
    _status("Review started")
    review_context = build_review_context()
    if not review_context:
        _status("Review skipped: no changed files found")
        logging.warning("No review context found; skipping review.")
        return
    logging.info("Review context:\n%s", review_context)

    model = build_provider(
        ai_provider=config.ai_provider,
        context="",
        ollama_type=config.ai_provider_type,
    )
    review_output = model.review_changes(review_context)
    logging.info("Review model output:\n%s", review_output)
    finding = review_finding(review_output)
    if not finding:
        _status("Review passed")
        logging.info("Review completed with no findings.")
        return

    _status("Review failed: finding detected")
    await notify_about_review(finding)
    _status("Review notification sent")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    asyncio.run(main())
