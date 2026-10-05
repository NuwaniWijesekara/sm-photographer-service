"""Background consumer for the guest.email_sent Redis Stream.

sm-notification-service publishes {event_id, guest_email, status} after an
invitation email is actually delivered to the SMTP server; this marks the
matching event_collaborators row. Runs as a daemon thread started from the
FastAPI lifespan (main.py) — the DB layer is synchronous SQLAlchemy, so a
thread is simpler than an asyncio task. Never takes the API down: Redis or
DB errors are logged and retried.
"""
import logging
import threading
import time

import redis as redis_lib
from sqlalchemy import func

from ..config.settings import settings

logger = logging.getLogger(__name__)

_BLOCK_MS = 5000
_VALID_STATUSES = {"SENT"}


class EmailStatusConsumer:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Own client: socket_timeout must exceed the xreadgroup block, and a
        # blocked read shouldn't hold a connection the request path needs.
        self._r = redis_lib.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=(_BLOCK_MS / 1000) + 5,
            socket_connect_timeout=5,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="email-status-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            # At most one xreadgroup block, then the loop sees the flag.
            self._thread.join(timeout=(_BLOCK_MS / 1000) + 2)

    def _ensure_group(self) -> None:
        try:
            self._r.xgroup_create(settings.email_sent_stream, settings.email_sent_consumer_group, id="0", mkstream=True)
            logger.info(f"Consumer group '{settings.email_sent_consumer_group}' created")
        except redis_lib.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def _run(self) -> None:
        logger.info(f"Email status consumer listening on '{settings.email_sent_stream}'")
        group_ready = False
        # "0" first: replay this consumer's own pending (delivered but never
        # acked, e.g. DB was down) messages, then switch to new ones (">").
        read_id = "0"
        while not self._stop.is_set():
            try:
                if not group_ready:
                    self._ensure_group()
                    group_ready = True

                messages = self._r.xreadgroup(
                    settings.email_sent_consumer_group,
                    settings.email_sent_consumer_name,
                    {settings.email_sent_stream: read_id},
                    count=50,
                    block=_BLOCK_MS,
                )
                entries = messages[0][1] if messages else []
                if read_id == "0" and not entries:
                    read_id = ">"
                    continue

                for msg_id, data in entries:
                    if self._handle(data):
                        self._r.xack(settings.email_sent_stream, settings.email_sent_consumer_group, msg_id)
                    else:
                        # Left pending; replayed from "0" after a short pause.
                        read_id = "0"
                if read_id == "0" and entries:
                    time.sleep(1)  # don't hot-loop on a persistent DB error
            except redis_lib.exceptions.ConnectionError as e:
                logger.warning(f"Email status consumer: Redis unavailable ({e}), retrying in 5s")
                self._stop.wait(5)
            except Exception as e:
                logger.error(f"Email status consumer error: {e}")
                self._stop.wait(5)
        logger.info("Email status consumer stopped")

    def _handle(self, data: dict) -> bool:
        """Apply one receipt. Returns True when the message is done with —
        applied, or unusable/stale (nothing to retry) — and False only for a
        transient DB failure that's worth retrying."""
        from ..main import SessionLocal, EventCollaborator, User

        event_id = data.get("event_id")
        email = (data.get("guest_email") or "").strip().lower()
        status = (data.get("status") or "").upper()
        if not event_id or not email or status not in _VALID_STATUSES:
            logger.error(f"Malformed guest.email_sent message, skipping: {data}")
            return True

        db = SessionLocal()
        try:
            link = (
                db.query(EventCollaborator)
                .join(User, User.id == EventCollaborator.user_id)
                .filter(EventCollaborator.event_id == event_id, func.lower(User.email) == email)
                .first()
            )
            if not link:
                # Collaborator removed (or event deleted) since the invite.
                logger.info(f"guest.email_sent for {email} on {event_id}: no collaborator row, ignoring")
                return True
            link.email_status = status
            db.commit()
            logger.info(f"Invitation email for {email} on event {event_id} marked {status}")
            return True
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update email_status for {email} on {event_id}: {e}")
            return False
        finally:
            db.close()


email_status_consumer = EmailStatusConsumer()
