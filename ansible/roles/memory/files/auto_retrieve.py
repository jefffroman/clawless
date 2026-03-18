import os, json, hashlib, sys, networkx as nx
from search import hybrid_search

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
WORKSPACE   = os.path.dirname(SCRIPT_DIR)
MEMORY_FILE = os.path.join(WORKSPACE, "MEMORY.md")
GRAPH_FILE  = os.path.join(SCRIPT_DIR, "memory_graph.json")
HEARTBEAT   = os.path.join(WORKSPACE, "memory", "heartbeat-state.json")


def get_sync_status():
    try:
        state        = json.load(open(HEARTBEAT))["memorySync"]
        current_hash = hashlib.md5(open(MEMORY_FILE, "rb").read()).hexdigest()
        if current_hash != state["memoryMdHash"]:
            state["status"] = "OUT_OF_SYNC"
        return state
    except Exception:
        return {"status": "UNKNOWN", "lastSync": "never"}


def query_graph(query, top_n=5):
    try:
        G     = nx.node_link_graph(json.load(open(GRAPH_FILE)))
        terms = query.lower().split()
        hits  = [n for n in G.nodes() if any(t in n.lower() for t in terms)]
        results = []
        for node in hits[:top_n]:
            neighbors = list(G.successors(node)) + list(G.predecessors(node))
            results.append({"node": node, "neighbors": neighbors[:6]})
        return {"nodes": G.number_of_nodes(), "related": results}
    except Exception:
        return {"nodes": 0, "related": []}


def auto_retrieve(query, compact=False):
    sync   = get_sync_status()
    chunks = hybrid_search(query)
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
        lines.append("\n### ⚠️ MEMORY OUT OF SYNC — run indexer.py before proceeding")

    return "\n".join(lines)


if __name__ == "__main__":
    args    = sys.argv[1:]
    compact = "--compact" in args
    status  = "--status" in args
    query   = " ".join(a for a in args if not a.startswith("--")) or "current status"
    if status:
        print(json.dumps(get_sync_status(), indent=2))
    else:
        print(auto_retrieve(query, compact=compact))
