"""Analytics and event tracking endpoints."""
import os
from flask import jsonify, request


def analytics_enabled():
    """Check if analytics is enabled via PostHog."""
    key = os.getenv('POSTHOG_API_KEY')
    return {
        'enabled': bool(key),
        'host': os.getenv('POSTHOG_HOST') or 'https://app.posthog.com'
    }


def create_analytics_routes(app, require_user_id, track_event, alias_user):
    """Register all analytics-related routes with the Flask app.
    
    Args:
        app: Flask application instance
        require_user_id: Auth function to get user ID from request
        track_event: Function to track analytics events
        alias_user: Function to alias user identities
    """
    
    @app.post('/api/analytics')
    def api_analytics():
        try:
            # allow analytics even when require_user_id falls back in dev
            user_id = None
            try:
                user_id = require_user_id(request)
            except Exception:
                user_id = None

            data = request.get_json(force=True) or {}
            event = data.get('event')
            props = data.get('properties') or {}
            if not event:
                return jsonify({'error': 'missing event'}), 400

            # Use distinct_id from frontend if provided, otherwise fallback to authenticated user_id
            distinct_id = props.pop('distinct_id', None) or user_id

            # Handle identity linking (merge anonymous with authenticated)
            if event == '$alias' and 'previous_id' in props:
                previous_id = props.pop('previous_id')
                if distinct_id and previous_id and distinct_id != previous_id:
                    try:
                        alias_user(previous_id, distinct_id)
                    except Exception:
                        pass
                return jsonify({'ok': True})

            try:
                track_event(distinct_id, event, props)
            except Exception:
                pass

            return jsonify({'ok': True})
        except PermissionError as e:
            return jsonify({'error': str(e)}), 401
        except Exception as e:
            return jsonify({'error': str(e)}), 500


    @app.get('/api/analytics_status')
    def api_analytics_status():
        try:
            status = analytics_enabled()
            return jsonify(status)
        except Exception as e:
            return jsonify({'error': str(e)}), 500


    @app.post('/api/analytics_debug')
    def api_analytics_debug():
        """Send a direct, one-off event using a fresh PostHog client and immediately shutdown.

        Useful to validate ingestion key, host, and network without relying on the
        app-level analytics client or background threads.
        """
        try:
            data = request.get_json(force=True) or {}
            event = data.get('event')
            properties = data.get('properties') or {}
            distinct_id = data.get('distinct_id') or None

            if not event:
                return jsonify({'error': 'missing event'}), 400

            key = os.getenv('POSTHOG_API_KEY')
            host = os.getenv('POSTHOG_HOST') or 'https://app.posthog.com'
            if not key:
                return jsonify({'error': 'missing POSTHOG_API_KEY in env'}), 500

            # Use a fresh PostHog client instance so we can shutdown immediately
            try:
                from posthog import Posthog as PH
                ph = PH(project_api_key=key, host=host)
                try:
                    # Preferred signature: capture(event, properties=..., distinct_id=...)
                    ph.capture(event, properties=properties, distinct_id=distinct_id or 'anonymous')
                except TypeError:
                    # Fallback for older/newer module-level API
                    try:
                        import posthog as ph_mod
                        ph_mod.capture(distinct_id or 'anonymous', event, properties=properties)
                    except Exception:
                        raise
                finally:
                    try:
                        ph.shutdown()
                    except Exception:
                        pass

                return jsonify({'ok': True})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        except Exception as e:
            return jsonify({'error': str(e)}), 500


    @app.post('/api/analytics_debug_raw')
    def api_analytics_debug_raw():
        """Directly POST to the PostHog `/capture` HTTP endpoint and return the raw response.

        This helps verify ingestion at the HTTP level and shows the exact response body
        returned by PostHog for debugging.
        """
        try:
            data = request.get_json(force=True) or {}
            event = data.get('event')
            properties = data.get('properties') or {}
            distinct_id = data.get('distinct_id') or 'anonymous'

            if not event:
                return jsonify({'error': 'missing event'}), 400

            key = os.getenv('POSTHOG_API_KEY')
            host = os.getenv('POSTHOG_HOST') or 'https://app.posthog.com'
            if not key:
                return jsonify({'error': 'missing POSTHOG_API_KEY in env'}), 500

            import requests

            url = host.rstrip('/') + '/capture'
            payload = {
                'api_key': key,
                'event': event,
                'properties': {**properties, 'distinct_id': distinct_id},
            }

            r = requests.post(url, json=payload, timeout=10)
            try:
                body = r.json()
            except Exception:
                body = r.text

            return jsonify({'status_code': r.status_code, 'body': body})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
