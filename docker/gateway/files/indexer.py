import os, re, json, hashlib, chromadb, networkx as nx
from chromadb.utils import embedding_functions
from datetime import datetime, timezone

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
WORKSPACE    = os.path.dirname(SCRIPT_DIR)
MEMORY_FILE  = os.path.join(WORKSPACE, "MEMORY.md")
VECTOR_DB    = os.path.join(SCRIPT_DIR, "chroma_db")
GRAPH_FILE   = os.path.join(SCRIPT_DIR, "memory_graph.json")
BM25_CORPUS  = os.path.join(SCRIPT_DIR, "bm25_corpus.json")
HEARTBEAT    = os.path.join(WORKSPACE, "memory", "heartbeat-state.json")
MODEL_NAME   = "all-MiniLM-L6-v2"


def parse_markdown(path):
    with open(path) as f:
        content = f.read()
    chunks, sections = [], re.split(r'(^##\s+.*$)', content, flags=re.MULTILINE)
    header = "Intro"
    if sections[0].strip():
        chunks.append({"content": sections[0].strip(), "metadata": {"section": header}})
    for i in range(1, len(sections), 2):
        header = sections[i].strip().lstrip('#').strip()
        body   = sections[i+1].strip() if i+1 < len(sections) else ""
        if body:
            chunks.append({"content": body, "metadata": {"section": header}})
    return chunks


def build_graph(chunks):
    G = nx.DiGraph()
    for c in chunks:
        G.add_node(c["metadata"]["section"], type="section")
        for concept in re.findall(r'\*\*(.*?)\*\*', c["content"]):
            if 3 <= len(concept) <= 50:
                G.add_node(concept, type="concept")
                G.add_edge(c["metadata"]["section"], concept, relation="contains")
    nodes = set(G.nodes())
    for c in chunks:
        for target in nodes:
            if target != c["metadata"]["section"] and target in c["content"]:
                G.add_edge(c["metadata"]["section"], target, relation="mentions")
    return G


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def index_memory():
    print("Indexing MEMORY.md...")
    embedder = embedding_functions.DefaultEmbeddingFunction()
    client   = chromadb.PersistentClient(path=VECTOR_DB)
    col      = client.get_or_create_collection("memory_chunks", embedding_function=embedder)
    chunks   = parse_markdown(MEMORY_FILE)

    ids        = [f"mem_{i}" for i in range(len(chunks))]
    documents  = [c["content"] for c in chunks]
    metadatas  = [c["metadata"] for c in chunks]
    col.upsert(ids=ids, documents=documents, metadatas=metadatas)

    existing = col.count()
    if existing > len(chunks):
        col.delete(ids=[f"mem_{i}" for i in range(len(chunks), existing)])

    corpus = [{"id": ids[i], "text": documents[i], "section": metadatas[i]["section"]}
              for i in range(len(chunks))]
    with open(BM25_CORPUS, "w") as f:
        json.dump(corpus, f, indent=2)

    G = build_graph(chunks)
    with open(GRAPH_FILE, "w") as f:
        json.dump(nx.node_link_data(G), f, indent=2)

    os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
    state = {
        "memorySync": {
            "memoryMdHash": md5(MEMORY_FILE),
            "chromadbChunks": len(chunks),
            "graphNodes": G.number_of_nodes(),
            "graphEdges": G.number_of_edges(),
            "lastSync": datetime.now(timezone.utc).isoformat(),
            "status": "synced"
        }
    }
    with open(HEARTBEAT, "w") as f:
        json.dump(state, f, indent=2)

    print(f"Done: {len(chunks)} chunks · {G.number_of_nodes()} graph nodes · BM25 corpus saved")


if __name__ == "__main__":
    index_memory()
