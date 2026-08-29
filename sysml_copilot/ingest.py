import sys

from neo4j import GraphDatabase

from . import config
from .embeddings import embed_texts, element_text
from .parser import parse_sysml

# Relationship types the parser is known to emit. Cypher relationship types
# can't be parameterized, so `load_relations` interpolates this value into
# the query string directly — validate against this allow-list first rather
# than trusting `relation["type"]` unchecked.
KNOWN_RELATIONSHIP_TYPES = {"CONTAINS", "TYPED_BY", "CONNECTS_TO", "SATISFIES"}


def get_driver():
    return GraphDatabase.driver(
        config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
    )


def reset_graph(session):
    session.run("MATCH (n:Element) DETACH DELETE n")


def ensure_vector_index(session):
    session.run(
        """
        CREATE VECTOR INDEX element_embeddings IF NOT EXISTS
        FOR (e:Element) ON (e.embedding)
        OPTIONS {
          indexConfig: {
            `vector.dimensions`: $dim,
            `vector.similarity_function`: 'cosine'
          }
        }
        """,
        dim=config.EMBEDDING_DIMENSIONS,
    )


def load_elements(session, elements, embeddings):
    for element, embedding in zip(elements, embeddings):
        session.run(
            """
            MERGE (e:Element {id: $id})
            SET e.kind = $kind,
                e.name = $name,
                e.qualified_name = $qualified_name,
                e.doc = $doc,
                e.embedding = $embedding
            """,
            id=element["id"],
            kind=element["kind"],
            name=element["name"],
            qualified_name=element["qualified_name"],
            doc=element.get("doc"),
            embedding=embedding,
        )


def load_relations(session, relations):
    for relation in relations:
        if relation["type"] not in KNOWN_RELATIONSHIP_TYPES:
            raise ValueError(f"Unknown relation type: {relation['type']!r}")
        session.run(
            f"""
            MATCH (a:Element {{id: $source}})
            MATCH (b:Element {{id: $target}})
            MERGE (a)-[:{relation['type']}]->(b)
            """,
            source=relation["source"],
            target=relation["target"],
        )


def ingest_file(path):
    with open(path) as f:
        text = f.read()
    elements, relations = parse_sysml(text)

    texts = [element_text(e) for e in elements]
    embeddings = embed_texts(texts)

    driver = get_driver()
    with driver.session() as session:
        reset_graph(session)
        ensure_vector_index(session)
        load_elements(session, elements, embeddings)
        load_relations(session, relations)
    driver.close()

    print(f"{len(elements)} elements and {len(relations)} relations loaded from {path}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/sample.sysml"
    ingest_file(path)
