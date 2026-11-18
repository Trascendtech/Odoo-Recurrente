import logging
import pprint

from werkzeug import urls

from odoo import _, fields, models
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
    recurrente_fee = fields.Monetary(
        string="Recurrente Fee",
        currency_field='currency_id',
        groups='base.group_system',
        help="The gateway processing fee charged by Recurrente."
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

        # Prepare the payload for checkout creation
        parts = self.reference.split("/")
        year = parts[1]
        seq = parts[2].split('-')[0]  # Remove retry suffix like -1, -2
        correlative = year + seq
        number = int(correlative)

        # Build items array as per API
        items = [{
            'name': self.reference,  # Use reference as item name
            'currency': self.currency_id.name,
            'amount_in_cents': int(self.amount * 100),  # Convert to cents
            'quantity': 1,
        }]

        payload = {
            'items': items,
            'success_url': f"{self.get_base_url()}{RecurrenteController._request_url}?tx_ref={self.reference}&status=request_success",
            'cancel_url': f"{self.get_base_url()}{RecurrenteController._request_url}?tx_ref={self.reference}&status=request_cancel",
        }

        # Add metadata if needed
        payload['metadata'] = {
            'correlative': correlative,
            'number': number,
        }

        payment_link_data = self.provider_id._recurrente_make_request('checkouts', payload=payload)

        self.id_recurrente_checkout = payment_link_data["id"]
        self.url_recurrente_checkout = payment_link_data["checkout_url"]

        # Extract the payment link URL and embed it in the redirect form.
        rendering_values = {
            'api_url': payment_link_data['checkout_url'],
        }
        return rendering_values

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
        if request_status in const.PAYMENT_STATUS_MAPPING['pending'] and self.state == 'draft':
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
        payment_status = notification_data['event_type'].lower()
        failure_reason = notification_data.get('failure_reason', 'No reason.')
        if payment_status in const.PAYMENT_STATUS_MAPPING['pending']:
            if self.state == "draft":
                self._set_pending()
            if failure_reason:
                self._save_failure_reason(failure_reason)
        elif payment_status in const.PAYMENT_STATUS_MAPPING['done']:
            self._set_done()
        elif payment_status in const.PAYMENT_STATUS_MAPPING['error']:
            self._set_error(_(
                f"An error occurred during the processing of your payment (status {payment_status}). Please try again.\nReason: {notification_data.get('failure_reason', 'No reason.')}",
            ))
        else:
            _logger.warning(
                f"Received data with invalid payment status ({payment_status}) for transaction with reference {self.reference}.",
            )
            self._set_error("Recurrente: " + _(f"Unknown payment status: {payment_status}"))

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
                    payment_id = payment_info.get("id")
                    self.provider_reference = payment_id
                    
                    # Fetch payment details from Recurrente API using GET invoices/{payment_id}
                    try:
                        _logger.info(f"Fetching invoice/payment details for {payment_id}")
                        payment_details = self.provider_id._recurrente_make_request(
                            f"invoices/{payment_id}", method="GET"
                        )
                        _logger.info(f"Payment details response: {payment_details}")
                        if isinstance(payment_details, dict):
                            fee_cents = payment_details.get("fee") or payment_details.get("payment", {}).get("fee")
                            vat_cents = payment_details.get("vat_withheld") or payment_details.get("payment", {}).get("vat_withheld")
                            
                            if fee_cents is not None:
                                self.recurrente_fee = float(fee_cents) / 100.0
                            if vat_cents is not None:
                                self.recurrente_vat_withheld = float(vat_cents) / 100.0
                    except Exception as e:
                        _logger.error(f"Error fetching payment details for payment {payment_id}: {e}")

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
        self.ensure_one()
        if self.provider_code != 'recurrente':
            return super()._create_payment(**extra_create_values)

        fee = self.recurrente_fee or 0.0
        vat_withheld = self.recurrente_vat_withheld or 0.0

        # If there are no amounts to separate, proceed normally
        if not fee and not vat_withheld:
            return super()._create_payment(**extra_create_values)

        provider = self.provider_id
        write_off_line_vals = []
        company_currency = self.company_id.currency_id
        tx_currency = self.currency_id

        # 1. Calculate the net amount that should hit the Outstanding Receipts/Transit account
        net_amount = self.amount - fee - vat_withheld

        # 2. Add Gateway Fee Commission Line (Debit)
        if fee and provider.recurrente_commission_account_id:
            fee_balance = tx_currency._convert(fee, company_currency, self.company_id, self.date or fields.Date.context_today(self))
            write_off_line_vals.append({
                'name': _("Comisión Pasarela Recurrente - %s") % self.reference,
                'amount_currency': fee if self.amount > 0 else -fee,
                'currency_id': tx_currency.id,
                'debit': fee_balance if fee_balance > 0.0 else 0.0,
                'credit': -fee_balance if fee_balance < 0.0 else 0.0,
                'balance': fee_balance,
                'partner_id': provider.recurrente_commission_partner_id.id or self.partner_id.id,
                'account_id': provider.recurrente_commission_account_id.id,
            })

        # 3. Add VAT Withholding Asset Line (Debit)
        if vat_withheld and provider.recurrente_vat_withholding_account_id:
            vat_balance = tx_currency._convert(vat_withheld, company_currency, self.company_id, self.date or fields.Date.context_today(self))
            write_off_line_vals.append({
                'name': _("Retención IVA de Tarjeta - %s") % self.reference,
                'amount_currency': vat_withheld if self.amount > 0 else -vat_withheld,
                'currency_id': tx_currency.id,
                'debit': vat_balance if vat_balance > 0.0 else 0.0,
                'credit': -vat_balance if vat_balance < 0.0 else 0.0,
                'balance': vat_balance,
                'partner_id': provider.recurrente_vat_withholding_partner_id.id or self.partner_id.id,
                'account_id': provider.recurrente_vat_withholding_account_id.id,
            })

        # 4. Modify creation values for the payment
        extra_vals = dict(extra_create_values)
        extra_vals.update({
            'amount': abs(net_amount),  # Net amount goes into the Outstanding Receipts/Bank transit account
            'write_off_line_vals': write_off_line_vals,  # Applied differences to balance the entry for the full Gross amount
        })

        return super()._create_payment(**extra_vals)
