from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ecommerce_multiagent.agents import CustomerInvestigator, FulfillmentInvestigator, PaymentInvestigator, PolicyAdjudicator
from ecommerce_multiagent.repository import OlistRepository
from ecommerce_multiagent.verifier import OutputVerifier


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = OlistRepository(ROOT / "data")

    def build(self, case_name: str):
        case = self.repo.load_case(ROOT / "input" / case_name)
        routed = self.repo.route_case(case)
        bundle = {
            "case_id": case["case_id"],
            "customer_report": CustomerInvestigator().investigate(routed["customer_packet"]),
            "payment_report": PaymentInvestigator().investigate(routed["payment_packet"]),
            "fulfillment_report": FulfillmentInvestigator().investigate(routed["fulfillment_packet"]),
        }
        draft = PolicyAdjudicator().adjudicate(bundle, case["policy_version"])
        return draft, bundle, routed

    def test_all_six_policy_branches(self) -> None:
        expected = {
            "EC_001.json": "unsupported_late_claim",
            "EC_002.json": "late_delivery_seller",
            "EC_003.json": "late_delivery_logistics",
            "EC_004.json": "canceled_order_paid",
            "EC_008.json": "valid_split_payment",
            "EC_012.json": "unavailable_order_paid",
        }
        for filename, primary in expected.items():
            with self.subTest(filename=filename):
                draft, bundle, routed = self.build(filename)
                self.assertEqual(primary, draft["case_assessment"]["primary_issue"])
                self.assertEqual("pass", OutputVerifier().verify(draft, bundle, routed)["status"])

    def test_no_item_null_contract(self) -> None:
        draft, _, _ = self.build("EC_012.json")
        reconciliation = draft["payment_reconciliation"]
        self.assertIsNone(reconciliation["expected_total_brl"])
        self.assertIsNone(reconciliation["difference_brl"])
        self.assertIsNone(reconciliation["reconciled"])
        self.assertEqual([], draft["affected_entities"]["item_ids"])
        self.assertEqual([], draft["delivery_analysis"]["seller_handoff_analysis"])

    def test_verifier_rejects_policy_priority_violation(self) -> None:
        draft, bundle, routed = self.build("EC_004.json")
        draft["case_assessment"]["primary_issue"] = "valid_split_payment"
        result = OutputVerifier().verify(draft, bundle, routed)
        self.assertEqual("fail", result["status"])
        self.assertIn("POLICY_PRIORITY_VIOLATION", {error["error_code"] for error in result["errors"]})

    def test_all_fifty_cases_pass_independent_verification(self) -> None:
        paths = sorted((ROOT / "input").glob("EC_*.json"))
        self.assertEqual(50, len(paths))
        for path in paths:
            with self.subTest(filename=path.name):
                draft, bundle, routed = self.build(path.name)
                result = OutputVerifier().verify(draft, bundle, routed)
                self.assertEqual([], result["errors"])


if __name__ == "__main__":
    unittest.main()
