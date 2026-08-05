from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .agents import ROOT_CAUSES
from .utils import decimal, hours_between, money, parse_timestamp, unique


class OutputVerifier:
    """Independent deterministic checks over a policy draft and canonical packets."""

    name = "output_verifier"
    model_profile = "llama3:8b"
    LIMITS = {
        "affected_entities.order_ids": 5,
        "affected_entities.item_ids": 5,
        "affected_entities.seller_ids": 3,
        "affected_entities.payment_ids": 5,
        "customer_context.related_order_ids": 5,
        "product_context.product_ids": 5,
        "product_context.category_names": 5,
        "root_cause_analysis.ranked_causes": 3,
        "root_cause_analysis.responsible_parties": 3,
        "evidence_ids": 20,
        "resolution_actions": 5,
    }

    def verify(
        self,
        draft: dict[str, Any],
        bundle: dict[str, Any],
        routed: dict[str, Any],
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []

        def error(field: str, code: str, expected: Any = None, actual: Any = None, owner: str = "policy_adjudicator") -> None:
            item: dict[str, Any] = {"field": field, "error_code": code, "owner": owner}
            if expected is not None:
                item["expected"] = expected
            if actual is not None:
                item["actual"] = actual
            errors.append(item)

        required = {
            "case_id", "case_assessment", "affected_entities", "customer_context",
            "product_context", "delivery_analysis", "payment_reconciliation",
            "root_cause_analysis", "evidence_ids", "financial_resolution", "resolution_actions",
        }
        if set(draft) != required:
            error("$", "SCHEMA_ERROR", sorted(required), sorted(draft))
            return {"status": "fail", "errors": errors}

        pp = routed["payment_packet"]
        fp = routed["fulfillment_packet"]
        index = routed["canonical_source_index"]

        customer_packet = routed["customer_packet"]
        expected_related = [
            row["order_id"]
            for row in customer_packet["customer_orders"]
            if row["order_id"] != customer_packet["claimed_order_id"]
        ][:5]
        expected_customer_context = {
            "customer_unique_id": customer_packet["current_customer_unique_id"],
            "related_order_ids": expected_related,
        }
        if draft["customer_context"] != expected_customer_context:
            error("customer_context", "RELATED_ORDER_INVALID", expected_customer_context, draft["customer_context"], "customer_investigator")

        # Financial validation is recomputed from raw projected rows, not trusted from an agent.
        raw_items = pp["item_financial_rows"]
        raw_payments = pp["payment_rows"]
        item_total = sum((decimal(row["price"]) for row in raw_items), Decimal(0))
        freight_total = sum((decimal(row["freight_value"]) for row in raw_items), Decimal(0))
        payment_total = sum((decimal(row["payment_value"]) for row in raw_payments), Decimal(0))
        expected_total = item_total + freight_total if raw_items else None
        difference = payment_total - expected_total if expected_total is not None else None
        expected_recon = {
            "currency": "BRL",
            "item_total_brl": money(item_total),
            "freight_total_brl": money(freight_total),
            "expected_total_brl": money(expected_total) if expected_total is not None else None,
            "payment_total_brl": money(payment_total),
            "difference_brl": money(difference) if difference is not None else None,
            "reconciled": abs(difference) <= decimal("0.10") if difference is not None else None,
            "payment_types": unique(row["payment_type"] for row in raw_payments),
        }
        if draft["payment_reconciliation"] != expected_recon:
            error("payment_reconciliation", "PAYMENT_TOTAL_MISMATCH", expected_recon, draft["payment_reconciliation"], "payment_investigator")

        # Delivery and seller handoff calculations are also independently recomputed.
        order = fp["order"]
        expected_products = unique(row["product_id"] for row in fp["item_rows"] if row["product_id"])[:5]
        expected_categories = unique(row["category_name"] for row in fp["item_rows"] if row["category_name"])
        expected_product_context = {
            "product_ids": expected_products if fp["include_product_context"] else [],
            "category_names": expected_categories[:5] if fp["include_product_context"] else [],
        }
        if draft["product_context"] != expected_product_context:
            error("product_context", "PRODUCT_CONTEXT_ERROR", expected_product_context, draft["product_context"], "fulfillment_investigator")
        expected_timestamps = {
            "delivered_at": order["order_delivered_customer_date"],
            "estimated_delivery_at": order["order_estimated_delivery_date"],
            "carrier_handoff_at": order["order_delivered_carrier_date"],
        }
        for field, expected_value in expected_timestamps.items():
            if draft["delivery_analysis"].get(field) != expected_value:
                error(f"delivery_analysis.{field}", "DELIVERY_TIMESTAMP_ERROR", expected_value, draft["delivery_analysis"].get(field), "fulfillment_investigator")
        expected_delivery_variance = hours_between(
            order["order_delivered_customer_date"], order["order_estimated_delivery_date"]
        )
        if draft["delivery_analysis"].get("delivery_variance_hours") != expected_delivery_variance:
            error("delivery_analysis.delivery_variance_hours", "DELIVERY_VARIANCE_ERROR", expected_delivery_variance, draft["delivery_analysis"].get("delivery_variance_hours"), "fulfillment_investigator")

        by_seller: dict[str, list[dict[str, Any]]] = {}
        for row in fp["item_rows"]:
            by_seller.setdefault(row["seller_id"], []).append(row)
        expected_handoffs = []
        for seller_id, rows in by_seller.items():
            limits = [row["shipping_limit_date"] for row in rows if row["shipping_limit_date"]]
            earliest = min(limits, key=parse_timestamp) if limits else None
            variance = hours_between(order["order_delivered_carrier_date"], earliest)
            carrier_at, limit_at = parse_timestamp(order["order_delivered_carrier_date"]), parse_timestamp(earliest)
            expected_handoffs.append({
                "seller_id": seller_id,
                "shipping_limit_at": earliest,
                "handoff_variance_hours": variance,
                "late_handoff": bool(carrier_at and limit_at and carrier_at > limit_at),
            })
        expected_handoffs = expected_handoffs[:3]
        expected_late_sellers = [row["seller_id"] for row in expected_handoffs if row["late_handoff"]]
        if draft["delivery_analysis"].get("seller_handoff_analysis") != expected_handoffs:
            error("delivery_analysis.seller_handoff_analysis", "DELIVERY_VARIANCE_ERROR", expected_handoffs, draft["delivery_analysis"].get("seller_handoff_analysis"), "fulfillment_investigator")
        if draft["delivery_analysis"].get("late_handoff_seller_ids") != expected_late_sellers:
            error("delivery_analysis.late_handoff_seller_ids", "INVALID_LATE_SELLER", expected_late_sellers, draft["delivery_analysis"].get("late_handoff_seller_ids"), "fulfillment_investigator")

        # Re-run policy priority without calling the adjudicator implementation.
        status = order["order_status"]
        delivered_at = parse_timestamp(order["order_delivered_customer_date"])
        estimated_at = parse_timestamp(order["order_estimated_delivery_date"])
        late = bool(delivered_at and estimated_at and delivered_at > estimated_at)
        if status == "canceled" and payment_total > 0:
            expected_primary = "canceled_order_paid"
        elif status == "unavailable" and payment_total > 0:
            expected_primary = "unavailable_order_paid"
        elif late and expected_late_sellers:
            expected_primary = "late_delivery_seller"
        elif late:
            expected_primary = "late_delivery_logistics"
        elif len(raw_payments) >= 2 and expected_recon["reconciled"] is True:
            expected_primary = "valid_split_payment"
        elif not late and expected_recon["reconciled"] is True:
            expected_primary = "unsupported_late_claim"
        else:
            expected_primary = None
        actual_primary = draft["case_assessment"].get("primary_issue")
        if actual_primary != expected_primary:
            error("case_assessment.primary_issue", "POLICY_PRIORITY_VIOLATION", expected_primary, actual_primary)

        expected_secondary = []
        if len(raw_items) >= 2:
            expected_secondary.append("multi_item_order")
        if len(by_seller) >= 2:
            expected_secondary.append("multi_seller_order")
        if len(raw_payments) >= 2:
            expected_secondary.append("split_payment")
        if expected_related:
            expected_secondary.append("repeat_customer")
        if len(expected_categories) >= 2:
            expected_secondary.append("multiple_categories")
        if draft["case_assessment"].get("secondary_issues") != expected_secondary:
            error("case_assessment.secondary_issues", "POLICY_SECONDARY_ORDER_ERROR", expected_secondary, draft["case_assessment"].get("secondary_issues"))

        expected_root = ROOT_CAUSES.get(expected_primary or "")
        ranked = draft["root_cause_analysis"].get("ranked_causes")
        if ranked != ([{"cause_code": expected_root, "rank": 1}] if expected_root else []):
            error("root_cause_analysis.ranked_causes", "POLICY_ROOT_CAUSE_ERROR", expected_root, ranked)

        if expected_primary in {"canceled_order_paid", "unavailable_order_paid"}:
            expected_parties = [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
            expected_refund, main = expected_recon["payment_total_brl"], "issue_full_refund"
        elif expected_primary == "late_delivery_seller":
            expected_parties = [{"party_type": "seller", "party_id": value} for value in expected_late_sellers]
            expected_refund, main = expected_recon["freight_total_brl"], "refund_freight"
        elif expected_primary == "late_delivery_logistics":
            expected_parties = [{"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}]
            expected_refund, main = expected_recon["freight_total_brl"], "refund_freight"
        elif expected_primary == "valid_split_payment":
            expected_parties, expected_refund, main = [], 0.0, "explain_valid_split_payment"
        else:
            expected_parties, expected_refund, main = [], 0.0, "reject_late_refund"
        if draft["root_cause_analysis"].get("responsible_parties") != expected_parties:
            error("root_cause_analysis.responsible_parties", "RESPONSIBILITY_ERROR", expected_parties, draft["root_cause_analysis"].get("responsible_parties"))
        actual_refund = draft["financial_resolution"].get("recommended_refund_brl")
        if actual_refund != expected_refund:
            error("financial_resolution.recommended_refund_brl", "REFUND_RULE_ERROR", expected_refund, actual_refund)
        expected_status = "action_required" if expected_refund > 0 else "no_action"
        if draft["case_assessment"].get("case_status") != expected_status:
            error("case_assessment.case_status", "CASE_STATUS_ERROR", expected_status, draft["case_assessment"].get("case_status"))

        expected_actions = [main]
        if expected_primary == "late_delivery_seller":
            expected_actions.append("review_seller_handoff")
        elif expected_primary == "late_delivery_logistics":
            expected_actions.append("review_carrier_delay")
        if expected_refund > 0:
            expected_actions.append("verify_refund_completion")
        if len(by_seller) >= 2:
            expected_actions.append("coordinate_multi_seller_case")
        if len(raw_payments) >= 2 and expected_primary != "valid_split_payment":
            expected_actions.append("verify_payment_allocation")
        if draft["resolution_actions"] != expected_actions[:5]:
            error("resolution_actions", "ACTION_ORDER_ERROR", expected_actions[:5], draft["resolution_actions"])

        expected_entities = {
            "order_ids": index["valid_order_ids"][:5],
            "item_ids": index["valid_item_ids"][:5],
            "seller_ids": index["valid_seller_ids"][:3],
            "payment_ids": index["valid_payment_ids"][:5],
        }
        if draft["affected_entities"] != expected_entities:
            error("affected_entities", "UNKNOWN_AFFECTED_ENTITY", expected_entities, draft["affected_entities"])

        evidence = [f"order:{value}" for value in expected_entities["order_ids"]]
        evidence += [f"item:{value}" for value in expected_entities["item_ids"]]
        evidence += [f"payment:{value}" for value in expected_entities["payment_ids"]]
        if expected_primary == "late_delivery_seller":
            evidence += [f"seller:{value}" for value in expected_late_sellers]
        if expected_root:
            evidence.append(f"policy:{expected_root}")
        if draft["evidence_ids"] != evidence[:20]:
            error("evidence_ids", "UNKNOWN_EVIDENCE_ID", evidence[:20], draft["evidence_ids"])

        confidence = draft["case_assessment"].get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            error("case_assessment.confidence", "SCHEMA_ERROR", "number in [0, 1]", confidence)
        for field, limit in self.LIMITS.items():
            value: Any = draft
            for part in field.split("."):
                value = value.get(part, []) if isinstance(value, dict) else []
            if not isinstance(value, list) or len(value) > limit:
                error(field, "ARRAY_LIMIT_EXCEEDED", limit, len(value) if isinstance(value, list) else type(value).__name__)

        for field in ("delivered_at", "estimated_delivery_at", "carrier_handoff_at"):
            value = draft["delivery_analysis"].get(field)
            try:
                if value is not None:
                    datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                error(f"delivery_analysis.{field}", "SCHEMA_ERROR", "YYYY-MM-DD HH:MM:SS or null", value)

        return {"status": "pass" if not errors else "fail", "errors": errors}
