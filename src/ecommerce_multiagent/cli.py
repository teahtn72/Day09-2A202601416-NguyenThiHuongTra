from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .orchestrator import CaseOrchestrator
from .model_runtime import OllamaRuntime
from .repository import OlistRepository
from .trace import TraceLogger


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Investigate Olist support cases with a hybrid multi-agent workflow")
    result.add_argument("--data-dir", default="data")
    result.add_argument("--input-dir", default="input")
    result.add_argument("--output-dir", default="output")
    result.add_argument("--trace", default="logging/trace.jsonl")
    result.add_argument("--case-concurrency", type=int, default=5)
    result.add_argument("--max-corrections", type=int, default=2)
    result.add_argument("--model-mode", choices=("off", "optional", "required"), default="off")
    result.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    result.add_argument("--model-timeout", type=float, default=300.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.case_concurrency < 1:
        raise SystemExit("--case-concurrency must be at least 1")
    input_paths = sorted(Path(args.input_dir).glob("EC_*.json"))
    if not input_paths:
        raise SystemExit(f"No EC_*.json files found in {args.input_dir}")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("EC_*.json"):
        stale.unlink()

    run_id = datetime.now().astimezone().strftime("run_%Y%m%d_%H%M%S")
    trace = TraceLogger(args.trace, run_id)
    repository = OlistRepository(args.data_dir)
    model_runtime = OllamaRuntime(args.ollama_url, args.model_mode, args.model_timeout)
    orchestrator = CaseOrchestrator(repository, output_dir, trace, args.max_corrections, model_runtime)
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=args.case_concurrency, thread_name_prefix="case") as pool:
        futures = {pool.submit(orchestrator.run_case, path): path for path in input_paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                future.result()
            except Exception as exc:  # Keep other isolated cases running and report all failures.
                failures[path.name] = str(exc)

    summary = {
        "run_id": run_id,
        "input_cases": len(input_paths),
        "outputs_written": len(list(output_dir.glob("EC_*.json"))),
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
