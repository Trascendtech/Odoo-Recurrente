from unittest.mock import patch

from odoo.tests import tagged

from .test_recurrente_api import REQUEST, RecurrenteApiCommon

INTENTS = [
    {'id': 'in_other', 'payment': {'paymentable': {'id': 'on_other'}}},
    {'id': 'in_token1', 'payment': {'paymentable': {'type': 'OneTimePayment', 'id': 'on_token1'}}},
]


def api(endpoint, headers=None, payload=None, method='POST'):
    """Recurrente as the Sandbox answers it, by endpoint."""
    if endpoint == 'one_time_payments':
        return {'object': 'one_time_payment', 'id': 'on_token1', 'status': 'paid'}
    if endpoint == 'intents':
        return INTENTS if payload.get('page') == 1 else []
    if endpoint == 'intents/in_token1':
        return {'id': 'in_token1', 'details': {'fee': 450, 'vat_withheld': 150}}
    if endpoint == 'refunds':
        return {'id': 're_token1', 'status': 'succeeded', 'account_refunded_amount_in_cents': 10000}
    raise AssertionError(f"unexpected call {method} {endpoint}")


@tagged('post_install', '-at_install')
class TestRecurrenteTokenAccounting(RecurrenteApiCommon):
    """E8.6: charges on a saved card get their intent like checkout payments: the fee and the
    VAT withheld are booked by the existing accounting, and they can be refunded from Odoo."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fee_account = cls.env['account.account'].create(
            {'name': 'Recurrente fees', 'code': 'RCFEE2', 'account_type': 'expense'}
        )
        cls.vat_account = cls.env['account.account'].create(
            {'name': 'VAT withheld', 'code': 'RCVAT2', 'account_type': 'asset_current'}
        )
        cls.provider.write({
            'recurrente_commission_account_id': cls.fee_account.id,
            'recurrente_vat_withholding_account_id': cls.vat_account.id,
        })

    def _charge(self):
        token = self._create_token(provider_ref='pay_m_test4242', payment_details='4242')
        tx = self._create_transaction('token', token_id=token.id, amount=100.0)
        with patch(REQUEST, side_effect=api):
            tx._send_payment_request()
        return tx

    def test_token_charge_books_fee_and_vat(self):
        tx = self._charge()
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.recurrente_intent_id, 'in_token1', "found by its one-time payment")
        self.assertEqual((tx.recurrente_fee, tx.recurrente_vat_withheld), (4.5, 1.5))
        payment = tx._create_payment()
        self.assertEqual(payment.amount, 94.0, "net deposited by Recurrente")
        lines = payment.move_id.line_ids
        self.assertEqual(lines.filtered(lambda l: l.account_id == self.fee_account).debit, 4.5)
        self.assertEqual(lines.filtered(lambda l: l.account_id == self.vat_account).debit, 1.5)
        counterpart = lines.filtered(lambda l: l.account_id == payment.destination_account_id)
        self.assertEqual(counterpart.credit, 100.0, "the customer is credited the gross amount")

    def test_token_charge_can_be_refunded(self):
        tx = self._charge()
        with patch(REQUEST, side_effect=api) as call:
            refund = tx._send_refund_request()
        refund_call = [c for c in call.call_args_list if c.args[0] == 'refunds'][0]
        self.assertEqual(refund_call.kwargs['payload'], {'intent_id': 'in_token1'})
        self.assertEqual((refund.state, refund.provider_reference), ('done', 're_token1'))

    def test_intent_not_found_does_not_block_the_payment(self):
        def without_intent(endpoint, headers=None, payload=None, method='POST'):
            return [] if endpoint == 'intents' else api(endpoint, headers, payload, method)

        token = self._create_token(provider_ref='pay_m_test4242', payment_details='4242')
        tx = self._create_transaction('token', token_id=token.id, amount=100.0)
        with patch(REQUEST, side_effect=without_intent):
            tx._send_payment_request()
        self.assertEqual((tx.state, tx.recurrente_intent_id), ('done', False))
        with patch(REQUEST, side_effect=api):
            self.assertEqual(tx._recurrente_fetch_intent(), 'in_token1', "looked up again later")
