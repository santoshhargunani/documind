"""
run_eval.py
-----------
The actual eval harness: runs every case in golden_dataset.py through
the real DocuMind agent graph, measures whether each one passed, and
prints a summary report with aggregate metrics.

Run with:  python scripts\\run_eval.py
"""

# --- IMPORTS ---

import time
# Standard library. Used to measure wall-clock latency for each eval
# case — time.perf_counter() is the right choice for measuring
# elapsed duration (as opposed to time.time(), which measures
# wall-clock time and can jump around due to system clock adjustments
# — perf_counter is monotonic and specifically designed for timing
# code execution).

from pydantic import BaseModel
# Used for EvalResult below — same reasoning as every other typed
# model in this project.

from documind.agents.graph import build_graph
# The compiled LangGraph graph — the exact same object the FastAPI
# service and MCP server both use. Running eval against this ensures
# we're measuring the REAL system's behavior, not a simplified
# stand-in.

from documind.eval.golden_dataset import GOLDEN_DATASET, EvalCase


# --- RESULT MODEL ---

from documind.eval.models import EvalResult
# --- SINGLE-CASE EVALUATION ---

def _evaluate_case(graph, case: EvalCase) -> EvalResult:
    """
    Runs one EvalCase through the graph and scores the result against
    that case's expectations.
    """

    start = time.perf_counter()
    result = graph.invoke(
        {"question": case.question, "retry_count": 0, "retrieved_chunks": []}
    )
    latency = time.perf_counter() - start
    # Wraps the exact same graph.invoke(...) call used everywhere else
    # in this project (test_graph.py, api.py, mcp_server.py) — timed
    # precisely around just the graph execution, not any surrounding
    # setup, so this latency number is a fair, comparable measurement
    # across every eval run.

    final_answer = result.get("final_answer", "")
    answer_lower = final_answer.lower()

    missing_keywords = [
        kw for kw in case.expected_keywords if kw.lower() not in answer_lower
    ]
    # A list comprehension checking each expected keyword against the
    # answer (case-insensitive on both sides). Any keyword NOT found
    # ends up in this list — an empty list means every keyword was
    # present, i.e. full recall on this case.

    if case.should_be_answerable:
        # Normal case: pass requires ALL expected keywords present.
        passed = len(missing_keywords) == 0
    else:
        # Deliberately-unanswerable case (like the pizza-topping
        # question): pass means the system correctly DIDN'T produce a
        # confident, specific answer — we treat this as passing if the
        # critic did NOT mark it grounded, OR if the router rejected
        # it outright (is_grounded would be None in that path, since
        # the critic never ran at all). Either honest outcome counts
        # as a pass; only a confidently-grounded hallucinated answer
        # to an unanswerable question counts as a failure.
        passed = result.get("is_grounded") is not True

    return EvalResult(
        question=case.question,
        passed=passed,
        is_grounded=result.get("is_grounded"),
        retry_count=result.get("retry_count", 0),
        latency_seconds=round(latency, 2),
        final_answer=final_answer,
        missing_keywords=missing_keywords,
    )


# --- FULL SUITE RUNNER ---

def run_eval_suite() -> list[EvalResult]:
    """
    Runs every case in GOLDEN_DATASET through the graph and returns
    the full list of results. Building the graph ONCE here, outside
    the loop, and reusing it across every case — same "expensive setup
    happens once" principle applied throughout this project.
    """

    graph = build_graph()
    results = []
    for case in GOLDEN_DATASET:
        results.append(_evaluate_case(graph, case))
        time.sleep(5)
        # Pacing between cases to stay under Vertex AI's per-minute
        # quota — each case involves several sequential LLM calls
        # already; running cases with zero gap between them triggered
        # a real 429 RESOURCE_EXHAUSTED error during testing.
    return results
# --- REPORTING ---

def print_report(results: list[EvalResult]) -> None:
    """
    Prints a human-readable summary of eval results: a per-case
    breakdown followed by aggregate pass rate and average latency.
    """

    print(f"{'=' * 70}")
    print("DocuMind Eval Report")
    print(f"{'=' * 70}\n")

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.question}")
        print(f"       grounded={r.is_grounded}  retries={r.retry_count}  "
              f"latency={r.latency_seconds}s")
        if not r.passed:
            print(f"       missing keywords: {r.missing_keywords}")
            print(f"       answer: {r.final_answer[:150]}")
        print()

    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    avg_latency = sum(r.latency_seconds for r in results) / total if total else 0
    grounded_count = sum(1 for r in results if r.is_grounded is True)

    print(f"{'-' * 70}")
    print(f"Pass rate:        {passed_count}/{total} ({100 * passed_count / total:.0f}%)")
    print(f"Grounded rate:     {grounded_count}/{total} ({100 * grounded_count / total:.0f}%)")
    print(f"Average latency:   {avg_latency:.2f}s")
    print(f"{'-' * 70}")
    # These three aggregate numbers are exactly the kind of thing
    # worth tracking OVER TIME (run to run, commit to commit) to
    # catch regressions — this is precisely what Step 9.3's BigQuery
    # storage and Step 10's CI/CD eval gate will build on top of this
    # same function's output.


# --- ENTRY POINT ---

if __name__ == "__main__":
    from documind.eval.bigquery_writer import write_eval_results_to_bigquery

    results = run_eval_suite()
    print_report(results)

    run_id = write_eval_results_to_bigquery(results)
    print(f"\nResults written to BigQuery (run_id: {run_id})")
