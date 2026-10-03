"""
One-off migration: add Event.watermark_logo_url (VARCHAR, nullable) to the
existing `events` table.

The service's startup hook (app/main.py lifespan) runs the same idempotent
ALTER, so this is only needed to migrate ahead of a deploy — which matters
here, because sm-ingestion-worker-service and this service both select the
column and fail until it exists. Existing rows get NULL ("use the worker's
default logo / text watermark"). Safe to run more than once.

Usage:
    cd sm-photographer-service
    venv\\Scripts\\python.exe scripts\\add_event_watermark_logo_url_column.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

from app.config.settings import settings


def main():
    engine = create_engine(settings.database_url)

    if any(col["name"] == "watermark_logo_url" for col in inspect(engine).get_columns("events")):
        print("'watermark_logo_url' column already exists on 'events' — nothing to do.")
        return

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS watermark_logo_url VARCHAR"))

    print("Added 'watermark_logo_url' column to 'events'.")


if __name__ == "__main__":
    main()
