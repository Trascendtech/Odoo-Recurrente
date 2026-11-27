import logging
import pprint

from werkzeug import urls

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment_recurrente import const
from odoo.addons.payment_recurrente.controllers.main import RecurrenteController

_logger = logging.getLogger(__name__)

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    id_recurrente_checkout = fields.Char(string="Recurrente Checkout ID")
    url_recurrente_checkout = fields.Char(string="Recurrente Checkout URL")
    product_recurrente_checkout = fields.Char(string="Recurrente Checkout Product")
    payment_intent_failure_reasons = fields.Html(string="Payment Intent Failure Reasons", default="<ul></ul>", help="Here you can find the multiple reasons why the client did have payment problems.")
    recurrente_intent_id = fields.Char(
        string="Recurrente Intent ID", readonly=True, copy=False,
        help="Payment intent (in_…) of the paid checkout: refunds and the fee details use it.",
    )
    recurrente_fee = fields.Monetary(
        string="Recurrente Fee",
        currency_field='currency_id',
        groups='base.group_system',
        help="The gateway processing fee charged by Recurrente."
    )
    recurrente_refunded_to_account = fields.Monetary(
        string="Debited from Recurrente Account",
        currency_field='currency_id',
        groups='base.group_system',
        help="Refunds: amount Recurrente took from the merchant balance (may differ from the "
             "amount returned to the customer because of fees).",
    )
    recurrente_vat_withheld = fields.Monetary(
        string="Recurrente VAT Withheld",
        currency_field='currency_id',
        groups='base.group_system',
        help="The VAT amount withheld by Recurrente (Guatemala regulation)."
    )

    def _get_specific_rendering_values(self, processing_values):
        """ Override of payment to return Recurrente-specific rendering values.

        Note: self.ensure_one() from `_get_processing_values`

        :param dict processing_values: The generic and specific processing values of the transaction
        :return: The dict of provider-specific processing values.
        :rtype: dict
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'recurrente':
            return res

        payload = {
            'success_url': f"{self.get_base_url()}{RecurrenteController._request_url}?tx_ref={self.reference}&status=request_success",
            'cancel_url': f"{self.get_base_url()}{RecurrenteController._request_url}?tx_ref={self.reference}&status=request_cancel",
        }
        if self.operation == 'validation':
            # Save the card without charging it: Recurrente authorizes zero and returns the
            # payment method (pay_m_…) that later one-time payments charge.
            payload['mode'] = 'setup'
        else:
            if self.tokenize:
                # Only a setup checkout is known to give a card for later charges: the card is
                # saved from "Payment methods" (validation), not while paying.
                _logger.info("Recurrente: %s pays without saving the card.", self.reference)
                self.tokenize = False
            payload['items'] = [{
                'name': self.reference,
                'currency': self.currency_id.name,
                'amount_in_cents': payment_utils.to_minor_currency_units(
                    self.amount, self.currency_id
                ),
                'quantity': 1,
            }]
        payload['metadata'] = self._recurrente_reference_metadata()

        payment_link_data = self.provider_id._recurrente_make_request('checkouts', payload=payload)

        self.id_recurrente_checkout = payment_link_data["id"]
        self.url_recurrente_checkout = payment_link_data["checkout_url"]

        # Extract the payment link URL and embed it in the redirect form.
        rendering_values = {
            'api_url': payment_link_data['checkout_url'],
        }
        return rendering_values

    def _recurrente_reference_metadata(self):
        """ ``correlative`` and ``number`` of references like INV/2026/00012-1 (the year and the
        sequence); empty for other references (card validations, sales orders). """
        parts = self.reference.split("/")
        if len(parts) < 3:
            return {}
        correlative = parts[1] + parts[2].split('-')[0]  # without the retry suffix (-1, -2)
        if not correlative.isdigit():
            return {}
        return {'correlative': correlative, 'number': int(correlative)}

    def _get_tx_return_data(self, notification_data):
        """ Find the transaction based on the request data.

        :param dict notification_data: The request data sent by the provider.
        :return: The transaction, if found.
        :rtype: recordset of `payment.transaction`
        """
        reference = notification_data['tx_ref']
        tx = self.search([('reference', '=', reference), ('provider_code', '=', 'recurrente')])
        if not tx:
            raise ValidationError(
                "Recurrente: " + _(f"No transaction found matching reference {reference}.")
            )
        return tx

    def _process_return_data(self, notification_data):
        """ Update the transaction state based on the request data.

        Note: `self.ensure_one()`

        :param dict notification_data: The request data sent by the provider.
        :return: None
        """
        self.ensure_one()
        request_status = notification_data["status"]
        if request_status == 'request_success' and self.state in ('draft', 'pending'):
            # Webhooks may come later (and never in sandbox): ask the API right away.
            self.action_check_payment_status()
        elif request_status in const.PAYMENT_STATUS_MAPPING['pending'] and self.state == 'draft':
            self._set_pending(_("The payment is in process."))
        elif request_status in const.PAYMENT_STATUS_MAPPING['cancel'] and self.state == 'draft':
            self._set_canceled(_("The client went back from the Recurrente's checkout."))

    def _handle_return_data(self, notification_data):
        """ Match the transaction with the notification data, update its state and return it.

        :param dict notification_data: The notification data sent by the provider.
        :return: The transaction.
        :rtype: recordset of `payment.transaction`
        """
        tx = self._get_tx_return_data(notification_data)
        tx._process_return_data(notification_data)
        # tx._execute_callback()
        return tx

    def _save_failure_reason(self, failure_reason):
        timezone = self._context.get('tz') or self.env.user.tz or 'America/Guatemala'
        self_tz = self.with_context(tz=timezone)
        datetime = fields.Datetime.context_timestamp(self_tz, fields.Datetime.now())

        failure_reason = f"<li>{datetime:%Y-%m-%d %H:%M:%S} {failure_reason}</li>"
        actual_reasons = str(self.payment_intent_failure_reasons) or "<ul></ul>"

        if "<ul>" in actual_reasons:
            actual_reasons = actual_reasons.replace("<ul>", f"<ul>{failure_reason}")
        else:
            actual_reasons = f"<ul>{failure_reason}</ul>"
        
        self.payment_intent_failure_reasons = actual_reasons

    def _get_tx_from_webhook_data(self, notification_data):
        """ Find the transaction based on the webhook data.

        :param dict notification_data: The notification data sent by the provider.
        :return: The transaction, if found.
        :rtype: recordset of `payment.transaction`
        """

        checkout = notification_data.get('checkout')
        checkout_id = checkout.get('id') if checkout else False
        if not checkout_id:
            raise ValidationError("Recurrente: " + _("Received data with missing checkout ID."))

        tx = self.search([('id_recurrente_checkout', '=', checkout_id), ('provider_code', '=', 'recurrente')])
        if not tx:
            raise ValidationError(
                "Recurrente: " + _(f"No transaction found matching checkout ID {checkout_id}.")
            )
        return tx

    def _process_webhook_data(self, notification_data):
        """ Update the transaction state and the provider reference based on the webhook data.

        Note: `self.ensure_one()`

        :param dict notification_data: The webhook data sent by the provider.
        :return: None
        """
        self.ensure_one()

        payment = notification_data.get("payment")
        payment_status = notification_data['event_type'].lower()
        if payment_status.startswith('setup_intent.'):
            # A card saved (or not) by a setup checkout: the checkout says which card.
            if payment_status in const.PAYMENT_STATUS_MAPPING['cancel']:
                if self.state in ('draft', 'pending'):
                    self._set_canceled(_("Recurrente could not verify the card."))
            else:
                self.action_check_payment_status()
            return

        payment_id = payment.get("id") if payment else False
        if not payment_id:
            raise ValidationError("Recurrente: " + _("Received data with missing payment."))

        # Update the provider reference.
        self.provider_reference = payment_id

        # Extract and save fee and vat_withheld if available
        fee_cents = None
        vat_cents = None
        if isinstance(payment, dict):
            fee_cents = payment.get("fee")
            vat_cents = payment.get("vat_withheld")
        if fee_cents is None:
            fee_cents = notification_data.get("fee")
        if vat_cents is None:
            vat_cents = notification_data.get("vat_withheld")

        if fee_cents is not None:
            self.recurrente_fee = float(fee_cents) / 100.0
        if vat_cents is not None:
            self.recurrente_vat_withheld = float(vat_cents) / 100.0

        # Update the payment state.
        failure_reason = notification_data.get('failure_reason', 'No reason.')
        if payment_status in const.PAYMENT_STATUS_MAPPING['pending']:
            if self.state == "draft":
                self._set_pending()
            if failure_reason:
                self._save_failure_reason(failure_reason)
        elif payment_status in const.PAYMENT_STATUS_MAPPING['done']:
            # Never trust the webhook body alone: the checkout id is visible to the customer.
            if self._recurrente_checkout_status() == const.CHECKOUT_PAID:
                self._set_done()
                self._recurrente_fetch_intent()
            else:
                _logger.warning(
                    "Recurrente: ignoring '%s' for %s: the API does not report the checkout as paid.",
                    payment_status, self.reference,
                )
        elif payment_status in const.PAYMENT_STATUS_MAPPING['error']:
            if self._recurrente_checkout_status() == const.CHECKOUT_PAID:
                _logger.warning(
                    "Recurrente: ignoring '%s' for %s: the API reports the checkout as paid.",
                    payment_status, self.reference,
                )
                return
            self._set_error(_(
                f"An error occurred during the processing of your payment (status {payment_status}). Please try again.\nReason: {notification_data.get('failure_reason', 'No reason.')}",
            ))
        else:
            # Recurrente sends more events than the ones of a payment (refunds, disputes, new
            # names): never turn a transaction into an error because of one we do not know.
            _logger.info(
                "Recurrente: event '%s' ignored for transaction %s.", payment_status, self.reference,
            )

    def _handle_webhook_data(self, notification_data):
        """ Match the transaction with the notification data, update its state and return it.

        :param str provider_code: The code of the provider handling the transaction.
        :param dict notification_data: The notification data sent by the provider.
        :return: The transaction.
        :rtype: recordset of `payment.transaction`
        """
        tx = self._get_tx_from_webhook_data(notification_data)
        tx._process_webhook_data(notification_data)
        # tx._execute_callback()
        return tx

    def _recurrente_checkout_status(self):
        """ Return the checkout status according to the API (``None`` if it cannot be read). """
        self.ensure_one()
        if not self.id_recurrente_checkout:
            return None
        try:
            data = self.provider_id._recurrente_make_request(
                f"checkouts/{self.id_recurrente_checkout}", method="GET"
            )
        except ValidationError:
            _logger.exception("Recurrente: could not read checkout %s", self.id_recurrente_checkout)
            return None
        return data.get('status')

    @api.model
    def _cron_recurrente_check_status(self):
        """ Poll open checkouts and refunds: covers lost webhooks and sandbox (no webhooks). """
        since = fields.Datetime.now() - timedelta(days=const.POLL_DAYS)
        txs = self.search([
            ('provider_code', '=', 'recurrente'),
            ('state', 'in', ('draft', 'pending')),
            ('create_date', '>=', since),
        ])
        for tx in txs:
            try:
                with self.env.cr.savepoint():
                    if tx.operation == 'refund':
                        tx._recurrente_check_refund_status()
                    elif tx.id_recurrente_checkout:
                        tx.action_check_payment_status()
            except Exception:
                _logger.exception("Recurrente: status check failed for %s", tx.reference)

    def action_check_payment_status(self):
        """ Manually check the payment status from Recurrente API. """
        self.ensure_one()
        if self.provider_code != 'recurrente' or not self.id_recurrente_checkout:
            _logger.warning(f"No checkout ID for transaction {self.reference}")
            return

        try:
            _logger.info(f"Checking status for checkout {self.id_recurrente_checkout}")
            checkout_data = self.provider_id._recurrente_make_request(
                f"checkouts/{self.id_recurrente_checkout}", method="GET"
            )
            _logger.info(f"Checkout data: {checkout_data}")
            status = checkout_data.get("status")
            if status == "paid":
                payment_info = checkout_data.get("payment")
                if isinstance(payment_info, dict) and payment_info.get("id"):
                    self.provider_reference = payment_info.get("id")
                if self.operation == 'validation':
                    self._recurrente_tokenize(checkout_data)
                else:
                    self._recurrente_apply_intent(
                        self._recurrente_intent_from_checkout(checkout_data)
                    )

                self._set_done()
            elif status == "unpaid":
                # If it's already pending, we don't need to do anything
                # If it's still draft, it stays draft until paid or webhook
                pass
            elif status == "payment_in_progress":
                self._set_pending()
            # The API also returns 'expired' or 'canceled'
            elif status in ["expired", "canceled"]:
                self._set_canceled()
        except Exception as e:
            _logger.error(f"Error checking payment status for {self.id_recurrente_checkout}: {e}")

    def _create_payment(self, **extra_create_values):
        """Override of `account_payment` to book Recurrente's fee and VAT withholding.

        The payment is created for the net amount actually deposited by Recurrente; the fee
        (expense) and the VAT withheld (asset) become write-off lines so the customer is still
        credited the gross amount. Amounts whose account is not configured on the provider are
        left inside the payment (no write-off), so the entry always stays balanced.
        """
        self.ensure_one()
        if self.provider_code != 'recurrente' or self.amount <= 0:
            return super()._create_payment(**extra_create_values)

        provider = self.provider_id
        tx_currency = self.currency_id
        company = provider.company_id or self.company_id
        conversion_date = (self.last_state_change or fields.Datetime.now()).date()

        write_off_line_vals = []
        for amount, account, partner, label in (
            (
                self.recurrente_fee,
                provider.recurrente_commission_account_id,
                provider.recurrente_commission_partner_id,
                _("Recurrente gateway fee - %s", self.reference),
            ),
            (
                self.recurrente_vat_withheld,
                provider.recurrente_vat_withholding_account_id,
                provider.recurrente_vat_withholding_partner_id,
                _("Card VAT withholding - %s", self.reference),
            ),
        ):
            if not amount or not account:
                continue
            write_off_line_vals.append({
                'name': label,
                'account_id': account.id,
                'partner_id': (partner or self.partner_id.commercial_partner_id).id,
                'currency_id': tx_currency.id,
                'amount_currency': amount,
                'balance': tx_currency._convert(amount, company.currency_id, company, conversion_date),
            })

        if not write_off_line_vals:
            return super()._create_payment(**extra_create_values)

        net_amount = self.amount - sum(line['amount_currency'] for line in write_off_line_vals)
        return super()._create_payment(**{
            **extra_create_values,
            'amount': net_amount,
            'write_off_line_vals': write_off_line_vals,
        })

    # === PAYMENT INTENT === #

    @staticmethod
    def _recurrente_intent_from_checkout(checkout_data):
        """ ``latest_intent.id`` (in_…) of a checkout read from GET /checkouts/{id}. """
        intent = (checkout_data or {}).get('latest_intent')
        intent_id = intent.get('id') if isinstance(intent, dict) else None
        return intent_id if isinstance(intent_id, str) and intent_id.startswith('in_') else None

    def _recurrente_apply_intent(self, intent_id):
        """ Store the payment intent and its fee and VAT withheld (GET /intents/{id},
        ``details.fee`` and ``details.vat_withheld`` in cents). Never blocks the payment. """
        self.ensure_one()
        if not intent_id:
            return
        self.recurrente_intent_id = intent_id
        try:
            data = self.provider_id._recurrente_make_request(f"intents/{intent_id}", method="GET")
        except ValidationError:
            _logger.warning("Recurrente: could not read intent %s for %s", intent_id, self.reference)
            return
        details = (data or {}).get('details') or {}
        if details.get('fee') is not None:
            self.recurrente_fee = float(details['fee']) / 100.0
        if details.get('vat_withheld') is not None:
            self.recurrente_vat_withheld = float(details['vat_withheld']) / 100.0

    def _recurrente_find_payment_intent(self, payment_id):
        """ Intent (in_…) of a one-time payment (on_…): the intent whose
        ``payment.paymentable.id`` is the payment, in the newest intents (GET /intents is
        sorted by date and has no filter; a charge is looked up right after it is made). Never
        blocks the payment: ``None`` when it cannot be found. """
        self.ensure_one()
        if not (isinstance(payment_id, str) and payment_id.startswith('on_')):
            return None
        for page in range(1, const.INTENT_SEARCH_PAGES + 1):
            try:
                intents = self.provider_id._recurrente_make_request(
                    'intents', payload={'page': page, 'items': const.INTENT_PAGE_SIZE},
                    method='GET',
                )
            except ValidationError:
                _logger.warning("Recurrente: could not list intents for %s", self.reference)
                return None
            if not isinstance(intents, list) or not intents:
                return None
            for intent in intents:
                payment = intent.get('payment') if isinstance(intent, dict) else None
                paymentable = payment.get('paymentable') if isinstance(payment, dict) else None
                if isinstance(paymentable, dict) and paymentable.get('id') == payment_id:
                    intent_id = intent.get('id')
                    if isinstance(intent_id, str) and intent_id.startswith('in_'):
                        return intent_id
        _logger.warning("Recurrente: no intent found for one-time payment %s (%s).",
                        payment_id, self.reference)
        return None

    def _recurrente_fetch_intent(self):
        """ The payment intent of the transaction, read from the API when it was not stored:
        from its checkout, or, for a charge on a saved card, from its one-time payment. """
        self.ensure_one()
        if (not self.recurrente_intent_id and not self.id_recurrente_checkout
                and (self.provider_reference or '').startswith('on_')):
            self._recurrente_apply_intent(self._recurrente_find_payment_intent(self.provider_reference))
        if not self.recurrente_intent_id and self.id_recurrente_checkout:
            try:
                checkout = self.provider_id._recurrente_make_request(
                    f"checkouts/{self.id_recurrente_checkout}", method="GET"
                )
            except ValidationError:
                return None
            self._recurrente_apply_intent(self._recurrente_intent_from_checkout(checkout))
        return self.recurrente_intent_id

    # === TOKENS === #

    def _recurrente_tokenize(self, checkout_data):
        """ Save the card of a paid setup checkout as a token of the customer: only the
        payment method reference (pay_m_…), its last 4 digits, network and expiry. """
        self.ensure_one()
        method = (checkout_data or {}).get('payment_method') or {}
        method_id = method.get('id') if isinstance(method, dict) else None
        if not (isinstance(method_id, str) and method_id.startswith('pay_m_')):
            _logger.warning("Recurrente: checkout of %s has no saved card.", self.reference)
            return self.env['payment.token']
        card = method.get('card') or {}
        intent = checkout_data.get('latest_intent') or {}
        customer = intent.get('customer') if isinstance(intent, dict) else None
        if isinstance(customer, dict):
            customer = customer.get('id')
        expiry = ""
        if card.get('expiration_month') and card.get('expiration_year'):
            expiry = f"{int(card['expiration_month']):02d}/{card['expiration_year']}"
        Token = self.env['payment.token'].sudo()
        token = Token.search([
            ('provider_id', '=', self.provider_id.id),
            ('partner_id', '=', self.partner_id.id),
            ('provider_ref', '=', method_id),
        ], limit=1)
        if not token:
            token = Token.create({
                'provider_id': self.provider_id.id,
                'payment_method_id': self.payment_method_id.id,
                'partner_id': self.partner_id.id,
                'provider_ref': method_id,
                'payment_details': card.get('last4') or '',
                'recurrente_customer_id': customer if isinstance(customer, str) else False,
                'recurrente_card_network': card.get('network') or False,
                'recurrente_card_expiry': expiry or False,
            })
            _logger.info("Recurrente: card saved as token %s for %s.", token.id, self.reference)
        self.token_id = token
        return token

    def _send_payment_request(self):
        """ Override of `payment` to charge a saved card: a one-time payment on its payment
        method, without the customer. The transaction reference is the idempotency key, so a
        repeated request never charges twice. """
        super()._send_payment_request()
        if self.provider_code != 'recurrente':
            return
        if not self.token_id:
            raise UserError("Recurrente: " + _("The transaction is not linked to a token."))
        payload = {
            'payment_method_id': self.token_id.provider_ref,
            'items': [{
                'name': self.reference,
                'currency': self.currency_id.name,
                'amount_in_cents': payment_utils.to_minor_currency_units(
                    self.amount, self.currency_id
                ),
                'quantity': 1,
            }],
        }
        try:
            data = self.provider_id._recurrente_make_request(
                'one_time_payments', payload=payload,
                headers={'Idempotency-Key': f"odoo-{self.reference}"},
            )
        except ValidationError as error:
            # Declined card, card of another customer, disputed card, 3DS required...: the
            # payment failed; the invoice keeps its payment link (dunning).
            self._save_failure_reason(str(error))
            self._set_error(str(error))
            return
        self._recurrente_apply_one_time_payment(data)

    def _recurrente_apply_one_time_payment(self, data):
        self.ensure_one()
        if data.get('id'):
            self.provider_reference = data['id']
        status = (data.get('status') or '').lower()
        if status == 'paid':
            # The intent gives the fee and the VAT withheld (booked by _create_payment) and is
            # what refunds need; the response of the one-time payment does not include it.
            self._recurrente_apply_intent(self._recurrente_find_payment_intent(data.get('id')))
            self._set_done()
        elif status in const.ONE_TIME_PAYMENT_FAILED:
            self._set_error(_("Recurrente did not charge the card (status %s).", status))
        else:
            # Not documented by Recurrente: left pending, never taken as paid.
            _logger.warning("Recurrente: one-time payment %s of %s is %r.",
                            data.get('id'), self.reference, status)
            self._set_pending(_("Recurrente has not confirmed the payment yet."))

    # === REFUNDS === #

    def _send_refund_request(self, amount_to_refund=None):
        """ Override of `payment` to refund a whole Recurrente payment through the API. """
        if self.provider_code != 'recurrente':
            return super()._send_refund_request(amount_to_refund=amount_to_refund)
        self.ensure_one()
        if amount_to_refund and self.currency_id.compare_amounts(amount_to_refund, self.amount) != 0:
            raise UserError(_("Recurrente only refunds the full amount of a payment."))
        intent_id = self._recurrente_fetch_intent()
        if not intent_id:
            raise UserError(_(
                "The transaction %s has no Recurrente payment intent: it cannot be refunded from Odoo.",
                self.reference,
            ))
        refund_tx = super()._send_refund_request(amount_to_refund=amount_to_refund)
        # POST /refunds with the unified intent id (in_…); the key avoids a double refund if the
        # request is repeated.
        data = self.provider_id._recurrente_make_request(
            'refunds', payload={'intent_id': intent_id},
            headers={'Idempotency-Key': f"odoo-{refund_tx.reference}"},
        )
        refund_tx._recurrente_apply_refund_data(data)
        return refund_tx

    def _recurrente_apply_refund_data(self, data):
        self.ensure_one()
        if data.get('id'):
            self.provider_reference = data['id']
        account_cents = data.get('account_refunded_amount_in_cents')
        if account_cents is not None:
            self.recurrente_refunded_to_account = -float(account_cents) / 100.0
        status = (data.get('status') or '').lower()
        if status in const.REFUND_DONE:
            self._set_done()
        elif status in const.REFUND_PENDING:
            self._set_pending()
        elif status in const.REFUND_FAILED:
            self._set_error(_("Recurrente rejected the refund (status %s).", status))
        else:
            _logger.warning("Recurrente: unknown refund status %r for %s", status, self.reference)
            self._set_pending()

    def _recurrente_check_refund_status(self):
        self.ensure_one()
        if not self.provider_reference:
            return
        data = self.provider_id._recurrente_make_request(
            f"refunds/{self.provider_reference}", method='GET'
        )
        self._recurrente_apply_refund_data(data)
