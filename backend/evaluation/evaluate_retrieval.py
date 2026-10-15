"""Run the curated retrieval set against the configured Chroma collection."""
import json
from pathlib import Path

from app.config import get_settings
from app.retrieval import _load_corpus, _owner_filter, hybrid_search, is_confident
from app.vectorstore import get_vectorstore


CASES = Path(__file__).with_name("retrieval_cases.json")


def main() -> None:
    settings = get_settings()
    store = get_vectorstore()
    owner_user_id = "shared"
    corpus = _load_corpus(
        store,
        owner_user_id,
        settings.retrieval_max_lexical_chunks,
    )
    if corpus is None:
        raise RuntimeError("Evaluation requires the lexical index; collection exceeds its configured limit")

    cases = json.loads(CASES.read_text(encoding="utf-8"))
    answerable_total = answerable_recalled = 0
    unsupported_total = unsupported_rejected = 0
    reciprocal_ranks: list[float] = []

    for case in cases:
        vector_hits = store.similarity_search_with_relevance_scores(
            case["question"],
            k=max(settings.retrieval_candidate_k, settings.retrieval_top_k),
            filter=_owner_filter(owner_user_id),
        )
        baseline = [(doc, float(score)) for doc, score in vector_hits[:settings.retrieval_top_k]]
        fused = hybrid_search(
            case["question"],
            vector_hits,
            corpus,
            settings.retrieval_candidate_k * 2,
            settings.retrieval_candidate_k,
        )
        accepted = [
            item for item in fused
            if is_confident(
                item[1],
                item[3],
                settings.retrieval_min_relevance,
                settings.retrieval_min_lexical_coverage,
            )
        ][:settings.retrieval_top_k]

        expected = set(case["expected_chunks"])
        baseline_chunks = [doc.metadata.get("chunk_index") for doc, _ in baseline]
        retrieved_chunks = [doc.metadata.get("chunk_index") for doc, _, _, _ in accepted]
        if expected:
            answerable_total += 1
            answerable_recalled += int(bool(expected.intersection(retrieved_chunks)))
            rank = next((i for i, chunk in enumerate(retrieved_chunks, 1) if chunk in expected), None)
            reciprocal_ranks.append(1 / rank if rank else 0.0)
            baseline_rank = next((i for i, chunk in enumerate(baseline_chunks, 1) if chunk in expected), None)
            print(
                f"{case['id']}: baseline_top={baseline_chunks[0] if baseline_chunks else '-'} "
                f"hybrid_top={retrieved_chunks[0] if retrieved_chunks else '-'} "
                f"expected={sorted(expected)} rank={rank or '-'} "
                f"baseline_rank={baseline_rank or '-'}"
            )
        else:
            unsupported_total += 1
            unsupported_rejected += int(not accepted)
            print(
                f"{case['id']}: top_score={vector_hits[0][1] if vector_hits else 0:.3f} "
                f"lexical_coverage={fused[0][3] if fused else 0:.3f} "
                f"accepted={bool(accepted)}"
            )

    print(
        f"answerable_recall@{settings.retrieval_top_k}="
        f"{answerable_recalled}/{answerable_total}"
    )
    print(
        f"answerable_mrr={sum(reciprocal_ranks) / len(reciprocal_ranks):.3f}"
    )
    print(
        f"unsupported_rejected={unsupported_rejected}/{unsupported_total}"
    )


if __name__ == "__main__":
    main()
