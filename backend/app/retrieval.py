"""Bounded hybrid retrieval: semantic candidates plus BM25 lexical evidence."""
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+")
_GENERIC_TERMS = {
    "学校", "学院", "什么", "哪些", "哪个", "哪里", "多少", "如何",
    "是否", "今年", "目前", "截至", "现在", "时候", "可以", "请问",
    "是什", "话是", "的是", "的那", "那句", "句话",
}


@dataclass(frozen=True)
class _LexicalCorpus:
    documents: list[Any]
    frequencies: list[Counter[str]]
    lengths: list[int]
    document_frequency: Counter[str]
    average_length: float


_CORPUS_LOCK = threading.Lock()
_CORPUS_CACHE: dict[str, tuple[int, _LexicalCorpus]] = {}
_LEXICAL_DISABLED_FOR: dict[str, int] = {}
_RRF_CONSTANT = 60


def tokenize(text: str) -> list[str]:
    """Tokenize CJK text into overlapping bigrams and keep Latin/numeric terms."""
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text.lower()):
        value = match.group()
        if "\u3400" <= value[0] <= "\u9fff":
            if len(value) == 1:
                tokens.append(value)
            else:
                tokens.extend(value[index:index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(value)
    return tokens


def invalidate_corpus_cache() -> None:
    """Invalidate lexical documents after this process changes the collection."""
    with _CORPUS_LOCK:
        _CORPUS_CACHE.clear()
        _LEXICAL_DISABLED_FOR.clear()


def _owner_filter(owner_user_id: str) -> dict[str, Any]:
    return {
        "$or": [
            {"owner_user_id": owner_user_id},
            {"owner_user_id": "shared"},
        ]
    }


def _load_corpus(
    store: Any,
    owner_user_id: str,
    max_chunks: int,
) -> _LexicalCorpus | None:
    scope = owner_user_id
    where = _owner_filter(owner_user_id)
    with _CORPUS_LOCK:
        cached = _CORPUS_CACHE.get(scope)
        if cached is not None:
            if cached[0] > max_chunks:
                return None
            return cached[1]

        ids = store.get(where=where, include=[])["ids"]
        corpus_size = len(ids)
        if corpus_size > max_chunks:
            if _LEXICAL_DISABLED_FOR.get(scope) != corpus_size:
                logger.warning(
                    "BM25 retrieval disabled for owner %s: collection has %d chunks, "
                    "exceeding RETRIEVAL_MAX_LEXICAL_CHUNKS=%d; using vector candidates only",
                    scope,
                    corpus_size,
                    max_chunks,
                )
                _LEXICAL_DISABLED_FOR[scope] = corpus_size
            return None

        data = store.get(where=where, include=["documents", "metadatas"])
        from langchain_core.documents import Document

        documents = [
            Document(page_content=content, metadata=metadata or {})
            for content, metadata in zip(data.get("documents") or [], data.get("metadatas") or [])
            if content
        ]
        tokenized = [
            tokenize(f"{document.metadata.get('filename', '')}\n{document.page_content}")
            for document in documents
        ]
        frequencies = [Counter(tokens) for tokens in tokenized]
        lengths = [len(tokens) for tokens in tokenized]
        document_frequency = Counter(
            term for terms in frequencies for term in terms
        )
        average_length = sum(lengths) / len(lengths) if lengths else 0.0
        corpus = _LexicalCorpus(
            documents=documents,
            frequencies=frequencies,
            lengths=lengths,
            document_frequency=document_frequency,
            average_length=average_length,
        )
        _CORPUS_CACHE[scope] = (corpus_size, corpus)
        logger.info("Loaded %d chunks for BM25 retrieval owner=%s", len(documents), scope)
        return corpus


def _bm25_scores(
    query: str,
    corpus: _LexicalCorpus,
) -> list[tuple[int, float]]:
    query_terms = set(tokenize(query))
    if not query_terms or not corpus.documents or not corpus.average_length:
        return []

    scores = [0.0] * len(corpus.documents)
    for term in query_terms:
        containing = corpus.document_frequency.get(term, 0)
        inverse_frequency = math.log(
            1 + (len(corpus.documents) - containing + 0.5) / (containing + 0.5)
        )
        for index, terms in enumerate(corpus.frequencies):
            term_frequency = terms.get(term, 0)
            if not term_frequency:
                continue
            denominator = term_frequency + 1.2 * (
                1 - 0.75 + 0.75 * corpus.lengths[index] / corpus.average_length
            )
            scores[index] += inverse_frequency * term_frequency * 2.2 / denominator

    ranked = [(index, score) for index, score in enumerate(scores) if score > 0]
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


def _lexical_coverage(query: str, document: Any) -> float:
    query_terms = {
        term for term in tokenize(query)
        if term not in _GENERIC_TERMS and len(term) > 1
    }
    if not query_terms:
        return 0.0
    document_terms = set(
        tokenize(f"{document.metadata.get('filename', '')}\n{document.page_content}")
    )
    return len(query_terms & document_terms) / len(query_terms)


def hybrid_search(
    question: str,
    vector_hits: list[tuple[Any, float]],
    corpus: _LexicalCorpus | list[Any] | None,
    top_k: int,
    candidate_k: int,
) -> list[tuple[Any, float, float, float]]:
    """Fuse semantic and BM25 ranks; return document, vector score, and RRF score."""
    candidates: dict[tuple[Any, ...], dict[str, Any]] = {}

    def key_for(document: Any) -> tuple[Any, ...]:
        metadata = document.metadata or {}
        doc_id = metadata.get("doc_id")
        chunk_index = metadata.get("chunk_index")
        if doc_id is not None and chunk_index is not None:
            return str(doc_id), str(chunk_index)
        return (document.page_content,)

    for rank, (document, relevance) in enumerate(vector_hits[:candidate_k], start=1):
        key = key_for(document)
        candidate = candidates.setdefault(
            key,
            {"document": document, "vector_score": float(relevance), "rrf_score": 0.0, "rank": rank},
        )
        candidate["rrf_score"] += 1 / (_RRF_CONSTANT + rank)
        candidate["vector_score"] = max(candidate["vector_score"], float(relevance))
        candidate["rank"] = min(candidate["rank"], rank)
        candidate["lexical_coverage"] = _lexical_coverage(question, document)

    if corpus:
        if isinstance(corpus, _LexicalCorpus):
            lexical_corpus = corpus
            documents = corpus.documents
        else:
            documents = corpus
            tokenized = [
                tokenize(f"{document.metadata.get('filename', '')}\n{document.page_content}")
                for document in documents
            ]
            frequencies = [Counter(tokens) for tokens in tokenized]
            lengths = [len(tokens) for tokens in tokenized]
            lexical_corpus = _LexicalCorpus(
                documents=documents,
                frequencies=frequencies,
                lengths=lengths,
                document_frequency=Counter(term for terms in frequencies for term in terms),
                average_length=sum(lengths) / len(lengths) if lengths else 0.0,
            )
        for rank, (index, _) in enumerate(
            _bm25_scores(question, lexical_corpus)[:candidate_k],
            start=1,
        ):
            document = documents[index]
            key = key_for(document)
            candidate = candidates.setdefault(
                key,
                {
                    "document": document,
                    "vector_score": 0.0,
                    "rrf_score": 0.0,
                    "rank": candidate_k + rank,
                    "lexical_coverage": _lexical_coverage(question, document),
                },
            )
            candidate["rrf_score"] += 1 / (_RRF_CONSTANT + rank)
            candidate["lexical_coverage"] = max(
                candidate["lexical_coverage"],
                _lexical_coverage(question, document),
            )

    ordered = sorted(
        candidates.values(),
        key=lambda item: (-item["rrf_score"], item["rank"], key_for(item["document"])),
    )
    return [
        (
            item["document"],
            item["vector_score"],
            item["rrf_score"],
            item["lexical_coverage"],
        )
        for item in ordered[:top_k]
    ]


def is_confident(
    vector_score: float,
    lexical_coverage: float,
    min_relevance: float,
    min_lexical_coverage: float,
) -> bool:
    return (
        vector_score >= min_relevance
        or lexical_coverage >= min_lexical_coverage
    )


def retrieve(
    store: Any,
    question: str,
    top_k: int,
    candidate_k: int,
    min_relevance: float,
    min_lexical_coverage: float,
    max_lexical_chunks: int,
    owner_user_id: str,
) -> list[tuple[Any, float, float, float]]:
    vector_hits = store.similarity_search_with_relevance_scores(
        question,
        k=max(top_k, candidate_k),
        filter=_owner_filter(owner_user_id),
    )
    corpus = _load_corpus(store, owner_user_id, max_lexical_chunks)
    fused = hybrid_search(question, vector_hits, corpus, candidate_k * 2, candidate_k)
    confident = [
        item for item in fused
        if is_confident(item[1], item[3], min_relevance, min_lexical_coverage)
    ]
    return confident[:top_k]
