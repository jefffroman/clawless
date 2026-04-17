"""Single-agent memory retrieval server for the Fargate gateway.

Long-running aiohttp process that keeps SentenceTransformer and ChromaDB warm
so the context-engine plugin gets sub-second responses on every agent turn.

One Fargate task = one agent, so everything is keyed off AGENT_SLUG from the
task-def env. Data lives at DATA_ROOT (ephemeral per-container — rebuilt on
boot from the workspace's memory source files).

Also owns all ChromaDB writes. The entrypoint's reindex loop POSTs to
/reindex rather than opening the databases directly, avoiding SQLite
contention.

Binds to 127.0.0.1:3271 (loopback only).
"""

import asyncio
import functools
import glob as globmod
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

import chromadb
import networkx as nx
import yaml
from aiohttp import web
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

AGENT_SLUG    = os.environ.get("AGENT_SLUG", "").strip()
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/home/openclaw")
DATA_ROOT     = os.environ.get("MEMORY_DATA_DIR", "/var/lib/clawless-memory")
HOST          = os.environ.get("MEMORY_SERVER_HOST", "127.0.0.1")
PORT          = int(os.environ.get("MEMORY_SERVER_PORT", "3271"))
MODEL_NAME    = "all-MiniLM-L6-v2"
RRF_K         = 60

log = logging.getLogger("memory-server")

model = None
chroma_client = None


def warmup():
    global model, chroma_client
    log.info("Loading SentenceTransformer %s ...", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)

    os.makedirs(DATA_ROOT, exist_ok=True)
    db_path = os.path.join(DATA_ROOT, "chroma_db")
    chroma_client = chromadb.PersistentClient(path=db_path)
    log.info("ChromaDB ready at %s", db_path)


# ---------------------------------------------------------------------------
# Source collection
# ---------------------------------------------------------------------------

ROOT_SOURCES = (
    "MEMORY.md", "SOUL.md", "AGENTS.md", "HEARTBEAT.md",
    "PROJECTS.md", "TOOLS.md", "IDENTITY.md", "USER.md",
    "ARCHITECTURE.md",
)


def _source_paths():
    paths = []
    for name in ROOT_SOURCES:
        p = os.path.join(WORKSPACE_DIR, name)
        if os.path.exists(p):
            paths.append(p)
    for sub in ("memory", "reference"):
        paths.extend(sorted(globmod.glob(os.path.join(WORKSPACE_DIR, sub, "*.md"))))
    paths.extend(sorted(globmod.glob(os.path.join(WORKSPACE_DIR, "skills", "*", "SKILL.md"))))
    return paths


def compute_sources_hash():
    h = hashlib.md5()
    for path in _source_paths():
        try:
            h.update(open(path, "rb").read())
        except FileNotFoundError:
            pass
    return h.hexdigest()


def parse_markdown(path):
    with open(path) as f:
        content = f.read()
    chunks, sections = [], re.split(r'(^##\s+.*$)', content, flags=re.MULTILINE)
    header = "Intro"
    if sections[0].strip():
        chunks.append({"content": sections[0].strip(), "metadata": {"section": header}})
    for i in range(1, len(sections), 2):
        header = sections[i].strip().lstrip('#').strip()
        body   = sections[i + 1].strip() if i + 1 < len(sections) else ""
        if body:
            chunks.append({"content": body, "metadata": {"section": header}})
    return chunks


def parse_frontmatter(path):
    try:
        with open(path) as f:
            content = f.read()
        if not content.startswith("---"):
            return {}
        end = content.index("---", 3)
        return yaml.safe_load(content[3:end]) or {}
    except Exception:
        return {}


def collect_sources():
    chunks = []

    for fname in ROOT_SOURCES:
        fpath = os.path.join(WORKSPACE_DIR, fname)
        if os.path.exists(fpath):
            for c in parse_markdown(fpath):
                c["metadata"]["source"] = fname
                chunks.append(c)

    for sub in ("memory", "reference"):
        for fpath in sorted(globmod.glob(os.path.join(WORKSPACE_DIR, sub, "*.md"))):
            fname = os.path.basename(fpath)
            for c in parse_markdown(fpath):
                c["metadata"]["source"] = f"{sub}/{fname}"
                chunks.append(c)

    # Skills: index name + description from frontmatter only.
    for skill_file in sorted(globmod.glob(os.path.join(WORKSPACE_DIR, "skills", "*", "SKILL.md"))):
        skill_dir = os.path.basename(os.path.dirname(skill_file))
        fm = parse_frontmatter(skill_file)
        if fm.get("name") and fm.get("description"):
            chunks.append({
                "content": f"Skill: {fm['name']} — {fm['description']}",
                "metadata": {"section": f"skill:{fm['name']}", "source": f"skills/{skill_dir}"},
            })

    return chunks


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

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


def _atomic_write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def needs_reindex():
    if not _source_paths():
        return False
    state_path = os.path.join(DATA_ROOT, "sync_state.json")
    if not os.path.exists(state_path):
        return True
    try:
        state = json.load(open(state_path))
        return compute_sources_hash() != state.get("sourcesHash", "")
    except Exception:
        return True


def do_reindex():
    chunks = collect_sources()
    if not chunks:
        return {"status": "skipped", "reason": "no sources"}

    bm25_path  = os.path.join(DATA_ROOT, "bm25_corpus.json")
    graph_path = os.path.join(DATA_ROOT, "memory_graph.json")
    state_path = os.path.join(DATA_ROOT, "sync_state.json")

    sources = sorted(set(c["metadata"]["source"] for c in chunks))
    log.info("Indexing %d chunks from %s", len(chunks), ", ".join(sources))

    col_name = f"memory_{AGENT_SLUG or 'default'}"
    try:
        chroma_client.delete_collection(col_name)
    except Exception:
        pass
    col = chroma_client.create_collection(col_name)

    ids        = [f"{c['metadata']['source']}:{i}" for i, c in enumerate(chunks)]
    documents  = [c["content"] for c in chunks]
    metadatas  = [c["metadata"] for c in chunks]
    embeddings = model.encode(documents).tolist()
    col.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)

    corpus = [
        {"id": ids[i], "text": documents[i], "section": metadatas[i]["section"]}
        for i in range(len(chunks))
    ]
    _atomic_write_json(bm25_path, corpus)

    G = build_graph(chunks)
    _atomic_write_json(graph_path, nx.node_link_data(G))

    state = {
        "agent_slug":     AGENT_SLUG,
        "sourcesHash":    compute_sources_hash(),
        "sources":        sources,
        "chromadbChunks": len(chunks),
        "graphNodes":     G.number_of_nodes(),
        "graphEdges":     G.number_of_edges(),
        "lastSync":       datetime.now(timezone.utc).isoformat(),
        "status":         "synced",
    }
    _atomic_write_json(state_path, state)

    log.info("Done: %d chunks, %d graph nodes", len(chunks), G.number_of_nodes())
    return {
        "status":     "reindexed",
        "chunks":     len(chunks),
        "graphNodes": G.number_of_nodes(),
        "sources":    sources,
    }


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def rrf_fuse(bm25_ranked, vector_ranked):
    scores = {}
    for rank, doc_id in enumerate(bm25_ranked):
        scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (RRF_K + rank + 1)
    for rank, doc_id in enumerate(vector_ranked):
        scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


def hybrid_search(query, n=5):
    bm25_path = os.path.join(DATA_ROOT, "bm25_corpus.json")
    try:
        with open(bm25_path) as f:
            corpus = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    tokenized   = [doc["text"].lower().split() for doc in corpus]
    bm25        = BM25Okapi(tokenized)
    bm25_scores = bm25.get_scores(query.lower().split())
    bm25_ranked = [corpus[i]["id"] for i in
                   sorted(range(len(bm25_scores)),
                          key=lambda x: bm25_scores[x], reverse=True)]

    col_name = f"memory_{AGENT_SLUG or 'default'}"
    try:
        col = chroma_client.get_collection(col_name)
    except Exception:
        return []

    vec = model.encode([query]).tolist()
    total = max(col.count(), 1)
    results = col.query(
        query_embeddings=vec,
        n_results=min(n * 2, total),
        include=["documents", "metadatas", "distances"],
    )
    vector_ranked = [results["ids"][0][i] for i in
                     sorted(range(len(results["ids"][0])),
                            key=lambda x: results["distances"][0][x])]

    fused     = rrf_fuse(bm25_ranked, vector_ranked)[:n]
    id_to_doc = {doc["id"]: doc for doc in corpus}
    return [id_to_doc[fid] for fid in fused if fid in id_to_doc]


def query_graph(query, top_n=5):
    graph_path = os.path.join(DATA_ROOT, "memory_graph.json")
    try:
        G = nx.node_link_graph(json.load(open(graph_path)))
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
    state_path = os.path.join(DATA_ROOT, "sync_state.json")
    try:
        state = json.load(open(state_path))
        if compute_sources_hash() != state.get("sourcesHash", ""):
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
        lines.append(f"- **{r['node']}** -> {', '.join(r['neighbors'][:4])}")

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
    return web.json_response({"markdown": markdown, "tokens_est": len(markdown) // 4})


async def handle_reindex(request):
    loop = asyncio.get_event_loop()
    body = await request.json() if request.content_length else {}
    force = bool(body.get("force", False))

    if not force and not await loop.run_in_executor(None, needs_reindex):
        return web.json_response({"status": "in_sync"})
    return web.json_response(await loop.run_in_executor(None, do_reindex))


async def handle_status(request):
    loop = asyncio.get_event_loop()
    return web.json_response(await loop.run_in_executor(None, get_sync_status))


async def handle_health(request):
    return web.json_response({"ok": True, "agent_slug": AGENT_SLUG})


def create_app():
    app = web.Application()
    app.router.add_post("/retrieve", handle_retrieve)
    app.router.add_post("/reindex",  handle_reindex)
    app.router.add_get ("/status",   handle_status)
    app.router.add_get ("/health",   handle_health)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    warmup()
    # Initial index on boot so /retrieve has something on the first turn.
    if needs_reindex():
        try:
            do_reindex()
        except Exception:
            log.exception("initial reindex failed")
    log.info("Listening on %s:%d (agent=%s)", HOST, PORT, AGENT_SLUG or "<unset>")
    web.run_app(create_app(), host=HOST, port=PORT, print=None)
