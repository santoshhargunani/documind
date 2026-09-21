"""
bigquery_writer.py
-------------------
Pushes eval results (from run_eval.py) into the BigQuery table
provisioned by the bigquery Terraform module. Turns a one-off printed
report into a permanent, queryable history of eval runs over time.
"""

# --- IMPORTS ---

import uuid
# Standard library. Used to generate a unique run_id for each eval
# run — same reasoning as the UUID primary keys in db.py's schema
# (Step 4.8): independently generated, effectively zero collision
# risk, no central coordination needed.

from datetime import datetime, timezone
# Standard library. Used to timestamp each eval run. timezone.utc is
# used explicitly rather than a naive datetime.now() — BigQuery's
# TIMESTAMP type expects timezone-aware values, and being explicit
# about UTC avoids any ambiguity about which timezone a stored
# timestamp represents (a classic, easy-to-get-wrong source of bugs
# in systems that get used across timezones or by a distributed team).

import structlog
# Structured logging, consistent with every other module in this
# project.

from google.cloud import bigquery
# The BigQuery client library — bigquery.Client() is the entry point
# for inserting rows, same "one client, load once, reuse" pattern
# we've applied to every other GCP client throughout this project.

from documind.config import get_settings

from documind.eval.models import EvalResult


# --- LOGGER SETUP ---

logger = structlog.get_logger()


# --- CONSTANTS ---

_DATASET_ID = "documind_eval"
_TABLE_ID = "eval_results"
# Matching exactly what the Terraform bigquery module created. These
# could be promoted to Settings fields if they needed to vary by
# environment later — kept as simple constants here since this
# project only has one environment (dev) currently.


# --- WRITER FUNCTION ---

def write_eval_results_to_bigquery(results: list[EvalResult]) -> str:
    """
    Inserts one row per EvalResult into BigQuery, all tagged with the
    same run_id and run_timestamp so they can be grouped together as
    one eval run later. Returns the generated run_id.
    """

    settings = get_settings()
    client = bigquery.Client(project=settings.gcp_project_id)
    # Same Application-Default-Credentials-based auth as every other
    # GCP client in this project — your gcloud login locally, Workload
    # Identity once running in G0KE.

    run_id = str(uuid.uuid4())
    run_timestamp = datetime.now(timezone.utc).isoformat()
    # Generated ONCE per call to this function (i.e. once per eval
    # run), then reused for every row below — this is what makes
    # run_id a valid grouping key for a "GROUP BY run_id" query later.

    table_ref = f"{settings.gcp_project_id}.{_DATASET_ID}.{_TABLE_ID}"

    rows_to_insert = [
        {
            "run_id": run_id,
            "run_timestamp": run_timestamp,
            "question": r.question,
            "passed": r.passed,
            "is_grounded": r.is_grounded,
            "retry_count": r.retry_count,
            "latency_seconds": r.latency_seconds,
            "final_answer": r.final_answer,
            "missing_keywords": r.missing_keywords,
        }
        for r in results
    ]
    # A list comprehension converting each Pydantic EvalResult into a
    # plain dict matching the BigQuery table's schema column-for-
    # column. BigQuery's insert_rows_json (below) expects a list of
    # plain dicts, not Pydantic model instances directly — this is the
    # explicit translation step between our internal typed model and
    # BigQuery's expected wire format.

    errors = client.insert_rows_json(table_ref, rows_to_insert)
    # insert_rows_json uses BigQuery's STREAMING INSERT API — rows
    # become queryable within seconds, as opposed to a batch LOAD job
    # (better suited for large bulk historical loads, not this
    # small-per-run use case). Returns a list of any per-row errors
    # that occurred — an EMPTY list means every row inserted
    # successfully; it does NOT raise an exception on partial
    # failure, which is why we check it explicitly below rather than
    # wrapping this in a try/except and assuming success.

    if errors:
        # Defensive, explicit error handling — same "fail loudly, not
        # silently" principle applied throughout this project (the
        # router and critic's JSON-parse fallbacks, db.py's
        # transaction commits). A silent partial failure here would
        # mean a future BigQuery query undercounts an eval run without
        # any indication why.
        logger.error("bigquery_insert_failed", run_id=run_id, errors=errors)
        raise RuntimeError(f"Failed to write eval results to BigQuery: {errors}")

    logger.info(
        "eval_results_written_to_bigquery",
        run_id=run_id,
        row_count=len(rows_to_insert),
        table=table_ref,
    )

    return run_id
