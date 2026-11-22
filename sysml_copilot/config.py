import os

# Local embedding model used by semantic search (fastembed, no API key).
# Multilingual on purpose: models are queried in French and English.
EMBEDDING_MODEL = os.environ.get(
    "MBSE_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

# Minimum cosine similarity for a semantic match to be reported. Below this,
# results are noise (unrelated elements still score ~0.2-0.3).
MIN_SEMANTIC_SCORE = float(os.environ.get("MBSE_MIN_SEMANTIC_SCORE", "0.35"))

# Where downloaded embedding models are cached (fastembed default if unset).
EMBEDDING_CACHE_DIR = os.environ.get("MBSE_EMBEDDING_CACHE_DIR") or None

# "auto": find_elements(mode="auto") combines lexical and semantic search when
# the embedding model is available. "off": auto mode stays lexical-only (no
# model download); mode="semantic" still works when asked for explicitly.
SEMANTIC_SEARCH = os.environ.get("MBSE_SEMANTIC", "auto")
