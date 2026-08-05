from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


class TraceLogger:
    def __init__(self, path: str | Path, run_id: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.run_id = run_id
        self._lock = threading.Lock()

    def event(self, case_id: str, agent: str, event: str, **details: Any) -> None:
        record = {
            "run_id": self.run_id,
            "case_id": case_id,
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "agent": agent,
            "event": event,
            **details,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
