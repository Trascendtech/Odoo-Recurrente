def migrate(cr, version):
    """The Recurrente payment method supports tokenization (cards saved with a setup
    checkout, Odoo-Recurrente#5): its data record is noupdate, so existing databases are
    updated here; otherwise "Payment methods" in the portal never offers it."""
    cr.execute(
        """
        UPDATE payment_method SET support_tokenization = TRUE
         WHERE id = (SELECT res_id FROM ir_model_data
                      WHERE module = 'payment_recurrente' AND name = 'payment_method_recurrente')
        """
    )
