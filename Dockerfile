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
LABEL description="Kite Intraday Trading Bot — NSE automated trading"

WORKDIR /app

# Install only runtime dependencies (tkinter for GUI, sqlite3 for DB)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-tk \
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
#   /app/data    — persistent SQLite database + access token cache
#   /app/logs    — log files
#   /app/config  — .env file with API credentials
VOLUME ["/app/data", "/app/logs", "/app/config"]

# Override default DB path to persistent volume
ENV DATABASE_URL=sqlite:////app/data/trades.db
ENV LOG_FILE=/app/logs/trading_bot.log
ENV ACCESS_TOKEN_FILE=/app/data/access_token.txt

# Health check: verify Python and dependencies are importable
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import kiteconnect; import pandas; print('OK')" || exit 1

# Make entrypoint executable
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Expose no ports by default (bot connects outbound to Kite API)
# Port 5555 for OAuth callback if using auto-auth
EXPOSE 5555

# Entrypoint: validates env, inits DB, prints config summary
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Default: run the trading bot (headless mode)
# Override with: docker run ... python gui/launcher.py  (for GUI mode)
CMD ["python", "main.py"]
