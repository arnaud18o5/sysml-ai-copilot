import sys

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
    # The path pattern below is undirected (no `>`/`<`) so CONNECTS_TO and
    # SATISFIES, which are genuinely symmetric between two related elements,
    # can be traversed either way. TYPED_BY is not symmetric though: it
    # points from a usage to its type, and traversing it backward walks into
    # every *other* usage that happens to share that type -- unrelated
    # siblings, not real impact. The WHERE clause below allows a TYPED_BY
    # hop only when it's traversed forward (usage -> type), by checking that
    # the relationship's actual start node matches the node the path visits
    # it from.
    result = session.run(
        f"""
        MATCH path = (start:Element {{id: $id}})
                     -[:{IMPACT_RELATIONSHIP_TYPES}*1..{max_hops}]-(other:Element)
        WHERE other.id <> $id
          AND all(i IN range(0, length(path) - 1) WHERE
                type(relationships(path)[i]) <> 'TYPED_BY'
                OR startNode(relationships(path)[i]) = nodes(path)[i]
              )
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


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "what is the impact of the fuel pump?"
    run(query)
