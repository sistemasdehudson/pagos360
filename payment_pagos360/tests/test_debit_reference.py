from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDebitReference(TransactionCase):
    """Covers ticket 123504: paid card debits stayed pending because the debit was
    created without ``external_reference`` (breaking webhook matching) and the effective
    payment date was never read from the payload already in hand."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env.ref("payment_pagos360.payment_provider_pagos360")
        cls.provider.write({"state": "test"})
        cls.payment_method = cls.env.ref("payment_pagos360.payment_method_pagos360")
        cls.currency = cls.env.company.currency_id
        cls.partner = cls.env["res.partner"].create({"name": "Test Buyer"})

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

    def _make_tx(self, token, amount=245402.33):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.provider.id,
                "payment_method_id": self.payment_method.id,
                "operation": "offline",
                "amount": amount,
                "currency_id": self.currency.id,
                "partner_id": self.partner.id,
                "token_id": token.id,
            }
        )

    # --- external_reference is sent to Pagos360 -------------------------------------

    def test_card_debit_request_sends_external_reference(self):
        tx = self._make_tx(self._make_token("card_adhesion"))
        with patch.object(type(self.provider), "_pagos360_make_request", return_value={"id": "999"}) as mock_req:
            tx._pagos360_card_debit_request()
        payload = mock_req.call_args.kwargs["data"]["card_debit_request"]
        self.assertEqual(payload["external_reference"], tx.reference)

    def test_debit_request_sends_external_reference(self):
        tx = self._make_tx(self._make_token("adhesion"))
        # get_debit_due_date hits the API on its own; stub it to isolate the payload.
        with patch.object(type(tx), "get_debit_due_date", return_value="2026-07-19"), patch.object(
            type(self.provider), "_pagos360_make_request", return_value={"id": "999"}
        ) as mock_req:
            tx._pagos360_debit_request()
        payload = mock_req.call_args.kwargs["data"]["debit_request"]
        self.assertEqual(payload["external_reference"], tx.reference)

    # --- paid card debit resolves and dates the payment from the payload ------------

    def test_paid_card_debit_sets_done_and_effective_date_from_payload(self):
        tx = self._make_tx(self._make_token("card_adhesion"))
        tx._set_pending()
        payload = {
            "entity_name": "card_debit_request",
            "entity_id": "121048091",
            "type": "paid",
            "payload": {
                "id": "121048091",
                "state": "paid",
                "external_reference": tx.reference,
                "amount": tx.amount,
                "request_result": [{"paid_at": "2026-07-15T12:33:10-03:00"}],
            },
        }
        # The paid_at must come from the payload; no API round-trip needed.
        with patch.object(type(self.provider), "_pagos360_make_request", side_effect=AssertionError("API hit")):
            tx._apply_updates(payload)
        self.assertEqual(tx.state, "done")
        self.assertEqual(str(tx.pagos360_effective_payment_date), "2026-07-15")
