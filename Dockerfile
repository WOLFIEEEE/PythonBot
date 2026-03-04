# ── Stage 1: Build dependencies ──────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies for native extensions (numpy, pandas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ─────────────────────────────────────────────────
FROM python:3.11-slim

LABEL maintainer="WOLFIEEEE"
LABEL description="Kite Intraday Trading Bot — NSE automated trading with live dashboard"

WORKDIR /app

# Install only runtime dependencies (sqlite3 for DB, curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsqlite3-0 \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set timezone to IST
ENV TZ=Asia/Kolkata
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . .

# Create required directories
RUN mkdir -p /app/logs /app/data /app/config

# Volume mounts:
#   /app/data    — persistent SQLite database + access token + evolution state
#   /app/logs    — log files
#   /app/config  — .env file with API credentials
VOLUME ["/app/data", "/app/logs", "/app/config"]

# ── Environment defaults (override via Coolify env vars) ─────────────
ENV DATABASE_URL=sqlite:////app/data/trades.db
ENV LOG_FILE=/app/logs/trading_bot.log
ENV ACCESS_TOKEN_FILE=/app/data/access_token.txt
ENV DASHBOARD_PORT=5000
ENV DASHBOARD_HOST=0.0.0.0

# Health check: hit the dashboard /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${DASHBOARD_PORT}/health || exit 1

# Make entrypoint executable
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Expose dashboard port (Coolify will map this)
EXPOSE ${DASHBOARD_PORT}

# Entrypoint: validates env, inits DB, prints config summary
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Default: run the trading bot (includes dashboard)
CMD ["python", "main.py"]
