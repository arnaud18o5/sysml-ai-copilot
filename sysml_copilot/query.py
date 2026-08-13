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
            print("Aucun élément trouvé.")
            return
        best = candidates[0]
        print(
            f"Élément résolu : {best['qualified_name']} "
            f"({best['kind']}, score={best['score']:.3f})"
        )
        if len(candidates) > 1:
            print("Autres candidats :")
            for c in candidates[1:]:
                print(f"  - {c['qualified_name']} (score={c['score']:.3f})")

        impacted = impact_analysis(session, best["id"], max_hops=max_hops)
        print(f"\nAnalyse d'impact ({len(impacted)} élément(s) atteint(s), {max_hops} hop(s) max) :")
        for item in impacted:
            print(f"  [{item['distance']}] {item['qualified_name']} ({item['kind']})")
    driver.close()


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "quel est l'impact de la pompe à carburant ?"
    run(query)
