"""
golden_dataset.py
------------------
A small, hand-curated set of question/expected-answer pairs used to
measure DocuMind's actual retrieval and answer quality — as opposed
to "I tried it once and it looked right." This is the foundation any
real eval pipeline is built on: a fixed, known-correct reference set
you can run the system against repeatedly, to catch regressions and
measure improvement over time.

Every question here is written against the actual content of
scripts/sample.pdf (the resume ingested throughout this project) —
if you ingest different documents, this dataset should be replaced
with questions matched to YOUR content, not left pointing at content
that no longer exists in the database.
"""

# --- IMPORTS ---

from pydantic import BaseModel
# Same reasoning as every other data model in this project: a typed,
# validated structure instead of a bare dict, so a malformed eval
# case (e.g. missing a required field) fails loudly and immediately
# rather than causing a confusing error deep inside the eval loop.


# --- DATA MODEL ---

class EvalCase(BaseModel):
    """
    One test case: a question to ask, and the set of keywords/phrases
    we expect a CORRECT, grounded answer to contain. Keyword-based
    checking is a simple, brittle-but-honest proxy for "did the
    answer actually contain the right information" — it won't catch
    every possible correct phrasing, but it's transparent, requires
    no extra model calls to compute, and is good enough to catch real
    regressions (e.g. retrieval breaking, or the model refusing to
    answer something it should be able to answer).
    """

    question: str
    expected_keywords: list[str]
    # Every keyword should appear (case-insensitive) somewhere in the
    # final answer for this case to "pass" on the recall metric. Kept
    # deliberately short and specific per case — long keyword lists
    # make a case brittle to reasonable paraphrasing.

    should_be_answerable: bool = True
    # Most cases expect a real, grounded answer. Setting this False
    # marks a case where we EXPECT the system to say "I don't have
    # enough information" or route to reject/clarify — testing the
    # system's honesty about its own limits is just as important as
    # testing whether it can answer things it should be able to.


# --- THE GOLDEN DATASET ---

GOLDEN_DATASET: list[EvalCase] = [
    EvalCase(
        question="What cloud technologies does this person have experience with?",
        expected_keywords=["Azure", "Google Cloud", "GCP"],
    ),
    EvalCase(
        question="How many years of experience does this person have?",
        expected_keywords=["13"],
    ),
    EvalCase(
        question="What is this person's email address?",
        expected_keywords=["santosh.hargunani@gmail.com"],
    ),
    EvalCase(
        question="What frontend frameworks or libraries has this person used?",
        expected_keywords=["Angular"],
    ),
    EvalCase(
        question="What is this person's favorite pizza topping?",
        expected_keywords=[],
        should_be_answerable=False,
        # A deliberately unanswerable question — nothing in the
        # ingested resume could possibly address this. A well-behaved
        # system should say so honestly rather than hallucinating an
        # answer. This is exactly the failure mode the critic agent
        # (Step 5.5) exists to catch — this eval case is what proves
        # that protection actually works, not just that it exists in
        # theory.
    ),
]
# Five cases is intentionally small for this prep project — real
# production eval sets run into the dozens or hundreds of cases,
# covering more question types and edge cases. The POINT here is
# having a repeatable, structured harness at all; growing the dataset
# is just a matter of adding more EvalCase entries once this
# foundation exists.
