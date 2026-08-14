"""
WBnator accounts / billing subsystem.

Self-contained: SQLite data layer, scrypt auth, server-side sessions, a unified
token economy, one-time promo codes, idempotent purchases, a referral system,
and a pluggable payment-provider abstraction (mock by default).

Wire-up in server.py::

    from accounts import router as account_router, init_db, install_error_handler
    app.include_router(account_router.router)
    install_error_handler(app)
    init_db()
"""

from . import db, pricing, security, payments, mailer, service, oauth, router, admin, backup  # noqa: F401
from .db import init_db                                          # noqa: F401
from .service import ServiceError                                # noqa: F401
from .admin import admin_router                                  # noqa: F401


def install_error_handler(app) -> None:
    """Map ServiceError → JSON {error, code} with the right status code."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(ServiceError)
    async def _handle(_req: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.message, "code": exc.code},
        )
