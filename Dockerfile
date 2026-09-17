FROM python:3.12-slim

# WeasyPrint needs Pango/Cairo; git for reading the transforms submodule metadata.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 \
        libffi8 shared-mime-info fonts-dejavu-core git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user; the agent runs as this user.
RUN useradd --create-home --uid 10001 agent
WORKDIR /srv/report-agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
# Design system: tokens + the .woff2 files the report shell references.
# Fonts are resolved from disk at render time; nothing is fetched.
COPY assets ./assets
# Includes workspace/qcp, the Query Context Pack the agent reads. Build it
# before `docker build`:  python scripts/query_context_pack_extractors/\
#   phrase_data_model/build.py
COPY workspace ./workspace
RUN test -f ./workspace/qcp/index.md \
    || (echo 'ERROR: workspace/qcp is missing; build the Query Context Pack first' \
        && exit 1)

# Writable locations: report output (local mode) and Claude Code's config dir.
RUN mkdir -p /srv/report-agent/data/reports /home/agent/.claude \
    && chown -R agent:agent /srv/report-agent /home/agent
USER agent

ENV PYTHONUNBUFFERED=1 \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    WORKSPACE_DIR=/srv/report-agent/workspace \
    REPORT_DIR=/srv/report-agent/data/reports \
    AGENT_MODE=sdk \
    CLAUDE_CODE_USE_BEDROCK=1 \
    PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --ws websockets --proxy-headers --forwarded-allow-ips='*'"]
