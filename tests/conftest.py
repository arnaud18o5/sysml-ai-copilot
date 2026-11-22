import os
import shutil

# Tests never download the embedding model: auto search stays lexical unless a
# test injects an embedder (see test_semantic.py) or opts in explicitly.
os.environ.setdefault("MBSE_SEMANTIC", "off")

import pytest

from sysml_copilot.operations import ModelService

ROOT = os.path.dirname(os.path.dirname(__file__))
SAMPLE = os.path.join(ROOT, "data", "sample.sysml")
PKG = "VehicleFuelSystem"


@pytest.fixture
def sample_path(tmp_path):
    dest = tmp_path / "sample.sysml"
    shutil.copy(SAMPLE, dest)
    return str(dest)


@pytest.fixture
def svc(sample_path):
    return ModelService([sample_path])


def ok(result):
    assert result.get("ok", True), result
    return result
