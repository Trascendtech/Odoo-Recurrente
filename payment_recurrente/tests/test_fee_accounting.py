from odoo.tests import tagged

from odoo.addons.account_payment.tests.common import AccountPaymentCommon


@tagged('post_install', '-at_install')
class TestRecurrenteFeeAccounting(AccountPaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls._prepare_provider(code='recurrente')
        cls.fee_account = cls.env['account.account'].create({
            'name': 'Recurrente fees', 'code': 'RCFEE', 'account_type': 'expense',
        })
        cls.vat_account = cls.env['account.account'].create({
            'name': 'VAT withheld', 'code': 'RCVAT', 'account_type': 'asset_current',
        })

    def _paid_tx(self, fee=0.0, vat=0.0):
        tx = self._create_transaction('redirect', amount=100.0, state='done')
        tx.write({'recurrente_fee': fee, 'recurrente_vat_withheld': vat})
        return tx

    def _lines(self, payment, account):
        return payment.move_id.line_ids.filtered(lambda l: l.account_id == account)

    def test_no_fee_keeps_gross_amount(self):
        payment = self._paid_tx()._create_payment()
        self.assertEqual(payment.amount, 100.0)

    def test_fee_and_vat_booked_as_write_off(self):
        self.provider.write({
            'recurrente_commission_account_id': self.fee_account.id,
            'recurrente_vat_withholding_account_id': self.vat_account.id,
        })
        payment = self._paid_tx(fee=4.5, vat=1.5)._create_payment()
        self.assertEqual(payment.amount, 94.0)
        self.assertEqual(self._lines(payment, self.fee_account).debit, 4.5)
        self.assertEqual(self._lines(payment, self.vat_account).debit, 1.5)
        counterpart = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == payment.destination_account_id
        )
        self.assertEqual(counterpart.credit, 100.0)

    def test_unconfigured_account_is_not_deducted(self):
        self.provider.recurrente_commission_account_id = self.fee_account
        payment = self._paid_tx(fee=4.5, vat=1.5)._create_payment()
        # VAT has no account: it stays inside the payment instead of unbalancing the entry.
        self.assertEqual(payment.amount, 95.5)
        counterpart = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == payment.destination_account_id
        )
        self.assertEqual(counterpart.credit, 100.0)
