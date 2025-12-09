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
