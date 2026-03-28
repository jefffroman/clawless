"""Clawless memory retrieval server.

Long-running aiohttp process that keeps SentenceTransformer and ChromaDB warm
so the context-engine plugin gets sub-second responses on every agent turn.
Binds to 127.0.0.1:3271 (loopback only).
"""

import json, os, hashlib, logging, asyncio, functools
from aiohttp import web

import chromadb, networkx as nx
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
WORKSPACE   = os.path.dirname(SCRIPT_DIR)
MEMORY_FILE = os.path.join(WORKSPACE, "MEMORY.md")
VECTOR_DB   = os.path.join(SCRIPT_DIR, "chroma_db")
BM25_CORPUS = os.path.join(SCRIPT_DIR, "bm25_corpus.json")
GRAPH_FILE  = os.path.join(SCRIPT_DIR, "memory_graph.json")
HEARTBEAT   = os.path.join(WORKSPACE, "memory", "heartbeat-state.json")
MODEL_NAME  = "all-MiniLM-L6-v2"
RRF_K       = 60

HOST = os.environ.get("MEMORY_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEMORY_SERVER_PORT", "3271"))

log = logging.getLogger("memory-server")


# ---------------------------------------------------------------------------
# Warm resources (loaded once at startup, reused across requests)
# ---------------------------------------------------------------------------

model = None
chroma_client = None


def warmup():
    global model, chroma_client
    log.info("Loading SentenceTransformer %s ...", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)
    log.info("Opening ChromaDB at %s ...", VECTOR_DB)
    chroma_client = chromadb.PersistentClient(path=VECTOR_DB)
    log.info("Warmup complete")


# ---------------------------------------------------------------------------
# Retrieval (mirrors search.py + auto_retrieve.py with pre-loaded resources)
# ---------------------------------------------------------------------------

def rrf_fuse(bm25_ranked, vector_ranked):
    scores = {}
    for rank, doc_id in enumerate(bm25_ranked):
        scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (RRF_K + rank + 1)
    for rank, doc_id in enumerate(vector_ranked):
        scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


def hybrid_search(query, n=5):
    try:
        with open(BM25_CORPUS) as f:
            corpus = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    tokenized   = [doc["text"].lower().split() for doc in corpus]
    bm25        = BM25Okapi(tokenized)
    bm25_scores = bm25.get_scores(query.lower().split())
    bm25_ranked = [corpus[i]["id"] for i in
                   sorted(range(len(bm25_scores)),
                          key=lambda x: bm25_scores[x], reverse=True)]

    col = chroma_client.get_collection("memory_chunks")
    vec = model.encode([query]).tolist()
    results = col.query(
        query_embeddings=vec,
        n_results=min(n * 2, max(col.count(), 1)),
        include=["documents", "metadatas", "distances"],
    )
    vector_ranked = [results["ids"][0][i] for i in
                     sorted(range(len(results["ids"][0])),
                            key=lambda x: results["distances"][0][x])]

    fused     = rrf_fuse(bm25_ranked, vector_ranked)[:n]
    id_to_doc = {doc["id"]: doc for doc in corpus}
    return [id_to_doc[fid] for fid in fused if fid in id_to_doc]


def query_graph(query, top_n=5):
    try:
        G = nx.node_link_graph(json.load(open(GRAPH_FILE)))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"nodes": 0, "related": []}
    terms = query.lower().split()
    hits  = [n for n in G.nodes() if any(t in n.lower() for t in terms)]
    results = []
    for node in hits[:top_n]:
        neighbors = list(G.successors(node)) + list(G.predecessors(node))
        results.append({"node": node, "neighbors": neighbors[:6]})
    return {"nodes": G.number_of_nodes(), "related": results}


def get_sync_status():
    try:
        state        = json.load(open(HEARTBEAT))["memorySync"]
        current_hash = hashlib.md5(open(MEMORY_FILE, "rb").read()).hexdigest()
        if current_hash != state["memoryMdHash"]:
            state["status"] = "OUT_OF_SYNC"
        return state
    except Exception:
        return {"status": "UNKNOWN", "lastSync": "never"}


def build_retrieval_markdown(query, top_n=5, compact=True):
    sync   = get_sync_status()
    chunks = hybrid_search(query, n=top_n)
    graph  = query_graph(query)

    lines = ["## Auto-Retrieved Memory Context"]
    lines.append(f"**Sync:** {sync['status']} · Last: {sync.get('lastSync', 'never')[:19]}")

    lines.append(f"\n### Hybrid Search ({len(chunks)} results — BM25 + vector + RRF)")
    for r in chunks:
        snippet = r["text"][:150] if compact else r["text"][:300]
        lines.append(f"- **[{r['section']}]** {snippet}")

    lines.append(f"\n### Knowledge Graph ({graph['nodes']} nodes)")
    for r in graph["related"]:
        lines.append(f"- **{r['node']}** → {', '.join(r['neighbors'][:4])}")

    if sync["status"] == "OUT_OF_SYNC":
        lines.append("\n### WARNING: MEMORY OUT OF SYNC — index may be stale")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_retrieve(request):
    body = await request.json()
    query   = body.get("query", "").strip()
    top_n   = body.get("top_n", 5)
    compact = body.get("compact", True)

    if not query:
        return web.json_response({"error": "query is required"}, status=400)

    loop = asyncio.get_event_loop()
    markdown = await loop.run_in_executor(
        None,
        functools.partial(build_retrieval_markdown, query, top_n=top_n, compact=compact),
    )
    tokens_est = len(markdown) // 4
    return web.json_response({"markdown": markdown, "tokens_est": tokens_est})


async def handle_status(request):
    loop   = asyncio.get_event_loop()
    status = await loop.run_in_executor(None, get_sync_status)
    return web.json_response(status)


async def handle_health(request):
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

def create_app():
    app = web.Application()
    app.router.add_post("/retrieve", handle_retrieve)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    warmup()
    app = create_app()
    log.info("Listening on %s:%d", HOST, PORT)
    web.run_app(app, host=HOST, port=PORT, print=None)
