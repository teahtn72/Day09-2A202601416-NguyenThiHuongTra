from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .utils import decimal, money


class DataError(ValueError):
    pass


class OlistRepository:
    """Read-only indexes over the Olist CSV files.

    Raw rows remain in this deterministic data layer. Investigators only receive
    projected packets created by :meth:`route_case`.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.orders = self._by_key("olist_orders_dataset.csv", "order_id")
        self.customers = self._by_key("olist_customers_dataset.csv", "customer_id")
        self.items = self._grouped("olist_order_items_dataset.csv", "order_id")
        self.payments = self._grouped("olist_order_payments_dataset.csv", "order_id")
        self.products = self._by_key("olist_products_dataset.csv", "product_id")

        self.customer_orders: dict[str, list[dict[str, str]]] = defaultdict(list)
        for order in self.orders.values():
            customer = self.customers.get(order["customer_id"])
            if customer:
                self.customer_orders[customer["customer_unique_id"]].append(order)
        for rows in self.customer_orders.values():
            rows.sort(key=lambda row: (row.get("order_purchase_timestamp", ""), row["order_id"]))

    def _rows(self, filename: str) -> list[dict[str, str]]:
        path = self.data_dir / filename
        if not path.is_file():
            raise DataError(f"Missing dataset: {path}")
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def _by_key(self, filename: str, key: str) -> dict[str, dict[str, str]]:
        return {row[key]: row for row in self._rows(filename)}

    def _grouped(self, filename: str, key: str) -> dict[str, list[dict[str, str]]]:
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self._rows(filename):
            grouped[row[key]].append(row)
        return grouped

    @staticmethod
    def load_case(path: str | Path) -> dict[str, Any]:
        with Path(path).open(encoding="utf-8") as handle:
            case = json.load(handle)
        required = {"case_id", "customer_request", "investigation_scope", "policy_version"}
        missing = required - case.keys()
        if missing:
            raise DataError(f"Input is missing fields: {sorted(missing)}")
        if case["policy_version"] != "EC_POLICY_V2":
            raise DataError(f"Unsupported policy_version: {case['policy_version']}")
        if not case["customer_request"].get("claimed_order_id"):
            raise DataError("customer_request.claimed_order_id is required")
        if not case["customer_request"].get("message"):
            raise DataError("customer_request.message is required")
        return case

    def route_case(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = case["case_id"]
        order_id = case["customer_request"]["claimed_order_id"]
        order = self.orders.get(order_id)
        if order is None:
            raise DataError(f"{case_id}: unknown claimed_order_id {order_id}")

        customer = self.customers.get(order["customer_id"])
        if customer is None:
            raise DataError(f"{case_id}: customer {order['customer_id']} not found")

        item_rows = self.items.get(order_id, [])
        payment_rows = self.payments.get(order_id, [])
        include_history = bool(case["investigation_scope"].get("include_customer_history"))
        include_products = bool(case["investigation_scope"].get("include_product_context"))
        related = self.customer_orders.get(customer["customer_unique_id"], []) if include_history else []

        customer_packet = {
            "case_id": case_id,
            "claimed_order_id": order_id,
            "current_customer_id": customer["customer_id"],
            "current_customer_unique_id": customer["customer_unique_id"],
            "customer_orders": [
                {
                    "order_id": row["order_id"],
                    "customer_id": row["customer_id"],
                    "order_purchase_timestamp": row["order_purchase_timestamp"] or None,
                    "order_status": row["order_status"],
                }
                for row in related
            ],
        }
        item_total = sum((decimal(row["price"]) for row in item_rows), decimal(0))
        freight_total = sum((decimal(row["freight_value"]) for row in item_rows), decimal(0))
        payment_total = sum((decimal(row["payment_value"]) for row in payment_rows), decimal(0))
        expected_total = item_total + freight_total if item_rows else None
        difference = payment_total - expected_total if expected_total is not None else None
        reconciled = abs(difference) <= decimal("0.10") if difference is not None else None

        payment_packet = {
            "case_id": case_id,
            "order_id": order_id,
            "item_financial_rows": [
                {
                    "order_item_id": int(row["order_item_id"]),
                    "price": row["price"],
                    "freight_value": row["freight_value"],
                }
                for row in item_rows
            ],
            "payment_rows": [
                {
                    "payment_sequential": int(row["payment_sequential"]),
                    "payment_type": row["payment_type"],
                    "payment_installments": int(row["payment_installments"]),
                    "payment_value": row["payment_value"],
                }
                for row in payment_rows
            ],
        }
        fulfillment_packet = {
            "case_id": case_id,
            "order": {
                "order_id": order_id,
                "order_status": order["order_status"],
                "order_delivered_customer_date": order["order_delivered_customer_date"] or None,
                "order_estimated_delivery_date": order["order_estimated_delivery_date"] or None,
                "order_delivered_carrier_date": order["order_delivered_carrier_date"] or None,
            },
            "item_rows": [
                {
                    "order_item_id": int(row["order_item_id"]),
                    "product_id": row["product_id"],
                    "seller_id": row["seller_id"],
                    "shipping_limit_date": row["shipping_limit_date"] or None,
                    "category_name": (
                        self.products.get(row["product_id"], {}).get("product_category_name") or None
                    ) if include_products else None,
                }
                for row in item_rows
            ],
            "include_product_context": include_products,
        }

        item_ids = [f"{order_id}:{row['order_item_id']}" for row in item_rows]
        payment_ids = [f"{order_id}:{row['payment_sequential']}" for row in payment_rows]
        seller_ids = list(dict.fromkeys(row["seller_id"] for row in item_rows))
        claim_packet = {
            "case_id": case_id,
            "claimed_order_id": order_id,
            "customer_request": {
                "language": case["customer_request"].get("language"),
                "message": case["customer_request"]["message"],
            },
            "order": {
                "order_id": order_id,
                "order_status": order["order_status"],
                "order_delivered_customer_date": order["order_delivered_customer_date"] or None,
                "order_estimated_delivery_date": order["order_estimated_delivery_date"] or None,
                "order_delivered_carrier_date": order["order_delivered_carrier_date"] or None,
            },
            "item_row_count": len(item_rows),
            "payment_row_count": len(payment_rows),
            "item_total_brl": money(item_total),
            "freight_total_brl": money(freight_total),
            "payment_total_brl": money(payment_total),
            "expected_total_brl": money(expected_total) if expected_total is not None else None,
            "difference_brl": money(difference) if difference is not None else None,
            "reconciled": reconciled,
        }
        return {
            "customer_packet": customer_packet,
            "payment_packet": payment_packet,
            "fulfillment_packet": fulfillment_packet,
            "claim_packet": claim_packet,
            "canonical_source_index": {
                "valid_order_ids": [order_id],
                "valid_item_ids": item_ids,
                "valid_payment_ids": payment_ids,
                "valid_seller_ids": seller_ids,
            },
        }
