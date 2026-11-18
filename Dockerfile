# Review app image. Build from the repository root:
#   az acr build -r <registry> -t btr-review:<tag> .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements_app.txt .
RUN pip install --no-cache-dir -r requirements_app.txt

COPY btr_pipeline/ ./btr_pipeline/
COPY review_app/ ./review_app/
COPY wsgi.py .

RUN useradd --create-home app
USER app

EXPOSE 8000

# gthread workers: requests are short, but candidate generation waits on the
# Azure OpenAI embedding call. Health probe path: /healthz
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", \
     "--worker-class", "gthread", "--timeout", "120", \
     "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
