"""Run RAGAS against the production retrieval and generation path."""
import argparse
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from app import auth_store, rag
from app.memory import get_memory

CASES = Path(__file__).with_name("ragas_cases.json")
OUTPUT = Path("/app/data/evaluation/ragas-latest.json")
REFUSAL_TEXT = "知识库中未找到足够相关的内容"
logger = logging.getLogger(__name__)


def _load_metrics():
    try:
        from ragas.llms import llm_factory
        from ragas.metrics.collections import ContextPrecision, ContextRecall, Faithfulness
    except ImportError as exc:
        raise RuntimeError(
            "RAGAS evaluation dependencies are missing; build the ragas-eval image"
        ) from exc

    # ragas 0.4 的 collections 指标只接受 InstructorLLM，且断言在异步事件循环里调用
    # （score() 内部是 asyncio.run(ascore) → agenerate），因此必须传异步客户端；
    # 传同步 OpenAI 客户端会在运行时报 "Cannot use agenerate() with a synchronous client"。
    # 判官模型复用与线上一致的 chat 端点（LLM_* / 旧名 DASHSCOPE_*），
    # 保证「评测用的模型」与「业务用的模型」同源；
    # temperature=0 让分数在多次回归对比中尽量可复现。
    from openai import AsyncOpenAI

    from app.config import get_settings

    settings = get_settings()
    judge_client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )
    evaluator_llm = llm_factory(
        settings.llm_model,
        provider="openai",
        client=judge_client,
        temperature=0,
    )
    return {
        "faithfulness": Faithfulness(llm=evaluator_llm),
        "context_precision": ContextPrecision(llm=evaluator_llm),
        "context_recall": ContextRecall(llm=evaluator_llm),
    }


def _full_contexts(sources: list[dict[str, Any]], owner_user_id: str) -> list[str]:
    collection = rag.get_vectorstore()._collection
    contexts = []
    for source in sources:
        result = collection.get(
            where={
                "$and": [
                    {"doc_id": source["doc_id"]},
                    {"chunk_index": source["chunk_index"]},
                ]
            },
            include=["documents", "metadatas"],
        )
        matches = [
            (text, metadata or {})
            for text, metadata in zip(
                result.get("documents") or [],
                result.get("metadatas") or [],
            )
            if (metadata or {}).get("owner_user_id") in {owner_user_id, "shared"}
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Could not resolve exactly one retrieved context for "
                f"{source['doc_id']} chunk {source['chunk_index']}"
            )
        contexts.append(matches[0][0])
    return contexts


def _score(metric: Any, **sample: Any) -> float:
    result = metric.score(**sample)
    value = getattr(result, "value", result)
    if value is None:
        raise RuntimeError(f"{type(metric).__name__} returned an empty score")
    return float(value)


def evaluate(username: str, password: str) -> dict[str, Any]:
    store = auth_store.get_auth_store()
    owner_user_id = store.authenticate(username, password, "ragas-evaluation")
    if owner_user_id is None:
        raise RuntimeError("RAGAS evaluation account authentication failed")

    private_documents = [
        document
        for document in rag.list_documents(owner_user_id)
        if not document["shared"]
    ]
    if private_documents:
        raise RuntimeError(
            "Use an evaluation account with no private documents; this run is scoped "
            "to the shared knowledge base only"
        )

    metrics = _load_metrics()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    memory = get_memory()
    report_cases = []
    for case in cases:
        session_id = f"ragas-{uuid.uuid4().hex}"
        try:
            result = rag.answer(case["question"], session_id, owner_user_id)
            sources = result["sources"]
            contexts = _full_contexts(sources, owner_user_id)
            answer = result["answer"]

            scores: dict[str, float | None]
            if case["should_answer"] and contexts:
                scores = {
                    "faithfulness": _score(
                        metrics["faithfulness"],
                        user_input=case["question"],
                        response=answer,
                        retrieved_contexts=contexts,
                    ),
                    "context_precision": _score(
                        metrics["context_precision"],
                        user_input=case["question"],
                        reference=case["reference"],
                        retrieved_contexts=contexts,
                    ),
                    "context_recall": _score(
                        metrics["context_recall"],
                        user_input=case["question"],
                        reference=case["reference"],
                        retrieved_contexts=contexts,
                    ),
                }
            elif case["should_answer"]:
                scores = {
                    "faithfulness": None,
                    "context_precision": 0.0,
                    "context_recall": 0.0,
                }
            else:
                scores = {
                    "faithfulness": None,
                    "context_precision": None,
                    "context_recall": None,
                }

            refusal = REFUSAL_TEXT in answer
            retrieved_chunks = [source["chunk_index"] for source in sources]
            expected_chunks = case["expected_chunks"]
            hit = bool(set(expected_chunks).intersection(retrieved_chunks))
            row = {
                "id": case["id"],
                "question": case["question"],
                "should_answer": case["should_answer"],
                "expected_chunks": expected_chunks,
                "retrieved_chunks": retrieved_chunks,
                "retrieval_hit": hit if case["should_answer"] else None,
                "refused": refusal,
                "abstention_correct": (
                    (not refusal and bool(sources))
                    if case["should_answer"]
                    else (refusal and not sources)
                ),
                "answer": answer,
                "scores": scores,
            }
            report_cases.append(row)
            rendered_scores = ", ".join(
                f"{name}={value:.3f}" if value is not None else f"{name}=n/a"
                for name, value in scores.items()
            )
            print(
                f"{case['id']}: retrieved={retrieved_chunks or '-'} "
                f"expected={expected_chunks or '-'} {rendered_scores}"
            )
            if not row["abstention_correct"]:
                print(f"  DEBUG answer={answer}")
        finally:
            memory.clear_for_user(owner_user_id, session_id)

    supported = [row for row in report_cases if row["should_answer"]]
    unsupported = [row for row in report_cases if not row["should_answer"]]
    summary = {
        name: mean(
            row["scores"][name]
            for row in supported
            if row["scores"][name] is not None
        )
        for name in ("faithfulness", "context_precision", "context_recall")
    }
    summary.update({
        "answerable_retrieval_hits": sum(row["retrieval_hit"] for row in supported),
        "answerable_cases": len(supported),
        "correct_abstentions": sum(row["abstention_correct"] for row in unsupported),
        "unanswerable_cases": len(unsupported),
    })
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": "configured application chat model",
        "summary": summary,
        "cases": report_cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help="write a JSON debugging report (default: data/evaluation/ragas-latest.json)",
    )
    args = parser.parse_args()
    username = os.environ.get("RAGAS_USERNAME", "")
    password = os.environ.get("RAGAS_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Set RAGAS_USERNAME and RAGAS_PASSWORD for a test account")

    try:
        report = evaluate(username, password)
    finally:
        auth_store.close_auth_store()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("\nRAGAS summary:")
    for name, value in report["summary"].items():
        print(f"  {name}: {value:.3f}" if isinstance(value, float) else f"  {name}: {value}")
    print(f"Detailed report: {args.output}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
