import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from odoo.addons.account_payment.tests.common import AccountPaymentCommon

REQUEST = 'odoo.addons.payment_recurrente.models.payment_provider.PaymentProvider._recurrente_make_request'
SECRET_KEY = base64.b64encode(b'test-signing-key').decode()


class RecurrenteApiCommon(AccountPaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls._prepare_provider(code='recurrente')

    def _checkout_tx(self, **values):
        tx = self._create_transaction('redirect', amount=150.0, **values)
        tx.id_recurrente_checkout = 'ch_test123'
        return tx

    def _webhook(self, event, checkout_id='ch_test123'):
        return {'event_type': event, 'checkout': {'id': checkout_id}, 'payment': {'id': 'pa_test456'}}


@tagged('post_install', '-at_install')
class TestRecurrenteApi(RecurrenteApiCommon):

    def test_webhook_done_requires_paid_checkout(self):
        tx = self._checkout_tx()
        with patch(REQUEST, return_value={'status': 'unpaid'}) as api:
            tx._handle_webhook_data(self._webhook('payment_intent.succeeded'))
        self.assertEqual(tx.state, 'draft', "a forged 'succeeded' must not confirm the payment")
        api.assert_called_once()
        self.assertEqual(api.call_args.args[0], 'checkouts/ch_test123')
        with patch(REQUEST, return_value={'status': 'paid'}):
            tx._handle_webhook_data(self._webhook('payment_intent.succeeded'))
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.provider_reference, 'pa_test456')

    def test_current_intent_succeeded_event(self):
        """Current accounts send intent.succeeded (older ones, payment_intent.succeeded)."""
        tx = self._checkout_tx()
        with patch(REQUEST, return_value={'status': 'paid'}):
            tx._handle_webhook_data(self._webhook('intent.succeeded'))
        self.assertEqual(tx.state, 'done')

    def test_unknown_event_does_not_break_the_payment(self):
        tx = self._checkout_tx()
        with patch(REQUEST, return_value={'status': 'paid'}):
            tx._handle_webhook_data(self._webhook('refund.create'))
        self.assertEqual(tx.state, 'draft')

    def test_secret_key_alone_authenticates(self):
        self.provider.write({'recurrente_secret_key': 'sk_test_fictitious', 'recurrente_public_key': False})
        response = MagicMock(status_code=200)
        response.json.return_value = {'environment': 'sandbox'}
        with patch('odoo.addons.payment_recurrente.models.payment_provider.requests.get',
                   return_value=response) as get:
            self.provider._recurrente_make_request('test', method='GET')
        headers = get.call_args.kwargs['headers']
        self.assertEqual(headers['X-SECRET-KEY'], 'sk_test_fictitious')
        self.assertNotIn('X-PUBLIC-KEY', headers)

    def test_bank_transfer_flow(self):
        tx = self._checkout_tx()
        with patch(REQUEST, return_value={'status': 'payment_in_progress'}):
            tx._handle_webhook_data(self._webhook('bank_transfer_intent.pending'))
        self.assertEqual(tx.state, 'pending')
        with patch(REQUEST, return_value={'status': 'paid'}):
            tx._handle_webhook_data(self._webhook('bank_transfer_intent.succeeded'))
        self.assertEqual(tx.state, 'done')

    def test_failed_webhook_ignored_when_paid(self):
        tx = self._checkout_tx()
        with patch(REQUEST, return_value={'status': 'paid'}):
            tx._handle_webhook_data(self._webhook('bank_transfer_intent.failed'))
        self.assertNotEqual(tx.state, 'error')

    def test_return_url_checks_api(self):
        tx = self._checkout_tx()
        with patch(REQUEST, return_value={'status': 'paid', 'payment': None}):
            tx._handle_return_data({'tx_ref': tx.reference, 'status': 'request_success'})
        self.assertEqual(tx.state, 'done')

    def test_cron_polls_open_transactions(self):
        tx = self._checkout_tx()
        with patch(REQUEST, return_value={'status': 'paid', 'payment': None}):
            self.env['payment.transaction']._cron_recurrente_check_status()
        self.assertEqual(tx.state, 'done')

    def test_refund_full_amount(self):
        self.assertEqual(self.provider.support_refund, 'full_only')
        tx = self._checkout_tx(state='done', provider_reference='pa_test456')
        tx.recurrente_intent_id = 'in_test456'
        with self.assertRaises(UserError):
            tx._send_refund_request(amount_to_refund=50.0)
        response = {
            'id': 're_test789',
            'status': 'succeeded',
            'account_refunded_amount_in_cents': 14500,
            'customer_refunded_amount_in_cents': 15000,
        }
        with patch(REQUEST, return_value=response) as api:
            tx.action_refund()
        self.assertEqual(api.call_args.args[0], 'refunds')
        self.assertEqual(api.call_args.kwargs['payload'], {'intent_id': 'in_test456'})
        self.assertTrue(api.call_args.kwargs['headers']['Idempotency-Key'].startswith('odoo-'))
        refund = tx.child_transaction_ids
        self.assertEqual(refund.operation, 'refund')
        self.assertEqual(refund.amount, -150.0)
        self.assertEqual(refund.state, 'done')
        self.assertEqual(refund.provider_reference, 're_test789')
        self.assertEqual(refund.recurrente_refunded_to_account, -145.0)

    def test_refund_pending_then_polled(self):
        tx = self._checkout_tx(state='done', provider_reference='pa_test456')
        tx.recurrente_intent_id = 'in_test456'
        with patch(REQUEST, return_value={'id': 're_1', 'status': 'pending'}):
            tx.action_refund()
        refund = tx.child_transaction_ids
        self.assertEqual(refund.state, 'pending')
        with patch(REQUEST, return_value={'id': 're_1', 'status': 'succeeded'}) as api:
            self.env['payment.transaction']._cron_recurrente_check_status()
        self.assertEqual(api.call_args.args[0], 'refunds/re_1')
        self.assertEqual(refund.state, 'done')

    def test_refund_without_payment_id(self):
        tx = self._checkout_tx(state='done')
        with patch(REQUEST, return_value={'status': 'paid'}), self.assertRaises(UserError):
            tx.action_refund()

    @staticmethod
    def _api(responses):
        """ Fake API answering by endpoint. """
        def call(endpoint, **kwargs):
            for prefix, response in responses.items():
                if endpoint.startswith(prefix):
                    return response
            raise AssertionError(f"unexpected call {endpoint}")
        return call

    def test_return_stores_intent_and_fees(self):
        tx = self._checkout_tx()
        api = self._api({
            'checkouts/': {'status': 'paid', 'payment': {'id': 'pa_1'},
                           'latest_intent': {'type': 'PaymentIntent', 'id': 'in_1'}},
            'intents/in_1': {'id': 'in_1', 'status': 'succeeded',
                             'details': {'fee': 450, 'vat_withheld': 75}},
        })
        with patch(REQUEST, side_effect=api):
            tx._handle_return_data({'tx_ref': tx.reference, 'status': 'request_success'})
        self.assertEqual(tx.state, 'done')
        self.assertEqual((tx.provider_reference, tx.recurrente_intent_id), ('pa_1', 'in_1'))
        self.assertEqual((tx.recurrente_fee, tx.recurrente_vat_withheld), (4.5, 0.75))

    def test_refund_reads_the_intent_of_an_old_transaction(self):
        """ Transactions confirmed before the intent was stored get it from the checkout. """
        tx = self._checkout_tx(state='done', provider_reference='pa_old')
        api = self._api({
            'checkouts/': {'status': 'paid', 'latest_intent': {'id': 'in_old'}},
            'intents/in_old': {'id': 'in_old', 'details': {}},
            'refunds': {'id': 're_old', 'status': 'succeeded'},
        })
        with patch(REQUEST, side_effect=api) as request:
            tx.action_refund()
        refund_call = [c for c in request.call_args_list if c.args[0] == 'refunds'][0]
        self.assertEqual(refund_call.kwargs['payload'], {'intent_id': 'in_old'})
        self.assertEqual(tx.child_transaction_ids.state, 'done')

    def test_signature(self):
        provider = self.provider
        body = b'{"event_type": "payment_intent.succeeded"}'
        self.assertTrue(provider._recurrente_verify_signature({}, body), "no secret: accepted")
        provider.recurrente_webhook_secret = f"whsec_{SECRET_KEY}"
        ts = str(int(time.time()))
        sig = base64.b64encode(
            hmac.new(b'test-signing-key', f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest()
        ).decode()
        good = {'svix-id': 'msg_1', 'svix-timestamp': ts, 'svix-signature': f"v1,wrong v1,{sig}"}
        self.assertTrue(provider._recurrente_verify_signature(good, body))
        self.assertFalse(provider._recurrente_verify_signature(good, body + b' '))
        self.assertFalse(provider._recurrente_verify_signature({**good, 'svix-timestamp': '1000'}, body))
        self.assertFalse(provider._recurrente_verify_signature({}, body))


@tagged('post_install', '-at_install')
class TestRecurrenteWebhookRoute(HttpCase, RecurrenteApiCommon):

    def test_unsigned_webhook_rejected_when_secret_set(self):
        self.provider.recurrente_webhook_secret = f"whsec_{SECRET_KEY}"
        res = self.url_open(
            '/payment/recurrente/webhook',
            data=json.dumps({'event_type': 'payment_intent.succeeded', 'checkout': {'id': 'ch_x'}}),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(res.status_code, 403)
