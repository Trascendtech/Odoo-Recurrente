SUPPORTED_CURRENCIES = (
    'GTQ',
    'USD',
)

DEFAULT_PAYMENT_METHODS_CODES = [
    'recurrente',
]

PAYMENT_STATUS_MAPPING = {
    'pending': (
        'bank_transfer_intent.pending', 'intent.failed', 'payment_intent.failed', 'subscription.past_due',
    ),
    # intent.succeeded is the current event; payment_intent.succeeded, the one of older accounts.
    'done': (
        'bank_transfer_intent.succeeded', 'intent.succeeded', 'payment_intent.succeeded',
        'subscription.create',
    ),
    'cancel': ('request_cancel', 'subscription.cancel', 'setup_intent.cancelled'),
    # setup_intent.succeeded: a card saved by a setup checkout (validation), handled apart.
    'error': ('bank_transfer_intent.failed',),
}

# Webhooks older or newer than this (seconds) are rejected when a signing secret is set.
WEBHOOK_TOLERANCE_SECONDS = 5 * 60

# Checkout statuses returned by GET /checkouts/{id}.
CHECKOUT_PAID = 'paid'
CHECKOUT_IN_PROGRESS = 'payment_in_progress'
CHECKOUT_CLOSED = ('expired', 'canceled')

# Refund statuses returned by POST /refunds and GET /refunds/{id}.
REFUND_DONE = ('succeeded',)
REFUND_PENDING = ('pending', 'processing', 'requires_action')
REFUND_FAILED = ('failed', 'canceled')

# Open transactions younger than this are polled by the status cron.
POLL_DAYS = 3

# One-time payment statuses (charges on a saved card) that mean it was not charged; anything
# other than these and 'paid' is left pending.
ONE_TIME_PAYMENT_FAILED = ('failed', 'canceled', 'cancelled', 'declined')

# GET /intents (newest first) is searched for the intent of a one-time payment (no filter).
INTENT_PAGE_SIZE = 50
INTENT_SEARCH_PAGES = 4
