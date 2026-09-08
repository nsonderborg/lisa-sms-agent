import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import MAX_AUTOMATED_MESSAGES_PER_DAY
from app.models import Message

logger = logging.getLogger(__name__)


def can_send_message(db: Session, conversation_id: int) -> bool:
    """Check if we can send another automated message to this student today."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    outbound_today = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == "outbound",
            Message.timestamp >= today_start,
        )
        .count()
    )

    if outbound_today >= MAX_AUTOMATED_MESSAGES_PER_DAY:
        logger.warning(
            "Rate limit reached for conversation %d (%d messages today)",
            conversation_id,
            outbound_today,
        )
        return False

    return True
