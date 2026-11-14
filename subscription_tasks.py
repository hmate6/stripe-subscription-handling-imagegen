from datetime import datetime
from time import time

from celery import shared_task

from app_init import app, Subscriptions, UserAccount, db, r

mode = "subscription_management"

# ennyi idő türelmi idő van a lejárat után, mielőtt visszaállítjuk free-re
# (a megújításos invoice.payment_succeeded webhook néha késik)
GRACE_PERIOD_SECONDS = 7200

# lejáratkor ennyi kreditet veszünk le, de 0 alá nem mehet
EXPIRED_PLAN_CREDIT_REDUCTION = {
    "pro": 800,
    "plus": 4000,
}

LOCK_KEY = "running:downgrade_plans"


def write_log(msg):
    print(msg)
    with open(f"./logs/{mode}.txt", mode="a", encoding="UTF-8") as f:
        f.write(msg + "\n")


@shared_task
def downgrade_expired_subscriptions():
    with app.app_context():
        # nx=True: csak akkor állítja be, ha még nem létezik, így két worker nem tudja egyszerre elindítani
        if not r.set(LOCK_KEY, 1, nx=True, ex=1000):
            return 0

        try:
            return _downgrade_expired_subscriptions()
        finally:
            r.delete(LOCK_KEY)


def _downgrade_expired_subscriptions():
    print(datetime.now(), "Checking subscriptions")
    today = datetime.now()

    # Fetch subscriptions that have expired and are not processed yet
    # aktív előfizetésnél a webhook minden megújításkor kitolja az expire_int-et, ezért ha egy aktív
    # előfizetés itt megjelenik, akkor a megújítás nem jött meg -> ugyanúgy le kell állítani
    expired_subscriptions = Subscriptions.query.filter(
        Subscriptions.expire_int < (int(time()) - GRACE_PERIOD_SECONDS),
        Subscriptions.status != "expired",
        ## Subscriptions.status != "cancelled"
        # Subscriptions.status.notin_(["active"])
    ).all()

    downgraded = 0
    for subscription in expired_subscriptions:
        user = UserAccount.query.filter_by(id=subscription.user_id).first()
        if user:
            if user.plan in EXPIRED_PLAN_CREDIT_REDUCTION:
                user.credits = max(0, user.credits - EXPIRED_PLAN_CREDIT_REDUCTION[user.plan])
            else:
                user.credits = 0  # Reset credits to 0 for non-paying users
            user.plan = "free"  # Downgrade the plan to 'free'

            # redis cache-t is frissíteni kell, különben a user továbbra is a régi plant/krediteket látná
            r.set(f"user_plan:{user.id}", "free")
            r.set(f"credits:{user.id}", user.credits)

            write_log(f"{datetime.now()} Subscription expired for user {user.email}, id: {user.id}")
            downgraded += 1
        else:
            write_log(f"{datetime.now()} Subscription {subscription.stripe_subscription_id} expired, but user id {subscription.user_id} doesn't exist")

        # "expired"-re állítjuk, hogy a következő futásnál ne vonja le újra a krediteket
        # (a "cancelled" státuszt a lemondás használja, amikor még fut a kifizetett időszak)
        subscription.previous_status = subscription.status
        subscription.status = "expired"
        subscription.updated_at = today

    db.session.commit()
    return downgraded


# celery beat-ben 6 óránként fut, pl.:
# app.conf.beat_schedule = {
#     "downgrade-expired-subscriptions": {
#         "task": "subscription_tasks.downgrade_expired_subscriptions",
#         "schedule": 6 * 60 * 60,
#     },
# }
