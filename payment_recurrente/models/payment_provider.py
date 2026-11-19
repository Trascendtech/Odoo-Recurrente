import base64
import binascii
import hashlib
import hmac
import logging
import pprint
import time

import requests
from werkzeug.urls import url_join

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.payment_recurrente import const
from odoo.addons.payment_recurrente.controllers.main import RecurrenteController

_logger = logging.getLogger(__name__)

class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('recurrente', "Recurrente")], ondelete={'recurrente': 'set default'}
    )
    recurrente_public_key = fields.Char(string="Recurrente Public Key", groups='base.group_system')
    recurrente_secret_key = fields.Char(string="Recurrente Secret Key", groups='base.group_system')
    recurrente_setup_token = fields.Char(string="Recurrente Setup Token", groups='base.group_system')
    recurrente_webhook_secret = fields.Char(
        string="Recurrente Webhook Signing Secret",
        groups='base.group_system',
        help="Signing secret of the webhook endpoint (starts with 'whsec_'). When set, webhooks "
             "without a valid signature are rejected. Payments are always confirmed against the "
             "API before being marked as paid.",
    )
    allow_transfers = fields.Boolean(string="Allow Transfers", groups='base.group_system')
    allow_installments = fields.Boolean(string="Allow Installments", groups='base.group_system')
    installment_ids = fields.Many2many(comodel_name="payment.installment_recurrente", string="Installments", groups='base.group_system')

    # Recurrente accounting settings
    recurrente_commission_account_id = fields.Many2one(
        comodel_name='account.account',
        string="Recurrente Commission Account",
        groups='base.group_system',
        domain="[('deprecated', '=', False), ('company_ids', '=', company_id)]",
        help="Account used to record payment gateway commission/fees."
    )
    recurrente_commission_partner_id = fields.Many2one(
        comodel_name='res.partner',
        string="Recurrente Commission Partner",
        groups='base.group_system',
        help="Partner used for the commission/fee line (e.g. Recurrente S.A.)."
    )
    recurrente_vat_withholding_account_id = fields.Many2one(
        comodel_name='account.account',
        string="Recurrente VAT Withholding Account",
        groups='base.group_system',
        domain="[('deprecated', '=', False), ('company_ids', '=', company_id)]",
        help="Account used to record VAT withholdings received."
    )
    recurrente_vat_withholding_partner_id = fields.Many2one(
        comodel_name='res.partner',
        string="Recurrente VAT Withholding Partner",
        groups='base.group_system',
        help="Partner used for the VAT withholding line (e.g. SAT or Recurrente S.A.)."
    )

    def _compute_feature_support_fields(self):
        """ Override of `payment` to enable full refunds (the API refunds a whole payment). """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'recurrente').update({'support_refund': 'full_only'})

    def _recurrente_verify_signature(self, headers, body):
        """ Verify a webhook signature (Svix scheme used by Recurrente).

        The signed content is ``{id}.{timestamp}.{body}``; ``svix-signature`` holds one or more
        ``v1,<base64 HMAC-SHA256>`` values computed with the base64 secret after ``whsec_``.

        :return: True when no secret is configured or the signature is valid.
        """
        self.ensure_one()
        secret = self.sudo().recurrente_webhook_secret
        if not secret:
            return True
        msg_id = headers.get('svix-id') or headers.get('webhook-id')
        timestamp = headers.get('svix-timestamp') or headers.get('webhook-timestamp')
        signatures = headers.get('svix-signature') or headers.get('webhook-signature') or ''
        if not (msg_id and timestamp and signatures):
            return False
        try:
            if abs(time.time() - int(timestamp)) > const.WEBHOOK_TOLERANCE_SECONDS:
                return False
            key = base64.b64decode(secret.split('_', 1)[1] if secret.startswith('whsec_') else secret)
        except (ValueError, binascii.Error):
            return False
        if isinstance(body, str):
            body = body.encode()
        signed = f"{msg_id}.{timestamp}.".encode() + body
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        for candidate in signatures.split():
            version, _sep, value = candidate.partition(',')
            if version == 'v1' and hmac.compare_digest(value, expected):
                return True
        return False

    @api.model
    def _get_compatible_providers(self, *args, is_validation=False, **kwargs):
        """ Override of `payment` to filter out Recurrente providers for validation operations. """
        providers = super()._get_compatible_providers(*args, is_validation=is_validation, **kwargs)

        if is_validation:
            providers = providers.filtered(lambda p: p.code != 'recurrente')
        return providers

    def _get_supported_currencies(self):
        """ Override of `payment` to return the supported currencies. """
        supported_currencies = super()._get_supported_currencies()
        if self.code == 'recurrente':
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    def _recurrente_make_request(self, endpoint, headers=None, payload=None, method='POST'):
        """ Make a request to Recurrente API at the specified endpoint.

        Note: self.ensure_one()

        :param str endpoint: The endpoint to be reached by the request.
        :param dict payload: The payload of the request.
        :param str method: The HTTP method of the request.
        :return The JSON-formatted content of the response.
        :rtype: dict
        :raise ValidationError: If an HTTP error occurs.
        """
        self.ensure_one()
        headers = dict(headers or {})
        payload = payload or {}

        url = url_join('https://app.recurrente.com/api/', endpoint)
        # Set authentication headers
        auth_headers = {
            "X-PUBLIC-KEY": self.recurrente_public_key,
            "X-SECRET-KEY": self.recurrente_secret_key,
        }
        headers.update(auth_headers)

        try:
            if method == 'GET':
                response = requests.get(url, params=payload, headers=headers, timeout=10)
            elif method == 'POST_FORM':
                response = requests.post(url, data=payload, headers=headers, timeout=10)
            else:
                response = requests.post(url, json=payload, headers=headers, timeout=10)
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError:
                _logger.exception(
                    f"Invalid API request at {url} with data:\n{pprint.pformat(payload)}"
                )
                try:
                    error_msg = response.json().get('message', '')
                except ValueError:
                    error_msg = response.text
                raise ValidationError("Recurrente: " + _(
                    "The communication with the API failed. Recurrente gave us the following information: '%s'" % error_msg
                ))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            _logger.exception(f"Unable to reach endpoint at {url}")
            raise ValidationError(
                "Recurrente: " + _("Could not establish the connection to the API.")
            )
        try:
            return response.json()
        except ValueError:
            _logger.error(f"Non-JSON response from {url}: {response.text}")
            raise ValidationError("Recurrente: " + _("Invalid response from API."))

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        default_codes = super()._get_default_payment_method_codes()
        if self.code != 'recurrente':
            return default_codes
        return const.DEFAULT_PAYMENT_METHODS_CODES
