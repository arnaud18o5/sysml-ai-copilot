"""Semantic (embedding-based) element search, in memory, local model.

Each element is described by a short label (name split into words, kind,
type) and its doc. Descriptions are embedded with a local multilingual
model (fastembed) and compared to the query by cosine similarity, so
"l'élément qui alimente le moteur en carburant" finds `FuelPump`.

Vectors are cached by description text, so after an edit only the
elements whose description changed are re-embedded. The model is loaded
lazily on the first semantic query; if fastembed isn't installed or the
model can't be downloaded, `SemanticUnavailable` explains why and callers
fall back to lexical search.
"""

from __future__ import annotations

import math
import re

from . import config


class SemanticUnavailable(Exception):
    pass


def split_words(name: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return " ".join(re.sub(r"[_\W]+", " ", text).split())


def describe(ws, el) -> list[str]:
    """Texts embedded for an element: a short label (name, kind, type) and,
    separately, its doc. An element's score is its best text's score, so a
    long doc doesn't dilute the name and vice versa."""
    label = f"{split_words(el.effective_name or '')} ({split_words(el.kind).lower()})"
    if el.types:
        label += ". type " + ", ".join(split_words(r.segments[-1]) for r in el.types)
    if el.specializes:
        label += ". kind of " + ", ".join(split_words(r.segments[-1]) for r in el.specializes)
    texts = [label]
    if el.doc:
        texts.append(el.doc)
    return texts


class FastEmbedder:
    def __init__(self, model_name=None, cache_dir=None):
        self.model_name = model_name or config.EMBEDDING_MODEL
        self.cache_dir = cache_dir or config.EMBEDDING_CACHE_DIR
        self._model = None

    def __call__(self, texts):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise SemanticUnavailable(
                    "semantic search needs fastembed: pip install -e '.[semantic]'") from exc
            try:
                kwargs = {"cache_dir": self.cache_dir} if self.cache_dir else {}
                self._model = TextEmbedding(model_name=self.model_name, **kwargs)
            except Exception as exc:
                raise SemanticUnavailable(
                    f"cannot load embedding model {self.model_name} "
                    f"(first use downloads it from Hugging Face): {exc}") from exc
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class SemanticIndex:
    def __init__(self, embedder=None):
        self.embedder = embedder or FastEmbedder()
        self._vectors: dict[str, list[float]] = {}
        self._error: str | None = None

    def _embed(self, texts):
        missing = [t for t in dict.fromkeys(texts) if t not in self._vectors]
        if missing:
            if self._error:
                raise SemanticUnavailable(self._error)
            try:
                vectors = self.embedder(missing)
            except SemanticUnavailable as exc:
                self._error = str(exc)
                raise
            for text, vec in zip(missing, vectors):
                self._vectors[text] = _normalize(vec)
        return [self._vectors[t] for t in texts]

    def search(self, ws, query: str, elements, limit=10, min_score=None):
        """[(cosine, element)] best first, above `min_score`."""
        min_score = config.MIN_SEMANTIC_SCORE if min_score is None else min_score
        elements = list(elements)
        if not elements:
            return []
        per_element = [describe(ws, el) for el in elements]
        (qvec,) = self._embed([query])
        vecs = dict(zip(sum(per_element, []), self._embed(sum(per_element, []))))
        scored = []
        for el, texts in zip(elements, per_element):
            score = max(sum(a * b for a, b in zip(qvec, vecs[t])) for t in texts)
            if score >= min_score:
                scored.append((score, el))
        scored.sort(key=lambda t: (-t[0], ws.id_of(t[1])))
        return scored[:limit]
