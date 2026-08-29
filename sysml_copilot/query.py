import argparse

from .embeddings import embed_texts
from .ingest import get_driver

IMPACT_RELATIONSHIP_TYPES = "CONNECTS_TO|TYPED_BY|SATISFIES"


def resolve_element(session, nl_query, top_k=3):
    embedding = embed_texts([nl_query])[0]
    result = session.run(
        """
        CALL db.index.vector.queryNodes('element_embeddings', $top_k, $embedding)
        YIELD node, score
        RETURN node.id AS id, node.kind AS kind, node.name AS name,
               node.qualified_name AS qualified_name, score
        """,
        top_k=top_k,
        embedding=embedding,
    )
    return [dict(record) for record in result]


def impact_analysis(session, element_id, max_hops=3):
    result = session.run(
        f"""
        MATCH path = (start:Element {{id: $id}})
                     -[:{IMPACT_RELATIONSHIP_TYPES}*1..{max_hops}]-(other:Element)
        WHERE other.id <> $id
        WITH other, min(length(path)) AS distance
        RETURN other.id AS id, other.kind AS kind, other.name AS name,
               other.qualified_name AS qualified_name, distance
        ORDER BY distance, id
        """,
        id=element_id,
    )
    return [dict(record) for record in result]


def run(nl_query, max_hops=3, top_k=3):
    driver = get_driver()
    with driver.session() as session:
        candidates = resolve_element(session, nl_query, top_k=top_k)
        if not candidates:
            print("No element found.")
            return
        best = candidates[0]
        print(
            f"Resolved element: {best['qualified_name']} "
            f"({best['kind']}, score={best['score']:.3f})"
        )
        if len(candidates) > 1:
            print("Other candidates:")
            for c in candidates[1:]:
                print(f"  - {c['qualified_name']} (score={c['score']:.3f})")

        impacted = impact_analysis(session, best["id"], max_hops=max_hops)
        print(f"\nImpact analysis ({len(impacted)} element(s) reached, {max_hops} hop(s) max):")
        for item in impacted:
            print(f"  [{item['distance']}] {item['qualified_name']} ({item['kind']})")
    driver.close()


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="python -m sysml_copilot.query",
        description="Resolves a natural-language question to a model element "
        "then analyzes its impact by traversing the graph.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="Number of candidates to resolve via vector search (default: 3).",
    )
    parser.add_argument(
        "--max-hops",
        type=int,
        default=3,
        help="Maximum traversal depth for impact analysis (default: 3).",
    )
    parser.add_argument(
        "query",
        nargs=argparse.REMAINDER,
        help="Natural-language question (options must precede it).",
    )
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    query = " ".join(args.query) or "what is the impact of the fuel pump?"
    run(query, max_hops=args.max_hops, top_k=args.top_k)
