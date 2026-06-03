"""
HealX Worker — RQ worker entry point.

Run this as a separate process:
    python -m app.worker

Or via docker-compose:
    command: python -m app.worker
"""

import logging

import structlog
from redis import Redis
from rq import Worker, Queue

from app.config import settings

# Configure structlog for worker process
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level.upper())
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


def main():
    """Start the RQ worker."""
    logger.info(
        "worker_starting",
        queues=["healx-jobs"],
        redis_url=settings.redis_url,
    )

    redis_conn = Redis.from_url(settings.redis_url)
    queues = [Queue("healx-jobs", connection=redis_conn)]

    worker = Worker(queues, connection=redis_conn)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
