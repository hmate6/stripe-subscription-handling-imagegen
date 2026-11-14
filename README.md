# ImageGen – Stripe subscriptions

Stripe subscription handling from my ImageGen project (Flask + Celery + Redis), from when it was in production and users could subscribe to the **Pro** and **Plus** plans. The subscriptions were later removed from the main project, so this repo is kept as a reference.

> Not runnable on its own: `app`, `db`, the Redis client and the models (`UserAccount`, `Subscriptions`, `Transactions`) come from the main ImageGen app.

## Files

### `stripe_subscriptions.py`
- `/create-checkout-session?plan=pro|plus` – creates a Stripe Checkout session. Blocked while the user still has a running paid period, to avoid two subscriptions at the same time.
- Webhook endpoint (signature verified):
  - `invoice.payment_succeeded` – saves or updates the subscription and refills credits (Pro: 1500, Plus: 5000) on the first payment and on every renewal
  - `invoice.payment_failed` – status `payment_failed`, email to the user
  - `customer.subscription.deleted` – status `deleted`
- `/cancel-subscription` – cancels at the end of the paid period (`cancel_at_period_end`)

### `subscription_tasks.py`
Celery task (every 6 hours) that downgrades users to `free` after their paid period has expired (2 hour grace period), reduces their credits and marks the subscription as `expired`. If a late renewal payment still arrives, the webhook reactivates the subscription.

## Subscription statuses
`active` → `cancelled` (user canceled, period still running) / `payment_failed` → `deleted` (Stripe ended it) → `expired` (downgraded by the task)

## Config
Environment variables are listed with placeholders in `.env.example`.
