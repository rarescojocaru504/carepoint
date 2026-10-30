FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# The keys directory is shared with the hardware simulator in the demo stack
# (app.scripts.demo_init); the secrets directory holds this installation's
# generated JWT secret and pickup-code pepper (app.scripts.local_secrets,
# ADR-031). Created here so a fresh volume inherits the owner and mode.
RUN useradd --create-home --uid 1000 carepoint \
    && mkdir -p /var/lib/carepoint/machine-keys /var/lib/carepoint/secrets \
    && chown carepoint /var/lib/carepoint/machine-keys /var/lib/carepoint/secrets \
    && chmod 700 /var/lib/carepoint/secrets
USER carepoint

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
