from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .orchestrator import CaseOrchestrator
from .model_runtime import OllamaRuntime, OpenAIRuntime
from .repository import OlistRepository
from .trace import TraceLogger


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Investigate Olist support cases with a hybrid multi-agent workflow")
    result.add_argument("--data-dir", default="data")
    result.add_argument("--input-dir", default="input")
    result.add_argument("--output-dir", default="output")
    result.add_argument("--trace", default="logging/trace.jsonl")
    result.add_argument("--case-concurrency", type=int, default=5)
    result.add_argument("--max-corrections", type=int, default=2)
    result.add_argument("--model-provider", choices=("ollama", "openai"), default=os.getenv("MODEL_PROVIDER", "ollama"))
    result.add_argument("--model", default=None, help="Override every agent model for the selected provider")
    result.add_argument("--model-mode", choices=("off", "optional", "required"), default=os.getenv("MODEL_MODE", "off"))
    result.add_argument("--ollama-url", default=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    result.add_argument("--openai-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    result.add_argument("--model-timeout", type=float, default=300.0)
    result.add_argument("--resume-failed", action="store_true", help="Keep valid outputs/trace and rerun only cases whose output is missing")
    return result


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = parser().parse_args(argv)
    if args.case_concurrency < 1:
        raise SystemExit("--case-concurrency must be at least 1")
    input_paths = sorted(Path(args.input_dir).glob("EC_*.json"))
    if not input_paths:
        raise SystemExit(f"No EC_*.json files found in {args.input_dir}")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.resume_failed:
        input_paths = [path for path in input_paths if not (output_dir / path.name).is_file()]
        if not input_paths:
            print(json.dumps({"status": "nothing_to_resume", "outputs_written": len(list(output_dir.glob('EC_*.json')))}, indent=2))
            return 0
    else:
        for stale in output_dir.glob("EC_*.json"):
            stale.unlink()

    run_id = datetime.now().astimezone().strftime("run_%Y%m%d_%H%M%S")
    if args.resume_failed and Path(args.trace).is_file():
        with Path(args.trace).open(encoding="utf-8") as handle:
            first = next((json.loads(line) for line in handle if line.strip()), None)
        if first:
            run_id = first["run_id"]
    trace = TraceLogger(args.trace, run_id, truncate=not args.resume_failed)
    repository = OlistRepository(args.data_dir)
    if args.model_provider == "openai":
        model_runtime = OpenAIRuntime(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=args.model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            base_url=args.openai_url,
            mode=args.model_mode,
            timeout=args.model_timeout,
        )
    else:
        model_runtime = OllamaRuntime(
            args.ollama_url,
            args.model_mode,
            args.model_timeout,
            model_override=args.model,
        )
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
