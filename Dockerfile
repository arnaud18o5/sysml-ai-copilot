# MBSE workbench image: web UI (mbse serve) and MCP server (mbse-mcp).
# Models are mounted at /models (a git checkout of your .sysml files).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sysml_copilot ./sysml_copilot
RUN pip install --no-cache-dir ".[mcp]" \
    && useradd --create-home --uid 1000 mbse

USER mbse
VOLUME /models
EXPOSE 8765 8766

# Default: web workbench. For the MCP server over HTTP:
#   mbse-mcp /models --transport streamable-http --host 0.0.0.0 --port 8766
CMD ["mbse", "-m", "/models", "serve", "host=0.0.0.0", "port=8765"]
