import os

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384

# Minimum cosine similarity score (from the Neo4j vector index) for a
# resolved element to be considered a confident match. Below this,
# query.run() reports no match instead of proceeding on a likely-wrong
# element.
MIN_MATCH_SCORE = float(os.environ.get("MIN_MATCH_SCORE", "0.5"))
