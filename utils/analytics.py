"""PostHog analytics integration for tracking user events and user aliasing.

Handles optional PostHog analytics client initialization and provides safe,
no-op functions when PostHog is not configured. All analytics operations
are best-effort and never fail the application.
"""

import logging
import os

logger = logging.getLogger(__name__)

# --- Analytics Client (optional) ---
try:
    try:
        # Newer SDKs expose Client; alias it to Posthog for compatibility
        from posthog import Client as Posthog
    except Exception:
        # Older SDKs expose Posthog directly
        from posthog import Posthog  # type: ignore

    _POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
    _POSTHOG_HOST = os.getenv("POSTHOG_HOST") or "https://app.posthog.com"
    if _POSTHOG_API_KEY and Posthog:
        _analytics_client = Posthog(project_api_key=_POSTHOG_API_KEY, host=_POSTHOG_HOST)
        try:
            logging.getLogger("posthog").info("PostHog analytics initialized")
        except Exception:
            pass
    else:
        _analytics_client = None
except Exception:
    _analytics_client = None


def track_event(distinct_id: str | None, event: str, properties: dict | None = None) -> None:
    """Safely send an event to PostHog if configured. No-op when unavailable.

    `distinct_id` may be `None` for anonymous events.
    """
    logger = logging.getLogger("hoopscout.analytics")
    try:
        if not _analytics_client:
            logger.info("analytics disabled - dropping event %s", event)
            return

        immediate = os.getenv("POSTHOG_IMMEDIATE_FLUSH") == "1"

        if immediate:
            # Use a fresh short-lived client so we can flush immediately without
            # affecting the global client state.
            try:
                from posthog import Client as PH

                ph = PH(project_api_key=os.getenv("POSTHOG_API_KEY"), host=os.getenv("POSTHOG_HOST") or "https://app.posthog.com")
                try:
                    ph.capture(distinct_id=distinct_id or "anonymous", event=event, properties=properties or {})
                except TypeError:
                    import posthog as ph_mod

                    ph_mod.capture(distinct_id or "anonymous", event, properties=properties or {})
                try:
                    ph.shutdown()
                except Exception:
                    pass
                logger.info("event flushed immediately: %s with properties: %s", event, properties or {})
                return
            except Exception as e:
                logger.exception("Immediate flush failed, falling back to pooled client: %s", e)

        # Normal path: use pooled client
        logger.info("tracking event %s for %s: %s", event, distinct_id or "anonymous", properties or {})
        try:
            _analytics_client.capture(distinct_id=distinct_id or "anonymous", event=event, properties=properties or {})
            logger.info("event queued (client.capture): %s", event)
            return
        except TypeError:
            try:
                import posthog as posthog_module

                logger.info("falling back to module-level posthog.capture for event %s", event)
                posthog_module.capture(distinct_id or "anonymous", event, properties=properties or {})
                logger.info("event queued (module.capture): %s", event)
                return
            except Exception as e2:
                logger.exception("Fallback posthog.capture failed: %s", e2)
                return
    except Exception as e:
        logger.exception("Error sending analytics event: %s", e)
        # Do not allow analytics failures to affect app behavior
        return


def alias_user(previous_id: str, distinct_id: str) -> None:
    """Link anonymous ID with authenticated user ID in PostHog.
    
    Automatically merges all events from previous_id into distinct_id's profile.
    """
    logger = logging.getLogger("hoopscout.analytics")
    try:
        if not _analytics_client:
            logger.info("analytics disabled - skipping alias")
            return

        logger.info("aliasing user: %s -> %s", previous_id, distinct_id)
        try:
            _analytics_client.alias(previous_id, distinct_id)
            logger.info("user aliased successfully")
        except Exception as e:
            logger.exception("Failed to alias user: %s", e)
    except Exception as e:
        logger.exception("Error aliasing user: %s", e)


def shutdown_analytics() -> None:
    """Flush and shutdown the PostHog analytics client on app exit.
    
    Critical for Render's ephemeral dynos to ensure queued events aren't lost on restart.
    """
    try:
        if _analytics_client:
            # Flush any pending events before shutdown
            if hasattr(_analytics_client, "flush"):
                _analytics_client.flush()
            # Then shutdown the client
            if hasattr(_analytics_client, "shutdown"):
                _analytics_client.shutdown()
            logger = logging.getLogger("hoopscout.analytics")
            logger.info("PostHog analytics flushed and shutdown on exit")
    except Exception as e:
        logger = logging.getLogger("hoopscout.analytics")
        logger.exception("Error during analytics shutdown: %s", e)


def analytics_enabled() -> dict:
    """Return a small dict describing analytics client state for debugging."""
    try:
        return {
            "enabled": bool(_analytics_client),
            "host": os.getenv("POSTHOG_HOST") or "https://app.posthog.com",
            "has_key": bool(os.getenv("POSTHOG_API_KEY")),
        }
    except Exception:
        return {"enabled": False, "host": None, "has_key": False}
