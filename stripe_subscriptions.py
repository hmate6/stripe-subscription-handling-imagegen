# ez a kódrészlet az ImageGen projektből származik, amikor még production environmentbe volt a program
# és a felhasználók stripe-on keresztül tudtak előfizetni a pro,plus csomagokra
# magában nem futtatható, a fő ImageGen projektből már el van távolítva az előfizetés lehetőség, a pro és plus csomagok
import os
import time
from datetime import datetime

import stripe
from dotenv import load_dotenv
from flask import session, request, redirect, jsonify, render_template, flash

# app, db, redis (r), mode és a db modellek az ImageGen app_init-jéből jönnek
from app_init import app, db, r, mode, UserAccount, Subscriptions, Transactions
from functions.auth_functions import user_status, log
from functions.emailsendzoho import send_email_new

load_dotenv()

stripe_keys = {
    "secret_key": os.environ["STRIPE_SECRET_KEY"],
    "publishable_key": os.environ["STRIPE_PUBLISHABLE_KEY"],
    "endpoint_secret": os.environ["STRIPE_ENDPOINT_SECRET"],
    "proprice": os.environ["SUBSCRIPTION_PRO_PRICE_ID"],
    "plusprice": os.environ["SUBSCRIPTION_PLUS_PRICE_ID"],
}

PLAN_PRICES = {
    "pro": stripe_keys["proprice"],
    "plus": stripe_keys["plusprice"],
}

# ennyi kreditet kap a user minden sikeres fizetésnél (első fizetés + minden megújítás)
PLAN_CREDITS = {
    "pro": 1500,
    "plus": 5000,
}

domain_url = os.environ["DOMAIN_URL"]
webhook_url = os.environ["WEBHOOK_URL"]

stripe.api_key = stripe_keys["secret_key"]


def notify(email, subject, text):
    # ha az email küldés elhasal, attól még a webhook ne dobjon 500-at,
    # különben a stripe újraküldi az eventet és duplán futna le minden
    try:
        send_email_new(email, subject, text)
    except Exception as e:
        log(mode, "ERROR", f"Email sending failed to {email}, subject: {subject}, error: {e}")


def parse_userid(metadata, context):
    try:
        return int(metadata["user_id"])
    except (KeyError, TypeError, ValueError) as e:
        log(mode, "ERROR", f"Error: malformed userid in subscription metadata: {metadata}, error: {e}. {context}")
        return None


def has_running_subscription(userid):
    # amíg a jelenlegi kifizetett időszak nem járt le, nem lehet új előfizetést indítani,
    # különben két élő subscription lenne stripe-ban és a user kétszer fizetne
    subscription = Subscriptions.query.filter_by(user_id=userid).first()
    if subscription is None or subscription.status == "deleted":
        return False
    return subscription.expire_int is not None and subscription.expire_int > time.time()


@app.route("/create-checkout-session")
def create_checkout_session():
    userid = session.get("userid")

    # a frontend fetch-el hívja, ezért redirect helyett json hibát adunk vissza
    if not userid:
        return jsonify({"error": "You have to be logged in to subscribe!"}), 401

    if not user_status(userid):
        return jsonify({"error": "You must verify your account before becoming a member! Go to your e-mail to verify, if you can't see the messages in your inbox, or spam, you can resend it on the settings page."}), 403

    plan = request.args.get("plan")

    if plan not in PLAN_PRICES:
        return jsonify({"error": "Invalid plan selected! Please select a valid plan!"}), 400

    if has_running_subscription(userid):
        return jsonify({"error": "You already have a subscription. You can subscribe again after your current period ends."}), 400

    try:
        # order new checkout session
        checkout_session = stripe.checkout.Session.create(
            client_reference_id=str(userid),
            success_url=domain_url + "success",
            cancel_url=domain_url + "canceled",
            payment_method_types=["card"],
            mode="subscription",
            line_items=[
                {"price": PLAN_PRICES[plan], "quantity": 1}
            ],
            subscription_data={
                "metadata": {
                    "user_id": str(userid),
                    "plan": plan
                },
            },
        )
        return jsonify({"sessionId": checkout_session["id"]})
    except Exception as e:
        log(mode, "ERROR", f"Checkout session creation failed for userid {userid}, error: {e}")
        return jsonify({"error": "An error occured, please try again later or contact our support!"}), 500


@app.route("/success")
def success():
    return render_template("codes/success.html")

@app.route("/canceled")
def canceled():
    return render_template("codes/cancel.html")


@app.route(webhook_url, methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, stripe_keys["endpoint_secret"]
        )

    except ValueError:
        return "Invalid payload", 400

    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    obj = event["data"]["object"]

    # webhook -> invoice.payment_succeeded event (első fizetés és minden havi megújítás is ez)
    if event["type"] == "invoice.payment_succeeded":
        return handle_payment_succeeded(obj)

    elif event["type"] == "invoice.payment_failed":
        return handle_payment_failed(obj)

    elif event["type"] == "customer.subscription.deleted":
        return handle_subscription_deleted(obj)

    # a többi eventtel nem foglalkozunk, de 200-at kell visszaadni, hogy a stripe ne küldje újra
    return "Unhandled event type", 200


def handle_payment_succeeded(invoice):
    subscription_id = invoice.get("subscription")
    customer_id = invoice.get("customer")

    # nem előfizetéshez tartozó számla, ezzel nincs dolgunk
    if not subscription_id:
        return "Not a subscription invoice, skipped.", 200

    line = invoice["lines"]["data"][0]
    start_date = line["period"]["start"]
    end_date = line["period"]["end"]
    price_id = line["price"]["id"]

    # userid = int(invoice["subscription_details"]["metadata"]["user_id"])
    metadata = stripe.Subscription.retrieve(subscription_id)["metadata"]
    userid = parse_userid(metadata, "Payment succeeded webhook.")
    if userid is None:
        return "Malformed metadata.", 200

    plan = metadata.get("plan")
    if plan not in PLAN_CREDITS:
        log(mode, "ERROR", f"Unknown plan in subscription metadata: {plan}, ID: {subscription_id}")
        return "Unknown plan.", 200

    useracc = UserAccount.query.filter_by(id=userid).first()
    if useracc is None:
        log(mode, "ERROR", f"Account doesnt exist for customer: {customer_id}, ID: {subscription_id}")
        return "Account doesn't exist!", 200

    subscription = Subscriptions.query.filter_by(user_id=userid).first()
    if subscription is None:
        # Create a new subscription if it doesn't exist
        subscription = Subscriptions(user_id=userid, created_at=datetime.now())
        db.session.add(subscription)
        log(mode, "INFO", f"New subscription created for {customer_id}, ID: {subscription_id}.")
    elif subscription.stripe_subscription_id != subscription_id:
        # a usernek volt már korábbi előfizetése, azt felülírjuk az újjal
        subscription.created_at = datetime.now()
        log(mode, "INFO", f"Previous subscription {subscription.stripe_subscription_id} replaced with {subscription_id} for user id {userid}.")
    else:
        log(mode, "INFO", f"Subscription {subscription_id} renewed for user id {userid}.")

    subscription.customer_id = customer_id
    subscription.stripe_subscription_id = subscription_id
    subscription.stripe_price_id = price_id
    subscription.status = "active"  # subscription is successfully paid and active
    subscription.current_period_start = datetime.fromtimestamp(start_date)
    subscription.current_period_end = datetime.fromtimestamp(end_date)
    subscription.expire_int = end_date
    subscription.updated_at = datetime.now()

    credits = PLAN_CREDITS[plan]
    useracc.credits = credits
    useracc.tool_uses_in_plan = credits
    useracc.plan = plan

    db.session.add(
        Transactions(user_id=useracc.id, text=f"Payment success, plan: {plan}. Userid in db: {useracc.id}. Subscription id: {subscription_id}.", credits_cost=0,
                     created_at=datetime.now(), type="stripe_payment"))
    db.session.commit()

    # redis-t csak a sikeres commit után frissítjük, hogy ne legyen eltérés a db és a cache között
    r.set(f"credits:{userid}", credits)
    r.set(f"user_plan:{userid}", plan)

    if invoice.get("billing_reason") == "subscription_create":
        notify(useracc.email, "Successfully subscribed!", "Hi there, the payment was successful.")
    else:
        notify(useracc.email, "Subscription renewed", "Hi there, your subscription has been renewed and your credits have been refilled.")

    return "Success", 200


def handle_payment_failed(invoice):
    subscription_id = invoice.get("subscription")
    customer_id = invoice.get("customer")

    if not subscription_id:
        return "Not a subscription invoice, skipped.", 200

    metadata = stripe.Subscription.retrieve(subscription_id)["metadata"]
    userid = parse_userid(metadata, "Payment failed webhook.")
    if userid is None:
        return "Malformed metadata.", 200

    useracc = UserAccount.query.filter_by(id=userid).first()
    if useracc is None:
        log(mode, "ERROR", f"Account doesnt exist for customer: {customer_id}, ID: {subscription_id}")
        return "Account doesn't exist!", 200

    subscription = Subscriptions.query.filter_by(user_id=userid).first()
    if subscription is None:
        log(mode, "INFO", f"There is no subscription in database ID: {subscription_id}. Webhook: Payment failed.")
        return "No subscription in DB.", 200

    # Update existing subscription status
    subscription.status = "payment_failed"
    subscription.updated_at = datetime.now()

    db.session.add(
        Transactions(user_id=useracc.id, text=f"Payment failed userid in metadata {userid}, userid in db: {useracc.id}. Subscription id: {subscription_id}.", credits_cost=0,
                     created_at=datetime.now(), type="stripe_payment"))
    db.session.commit()

    log(mode, "INFO", f"Subscription {subscription_id} payment failed.")
    notify(useracc.email, "Subscription payment fail", "Hi there, this is an automated message that your payment has failed. You may have to resubscribe.")

    return "Success", 200


def handle_subscription_deleted(stripe_subscription):
    # itt az event objektuma maga a subscription, nem invoice
    subscription_id = stripe_subscription.get("id")
    userid = parse_userid(stripe_subscription["metadata"], "Subscription deleted webhook.")
    if userid is None:
        return "Malformed metadata.", 200

    useracc = UserAccount.query.filter_by(id=userid).first()
    if useracc is None:
        log(mode, "ERROR", f"Account doesnt exist for user id {userid}, ID: {subscription_id}")
        return "Account doesn't exist!", 200

    subscription = Subscriptions.query.filter_by(user_id=userid).first()
    if subscription is None:
        log(mode, "INFO", f"There is no subscription for user id {userid}, ID: {subscription_id}. Subscription deleted.")
        return "No subscription in DB.", 200

    # ha közben már új előfizetése van a usernek, a régi törlése nem írhatja felül
    if subscription.stripe_subscription_id != subscription_id:
        log(mode, "INFO", f"Old subscription {subscription_id} deleted for user id {userid}, the current one ({subscription.stripe_subscription_id}) is untouched.")
        return "Old subscription, skipped.", 200

    # Update existing subscription status
    subscription.status = "deleted"
    subscription.updated_at = datetime.now()

    db.session.add(
        Transactions(user_id=useracc.id,
                     text=f"Subscription deleted for {useracc.id}. Subscription id: {subscription_id}.",
                     credits_cost=0,
                     created_at=datetime.now(), type="stripe_payment"))
    db.session.commit()

    log(mode, "INFO", f"Subscription {subscription_id} deleted for user id: {userid}.")
    notify(useracc.email, "Successfully unsubscribed", "Hey! We are sorry to see you go! Your subscription has been successfully canceled, and you will no longer be charged in the future.")

    return "Success", 200


@app.route("/cancel-subscription", methods=["POST"])
def cancel_subscription():
    user_id = session.get("userid")
    if not user_id:
        flash("You have to be logged in to cancel your subscription!")
        return redirect("/login")

    subscription_db = Subscriptions.query.filter_by(user_id=user_id).first()
    if subscription_db is None or subscription_db.status == "deleted" or not subscription_db.stripe_subscription_id:
        flash("You are not subscribed.")
        return redirect("/settings")

    expire_date = datetime.fromtimestamp(subscription_db.expire_int).strftime('%m/%d')

    if subscription_db.status == "cancelled":
        flash(f"You have already canceled your subscription. You will not be charged and you will not be able to resubscribe until {expire_date}")
        return redirect("/settings")

    try:
        # nem azonnal töröljük, hanem a kifizetett időszak végén jár le, addig a user még használhatja
        # az időszak végén jön a customer.subscription.deleted webhook, ami "deleted"-re állítja
        stripe.Subscription.modify(subscription_db.stripe_subscription_id, cancel_at_period_end=True)
        subscription_db.status = "cancelled"
        subscription_db.updated_at = datetime.now()
        db.session.commit()
        log(mode, "INFO", f"Cancel subscription success for userid {user_id}.")
    except Exception as e:
        log(mode, "ERROR", f"Cancel subscription error for userid {user_id}, error: {e}")
        flash("Unexpected error happened while canceling your subscription! Please contact our support team!")
        return redirect("/settings")

    flash(f"You have succesfully canceled your subscription. You can continue using our service until {expire_date}. You can't resubscribe until that date.")
    return redirect("/settings")
