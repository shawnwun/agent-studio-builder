FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# ── Claude Code CLI ────────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── las CLI (PolyAI internal) ──────────────────────────────────────────────
# Pass the internal repo URL at build time:
#   docker build --build-arg LAS_REPO_URL=https://<token>@github.com/polyai/local-agent-studio.git
ARG LAS_REPO_URL
RUN if [ -n "$LAS_REPO_URL" ]; then \
      pip install --no-cache-dir "git+${LAS_REPO_URL}"; \
    else \
      echo "WARNING: LAS_REPO_URL not set — las will not be available"; \
    fi

# ── Python server deps ─────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App ────────────────────────────────────────────────────────────────────
COPY server.py .
COPY static/ ./static/

# ── Startup ────────────────────────────────────────────────────────────────
ENV PORT=8788
ENV JOBS_DIR=/tmp/ptb_jobs
ENV CLAUDE_BIN=claude
ENV LAS_BIN=las

EXPOSE 8788

CMD ["python", "server.py"]
