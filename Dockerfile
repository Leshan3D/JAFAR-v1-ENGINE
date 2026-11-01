# JAFAR INTELLIGENCE — container image.
# CPU-only torch, FinBERT baked in, data written to $DATA_DIR (mount a volume
# there if your plan supports it — the service still works without one, it
# just loses alert/cooldown state and history on redeploy).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.hf_cache \
    DATA_DIR=/data \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# torch from the CPU wheel index — the default PyPI wheel drags in ~2.5GB of
# CUDA libraries we don't want on a Railway container.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -r requirements.txt

# .dockerignore keeps this lean (no .venv, snapshots, logs, cloudflared.exe)
COPY . ./

# Bake FinBERT so first boot doesn't spend 400MB of download on a cold start.
RUN python -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; \
AutoTokenizer.from_pretrained('ProsusAI/finbert'); \
AutoModelForSequenceClassification.from_pretrained('ProsusAI/finbert')"

RUN mkdir -p /data
WORKDIR /data

EXPOSE 5000

CMD ["python", "/app/run_service.py"]
