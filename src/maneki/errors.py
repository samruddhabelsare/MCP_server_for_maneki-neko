"""ManekiError hierarchy.

All business-logic errors inherit from ManekiError and carry:
  - code   : machine-readable slug  (e.g. "item_not_found")
  - message: human-readable text    (surfaces to the LLM / client)
  - status : HTTP status code hint  (used by REST routes)

MCP tool handlers convert ManekiError → {"ok": false, "error": code, "message": ...}.
REST route handlers convert ManekiError → JSONResponse with the matching status code.
"""
from __future__ import annotations


class ManekiError(Exception):
    """Base class for all application errors."""

    code: str = "internal_error"
    status: int = 500

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict[str, object]:
        return {"ok": False, "error": self.code, "message": self.message}


# ── Session errors ──────────────────────────────────────────────────────────

class SessionNotFoundError(ManekiError):
    code = "session_not_found"
    status = 401

    def __init__(self, session_id: str | None = None) -> None:
        msg = "Session not found or expired"
        if session_id:
            msg = f"Session '{session_id}' not found or expired"
        super().__init__(msg)


class SessionExpiredError(ManekiError):
    code = "session_expired"
    status = 401

    def __init__(self) -> None:
        super().__init__("Session has expired. Please start a new session.")


# ── Auth errors ─────────────────────────────────────────────────────────────

class UnauthorizedError(ManekiError):
    code = "unauthorized"
    status = 401

    def __init__(self, detail: str = "Authentication required") -> None:
        super().__init__(detail)


class ForbiddenError(ManekiError):
    code = "forbidden"
    status = 403

    def __init__(self, detail: str = "Access denied") -> None:
        super().__init__(detail)


# ── Menu / item errors ───────────────────────────────────────────────────────

class ItemNotFoundError(ManekiError):
    code = "item_not_found"
    status = 404

    def __init__(self, name: str) -> None:
        super().__init__(f"Menu item '{name}' not found.")
        self.item_name = name


class AmbiguousItemError(ManekiError):
    """Raised when a search matches more than one item — let the LLM clarify."""

    code = "ambiguous_item"
    status = 422

    def __init__(self, name: str, candidates: list[str]) -> None:
        super().__init__(
            f"'{name}' matches multiple items: {', '.join(candidates)}. "
            "Please be more specific."
        )
        self.candidates = candidates

    def to_dict(self) -> dict[str, object]:
        d = super().to_dict()
        d["candidates"] = self.candidates
        return d


class ItemUnavailableError(ManekiError):
    code = "item_unavailable"
    status = 422

    def __init__(self, name: str) -> None:
        super().__init__(f"Sorry, '{name}' is currently unavailable.")
        self.item_name = name


# ── Draft / order errors ─────────────────────────────────────────────────────

class DraftNotFoundError(ManekiError):
    code = "draft_not_found"
    status = 404

    def __init__(self) -> None:
        super().__init__("No open draft found for this session.")


class EmptyDraftError(ManekiError):
    code = "empty_draft"
    status = 422

    def __init__(self) -> None:
        super().__init__("Cannot confirm an empty order. Please add items first.")


class InvalidStatusTransitionError(ManekiError):
    code = "invalid_status_transition"
    status = 422

    def __init__(self, current: str, attempted: str, allowed: list[str]) -> None:
        super().__init__(
            f"Cannot transition order from '{current}' to '{attempted}'. "
            f"Allowed next states: {', '.join(allowed)}."
        )
        self.allowed = allowed

    def to_dict(self) -> dict[str, object]:
        d = super().to_dict()
        d["allowed_transitions"] = self.allowed
        return d


class OrderNotFoundError(ManekiError):
    code = "order_not_found"
    status = 404

    def __init__(self, order_id: str | None = None) -> None:
        msg = "Order not found"
        if order_id:
            msg = f"Order '{order_id}' not found"
        super().__init__(msg)


# ── Validation ───────────────────────────────────────────────────────────────

class ValidationError(ManekiError):
    code = "validation_error"
    status = 422

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


# ── Restaurant ───────────────────────────────────────────────────────────────

class RestaurantNotFoundError(ManekiError):
    code = "restaurant_not_found"
    status = 404

    def __init__(self, restaurant_id: str | None = None) -> None:
        msg = "Restaurant not found"
        if restaurant_id:
            msg = f"Restaurant '{restaurant_id}' not found"
        super().__init__(msg)
