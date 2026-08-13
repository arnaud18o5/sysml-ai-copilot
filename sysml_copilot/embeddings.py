from fastembed import TextEmbedding

from . import config

_model = None


def _get_model():
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=config.EMBEDDING_MODEL)
    return _model


def embed_texts(texts):
    model = _get_model()
    return [vec.tolist() for vec in model.embed(texts)]


def element_text(element):
    parts = [element["kind"], element["name"]]
    if element.get("doc"):
        parts.append(element["doc"])
    return " - ".join(parts)
