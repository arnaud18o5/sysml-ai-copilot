import os
import re

import pytest

from sysml_copilot import config
from sysml_copilot.operations import ModelService, call
from sysml_copilot.semantic import SemanticIndex, SemanticUnavailable, describe

from .conftest import PKG, SAMPLE, ok

# A tiny deterministic "multilingual" embedder: bag of concepts, with a few
# French words mapped to the English concept. Enough to test the plumbing.
CONCEPTS = ["fuel", "pump", "engine", "tank", "port", "requirement", "contaminant", "deliver", "vehicle"]
SYNONYMS = {"carburant": "fuel", "essence": "fuel", "pompe": "pump", "moteur": "engine",
            "réservoir": "tank", "exigence": "requirement", "alimente": "deliver",
            "contaminants": "contaminant", "contamination": "contaminant", "delivered": "deliver"}


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            words = [SYNONYMS.get(w, w) for w in re.findall(r"\w+", t.lower())]
            out.append([1.0 if c in words else 0.0 for c in CONCEPTS] + [0.1])
        return out


@pytest.fixture
def svc(sample_path, monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_SEARCH", "auto")
    return ModelService([sample_path], semantic=SemanticIndex(FakeEmbedder()))


def test_semantic_search_finds_by_meaning(svc):
    r = ok(call(svc, "find_elements", {"query": "pompe à carburant", "mode": "semantic", "limit": 3}))
    assert r["mode"] == "semantic"
    assert r["results"][0]["id"] in (f"{PKG}.FuelPump", f"{PKG}.Vehicle.pump")
    assert all("semantic_score" in x for x in r["results"])


def test_auto_mode_is_hybrid(svc):
    r = ok(call(svc, "find_elements", {"query": "FuelTank"}))
    assert r["mode"] == "hybrid"
    top = r["results"][0]
    assert top["id"] == f"{PKG}.FuelTank" and top["lexical_score"] == 100.0
    r = ok(call(svc, "find_elements", {"query": "contamination", "kind": "requirement def"}))
    assert [x["id"] for x in r["results"]] == [f"{PKG}.FuelPurityRequirement"]


def test_vectors_are_cached_and_only_changes_reembedded(svc):
    call(svc, "find_elements", {"query": "moteur", "mode": "semantic"})
    embedder = svc.semantic.embedder
    n = len(embedder.calls)
    call(svc, "find_elements", {"query": "moteur", "mode": "semantic"})
    assert len(embedder.calls) == n  # nothing new to embed
    ok(call(svc, "update_element", {"element": f"{PKG}.Engine", "doc": "Burns the fuel"}))
    call(svc, "find_elements", {"query": "moteur", "mode": "semantic"})
    assert embedder.calls[-1] == ["Burns the fuel"]


def test_unavailable_model_falls_back_or_errors(sample_path, monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_SEARCH", "auto")

    def broken(texts):
        raise SemanticUnavailable("no model here")

    svc = ModelService([sample_path], semantic=SemanticIndex(broken))
    r = ok(call(svc, "find_elements", {"query": "FuelPump"}))
    assert r["mode"] == "lexical" and "no model here" in r["note"]
    assert r["results"][0]["id"] == f"{PKG}.FuelPump"
    r = call(svc, "find_elements", {"query": "FuelPump", "mode": "semantic"})
    assert r["error"]["code"] == "semantic_unavailable"


def test_off_setting_keeps_auto_lexical(sample_path, monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_SEARCH", "off")
    emb = FakeEmbedder()
    svc = ModelService([sample_path], semantic=SemanticIndex(emb))
    assert ok(call(svc, "find_elements", {"query": "pump"}))["mode"] == "lexical"
    assert emb.calls == []


def test_descriptions(svc):
    el = svc.ws.get(f"{PKG}.Vehicle.tank")
    assert describe(svc.ws, el) == ["tank (part usage). type Fuel Tank"]
    req = svc.ws.get(f"{PKG}.FuelPurityRequirement")
    assert describe(svc.ws, req)[1].startswith("The fuel delivered")


@pytest.mark.skipif(not os.environ.get("MBSE_TEST_REAL_EMBEDDINGS"),
                    reason="set MBSE_TEST_REAL_EMBEDDINGS=1 to run with the real model (downloads ~220 MB)")
def test_real_model_cross_lingual(monkeypatch):
    pytest.importorskip("fastembed")
    monkeypatch.setattr(config, "SEMANTIC_SEARCH", "auto")
    svc = ModelService([SAMPLE])
    names = [x["name"] for x in svc.find_elements("pompe", limit=3, mode="semantic")["results"]]
    assert "FuelPump" in names or "pump" in names
    names = [x["name"] for x in svc.find_elements("moteur", limit=2, mode="semantic")["results"]]
    assert "Engine" in names
