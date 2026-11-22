# MBSE workbench image: web UI (mbse serve) and MCP server (mbse-mcp).
# Models are mounted at /models (a git checkout of your .sysml files).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sysml_copilot ./sysml_copilot
RUN pip install --no-cache-dir ".[mcp,semantic]" \
    && useradd --create-home --uid 1000 mbse \
    && mkdir /cache && chown mbse /cache

USER mbse
# The embedding model (~220 MB) is downloaded on the first semantic search.
ENV MBSE_EMBEDDING_CACHE_DIR=/cache
VOLUME /models /cache
EXPOSE 8765 8766

# Default: web workbench. For the MCP server over HTTP:
#   mbse-mcp /models --transport streamable-http --host 0.0.0.0 --port 8766
CMD ["mbse", "-m", "/models", "serve", "host=0.0.0.0", "port=8765"]
