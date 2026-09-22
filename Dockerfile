# ==========================================
# Stage 1: Build Dependencies
# ==========================================
FROM python:3.11-slim AS builder

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ==========================================
# Stage 2: Production Slim Runtime
# ==========================================
FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Copy installed python dependencies from builder
COPY --from=builder /install /usr/local

# Create non-root user for security best practices
RUN addgroup --system appgroup && adduser --system --group appuser

# Copy application source code
COPY . /app

# Ensure proper ownership
RUN chown -R appuser:appgroup /app /tmp

USER appuser

EXPOSE 8080

# gunicorn serves the WSGI callable `application` in main.py.
# --timeout 320 > Cloud Run request timeout (300s): pipeline.process() runs
# inline (LLM calls + one send per supplier). --threads lets one instance
# take a webhook while an admin/renew call is in flight.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", \
     "--workers", "2", "--threads", "8", "--worker-class", "gthread", \
     "--timeout", "320", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "main:application"]
