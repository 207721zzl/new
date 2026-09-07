"""运行检索离线评测并输出 JSON 与 CSV 报告。"""

import argparse
import csv
import json
from pathlib import Path

from app.config import PROJECT_ROOT, get_settings
from app.logging_config import get_logger
from app.rag.embeddings import get_embedder
from app.rag.evaluation import RetrievalEvaluator, load_evaluation_cases
from app.rag.milvus_store import MilvusKnowledgeStore
from app.rag.reranker import get_reranker


logger = get_logger("scripts.evaluate_retrieval")

DEFAULT_DEVELOPMENT_CASES = (
    PROJECT_ROOT / "data" / "knowledge" / "test_questions.json"
).resolve()


def parse_args() -> argparse.Namespace:
    """解析评测集、候选数、重排数和输出目录。"""
    parser = argparse.ArgumentParser(
        description="Evaluate Milvus hybrid retrieval and BGE reranking.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("data/knowledge/test_questions.json"),
        help="Evaluation case JSON file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports"),
        help="Directory for retrieval_evaluation.json and .csv.",
    )
    parser.add_argument("--candidate-top-k", type=int)
    parser.add_argument("--rerank-top-k", type=int)
    parser.add_argument(
        "--limit",
        type=int,
        help="Only evaluate the first N cases (useful for smoke tests).",
    )
    return parser.parse_args()


def resolve_project_path(path: Path) -> Path:
    """把相对路径统一解析到项目根目录。"""
    return path if path.is_absolute() else PROJECT_ROOT / path


def describe_evaluation_context(cases_path: Path) -> dict:
    """声明数据集来源边界，防止把开发回归结果包装成盲测效果。"""
    if cases_path.resolve() == DEFAULT_DEVELOPMENT_CASES:
        return {
            "evaluation_kind": "development_regression",
            "is_blind": False,
            "labels_visible_during_development": True,
            "corpus_and_cases_co_designed": True,
            "generalization_claim_allowed": False,
            "interpretation": (
                "仅用于发现固定链路回归；不得作为真实场景准确率或泛化能力证明。"
            ),
        }
    return {
        "evaluation_kind": "custom_dataset_unverified",
        "is_blind": None,
        "labels_visible_during_development": None,
        "corpus_and_cases_co_designed": None,
        "generalization_claim_allowed": False,
        "interpretation": (
            "评测器无法验证自定义数据是否为开发阶段未见的独立盲测集，"
            "数据来源和标注流程必须另行审计。"
        ),
    }


def csv_rows(report: dict) -> list[dict]:
    """把嵌套报告转换为便于人工筛选的扁平 CSV 行。"""
    rows = []
    for case in report["cases"]:
        hybrid_ids = [item["document_id"] for item in case["hybrid_hits"]]
        reranked_ids = [item["document_id"] for item in case["reranked_hits"]]
        expected = case["expected_document_ids"]
        rows.append(
            {
                "id": case["id"],
                "question": case["question"],
                "purpose": case["purpose"],
                "expected_document_ids": "|".join(expected),
                "hybrid_document_ids": "|".join(hybrid_ids),
                "reranked_document_ids": "|".join(reranked_ids),
                "matched_expected_ids": "|".join(case["matched_expected_ids"]),
                "passed_at_3": case["passed_at_3"],
                "reciprocal_rank": case["reciprocal_rank"],
                "embedding_ms": case["latency_ms"]["embedding"],
                "hybrid_search_ms": case["latency_ms"]["hybrid_search"],
                "rerank_ms": case["latency_ms"]["rerank"],
                "total_ms": case["latency_ms"]["total"],
            }
        )
    return rows


def write_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    """将同一评测结果同时持久化为 UTF-8 JSON 和 Excel 友好 CSV。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "retrieval_evaluation.json"
    csv_path = output_dir / "retrieval_evaluation.csv"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rows = csv_rows(report)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


def main() -> int:
    """加载真实检索依赖、运行评测并打印关键摘要。"""
    args = parse_args()
    cases_path = resolve_project_path(args.cases)
    output_dir = resolve_project_path(args.output_dir)
    cases = load_evaluation_cases(cases_path)
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("limit must be greater than zero")
        cases = cases[: args.limit]

    settings = get_settings()
    evaluator = RetrievalEvaluator(
        settings=settings,
        embedder=get_embedder(),
        reranker=get_reranker(),
        store_factory=lambda: MilvusKnowledgeStore(settings),
    )
    report = evaluator.evaluate(
        cases,
        candidate_top_k=args.candidate_top_k,
        rerank_top_k=args.rerank_top_k,
    )
    report = {
        "evaluation_context": describe_evaluation_context(cases_path),
        **report,
    }
    json_path, csv_path = write_report(report, output_dir)
    summary = report["summary"]
    print(
        json.dumps(
            {
                "evaluation_kind": report["evaluation_context"][
                    "evaluation_kind"
                ],
                "generalization_claim_allowed": False,
                "case_count": summary["case_count"],
                "hybrid_recall_at_3": summary["hybrid"]["recall_at_3"],
                "reranked_recall_at_3": summary["reranked"]["recall_at_3"],
                "reranked_mrr": summary["reranked"]["mrr"],
                "passed_at_3_rate": summary["passed_at_3_rate"],
                "json_report": str(json_path),
                "csv_report": str(csv_path),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
