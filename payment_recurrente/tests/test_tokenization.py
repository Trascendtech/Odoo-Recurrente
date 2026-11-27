from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .test_recurrente_api import REQUEST, RecurrenteApiCommon

CHECKOUT = {'id': 'ch_setup1', 'checkout_url': 'https://app.recurrente.com/checkout/ch_setup1'}
PAID_SETUP = {
    'id': 'ch_setup1',
    'status': 'paid',
    'payment': {'id': 'pa_setup1', 'paymentable': {'type': 'Setup'}},
    'payment_method': {
        'id': 'pay_m_test4242',
        'type': 'card',
        'card': {'last4': '4242', 'network': 'visa', 'expiration_month': 4,
                 'expiration_year': 2031, 'issuer_name': None},
    },
    'latest_intent': {'id': 'in_setup1', 'type': 'SetupIntent', 'customer': {'id': 'cus_test1'}},
}


@tagged('post_install', '-at_install')
class TestRecurrenteTokenization(RecurrenteApiCommon):
    """E8.1: the card is saved by a setup checkout (nothing charged) as a payment.token that
    keeps only Recurrente's references; later payments charge it without the customer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider.allow_tokenization = True

    def _validation_tx(self, reference='setup-1'):
        tx = self._create_transaction(
            'redirect', operation='validation', amount=0.0, reference=reference
        )
        tx.id_recurrente_checkout = 'ch_setup1'
        return tx

    def _token(self):
        return self._create_token(provider_ref='pay_m_test4242', payment_details='4242')

    def test_provider_supports_tokenization(self):
        self.assertTrue(self.provider.support_tokenization)
        self.assertEqual(self.provider._get_validation_amount(), 0.0)
        compatible = self.env['payment.provider']._get_compatible_providers(
            self.company.id, self.partner.id, 0.0, is_validation=True,
        )
        self.assertIn(self.provider, compatible, "saving a card is allowed")
        self.provider.allow_tokenization = False
        compatible = self.env['payment.provider']._get_compatible_providers(
            self.company.id, self.partner.id, 0.0, is_validation=True,
        )
        self.assertNotIn(self.provider, compatible)

    def test_saving_a_card_opens_a_setup_checkout(self):
        tx = self._create_transaction('redirect', operation='validation', amount=0.0)
        with patch(REQUEST, return_value=CHECKOUT) as api:
            values = tx._get_specific_rendering_values({})
        endpoint = api.call_args.args[0]
        payload = api.call_args.kwargs['payload']
        self.assertEqual(endpoint, 'checkouts')
        self.assertEqual(payload['mode'], 'setup')
        self.assertNotIn('items', payload, "nothing is charged")
        self.assertEqual(values['api_url'], CHECKOUT['checkout_url'])
        self.assertEqual(tx.id_recurrente_checkout, 'ch_setup1')

    def test_payment_checkout_does_not_save_the_card(self):
        tx = self._create_transaction('redirect', amount=0.29, tokenize=True)
        with patch(REQUEST, return_value=CHECKOUT) as api:
            tx._get_specific_rendering_values({})
        payload = api.call_args.kwargs['payload']
        self.assertNotIn('mode', payload)
        self.assertEqual(payload['items'][0]['amount_in_cents'], 29, "rounded, not truncated")
        self.assertFalse(tx.tokenize)

    def test_paid_setup_checkout_creates_the_token(self):
        tx = self._validation_tx()
        with patch(REQUEST, return_value=PAID_SETUP):
            tx.action_check_payment_status()
        token = tx.token_id
        self.assertEqual(tx.state, 'done')
        self.assertEqual(
            (token.provider_ref, token.payment_details, token.partner_id),
            ('pay_m_test4242', '4242', self.partner),
        )
        self.assertEqual(
            (token.recurrente_card_network, token.recurrente_card_expiry,
             token.recurrente_customer_id),
            ('visa', '04/2031', 'cus_test1'),
        )
        stored = ' '.join(str(v) for v in token.read()[0].values())
        self.assertNotIn('4242424242424242', stored, "never the card number")
        again = self._validation_tx('setup-2')
        with patch(REQUEST, return_value=PAID_SETUP):
            again.action_check_payment_status()
        self.assertEqual(again.token_id, token, "the same card, the same token")

    def test_setup_webhook_saves_the_card(self):
        tx = self._validation_tx()
        event = {'event_type': 'setup_intent.succeeded', 'checkout': {'id': 'ch_setup1'}}
        with patch(REQUEST, return_value=PAID_SETUP):
            tx._handle_webhook_data(event)
        self.assertEqual((tx.state, tx.token_id.provider_ref), ('done', 'pay_m_test4242'))
        other = self._create_transaction('redirect', operation='validation', amount=0.0,
                                         reference='setup-cancelled')
        other.id_recurrente_checkout = 'ch_setup2'
        other._handle_webhook_data(
            {'event_type': 'setup_intent.cancelled', 'checkout': {'id': 'ch_setup2'}}
        )
        self.assertEqual(other.state, 'cancel')

    def test_token_payment_charges_the_saved_card(self):
        tx = self._create_transaction('token', token_id=self._token().id, amount=1117.5)
        answer = {'object': 'one_time_payment', 'id': 'on_test1', 'status': 'paid'}
        with patch(REQUEST, return_value=answer) as api:
            tx._send_payment_request()
        endpoint = api.call_args.args[0]
        payload, headers = api.call_args.kwargs['payload'], api.call_args.kwargs['headers']
        self.assertEqual(endpoint, 'one_time_payments')
        self.assertEqual(payload['payment_method_id'], 'pay_m_test4242')
        self.assertEqual(payload['items'][0]['amount_in_cents'], 111750)
        self.assertEqual(headers['Idempotency-Key'], f"odoo-{tx.reference}")
        self.assertEqual((tx.state, tx.provider_reference), ('done', 'on_test1'))

    def test_declined_or_unconfirmed_token_payment(self):
        tx = self._create_transaction('token', token_id=self._token().id, amount=100.0)
        with patch(REQUEST, side_effect=ValidationError("Recurrente: Error en el pago")):
            tx._send_payment_request()
        self.assertEqual(tx.state, 'error')
        self.assertIn('Error en el pago', tx.state_message)
        other = self._create_transaction('token', token_id=self._token().id, amount=100.0,
                                         reference='token-pending')
        with patch(REQUEST, return_value={'id': 'on_test2', 'status': 'processing'}):
            other._send_payment_request()
        self.assertEqual(other.state, 'pending', "never taken as paid")
