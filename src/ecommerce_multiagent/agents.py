from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .utils import decimal, hours_between, money, parse_timestamp, unique

ROOT_CAUSES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_AFTER_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERED_AFTER_ESTIMATE",
    "valid_split_payment": "MULTIPLE_PAYMENTS_RECONCILED",
    "unsupported_late_claim": "DELIVERY_WITHIN_ESTIMATE",
}


class CustomerInvestigator:
    name = "customer_investigator"
    model_profile = "qwen2.5:7b"

    def investigate(self, packet: dict[str, Any]) -> dict[str, Any]:
        current = packet["claimed_order_id"]
        related = [row["order_id"] for row in packet["customer_orders"] if row["order_id"] != current][:5]
        return {
            "agent": self.name,
            "case_id": packet["case_id"],
            "customer_unique_id": packet["current_customer_unique_id"],
            "related_order_ids": related,
            "repeat_customer": bool(related),
            "source_row_counts": {
                "customer_rows": 1,
                "related_order_rows": len([r for r in packet["customer_orders"] if r["order_id"] != current]),
            },
            "warnings": [],
        }


class PaymentInvestigator:
    name = "payment_investigator"
    model_profile = "qwen2.5:7b"

    def investigate(self, packet: dict[str, Any]) -> dict[str, Any]:
        items = packet["item_financial_rows"]
        payments = packet["payment_rows"]
        item_total = sum((decimal(row["price"]) for row in items), Decimal(0))
        freight_total = sum((decimal(row["freight_value"]) for row in items), Decimal(0))
        payment_total = sum((decimal(row["payment_value"]) for row in payments), Decimal(0))
        expected = item_total + freight_total if items else None
        difference = payment_total - expected if expected is not None else None
        reconciled = abs(difference) <= decimal("0.10") if difference is not None else None
        order_id = packet["order_id"]
        return {
            "agent": self.name,
            "case_id": packet["case_id"],
            "payment_reconciliation": {
                "currency": "BRL",
                "item_total_brl": money(item_total),
                "freight_total_brl": money(freight_total),
                "expected_total_brl": money(expected) if expected is not None else None,
                "payment_total_brl": money(payment_total),
                "difference_brl": money(difference) if difference is not None else None,
                "reconciled": reconciled,
                "payment_types": unique(row["payment_type"] for row in payments),
            },
            "payment_ids": [f"{order_id}:{row['payment_sequential']}" for row in payments][:5],
            "payment_row_count": len(payments),
            "split_payment": len(payments) >= 2,
            "warnings": [],
        }


class FulfillmentInvestigator:
    name = "fulfillment_investigator"
    model_profile = "qwen2.5:7b"

    def investigate(self, packet: dict[str, Any]) -> dict[str, Any]:
        order = packet["order"]
        rows = packet["item_rows"]
        order_id = order["order_id"]
        delivered = order["order_delivered_customer_date"]
        estimated = order["order_estimated_delivery_date"]
        carrier = order["order_delivered_carrier_date"]
        variance = hours_between(delivered, estimated)
        delivered_at, estimated_at = parse_timestamp(delivered), parse_timestamp(estimated)
        delivered_late = bool(delivered_at and estimated_at and delivered_at > estimated_at)

        seller_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            seller_rows[row["seller_id"]].append(row)
        handoff_analysis: list[dict[str, Any]] = []
        for seller_id, seller_items in seller_rows.items():
            limits = [row["shipping_limit_date"] for row in seller_items if row["shipping_limit_date"]]
            earliest = min(limits, key=parse_timestamp) if limits else None
            handoff_variance = hours_between(carrier, earliest)
            carrier_at, limit_at = parse_timestamp(carrier), parse_timestamp(earliest)
            handoff_analysis.append({
                "seller_id": seller_id,
                "shipping_limit_at": earliest,
                "handoff_variance_hours": handoff_variance,
                "late_handoff": bool(carrier_at and limit_at and carrier_at > limit_at),
            })
        late_sellers = [row["seller_id"] for row in handoff_analysis if row["late_handoff"]]
        product_ids = unique(row["product_id"] for row in rows if row["product_id"])
        categories = unique(row["category_name"] for row in rows if row["category_name"])
        seller_ids = list(seller_rows)
        return {
            "agent": self.name,
            "case_id": packet["case_id"],
            "order_status": order["order_status"],
            "affected_entities": {
                "order_ids": [order_id],
                "item_ids": [f"{order_id}:{row['order_item_id']}" for row in rows][:5],
                "seller_ids": seller_ids[:3],
            },
            "product_context": {
                "product_ids": product_ids[:5] if packet["include_product_context"] else [],
                "category_names": categories[:5] if packet["include_product_context"] else [],
            },
            "delivery_analysis": {
                "delivered_at": delivered,
                "estimated_delivery_at": estimated,
                "carrier_handoff_at": carrier,
                "delivery_variance_hours": variance,
                "seller_handoff_analysis": handoff_analysis[:3],
                "late_handoff_seller_ids": late_sellers[:3],
            },
            "facts": {
                "delivered_late": delivered_late,
                "multi_item_order": len(rows) >= 2,
                "multi_seller_order": len(seller_ids) >= 2,
                "multiple_categories": len(categories) >= 2,
                "item_row_count": len(rows),
            },
            "warnings": [],
        }


class PolicyAdjudicator:
    name = "policy_adjudicator"
    model_profile = "llama3:8b"

    @staticmethod
    def select_issue(payment: dict[str, Any], fulfillment: dict[str, Any]) -> str:
        status = fulfillment["order_status"]
        reconciliation = payment["payment_reconciliation"]
        paid = reconciliation["payment_total_brl"] > 0
        late = fulfillment["facts"]["delivered_late"]
        late_sellers = fulfillment["delivery_analysis"]["late_handoff_seller_ids"]
        if status == "canceled" and paid:
            return "canceled_order_paid"
        if status == "unavailable" and paid:
            return "unavailable_order_paid"
        if late and late_sellers:
            return "late_delivery_seller"
        if late and not late_sellers:
            return "late_delivery_logistics"
        if payment["payment_row_count"] >= 2 and reconciliation["reconciled"] is True:
            return "valid_split_payment"
        if not late and reconciliation["reconciled"] is True:
            return "unsupported_late_claim"
        raise ValueError("No EC_POLICY_V2 primary issue matches the canonical facts")

    def adjudicate(self, bundle: dict[str, Any], policy_version: str) -> dict[str, Any]:
        if policy_version != "EC_POLICY_V2":
            raise ValueError(f"Unsupported policy: {policy_version}")
        customer = bundle["customer_report"]
        payment = bundle["payment_report"]
        fulfillment = bundle["fulfillment_report"]
        primary = self.select_issue(payment, fulfillment)
        root_cause = ROOT_CAUSES[primary]
        recon = payment["payment_reconciliation"]
        facts = fulfillment["facts"]

        secondary: list[str] = []
        if facts["multi_item_order"]:
            secondary.append("multi_item_order")
        if facts["multi_seller_order"]:
            secondary.append("multi_seller_order")
        if payment["split_payment"]:
            secondary.append("split_payment")
        if customer["repeat_customer"]:
            secondary.append("repeat_customer")
        if facts["multiple_categories"]:
            secondary.append("multiple_categories")

        parties: list[dict[str, str]] = []
        if primary in {"canceled_order_paid", "unavailable_order_paid"}:
            parties = [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
            refund = recon["payment_total_brl"]
            main_action = "issue_full_refund"
        elif primary == "late_delivery_seller":
            parties = [
                {"party_type": "seller", "party_id": seller_id}
                for seller_id in fulfillment["delivery_analysis"]["late_handoff_seller_ids"][:3]
            ]
            refund = recon["freight_total_brl"]
            main_action = "refund_freight"
        elif primary == "late_delivery_logistics":
            parties = [{"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}]
            refund = recon["freight_total_brl"]
            main_action = "refund_freight"
        elif primary == "valid_split_payment":
            refund, main_action = 0.0, "explain_valid_split_payment"
        else:
            refund, main_action = 0.0, "reject_late_refund"

        actions = [main_action]
        if primary == "late_delivery_seller":
            actions.append("review_seller_handoff")
        elif primary == "late_delivery_logistics":
            actions.append("review_carrier_delay")
        if refund > 0:
            actions.append("verify_refund_completion")
        if facts["multi_seller_order"]:
            actions.append("coordinate_multi_seller_case")
        if payment["split_payment"] and primary != "valid_split_payment":
            actions.append("verify_payment_allocation")

        affected = {
            **fulfillment["affected_entities"],
            "payment_ids": payment["payment_ids"],
        }
        order_id = affected["order_ids"][0]
        evidence = [f"order:{order_id}"]
        evidence.extend(f"item:{value}" for value in affected["item_ids"])
        evidence.extend(f"payment:{value}" for value in affected["payment_ids"])
        if primary == "late_delivery_seller":
            evidence.extend(f"seller:{party['party_id']}" for party in parties)
        evidence.append(f"policy:{root_cause}")

        return {
            "case_id": bundle["case_id"],
            "case_assessment": {
                "primary_issue": primary,
                "secondary_issues": secondary,
                "case_status": "action_required" if refund > 0 else "no_action",
                "confidence": 1.0,
            },
            "affected_entities": affected,
            "customer_context": {
                "customer_unique_id": customer["customer_unique_id"],
                "related_order_ids": customer["related_order_ids"],
            },
            "product_context": fulfillment["product_context"],
            "delivery_analysis": fulfillment["delivery_analysis"],
            "payment_reconciliation": recon,
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": root_cause, "rank": 1}],
                "responsible_parties": parties,
            },
            "evidence_ids": evidence[:20],
            "financial_resolution": {"currency": "BRL", "recommended_refund_brl": money(decimal(refund))},
            "resolution_actions": actions[:5],
        }
