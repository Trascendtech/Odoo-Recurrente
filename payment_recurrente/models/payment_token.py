from odoo import fields, models


class PaymentToken(models.Model):
    _inherit = 'payment.token'

    # Only what Recurrente returns about a saved card: its references and what is shown to the
    # customer. The card number and CVV never reach Odoo (they are typed in Recurrente's
    # checkout); provider_ref holds the payment method (pay_m_…) charged by later payments.
    recurrente_customer_id = fields.Char(
        string="Recurrente Customer", readonly=True,
        help="Customer (cus_…) Recurrente created when the card was saved.",
    )
    recurrente_card_network = fields.Char(string="Card Network", readonly=True)
    recurrente_card_expiry = fields.Char(
        string="Card Expiry", readonly=True, help="MM/YYYY, to warn before the card expires.",
    )
