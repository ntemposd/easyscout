"""Stripe billing and payment handling."""
import os
from flask import Blueprint, jsonify, request

# Create blueprint for billing routes
billing_bp = Blueprint('billing', __name__)


def create_billing_routes(app, stripe, require_user_id, record_stripe_event, 
                         record_stripe_purchase, refund_credits, app_base_url):
    """Register all billing-related routes with the Flask app.
    
    Args:
        app: Flask application instance
        stripe: Stripe module
        require_user_id: Auth function to get user ID from request
        record_stripe_event: Function to record Stripe events
        record_stripe_purchase: Function to record purchases
        refund_credits: Function to add credits to user account
        app_base_url: Function to get the application base URL
    """
    
    @app.post("/api/stripe/create-checkout-session")
    def stripe_create_checkout_session():
        if not stripe.api_key:
            return (
                jsonify({"error": "Stripe not configured (missing STRIPE_SECRET_KEY)"}),
                500,
            )

        try:
            user_id = require_user_id(request)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 401

        data = request.get_json(force=True) or {}
        credits = int(data.get("credits", 0))
        if credits <= 0 or credits > 1000:
            return jsonify({"error": "credits must be between 1 and 1000"}), 400

        b = app_base_url()

        # 1 credit = €1 => 100 cents, quantity = credits
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "eur",
                        "unit_amount": 100,
                        "product_data": {"name": "Scoutbot report credits"},
                    },
                    "quantity": credits,
                }
            ],
            success_url=f"{b}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{b}/app",
            client_reference_id=user_id,
            metadata={"user_id": user_id, "credits": str(credits)},
        )

        return jsonify({"url": session.url})


    @app.post("/api/stripe/webhook")
    def stripe_webhook():
        whsec = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        if not whsec:
            return (
                jsonify(
                    {
                        "error": "Stripe webhook not configured (missing STRIPE_WEBHOOK_SECRET)"
                    }
                ),
                500,
            )

        payload = request.data
        sig_header = request.headers.get("Stripe-Signature", "")

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, whsec)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

        # Convert StripeObject -> plain dict for storage (safe for JSONB)
        try:
            event_dict = event.to_dict()  # stripe-python objects support to_dict()
        except Exception:
            event_dict = dict(event) if isinstance(event, dict) else {"raw": str(event)}

        # Idempotent event processing: only act the first time we see this event_id
        try:
            first_time = record_stripe_event(
                event_dict.get("id", ""), event_dict.get("type", ""), event_dict
            )
        except Exception:
            # If logging fails, don't block Stripe retries
            first_time = True

        if not first_time:
            return {"ok": True}

        etype = event_dict.get("type")

        if etype in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
        ):
            session = (event_dict.get("data") or {}).get("object") or {}
            payment_status = session.get("payment_status")
            if payment_status in ("paid", "no_payment_required"):
                meta = session.get("metadata") or {}
                user_id = meta.get("user_id") or session.get("client_reference_id")
                credits_str = meta.get("credits")
                session_id = session.get("id")

                if user_id and credits_str and session_id:
                    credits = int(credits_str)

                    # Optional bookkeeping
                    try:
                        amount_total = int(session.get("amount_total") or 0)
                        currency = (session.get("currency") or "eur").lower()
                        record_stripe_purchase(
                            user_id=user_id,
                            session_id=session_id,
                            amount_cents=amount_total,
                            currency=currency,
                            credits=credits,
                        )
                    except Exception:
                        pass

                    # Grant credits (idempotent via credit_ledger unique index on source_type/source_id)
                    try:
                        refund_credits(
                            user_id,
                            credits,
                            reason="purchase",
                            source_type="stripe_session",
                            source_id=session_id,
                        )
                    except Exception:
                        # Don't fail webhook; Stripe will retry if needed
                        pass

        return {"ok": True}


    @app.post("/api/stripe/confirm")
    def stripe_confirm():
        if not stripe.api_key:
            return (
                jsonify({"error": "Stripe not configured (missing STRIPE_SECRET_KEY)"}),
                500,
            )

        try:
            user_id = require_user_id(request)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 401

        data = request.get_json(force=True) or {}
        session_id = (data.get("session_id") or "").strip()
        if not session_id:
            return jsonify({"error": "missing session_id"}), 400

        session = stripe.checkout.Session.retrieve(session_id)

        if session.get("payment_status") not in ("paid", "no_payment_required"):
            return jsonify({"error": "not paid yet"}), 409

        meta = session.get("metadata") or {}
        if (meta.get("user_id") or "") != user_id:
            return jsonify({"error": "session does not belong to this user"}), 403

        credits = int(meta.get("credits", "0") or "0")
        if credits <= 0:
            return jsonify({"error": "invalid credits"}), 400

        # Optional bookkeeping
        try:
            amount_total = int(session.get("amount_total") or 0)
            currency = (session.get("currency") or "eur").lower()
            record_stripe_purchase(
                user_id=user_id,
                session_id=session_id,
                amount_cents=amount_total,
                currency=currency,
                credits=credits,
            )
        except Exception:
            pass

        new_balance = refund_credits(
            user_id,
            credits,
            reason="purchase",
            source_type="stripe_session",
            source_id=session_id,  # same idempotency key as webhook
        )
        return jsonify({"credits": new_balance})
