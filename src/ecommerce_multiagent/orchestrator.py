from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .agents import CustomerInvestigator, FulfillmentInvestigator, PaymentInvestigator, PolicyAdjudicator
from .repository import OlistRepository
from .trace import TraceLogger
from .verifier import OutputVerifier


class ValidationFailure(RuntimeError):
    pass


class CaseOrchestrator:
    """Supervisor: parallel investigation, sequential policy, correction, and write."""

    def __init__(self, repository: OlistRepository, output_dir: str | Path, trace: TraceLogger, max_corrections: int = 2) -> None:
        self.repository = repository
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.trace = trace
        self.max_corrections = max_corrections
        self.customer = CustomerInvestigator()
        self.payment = PaymentInvestigator()
        self.fulfillment = FulfillmentInvestigator()
        self.policy = PolicyAdjudicator()
        self.verifier = OutputVerifier()

    def _run_agent(self, case_id: str, agent: Any, packet: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self.trace.event(
            case_id,
            agent.name,
            "agent_started",
            input_ref=f"{agent.name.replace('_investigator', '')}_packet:{case_id}",
            model=agent.model_profile,
            execution_mode="deterministic_tools",
        )
        report = agent.investigate(packet)
        summary = {"warning_count": len(report.get("warnings", []))}
        if agent.name == "customer_investigator":
            summary.update({"repeat_customer": report["repeat_customer"], "related_order_count": len(report["related_order_ids"])})
        elif agent.name == "payment_investigator":
            summary.update({"payment_total_brl": report["payment_reconciliation"]["payment_total_brl"], "reconciled": report["payment_reconciliation"]["reconciled"], "payment_row_count": report["payment_row_count"]})
        else:
            summary.update({"delivered_late": report["facts"]["delivered_late"], "late_seller_count": len(report["delivery_analysis"]["late_handoff_seller_ids"])})
        self.trace.event(case_id, agent.name, "agent_completed", output_summary=summary, duration_ms=round((time.perf_counter() - started) * 1000, 2))
        return report

    def run_case(self, case_path: str | Path) -> dict[str, Any]:
        case = self.repository.load_case(case_path)
        case_id = case["case_id"]
        self.trace.event(case_id, "case_orchestrator", "case_started", input_ref=Path(case_path).name)
        routed = self.repository.route_case(case)
        self.trace.event(
            case_id, "data_router", "data_routed",
            output_summary={
                "customer_order_rows": len(routed["customer_packet"]["customer_orders"]),
                "financial_item_rows": len(routed["payment_packet"]["item_financial_rows"]),
                "payment_rows": len(routed["payment_packet"]["payment_rows"]),
                "fulfillment_item_rows": len(routed["fulfillment_packet"]["item_rows"]),
            },
        )

        jobs = {
            "customer_report": (self.customer, routed["customer_packet"]),
            "payment_report": (self.payment, routed["payment_packet"]),
            "fulfillment_report": (self.fulfillment, routed["fulfillment_packet"]),
        }
        reports: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"{case_id}-investigator") as pool:
            future_names = {
                pool.submit(self._run_agent, case_id, agent, packet): name
                for name, (agent, packet) in jobs.items()
            }
            for future in as_completed(future_names):
                reports[future_names[future]] = future.result()
        bundle = {"case_id": case_id, **reports}

        for correction_round in range(self.max_corrections + 1):
            started = time.perf_counter()
            self.trace.event(case_id, self.policy.name, "agent_started", correction_round=correction_round, model=self.policy.model_profile, execution_mode="deterministic_rule_engine")
            draft = self.policy.adjudicate(bundle, case["policy_version"])
            self.trace.event(case_id, self.policy.name, "agent_completed", output_summary={"primary_issue": draft["case_assessment"]["primary_issue"], "refund_brl": draft["financial_resolution"]["recommended_refund_brl"]}, duration_ms=round((time.perf_counter() - started) * 1000, 2))

            checked = self.verifier.verify(draft, bundle, routed)
            if checked["status"] == "pass":
                self.trace.event(case_id, self.verifier.name, "verification_passed", correction_round=correction_round)
                self._write(case_path, draft)
                self.trace.event(case_id, "deterministic_writer", "output_written", output_ref=Path(case_path).name)
                self.trace.event(case_id, "case_orchestrator", "case_completed", status="completed", correction_rounds=correction_round)
                return draft

            self.trace.event(case_id, self.verifier.name, "verification_failed", correction_round=correction_round, errors=checked["errors"])
            if correction_round == self.max_corrections:
                break
            owners = {item["owner"] for item in checked["errors"]}
            self.trace.event(case_id, "correction_router", "correction_requested", owners=sorted(owners), error_count=len(checked["errors"]))
            # Re-run only the report owners named by structured verifier errors.
            reruns = {
                "customer_investigator": ("customer_report", self.customer, routed["customer_packet"]),
                "payment_investigator": ("payment_report", self.payment, routed["payment_packet"]),
                "fulfillment_investigator": ("fulfillment_report", self.fulfillment, routed["fulfillment_packet"]),
            }
            for owner in owners:
                if owner in reruns:
                    report_name, agent, packet = reruns[owner]
                    bundle[report_name] = self._run_agent(case_id, agent, packet)
            self.trace.event(case_id, "correction_router", "correction_completed", owners=sorted(owners))

        self.trace.event(case_id, "case_orchestrator", "case_completed", status="failed_validation")
        raise ValidationFailure(f"{case_id} failed validation after {self.max_corrections} correction rounds")

    def _write(self, case_path: str | Path, draft: dict[str, Any]) -> None:
        destination = self.output_dir / Path(case_path).name
        temporary = destination.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(draft, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(destination)
