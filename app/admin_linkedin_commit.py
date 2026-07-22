"""Request limits for the authenticated LinkedIn import commit JSON API."""

from __future__ import annotations

from app.linkedin_export_parser import MAX_CSV_ROWS

# Bounded before JSON materialization; sized for the canonical export row cap.
LINKEDIN_COMMIT_MAX_BODY_BYTES = 33_554_432  # 32 MiB
LINKEDIN_COMMIT_MAX_CONNECTIONS = MAX_CSV_ROWS
LINKEDIN_COMMIT_MAX_MESSAGE_METADATA = MAX_CSV_ROWS
