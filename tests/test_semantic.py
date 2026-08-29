from __future__ import annotations

from typing import Any

import pytest

from trade_news_analysis.config import Settings
from trade_news_analysis.services.semantic import SentenceTransformerTitleMatcher


class FakeEmbeddingModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str], **_kwargs: Any) -> list[list[float]]:
        self.calls.append(texts)
        vectors = {
            "query": [1.0, 0.0],
            "same": [0.9, 0.1],
            "different": [0.0, 1.0],
        }
        return [vectors[text] for text in texts]


def test_semantic_matcher_is_lazy_and_caches_embeddings() -> None:
    settings = Settings(semantic_clustering_enabled=True)
    model = FakeEmbeddingModel()
    matcher = SentenceTransformerTitleMatcher(settings, model_factory=lambda _name: model)

    assert matcher.similarities("query", ["same", "different"]) == pytest.approx([0.9, 0])
    assert matcher.similarities("query", ["same", "different"]) == pytest.approx([0.9, 0])
    assert model.calls == [["query", "same", "different"]]
    assert matcher.status()["lexical_fallback"] is False


def test_disabled_semantic_matcher_does_not_load_model() -> None:
    settings = Settings(semantic_clustering_enabled=False)

    def fail_if_loaded(_name: str) -> Any:
        raise AssertionError("disabled matcher must not load a model")

    matcher = SentenceTransformerTitleMatcher(settings, model_factory=fail_if_loaded)
    assert matcher.similarities("query", ["same"]) is None
    assert matcher.status()["lexical_fallback"] is True


def test_semantic_matcher_records_failure_and_falls_back() -> None:
    settings = Settings(semantic_clustering_enabled=True)

    def broken_model(_name: str) -> Any:
        raise RuntimeError("model unavailable")

    matcher = SentenceTransformerTitleMatcher(settings, model_factory=broken_model)
    assert matcher.similarities("query", ["same"]) is None
    assert matcher.similarities("query", ["same"]) is None
    status = matcher.status()
    assert status["lexical_fallback"] is True
    assert "RuntimeError" in str(status["error"])
