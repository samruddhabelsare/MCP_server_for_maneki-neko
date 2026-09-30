"""Customer feedback service.

Validates rating range (1-5), links feedback to verified orders,
and persists customer ratings and comments.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from maneki.config import get_settings
from maneki.db import get_db
from maneki.errors import ValidationError
from maneki.models import Feedback
from maneki.services.orders import get_order


def submit_feedback(
    order_id: UUID | str,
    rating: int,
    comment: str | None = None,
) -> Feedback:
    """Submit a rating (1-5) and optional comment for a placed order."""
    if rating < 1 or rating > 5:
        raise ValidationError("Rating must be an integer between 1 and 5.")

    # Verify order exists
    order = get_order(order_id)

    cfg = get_settings()
    db = get_db()
    fid = uuid4()
    row: dict[str, Any] = {
        "id": str(fid),
        "order_id": str(order.id),
        "rating": rating,
        "comment": comment[:500].strip() if comment else None,
    }

    res = db.table(cfg.feedback_table).insert(row).execute()
    rows = cast(list[dict[str, Any]], res.data) if res.data else []
    data = rows[0] if rows else row

    return Feedback(
        id=UUID(str(data["id"])),
        order_id=UUID(str(data["order_id"])),
        rating=int(data["rating"]),
        comment=str(data["comment"]) if data.get("comment") else None,
        created_at=datetime.fromisoformat(str(data["created_at"])) if data.get("created_at") else None,
    )
