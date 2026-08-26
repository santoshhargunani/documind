# DocuMind

An enterprise document Q&A / knowledge assistant built on Google Cloud, developed as hands-on preparation for a **Forward Deployed Engineer III (Generative AI, Google Cloud)** role. The goal is a production-shaped RAG system: real infrastructure-as-code, a multi-agent orchestration layer, and the coding standards a customer-facing engineering team would expect — not a notebook demo.

## Architecture

```
┌─────────────────────────────┐
│  Data ingestion & storage    │
│  Cloud Storage + Document AI │
│  → AlloyDB-style pgvector    │  (Cloud SQL Postgres + pgvector)
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│  Agent orchestration          │
│  LangGraph: router → retriever│
│  → synthesis → critic         │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│  Serving layer                 │
│  FastAPI on GKE Autopilot       │  (planned)
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│  Observability & CI/CD          │
│  Cloud Trace, BigQuery evals,   │
│  GitHub Actions → Terraform     │  (planned)
└─────────────────────────────┘
```

## Tech stack

| Layer | Technology |
|---|---|
| Ingestion | Cloud Storage, Document AI (OCR/parsing), Vertex AI embeddings (`text-embedding-005`) |
| Storage | Cloud SQL for Postgres + pgvector (HNSW index, cosine similarity) |
| Agents | LangGraph (custom `StateGraph`: router, retriever, synthesis, critic), `google-genai` SDK, Gemini 2.5 Flash |
| Serving | FastAPI (planned), containerized, deployed to GKE Autopilot (planned) |
| Observability | OpenTelemetry → Cloud Trace, structured logs (`structlog`) → Cloud Logging, eval results → BigQuery (planned) |
| IaC | Terraform (VPC, Cloud SQL, Cloud Storage, Document AI processor, IAM, Secret Manager) |
| CI/CD | GitHub Actions with an automated eval gate (planned) |

## Repository layout

```
documind/
├── src/documind/
│   ├── config.py              # Pydantic Settings — all runtime config
│   ├── config_secrets.py      # Secret Manager client (DB password)
│   ├── db.py                  # pg8000 connection + write pipeline to pgvector
│   ├── ingestion/
│   │   ├── storage.py         # Cloud Storage upload
│   │   ├── parser.py          # Document AI parsing
│   │   ├── chunker.py         # Sliding-window chunking with overlap
│   │   └── embedder.py        # Vertex AI embeddings (ingestion side)
│   └── agents/
│       ├── state.py           # Shared LangGraph state schema
│       ├── router.py          # Routes a question: retrieve / clarify / reject
│       ├── retriever.py       # pgvector similarity search
│       └── synthesis.py       # Grounded answer generation (Gemini)
├── infra/
│   ├── terraform/
│   │   ├── modules/           # vpc, cloudsql, ingestion
│   │   └── environments/dev/  # the actual deployed environment
│   └── sql/schema.sql         # documents + chunks table definitions
├── scripts/                   # manual integration test scripts
└── tests/                     # pytest unit/integration tests (planned)
```

## Local development setup

**Prerequisites:** Python 3.12, Docker Desktop, Terraform, the `gcloud` CLI, and a GCP project with billing enabled.

```powershell
# Python environment
py install 3.12
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

# GCP auth
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Copy `.env.example` to `.env` and fill in the values (see **Configuration** below).

## Infrastructure

```powershell
cd infra/terraform/environments/dev
terraform init
terraform apply
```

This provisions: a custom-mode VPC with a GKE-ready subnet and private services peering, a Cloud SQL Postgres instance (with `pgvector`), a Cloud Storage bucket for raw document uploads, and a Document AI OCR processor.

After every `terraform apply`, refresh generated values into `.env`:

```powershell
terraform output docai_processor_id   # → DOCAI_PROCESSOR_ID
terraform output public_ip_address    # → DB_HOST
```

Then apply the database schema (extensions and tables do **not** survive a destroy/recreate cycle):

```powershell
psql "host=<public_ip> port=5432 dbname=documind user=documind_admin sslmode=require" -f infra\sql\schema.sql
```

### ⚠️ Cost control

Cloud SQL bills continuously while running. This project is set up to be destroyed between working sessions:

```powershell
terraform destroy   # end of session
terraform apply     # start of next session
```

Because of this, several values are **not stable across sessions** and must be refreshed each time you resume: the Document AI processor ID, the Cloud SQL instance's public IP, and the database schema/data itself (wiped on every recreate). If your home IP has changed since the last session, also update `authorized_dev_ip` in `terraform.tfvars` before applying.

## Configuration

All runtime configuration is loaded via Pydantic Settings (`src/documind/config.py`) from environment variables / a local `.env` file. Required variables:

| Variable | Description |
|---|---|
| `GCP_PROJECT_ID` | GCP project ID |
| `RAW_DOCS_BUCKET` | Cloud Storage bucket for uploaded documents |
| `DOCAI_PROCESSOR_ID` | Document AI processor ID (changes on every Terraform recreate) |
| `DB_HOST` | Cloud SQL instance address (public IP for local dev, private IP once deployed inside the VPC) |
| `DB_PASSWORD_SECRET_ID` | Secret Manager secret name holding the DB password (the password itself is never stored in config) |

## Running the pipeline locally

```powershell
python scripts\test_ingestion.py        # upload → parse → chunk → embed → persist
python scripts\test_retrieval.py        # raw pgvector similarity search
python scripts\test_router.py           # router agent in isolation
python scripts\test_retriever_agent.py  # retriever agent in isolation
python scripts\test_synthesis_agent.py  # retriever + synthesis chained together
```

## Project status

- [x] VPC networking + private Cloud SQL connectivity
- [x] Cloud SQL Postgres + pgvector, schema versioned in `infra/sql/schema.sql`
- [x] Ingestion pipeline: upload, parse, chunk, embed, persist — tested end-to-end
- [x] LangGraph agent state design
- [x] Router agent
- [x] Retriever agent
- [x] Synthesis agent
- [ ] Critic agent (grounding verification + bounded retry loop)
- [ ] Full `StateGraph` wiring all four agents with conditional edges
- [ ] MCP server exposing an internal tool
- [ ] FastAPI service wrapping the agent graph
- [ ] Containerization + GKE Autopilot deployment
- [ ] OpenTelemetry tracing + BigQuery eval pipeline
- [ ] GitHub Actions CI/CD with an automated eval gate

## Notable design decisions

- **`pg8000` over `psycopg2`** — pure-Python driver, avoids requiring a C compiler toolchain on Windows.
- **Cloud SQL over AlloyDB** — AlloyDB has no free tier; Cloud SQL + pgvector gives the same core capability (Postgres-compatible, vector search) at a fraction of the idle cost for a prep project, at the cost of AlloyDB's higher-end scaling/performance features.
- **Public IP + SSL + IP allowlist for local dev, private IP for production** — the database defaults to private-IP-only (correct for production); a tightly scoped, SSL-enforced public IP with an authorized-networks allowlist is layered on top purely so local development tooling can reach it without a VPN.
- **`google-genai` SDK for new agent code** — `src/documind/ingestion/embedder.py` (built earlier) still uses the older `vertexai.language_models` SDK, which is deprecated. All Step 5 agent code uses the current `google-genai` SDK instead; unifying the two is a known cleanup item.
- **Router biases toward `retrieve` over `clarify`** — an overly cautious router that interrupts the user unnecessarily is worse UX than an occasional imperfect answer, which the critic agent is designed to catch.
