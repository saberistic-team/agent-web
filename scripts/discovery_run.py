#!/usr/bin/env python3
"""Run scheduled discovery when due — intended for Render cron or operator shell."""

from __future__ import annotations

import logging
import sys
import uuid

from app import db
from app.config import get_settings
from app.discovery.service import get_discovery_run_service

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.discovery_schedule_active:
        logger.info(
            "discovery scheduler inactive (set DISCOVERY_SCHEDULER_ENABLED=true in production)"
        )
        return 0
    if not settings.database_url:
        logger.error("DATABASE_URL is required for discovery runs")
        return 1

    service = get_discovery_run_service()
    correlation_id = f"discovery-cron-{uuid.uuid4()}"
    with db.db_connection(settings.database_url) as conn:
        result = service.trigger_scheduled_run_if_due(
            conn,
            settings,
            correlation_id=correlation_id,
        )

    if result is None:
        logger.info("discovery run not due yet")
        return 0

    logger.info(
        "discovery run finished status=%s run_id=%s lock=%s",
        result.status,
        result.run_id,
        result.lock_acquired,
    )
    if result.status == "failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
