# Dockerfile
# ----------
# Builds a production container image for the DocuMind FastAPI
# service (src/documind/api.py). This is what gets pushed to
# Artifact Registry and deployed to GKE Autopilot.

# --- BASE IMAGE ---
FROM python:3.12-slim AS base

# --- WORKING DIRECTORY ---
WORKDIR /app

# --- DEPENDENCY INSTALLATION (separate layer for caching) ---
COPY pyproject.toml ./

# --- APPLICATION CODE ---
COPY src/ ./src/

RUN pip install --no-cache-dir .

# --- NON-ROOT USER ---
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

# --- NETWORK PORT ---
EXPOSE 8000

# --- ENTRYPOINT ---
CMD ["uvicorn", "documind.api:app", "--host", "0.0.0.0", "--port", "8000"]