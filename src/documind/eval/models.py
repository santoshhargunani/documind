"""
models.py
---------
Shared data models for the eval pipeline, used by both run_eval.py
(the harness) and bigquery_writer.py (the storage layer). Kept
separate from run_eval.py itself specifically so it's importable as
part of the documind package, not stuck in scripts/ where nothing
else can reach it.
"""

from pydantic import BaseModel


class EvalResult(BaseModel):
    """
    The outcome of running one EvalCase through the graph — captures
    everything needed to both report a pass/fail verdict AND diagnose
    WHY a case failed, without re-running it.
    """

    question: str
    passed: bool
    is_grounded: bool | None
    retry_count: int
    latency_seconds: float
    final_answer: str
    missing_keywords: list[str]