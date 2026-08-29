"""Optional multilingual title similarity with a lexical-only fallback."""

from __future__ import annotations

import importlib.util
import logging
from collections import OrderedDict
from collections.abc import Callable, Sequence
from importlib import import_module
from typing import Any, Protocol

from ..config import Settings

logger = logging.getLogger(__name__)

Embedding = tuple[float, ...]
ModelFactory = Callable[[str], Any]


class SemanticTitleMatcher(Protocol):
    def similarities(self, query: str, candidates: Sequence[str]) -> list[float] | None: ...

    def status(self) -> dict[str, Any]: ...


def _sentence_transformer_factory(model_name: str) -> Any:
    module = import_module("sentence_transformers")
    return module.SentenceTransformer(model_name)


class SentenceTransformerTitleMatcher:
    """Lazily load Sentence Transformers and cache normalized title embeddings."""

    def __init__(
        self,
        settings: Settings,
        model_factory: ModelFactory | None = None,
        cache_size: int = 4096,
    ):
        self.enabled = settings.semantic_clustering_enabled
        self.model_name = settings.semantic_clustering_model
        self._model_factory = model_factory
        self._cache_size = cache_size
        self._model: Any | None = None
        self._cache: OrderedDict[str, Embedding] = OrderedDict()
        self._error: str | None = None

    @property
    def dependency_available(self) -> bool:
        return self._model_factory is not None or importlib.util.find_spec(
            "sentence_transformers"
        ) is not None

    def status(self) -> dict[str, Any]:
        dependency_available = self.dependency_available
        return {
            "enabled": self.enabled,
            "dependency_available": dependency_available,
            "model": self.model_name,
            "lexical_fallback": (
                not self.enabled or not dependency_available or self._error is not None
            ),
            "error": self._error,
        }

    def similarities(
        self, query: str, candidates: Sequence[str]
    ) -> list[float] | None:
        if not self.enabled or not candidates or self._error is not None:
            return None
        try:
            embeddings = self._embeddings([query, *candidates])
            query_embedding = embeddings[query]
            return [
                max(-1.0, min(1.0, self._dot(query_embedding, embeddings[candidate])))
                for candidate in candidates
            ]
        except Exception as exc:
            self._record_failure(exc)
            return None

    def _load_model(self) -> Any:
        if self._model is None:
            factory = self._model_factory or _sentence_transformer_factory
            self._model = factory(self.model_name)
        return self._model

    def _embeddings(self, texts: Sequence[str]) -> dict[str, Embedding]:
        unique_texts = list(dict.fromkeys(texts))
        missing = [text for text in unique_texts if text not in self._cache]
        if missing:
            encoded = self._load_model().encode(
                missing,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            vectors = list(encoded)
            if len(vectors) != len(missing):
                raise RuntimeError("语义模型返回的向量数量不匹配")
            for text, vector in zip(missing, vectors, strict=True):
                self._cache[text] = tuple(float(value) for value in vector)
                self._cache.move_to_end(text)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        for text in unique_texts:
            self._cache.move_to_end(text)
        return {text: self._cache[text] for text in unique_texts}

    @staticmethod
    def _dot(left: Embedding, right: Embedding) -> float:
        if len(left) != len(right):
            raise RuntimeError("语义模型返回的向量维度不匹配")
        return sum(
            left_value * right_value
            for left_value, right_value in zip(left, right, strict=True)
        )

    def _record_failure(self, exc: Exception) -> None:
        if self._error is not None:
            return
        self._error = f"{type(exc).__name__}: {exc}"[:1000]
        logger.warning("Semantic clustering unavailable; using lexical fallback: %s", self._error)
