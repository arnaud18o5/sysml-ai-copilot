import sys

from neo4j import GraphDatabase

from . import config
from .embeddings import embed_texts, element_text
from .parser import SysmlSyntaxError, parse_sysml


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
                e.value = $value,
                e.embedding = $embedding
            """,
            id=element["id"],
            kind=element["kind"],
            name=element["name"],
            qualified_name=element["qualified_name"],
            doc=element.get("doc"),
            value=element.get("value"),
            embedding=embedding,
        )


def load_relations(session, relations):
    for relation in relations:
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
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        print(f"Could not read file {path}: {e.strerror or e}")
        sys.exit(1)

    try:
        elements, relations = parse_sysml(text)
    except SysmlSyntaxError as exc:
        if isinstance(exc.line, int) and exc.line > 0:
            print(f"SysML v2 syntax error in {path}, at line {exc.line}, column {exc.column}:")
        else:
            print(f"SysML v2 syntax error in {path}:")
        if exc.context:
            print(exc.context)
        else:
            print(str(exc))
        sys.exit(1)

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
