SUPPORTED_CURRENCIES = (
    'GTQ',
    'USD',
)

DEFAULT_PAYMENT_METHODS_CODES = [
    'recurrente',
]

PAYMENT_STATUS_MAPPING = {
    'pending': ('bank_transfer_intent.pending', 'payment_intent.failed', 'subscription.past_due'),
    'done': ('bank_transfer_intent.succeeded', 'payment_intent.succeeded', 'subscription.create'),
    'cancel': ('request_cancel', 'subscription.cancel', 'setup_intent.cancelled'),
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
