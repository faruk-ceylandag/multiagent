"""Standard API response helpers."""
from fastapi.responses import JSONResponse


def ok(data=None):
    """Standard success response."""
    result = {"ok": True}
    if data is not None:
        result["data"] = data
    return result


def error(message: str, code: str = "ERROR", status_code: int = 400):
    """Standard error response with proper HTTP status."""
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": message, "code": code}
    )


def not_found(message: str = "Not found"):
    return error(message, "NOT_FOUND", 404)


def conflict(message: str = "Conflict"):
    return error(message, "CONFLICT", 409)


def validation_error(message: str = "Validation error"):
    return error(message, "VALIDATION_ERROR", 400)
