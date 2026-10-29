# Build the kiosk screen (React + TypeScript, served by the simulator at /).
FROM node:20-slim AS kiosk
WORKDIR /kiosk
COPY kiosk/package.json kiosk/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY kiosk/ ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY carepoint_sim ./carepoint_sim
COPY config ./config
COPY --from=kiosk /kiosk/dist ./kiosk

# Same uid as the backend image, which writes the demo machine keys this reads.
RUN useradd --create-home --uid 1000 carepoint \
    && mkdir -p /var/lib/carepoint-sim/state \
    && chown carepoint /var/lib/carepoint-sim/state
USER carepoint

EXPOSE 8080

CMD ["python", "-m", "carepoint_sim", "--config", "config/demo.json"]
