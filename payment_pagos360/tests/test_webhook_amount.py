from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

# Pagos360 notifications carry ids and the external reference only, never an amount.
PAID_REFERENCE = "TEST/2026/00001"
PAID_AMOUNT = 1234.56


@tagged("post_install", "-at_install")
class TestWebhookAmount(TransactionCase):
    """Covers ticket 124521: webhook notifications carry no amount, so reporting 0 made
    core flag the payment data as invalid and return before `_apply_updates`, leaving
    paid invoices unpaid and rejections stuck in error."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_pagos360.payment_provider_pagos360")
        cls.provider.write({"state": "test"})
        cls.payment_method = cls.env.ref("payment_pagos360.payment_method_pagos360")
        cls.currency = cls.env.company.currency_id
        cls.partner = cls.env["res.partner"].create({"name": "Test Buyer"})

    def _make_tx(self, reference=PAID_REFERENCE, amount=PAID_AMOUNT, token=None):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.provider.id,
                "payment_method_id": self.payment_method.id,
                "operation": "online_token" if token else "online_redirect",
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency.id,
                "partner_id": self.partner.id,
                "token_id": token.id if token else False,
            }
        )

    def _make_token(self, adhesion_type):
        return self.env["payment.token"].create(
            {
                "provider_id": self.provider.id,
                "payment_method_id": self.payment_method.id,
                "partner_id": self.partner.id,
                "provider_ref": "63897",
                "pagos360_adhesion_type": adhesion_type,
                "payment_details": "VISA **** - 5976",
            }
        )

    def _webhook_data(self, notification_type, reference=PAID_REFERENCE, entity_name="payment_request", payload=None):
        """Build a notification as Pagos360 posts it to /payment/pagos360/webhook."""
        data = {"id": 999001, "request_result_id": 999002, "external_reference": reference}
        data.update(payload or {})
        return {
            "entity_name": entity_name,
            "entity_id": 999001,
            "type": notification_type,
            "payload": data,
            "from_webhook": True,
        }

    def _process(self, data):
        # _apply_updates falls back to the API for the effective payment date; keep the
        # test offline. The module already tolerates a failing call.
        with patch.object(type(self.provider), "_pagos360_make_request", return_value={}):
            return self.env["payment.transaction"].sudo()._process("pagos360", data)

    # --- webhook payloads without an amount -------------------------------------------

    def test_paid_webhook_sets_transaction_done(self):
        tx = self._make_tx()
        self._process(self._webhook_data("paid"))
        self.assertEqual(tx.state, "done")
        self.assertFalse(tx.state_message)

    def test_rejected_webhook_sets_transaction_canceled(self):
        tx = self._make_tx()
        self._process(self._webhook_data("rejected"))
        self.assertEqual(tx.state, "cancel")

    def test_expired_webhook_sets_transaction_canceled(self):
        tx = self._make_tx()
        self._process(self._webhook_data("expired"))
        self.assertEqual(tx.state, "cancel")

    def test_paid_webhook_on_card_adhesion_sets_transaction_done(self):
        tx = self._make_tx(token=self._make_token("card_adhesion"))
        self._process(self._webhook_data("paid", entity_name="card_adhesion"))
        self.assertEqual(tx.state, "done")

    def test_amount_data_opts_out_when_payload_has_no_amount(self):
        tx = self._make_tx()
        self.assertIsNone(tx._extract_amount_data(self._webhook_data("paid")))

    def test_amount_data_opts_out_when_request_result_is_empty(self):
        """The API returns an empty request_result until the payment request is paid."""
        tx = self._make_tx()
        data = self._webhook_data("pending", payload={"request_result": []})
        self.assertIsNone(tx._extract_amount_data(data))

    # --- payloads that do carry an amount: the check still runs ------------------------

    def test_matching_amount_is_still_validated(self):
        tx = self._make_tx()
        data = self._webhook_data("paid", payload={"request_result": [{"amount": PAID_AMOUNT}]})
        self.assertEqual(tx._extract_amount_data(data)["amount"], PAID_AMOUNT)
        self._process(data)
        self.assertEqual(tx.state, "done")

    def test_mismatching_amount_still_sets_error(self):
        tx = self._make_tx()
        data = self._webhook_data("paid", payload={"request_result": [{"amount": PAID_AMOUNT - 1000}]})
        self._process(data)
        self.assertEqual(tx.state, "error")
