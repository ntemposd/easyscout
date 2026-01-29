"""Application configuration and third-party service initialization."""

import logging
import os
import types

logger = logging.getLogger(__name__)


def initialize_sentry():
    """Initialize Sentry error monitoring if available.
    
    No-op if SENTRY_DSN is unset or sentry_sdk not installed.
    """
    try:
        import sentry_sdk
        from sentry_sdk.integrations.excepthook import ExcepthookIntegration
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        return

    SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05"))
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN", ""),
        integrations=[
            FlaskIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ExcepthookIntegration(),
        ],
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        environment=os.getenv("SENTRY_ENV", os.getenv("ENV", "development")),
        send_default_pii=False,
    )


def initialize_stripe():
    """Initialize Stripe payment processing.
    
    Returns a Stripe module (real or stub) configured with API key.
    Safety: clears live Stripe keys in development mode to avoid accidental charges.
    """
    try:
        import stripe
    except Exception:
        # Fallback stub for environments without `stripe` installed (dev/test)
        stripe = types.SimpleNamespace()
        stripe.api_key = ""

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    # Safety: avoid using a live Stripe secret in development mode
    try:
        if os.getenv("DEV_TOOLS") == "1" and isinstance(stripe.api_key, str) and stripe.api_key.startswith("sk_live"):
            logger.warning("DEV_TOOLS=1 and a live Stripe secret detected — clearing `stripe.api_key` to avoid accidental live charges.")
            stripe.api_key = ""
    except Exception:
        pass

    return stripe


def initialize_openai():
    """Initialize OpenAI client if enabled.
    
    Returns OpenAI client or None if disabled or import failed.
    If ENABLE_OPENAI is explicitly set, honors that. Otherwise, enables
    automatically if OPENAI_API_KEY is present.
    """
    try:
        from openai import OpenAI
    except Exception:
        # Provide a minimal stub so the app can import when `openai` isn't installed.
        class OpenAI:  # type: ignore
            def __init__(self, *args, **kwargs):
                pass

    _env_enable = os.getenv("ENABLE_OPENAI")
    # If ENABLE_OPENAI explicitly provided, honor it. Otherwise, enable automatically
    # when an `OPENAI_API_KEY` is present in the environment so the server can
    # generate reports when needed without requiring an extra opt-in step.
    if _env_enable is not None:
        ENABLE_OPENAI = _env_enable.lower() in ("1", "true", "yes")
    else:
        ENABLE_OPENAI = bool(os.getenv("OPENAI_API_KEY"))

    client = OpenAI() if ENABLE_OPENAI else None

    if ENABLE_OPENAI and client is not None:
        logger.info(
            "OpenAI generation ENABLED (client initialized). Set ENABLE_OPENAI=0 to disable."
        )
    elif ENABLE_OPENAI and client is None:
        logger.warning(
            "OpenAI generation requested via ENABLE_OPENAI but client failed to initialize."
        )
    else:
        logger.info(
            "OpenAI generation DISABLED. Set OPENAI_API_KEY or ENABLE_OPENAI=1 to enable."
        )

    return client


def setup_compression(app):
    """Setup optional HTTP response compression if flask_compress is available."""
    try:
        from flask_compress import Compress
        Compress(app)
    except Exception:
        pass
