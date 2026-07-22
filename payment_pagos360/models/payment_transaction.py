import logging
import pprint
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from odoo import _, api, fields, models
from odoo.addons.payment import utils as payment_utils
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval
from odoo.tools.urls import urljoin

from ..controllers.main import Pagos360Controller

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    pagos360_adhesion_type = fields.Selection(related="token_id.pagos360_adhesion_type", store=True)
    pagos360_effective_payment_date = fields.Date()
    pagos360_child_amount = fields.Float(
        string="Pagos 360 Child Charge Amount",
        help="Original transaction amount preserved when the operation is converted to "
        "`validation` (adhesion flow). Used as the amount of the child "
        "online_token transaction spawned after the adhesion is signed.",
    )

    @api.model
    def _get_specific_create_values(self, provider_code, values):
        """Convert a direct/redirect payment into a validation (adhesion) transaction when needed.

        Trigger: ``tokenize=True`` with a configured adhesion form URL — the user requested to save
        their payment method and the provider has the adhesion form configured.

        Once the adhesion is signed and a token is created, ``_pagos360_spawn_child_charge``
        fires the actual charge as a child transaction against that token.
        """
        res = super()._get_specific_create_values(provider_code, values)
        if provider_code != "pagos360":
            return res
        if values.get("operation") not in ("online_redirect", "online_direct"):
            return res
        provider = self.env["payment.provider"].browse(values.get("provider_id"))
        force_by_tokenize = bool(values.get("tokenize") and provider.pagos360_form_url)
        if not force_by_tokenize:
            return res
        res.update(
            {
                "operation": "validation",
                "tokenize": True,
                "pagos360_child_amount": values.get("amount", 0.0),
                "amount": 0.0,
                "currency_id": provider._get_validation_currency().id,
            }
        )
        return res

    def _create_payment(self, **extra_create_values):
        self.ensure_one()

        if self.provider_code == "pagos360" and self.pagos360_effective_payment_date:
            extra_create_values.update(
                {
                    "date": self.pagos360_effective_payment_date,
                }
            )
        return super()._create_payment(**extra_create_values)

    def _get_specific_rendering_values(self, processing_values):
        """Override of `payment` to return Pagos360-specific rendering values.

        Note: self.ensure_one() from `_get_rendering_values`.

        :param dict processing_values: The generic and specific processing values of the transaction
        :return: The dict of provider-specific processing values.
        :rtype: dict
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "pagos360":
            return res
        if self.operation == "validation":
            return {"api_url": "%s&pReference=%s" % (self.provider_id.pagos360_form_url, self.reference)}

        # Initiate the payment and retrieve the payment link data.
        payload = self._pagos360_prepare_preference_request_payload()
        _logger.info("Sending '/payment-request' request for link creation:\n%s", pprint.pformat(payload))

        payment_data = self.provider_id._pagos360_make_request("/payment-request", data=payload)
        self.sudo().provider_reference = payment_data.get("id")
        if self.payment_method_code == "pagos360":
            api_url = payment_data["checkout_url"]
        elif self.payment_method_code == "pagofacil":
            access_token = payment_utils.generate_access_token(self.partner_id.id, self.amount, self.currency_id.id)
            api_url = "/payment/pagos360/pagofacil?tx_id=%s&access_token=%s" % (self.id, access_token)
        elif self.payment_method_code == "rapipago":
            access_token = payment_utils.generate_access_token(self.partner_id.id, self.amount, self.currency_id.id)
            api_url = "/payment/pagos360/rapipago?tx_id=%s&access_token=%s" % (self.id, access_token)

        return {
            "api_url": api_url,
        }

    def _pagos360_prepare_preference_request_payload(self):
        """Create the payload for the payment request based on the transaction values.

        :return: The request payload
        :rtype: dict
        """
        base_url = self.provider_id.get_base_url()
        redirect_url = urljoin(base_url, Pagos360Controller._return_url)

        first_due_date, first_total = self.get_first_due_values()
        # second_due_date, second_total = self.get_second_due_values()

        res = {
            "payment_request": {
                "description": self.reference,
                "external_reference": self.reference,  # No requerido
                "payer_name": self.partner_name,
                "payer_email": self.partner_email,  # No requerido
                "first_due_date": (first_due_date).strftime("%d-%m-%Y"),
                "first_total": first_total,
                # 'second_due_date': (second_due_date).strftime('%d-%m-%Y'),   # No requerido
                # 'second_total': second_total,            # No requerido
                "back_url_success": redirect_url,  # No requerido
                "back_url_pending": redirect_url,  # No requerido
                "back_url_rejected": redirect_url,  # No requerido
            }
        }
        if self.provider_id.pagos360_excluded_channels:
            res["payment_request"].update({"excluded_channels": safe_eval(self.provider_id.pagos360_excluded_channels)})
        if self.provider_id.pagos360_excluded_installments:
            res["payment_request"].update(
                {"excluded_installments": safe_eval(self.provider_id.pagos360_excluded_installments)}
            )
        if self.provider_id.pagos360_excluded_card_brands:
            res["payment_request"].update(
                {"excluded_card_brands": safe_eval(self.provider_id.pagos360_excluded_card_brands)}
            )
        return res

    def get_first_due_values(self):
        first_due_date = fields.Datetime.now() + timedelta(days=self.provider_id.validity_days)
        first_total = self.amount
        return first_due_date, first_total

    def get_second_due_values(self):
        second_due_date = fields.Datetime.now() + timedelta(days=self.provider_id.second_validity_days)
        second_total = self.amount * (1 + self.provider_id.second_due_fees / 100.0)
        return second_due_date, second_total

    @api.model
    def _search_by_reference(self, provider_code, payment_data):
        """Override of payment to search the transaction based on Pagos360 data.

        :param str provider_code: The code of the provider that handled the transaction.
        :param dict payment_data: The payment data sent by the provider.
        :return: The transaction, if found.
        :rtype: recordset of `payment.transaction`
        """
        tx = super()._search_by_reference(provider_code, payment_data)
        if provider_code != "pagos360" or tx:
            return tx

        payload = payment_data.get("payload", {})
        entity_name = payment_data.get("entity_name")

        if not entity_name:
            _logger.warning("PAGOS360: Received data with missing entity name.")
            return self

        if entity_name in ["debit_request", "card_debit_request"]:
            domain = [
                "|",
                ("provider_reference", "=", str(payload.get("id"))),
                ("reference", "=", payload.get("external_reference")),
                ("provider_code", "=", "pagos360"),
            ]
        else:
            domain = [("reference", "=", payload.get("external_reference")), ("provider_code", "=", "pagos360")]
        if payload.get("entity_name") == "payment_request":
            domain.append(["pagos360_adhesion_type", "=", False])
        tx = self.search(domain)
        if not tx:
            _logger.warning("Pagos360: No transaction found matching reference %s.", payment_data.get("ref"))
        return tx

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        """Override of payment to extract the transaction reference from Pagos360 data.

        :param str provider_code: The code of the provider handling the transaction.
        :param dict payment_data: The payment data sent by the provider.
        :return: The transaction reference.
        :rtype: str
        """
        if provider_code != "pagos360":
            return super()._extract_reference(provider_code, payment_data)

        payload = payment_data.get("payload", {})
        return payload.get("external_reference")

    def _extract_amount_data(self, payment_data):
        """Override of payment to extract the amount and currency from the payment data."""
        if self.provider_code != "pagos360":
            return super()._extract_amount_data(payment_data)
        amount = 0.0
        if not self.pagos360_adhesion_type:
            request_result = payment_data.get("payload", {}).get("request_result", [])
            for result in request_result:
                amount += result.get("amount", 0.0)
        elif self.pagos360_adhesion_type == "adhesion":
            amount += payment_data.get("payload", {}).get("first_total", 0.0)
        elif self.pagos360_adhesion_type == "card_adhesion":
            amount += payment_data.get("payload", {}).get("amount", 0.0)

        return {
            "amount": amount,
            "currency_code": self.currency_id.name,
        }

    def _apply_updates(self, payment_data):
        """Override of payment to update the transaction based on Pagos360 data.

        Note: self.ensure_one()

        :param dict payment_data: The payment data sent by the provider.
        :return: None
        """
        super()._apply_updates(payment_data)
        if self.provider_code != "pagos360":
            return

        entity_name = payment_data.get("entity_name")
        entity_id = payment_data.get("entity_id")
        if not entity_id:
            _logger.warning("PAGOS360: Received data with missing entity id.")
            return

        self.provider_reference = entity_id
        # Prefer the paid_at already present in the received payload (debits carry it in
        # request_result); only hit the API as a fallback for entities that don't.
        paid_at = self._pagos360_extract_paid_at(payment_data.get("payload")) or (
            self._pagos360_get_paid_at_from_request(entity_name, entity_id)
        )
        if paid_at:
            self.pagos360_effective_payment_date = paid_at[:10]
        payment_status = payment_data.get("type")
        try:
            if payment_status in [
                "pending",
                "in_process",
                "pending_to_sign",
                "transfer_created",
                "link_pagos_created",
                "banelco_pmc_created",
                "debin_created",
            ]:
                if self.state != "pending":
                    self._set_pending()
            elif payment_status == "signed" and self.operation == "validation":
                self._set_done()
                if not self.token_id and self.tokenize:
                    self._tokenize(payment_data)
                if not self.child_transaction_ids:
                    self._pagos360_spawn_child_charge()
            elif payment_status == "paid":
                if paid_at:
                    self.pagos360_effective_payment_date = paid_at[:10]
                self._set_done(extra_allowed_states=("cancel", "error"))
            elif payment_status == "reverted":
                self.payment_id.action_draft()
                self.payment_id.action_cancel()
                self._set_canceled(
                    "PAGOS360: " + _("Canceled payment with status: %s", payment_status), extra_allowed_states=("done",)
                )
            elif payment_status in ["expired", "canceled", "rejected", "transfer_canceled"]:
                # Solo cambio el estado en los casos que puedo hacerlo.
                # las autorizaciones se pueden cancelar cuando estan ya en done
                if self.state in ["draft", "pending", "authorized"]:
                    self._set_canceled("PAGOS360: " + _("Canceled payment with status: %s", payment_status))
                if entity_name in ["card_adhesion", "adhesion"]:
                    if self.token_id and self.token_id.active:
                        self.token_id.with_context(is_notification=True).write({"active": False})
            else:
                _logger.info(
                    "received data with invalid payment status (%s) for transaction with reference %s",
                    payment_status,
                    self.reference,
                )
                message = """
                    Parece que esta transacción no se pudo realizar, ante algún inconveniente por favor comunicarse a
                     través de los siguientes canales:<br/>
                    Correo Electrónico: soporte@pagos360.com.ar<br/>
                    WhatsApp: +54 3512548747\n
                    Información:\n
                    - Transacción PAGOS360: {transaction}<br/>
                    - Código de Error: {error_code}<br/>
                    - Mensaje de Error: {error_msg}<br/>
                """.format(transaction=self.provider_reference, error_code=payment_status, error_msg="")
                self._set_error("PAGOS360: " + message)
        except Exception as e:
            _logger.info("PAGOS360 Error: (%s) for transaction with id %s", e, self.id)
            message = f"""
                Parece que esta transacción no se pudo realizar, le sugerimos revisar en su portal de PAGOS360 el
                 estado de la solicitud de pago.
                Ante algún inconveniente con la misma por favor comunicarse a través de los siguientes canales:
                Correo Electrónico: soporte@pagos360.com.ar\n
                WhatsApp: +54 3512548747\n
                - Transacción id: {self.id}<br/>
                - Mensaje de Error": {e}<br/>
            """
            self._set_error("PAGOS360: " + message)

    def _pagos360_get_paid_at_from_request(self, entity_name, entity_id):
        """Fetch entity info from Pagos360 and extract the first available paid_at value."""
        endpoint_by_entity = {
            "payment_request": f"/payment-request?id={entity_id}",
            "card_adhesion": f"/card-adhesion/{entity_id}",
            "adhesion": f"/adhesion/{entity_id}",
            "card_debit_request": f"/card-debit-request?id={entity_id}",
            "debit_request": f"/debit-request?id={entity_id}",
        }
        endpoint = endpoint_by_entity.get(entity_name)
        if not endpoint:
            return False

        try:
            entity_data = self.provider_id._pagos360_make_request(endpoint, method="GET")
        except Exception as e:
            _logger.warning(
                "Could not fetch paid_at from Pagos360 API for entity %s (%s): %s", entity_name, entity_id, e
            )
            return False

        return self._pagos360_extract_paid_at(entity_data)

    def _pagos360_extract_paid_at(self, data):
        """Recursively look for a paid_at key in Pagos360 response payloads."""
        if isinstance(data, dict):
            paid_at = data.get("paid_at")
            if paid_at:
                return paid_at
            for value in data.values():
                paid_at = self._pagos360_extract_paid_at(value)
                if paid_at:
                    return paid_at
            return False

        if isinstance(data, list):
            for item in data:
                paid_at = self._pagos360_extract_paid_at(item)
                if paid_at:
                    return paid_at

        return False

    def _extract_token_values(self, payment_data):
        """Create a new token based on the feedback data.

        Note: self.ensure_one()

        :param dict payment_data: The payment data sent by the provider.
        :return: The token values to create a new token.
        :rtype: dict
        """
        self.ensure_one()
        if self.provider_code != "pagos360":
            return super()._extract_token_values(payment_data)

        adhesion_id = payment_data.get("entity_id")
        entity_name = payment_data.get("entity_name")

        if not adhesion_id or not entity_name:
            _logger.warning("PAGOS360: Missing entity_id or entity_name in payment data")
            return {}

        if entity_name == "card_adhesion":
            endpoint = f"/card-adhesion/{adhesion_id}"
        else:
            endpoint = f"/adhesion/{adhesion_id}"

        adhesion_data = self.provider_id._pagos360_make_request(endpoint, data=None, method="GET")
        if not adhesion_data:
            return {}

        if entity_name == "card_adhesion":
            payment_details = "Debito automático en Tarjeta: {} **** - {}".format(
                adhesion_data.get("card"), adhesion_data.get("last_four_digits")
            )
        elif entity_name == "adhesion":
            payment_details = "Debito automático en CBU: {} ****{}".format(
                adhesion_data.get("bank"), adhesion_data.get("cbu_number")
            )
        else:
            payment_details = ""

        return {
            "provider_ref": adhesion_id,
            "payment_details": payment_details,
            "pagos360_adhesion_type": entity_name,
            "pagos360_external_reference": adhesion_data.get("external_reference"),
            "pagos360_card": adhesion_data.get("card"),
            "pagos360_card_number": adhesion_data.get("last_four_digits"),
            "pagos360_cbu_number": adhesion_data.get("cbu_number"),
            "pagos360_bank": adhesion_data.get("bank"),
        }

    def _send_payment_request(self):
        if self.provider_code == "pagos360":
            if self.state != "draft" or self.provider_reference:
                # No se puede enviar la solicitud de pago si no esta en borrador o ya tiene referencia
                _logger.error(
                    f"pagos360: cant send transaction {self.id}. State: {self.state} - Ref: {self.provider_reference}"
                )
                return
            if self.token_id.pagos360_adhesion_type == "card_adhesion":
                req = self._pagos360_card_debit_request()
                self._process(self.provider_code, self.simulate_webhook("card_debit_request", req))
            if self.token_id.pagos360_adhesion_type == "adhesion":
                req = self._pagos360_debit_request()
            self.env.cr.commit()  # pylint: disable=invalid-commit
            if req:
                self._process(self.provider_code, self.simulate_webhook(self.token_id.pagos360_adhesion_type, req))
                self.env.cr.commit()  # pylint: disable=invalid-commit
        return super()._send_payment_request()

    def _pagos360_card_debit_request(self):
        operation_date = fields.Date.today()
        cut_day = int(self.env["ir.config_parameter"].sudo().get_param("pagos360.cut_day", "19"))
        if operation_date.day > cut_day:
            operation_date = operation_date + relativedelta(months=1)
        data = {
            "card_debit_request": {
                "description": _("Payment %s") % self.company_id.display_name,
                "external_reference": self.reference,
                "amount": self.amount,
                "month": operation_date.month,
                "year": operation_date.year,
                "card_adhesion_id": int(self.token_id.provider_ref),
            }
        }
        res = self.provider_id._pagos360_make_request("card-debit-request", data=data, method="POST")
        self.provider_reference = res.get("id")
        return res

    def _pagos360_next_business_day(self, due_date, days=3):
        data = {"next_business_day": {"date": due_date.strftime("%d-%m-%Y"), "days": days}}
        return self.provider_id._pagos360_make_request("validator/next-business-day", data=data, method="POST")

    def _pagos360_debit_request(self):
        first_due_date, first_total = self.get_first_due_values()
        next_business_day = self._pagos360_next_business_day(first_due_date)
        data = {
            "debit_request": {
                "description": _("Payment %s") % self.company_id.display_name,
                "external_reference": self.reference,
                "first_total": self.amount,
                # la fecha de vencimiento para cbu es un dia habil hay un sevicio para eso
                "first_due_date": fields.Datetime.from_string(next_business_day[:10]).strftime("%d-%m-%Y"),
                "adhesion_id": int(self.token_id.provider_ref),
            }
        }
        res = self.provider_id._pagos360_make_request("debit-request", data=data, method="POST")
        self.provider_reference = res.get("id")
        return res

    def get_pagos360_info(self, check_payment_state=True):
        result_msg = []
        for tx in self.filtered(lambda x: x.provider_code == "pagos360"):
            # Check state of adhesion
            payload = False
            ref_sanitarzed = tx.reference.replace("%", "%25")
            if tx.operation == "validation":
                datas = tx.provider_id._pagos360_make_request(
                    "/card-adhesion?external_reference=%s&page=1" % ref_sanitarzed, method="GET"
                )
                entity_name = "card_adhesion"
                for data in datas["data"]:
                    payload = tx.simulate_webhook(entity_name, data)
                    result_msg.append(payload)
                    tx.sudo()._process(tx.provider_code, payload)
                datas = tx.provider_id._pagos360_make_request(
                    "/adhesion?external_reference=%s&page=1" % ref_sanitarzed, method="GET"
                )
                entity_name = "adhesion"
                for data in datas["data"]:
                    payload = tx.simulate_webhook(entity_name, data)
                    result_msg.append(payload)
                    tx.sudo()._process(tx.provider_code, payload)

            # Check state of payment
            elif not tx.pagos360_adhesion_type and tx.operation != "validation":
                # https://api.sandbox.pagos360.com/debit-request?page=1
                if tx.provider_reference:
                    from_date = (tx.create_date - relativedelta(months=1)).strftime("%d-%m-%Y")
                    to_date = (tx.create_date + relativedelta(months=1)).strftime("%d-%m-%Y")
                    url = f"/payment-request?id={tx.provider_reference}&created_at_gte={from_date}&created_at_lte={to_date}"
                else:
                    url = "/payment-request?external_reference=%s" % ref_sanitarzed
                data = tx._get_operation_info_from_data(tx.provider_id._pagos360_make_request(url, method="GET"))
                payload = tx.simulate_webhook("payment_request", data)
                result_msg.append(payload)
                tx.sudo()._process(tx.provider_code, payload)
            # Check state of payment
            elif tx.pagos360_adhesion_type == "adhesion":
                data = tx.provider_id._pagos360_make_request(
                    "/debit-request?id=%s" % tx.provider_reference, method="GET"
                )
                payload = tx.simulate_webhook("debit_request", data["data"][0])
                result_msg.append(payload)
                tx.sudo()._process(tx.provider_code, payload)

            elif tx.pagos360_adhesion_type == "card_adhesion":
                data = tx.provider_id._pagos360_make_request(
                    "/card-debit-request?id=%s" % tx.provider_reference, method="GET"
                )
                payload = self.simulate_webhook("card_debit_request", data["data"][0])
                result_msg.append(payload)
                tx.sudo()._process(tx.provider_code, payload)
            self.env.cr.commit()  # pylint: disable=invalid-commit
        return self.pagos360_readable_result(result_msg)

    def pagos360_cancel_transactions(self):
        for tx in self.filtered(lambda t: t.pagos360_adhesion_type in ["adhesion", "card_adhesion"]):
            payment_request_id = tx.provider_reference
            if tx.pagos360_adhesion_type == "adhesion":
                endpoint = "debit-request"
            elif tx.pagos360_adhesion_type == "card_adhesion":
                endpoint = "card-debit-request"
            else:
                continue
            pagos360_tx = tx.provider_id._pagos360_make_request(f"/{endpoint}/{payment_request_id}", method="GET")
            if pagos360_tx and pagos360_tx.get("state") == "pending":
                response_json = tx.provider_id._pagos360_make_request(
                    f"/{endpoint}/{payment_request_id}/cancel", method="PUT"
                )
                if response_json and response_json.get("state") == "canceled":
                    tx._set_canceled()
        return

    def _get_operation_info_from_data(self, request_info):
        for data in request_info["data"]:
            if data["external_reference"] == self.reference:
                return data
        return []

    def pagos360_readable_result(self, result_msg):
        txt = []
        for data in result_msg:
            txt += ["---------------------------"]
            txt += ["external_reference: %s" % data["payload"].get("external_reference")]
            txt += ["state: %s" % data["payload"].get("state")]
            txt += ["---------------------------"]
            txt += ["%s: %s" % (x, data[x]) for x in data if x != "payload"]
            txt += ["- %s: %s" % (x, data.get("payload", []).get(x)) for x in data.get("payload", [])]
            txt += ["---------------------------"]

        raise UserError("%s" % " \n".join(txt))

    def pagos360_spawn_child_charge(self):
        self._pagos360_spawn_child_charge()

    def _pagos360_spawn_child_charge(self):
        """Create and trigger a child charge transaction after an adhesion is signed.

        Called from `_apply_updates` when a Pagos 360 validation transaction is signed.
        Uses `pagos360_child_amount` (preserved at creation time by `_get_specific_create_values`)
        as the charge amount, propagates sale/invoice links so downstream reconciliation works,
        and triggers `_charge_with_token` to actually charge the customer.
        """
        self.ensure_one()
        if self.provider_code != "pagos360":
            return
        if self.operation != "validation":
            return
        if self.source_transaction_id:
            return
        if not self.token_id:
            return
        if self.child_transaction_ids.filtered(lambda c: c.operation == "online_token"):
            return

        amount = self.pagos360_child_amount
        if not amount:
            _logger.info(
                "Pagos 360: skipping child charge spawn for tx %s — pagos360_child_amount is empty",
                self.reference,
            )
            return

        child = self._create_child_transaction(
            amount,
            operation="online_token",
            **self._pagos360_get_child_link_vals(),
        )
        _logger.info(
            "Pagos 360: spawned child charge %s on token %s from validation tx %s.",
            child.reference,
            self.token_id.display_name,
            self.reference,
        )
        child._charge_with_token()

    def _pagos360_get_child_link_vals(self):
        """Return M2M commands to propagate sale order and invoice links to the child.

        Keeping the same links on the child cobro is what lets the standard Odoo flows
        (reconciliation, invoice payment, sale confirmation) react to the child's done
        state as if the user had paid directly.
        """
        self.ensure_one()
        link_vals = {}
        if "sale_order_ids" in self._fields and self.sale_order_ids:
            link_vals["sale_order_ids"] = [(6, 0, self.sale_order_ids.ids)]
        if "invoice_ids" in self._fields and self.invoice_ids:
            link_vals["invoice_ids"] = [(6, 0, self.invoice_ids.ids)]
        return link_vals

    def simulate_webhook(self, entity_name, data):
        if not data:
            _logger.warning("No data recieved")
            return
        return {"entity_name": entity_name, "entity_id": data["id"], "type": data["state"], "payload": data}
