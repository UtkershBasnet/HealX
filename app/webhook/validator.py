"""
HealX Webhook Validator — GitHub signature verification.

Validates incoming webhook requests using HMAC-SHA256 to ensure
they originate from GitHub and haven't been tampered with.
"""

import hashlib
import hmac

import structlog
from fastapi import HTTPException, Request

from app.config import settings

logger = structlog.get_logger(__name__)


async def validate_github_signature(request: Request) -> bytes:
    """
    Validate the GitHub webhook signature.

    GitHub signs each webhook payload with the secret configured on the webhook.
    We verify this signature to ensure:
    1. The request actually came from GitHub
    2. The payload hasn't been tampered with in transit

    Args:
        request: The incoming FastAPI request.

    Returns:
        The raw request body (bytes) if validation succeeds.

    Raises:
        HTTPException: 401 if signature is missing or invalid.
    """
    signature_header = request.headers.get("X-Hub-Signature-256")

    if not signature_header:
        logger.warning("webhook_missing_signature", path=str(request.url))
        raise HTTPException(
            status_code=401,
            detail="Missing X-Hub-Signature-256 header",
        )

    body = await request.body()

    # Compute expected signature
    secret = settings.github_webhook_secret.encode("utf-8")
    expected_signature = (
        "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    )

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(signature_header, expected_signature):
        logger.warning(
            "webhook_invalid_signature",
            path=str(request.url),
            received=signature_header[:20] + "...",
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    logger.debug("webhook_signature_valid")
    return body
