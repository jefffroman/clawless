"""In-process memory retrieval (lifted from former memory_server.py).

ChromaDB + BM25 + NetworkX hybrid retrieval with RRF fusion. Source files live
under ``${WORKSPACE_DIR}/memory/``; ChromaDB persists at
``${MEMORY_DATA_DIR}`` (ephemeral per-container, rebuilt at boot).

Embeddings come from ChromaDB's bundled ONNX runtime (all-MiniLM-L6-v2) — no
torch / sentence-transformers, shaves ~300 MB off the image.

The OpenClaw-era separation (memory as a separate aiohttp process on
loopback) was forced by the Node↔Python boundary. With a Python gateway it
collapses into a function call: this module owns the indexes; the gateway
calls ``retrieve_markdown`` on every turn before assembling the prompt.
"""

from __future__ import annotations

import asyncio
import functools
import glob as globmod
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import chromadb
import networkx as nx
from chromadb.utils import embedding_functions
from rank_bm25 import BM25Okapi

log = logging.getLogger("clawless.memory")

MODEL_NAME = "all-MiniLM-L6-v2"
RRF_K = 60

# Calibrated against MiniLM-L6-v2 distance distributions: topical hits cluster
# at 1.41-1.44, weak/gibberish queries plateau at 1.50+.
VECTOR_DISTANCE_MAX = 1.50

EXCLUDED_SECTION_PATTERNS = re.compile(
    r"^(Auto-Retrieved Memory Context|Conversation Summary|Hybrid Search|Knowledge Graph)\b"
)

DAILY_NOTE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

ROOT_SOURCES = (
    "MEMORY.md", "SOUL.md", "AGENTS.md", "HEARTBEAT.md",
    "PROJECTS.md", "TOOLS.md", "IDENTITY.md", "USER.md",
    "ARCHITECTURE.md",
)


class MemoryIndex:
    def __init__(self, source_dir: str, data_dir: str, slug_safe: str) -> None:
        self.source_dir = source_dir
        self.data_dir = data_dir
        self.slug_safe = slug_safe or "default"
        self.embedder: Any | None = None
        self.chroma_client: chromadb.api.ClientAPI | None = None
        # Lock prevents reindex from racing against concurrent retrieval reads
        # of the same JSON sidecars (bm25_corpus.json, memory_graph.json).
        self.lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------

    def warmup(self) -> None:
        log.info("loading embedding function (%s via ONNX)", MODEL_NAME)
        self.embedder = embedding_functions.DefaultEmbeddingFunction()
        os.makedirs(self.data_dir, exist_ok=True)
        db_path = os.path.join(self.data_dir, "chroma_db")
        self.chroma_client = chromadb.PersistentClient(path=db_path)
        log.info("chromadb ready at %s", db_path)

    async def warmup_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.warmup)

    # --- source collection --------------------------------------------------

    def _daily_notes(self) -> list[str]:
        return [
            p for p in sorted(globmod.glob(os.path.join(self.source_dir, "memory", "*.md")))
            if DAILY_NOTE_PATTERN.match(os.path.basename(p))
        ]

    def _source_paths(self) -> list[str]:
        paths: list[str] = []
        for name in ROOT_SOURCES:
            p = os.path.join(self.source_dir, name)
            if os.path.exists(p):
                paths.append(p)
        paths.extend(self._daily_notes())
        return paths

    def _compute_sources_hash(self) -> str:
        h = hashlib.md5()
        for path in self._source_paths():
            try:
                with open(path, "rb") as f:
                    h.update(f.read())
            except FileNotFoundError:
                pass
        return h.hexdigest()

    @staticmethod
    def _parse_markdown(path: str) -> list[dict[str, Any]]:
        with open(path) as f:
            content = f.read()
        chunks: list[dict[str, Any]] = []
        sections = re.split(r"(^##\s+.*$)", content, flags=re.MULTILINE)
        if sections[0].strip():
            chunks.append({"content": sections[0].strip(), "metadata": {"section": "Intro"}})
        for i in range(1, len(sections), 2):
            header = sections[i].strip().lstrip("#").strip()
            body = sections[i + 1].strip() if i + 1 < len(sections) else ""
            if body and not EXCLUDED_SECTION_PATTERNS.match(header):
                chunks.append({"content": body, "metadata": {"section": header}})
        return chunks

    def _collect_sources(self) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for fname in ROOT_SOURCES:
            fpath = os.path.join(self.source_dir, fname)
            if os.path.exists(fpath):
                for c in self._parse_markdown(fpath):
                    c["metadata"]["source"] = fname
                    chunks.append(c)
        for fpath in self._daily_notes():
            for c in self._parse_markdown(fpath):
                c["metadata"]["source"] = f"memory/{os.path.basename(fpath)}"
                chunks.append(c)
        return chunks

    # --- indexing -----------------------------------------------------------

    @staticmethod
    def _build_graph(chunks: list[dict[str, Any]]) -> nx.DiGraph:
        G: nx.DiGraph = nx.DiGraph()
        for c in chunks:
            G.add_node(c["metadata"]["section"], type="section")
            for concept in re.findall(r"\*\*(.*?)\*\*", c["content"]):
                if 3 <= len(concept) <= 50:
                    G.add_node(concept, type="concept")
                    G.add_edge(c["metadata"]["section"], concept, relation="contains")
        nodes = set(G.nodes())
        for c in chunks:
            for target in nodes:
                if target != c["metadata"]["section"] and target in c["content"]:
                    G.add_edge(c["metadata"]["section"], target, relation="mentions")
        return G

    @staticmethod
    def _atomic_write_json(path: str, obj: Any) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)

    def needs_reindex(self) -> bool:
        if not self._source_paths():
            return False
        state_path = os.path.join(self.data_dir, "sync_state.json")
        if not os.path.exists(state_path):
            return True
        try:
            with open(state_path) as f:
                state = json.load(f)
            return self._compute_sources_hash() != state.get("sourcesHash", "")
        except Exception:
            return True

    def do_reindex(self) -> dict[str, Any]:
        assert self.chroma_client is not None and self.embedder is not None, "warmup() not called"
        chunks = self._collect_sources()
        if not chunks:
            return {"status": "skipped", "reason": "no sources"}

        bm25_path = os.path.join(self.data_dir, "bm25_corpus.json")
        graph_path = os.path.join(self.data_dir, "memory_graph.json")
        state_path = os.path.join(self.data_dir, "sync_state.json")

        sources = sorted({c["metadata"]["source"] for c in chunks})
        log.info("indexing %d chunks from %s", len(chunks), ", ".join(sources))

        col_name = f"memory_{self.slug_safe}"
        try:
            self.chroma_client.delete_collection(col_name)
        except Exception:
            pass
        col = self.chroma_client.create_collection(col_name, embedding_function=self.embedder)

        ids = [f"{c['metadata']['source']}:{i}" for i, c in enumerate(chunks)]
        documents = [c["content"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)

        corpus = [
            {"id": ids[i], "text": documents[i], "section": metadatas[i]["section"]}
            for i in range(len(chunks))
        ]
        self._atomic_write_json(bm25_path, corpus)

        G = self._build_graph(chunks)
        self._atomic_write_json(graph_path, nx.node_link_data(G))

        state = {
            "agent_slug": self.slug_safe,
            "sourcesHash": self._compute_sources_hash(),
            "sources": sources,
            "chromadbChunks": len(chunks),
            "graphNodes": G.number_of_nodes(),
            "graphEdges": G.number_of_edges(),
            "lastSync": datetime.now(timezone.utc).isoformat(),
            "status": "synced",
        }
        self._atomic_write_json(state_path, state)
        log.info("done: %d chunks, %d graph nodes", len(chunks), G.number_of_nodes())
        return {
            "status": "reindexed",
            "chunks": len(chunks),
            "graphNodes": G.number_of_nodes(),
            "sources": sources,
        }

    async def reindex_if_stale(self, force: bool = False) -> dict[str, Any]:
        async with self.lock:
            loop = asyncio.get_running_loop()
            if not force and not await loop.run_in_executor(None, self.needs_reindex):
                return {"status": "in_sync"}
            return await loop.run_in_executor(None, self.do_reindex)

    # --- retrieval ----------------------------------------------------------

    @staticmethod
    def _rrf_fuse(bm25_ranked: list[str], vector_ranked: list[str]) -> list[str]:
        scores: dict[str, float] = {}
        for rank, doc_id in enumerate(bm25_ranked):
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (RRF_K + rank + 1)
        for rank, doc_id in enumerate(vector_ranked):
            scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (RRF_K + rank + 1)
        return sorted(scores, key=lambda x: scores[x], reverse=True)

    def _hybrid_search(self, query: str, n: int = 5) -> list[dict[str, Any]]:
        bm25_path = os.path.join(self.data_dir, "bm25_corpus.json")
        try:
            with open(bm25_path) as f:
                corpus = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

        tokenized = [doc["text"].lower().split() for doc in corpus]
        bm25 = BM25Okapi(tokenized)
        bm25_scores = bm25.get_scores(query.lower().split())
        bm25_ranked = [
            corpus[i]["id"]
            for i in sorted(range(len(bm25_scores)), key=lambda x: bm25_scores[x], reverse=True)
            if bm25_scores[i] > 0
        ]

        if self.chroma_client is None:
            return []
        col_name = f"memory_{self.slug_safe}"
        try:
            col = self.chroma_client.get_collection(col_name, embedding_function=self.embedder)
        except Exception:
            return []

        total = max(col.count(), 1)
        results = col.query(
            query_texts=[query],
            n_results=min(n * 2, total),
            include=["documents", "metadatas", "distances"],
        )
        vector_ranked = [
            results["ids"][0][i]
            for i in sorted(range(len(results["ids"][0])), key=lambda x: results["distances"][0][x])
            if results["distances"][0][i] < VECTOR_DISTANCE_MAX
        ]

        fused = self._rrf_fuse(bm25_ranked, vector_ranked)[:n]
        id_to_doc = {doc["id"]: doc for doc in corpus}
        return [id_to_doc[fid] for fid in fused if fid in id_to_doc]

    def _query_graph(self, query: str, top_n: int = 5) -> dict[str, Any]:
        graph_path = os.path.join(self.data_dir, "memory_graph.json")
        try:
            with open(graph_path) as f:
                G = nx.node_link_graph(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"nodes": 0, "related": []}
        terms = query.lower().split()
        hits = [n for n in G.nodes() if any(t in n.lower() for t in terms)]
        results = []
        for node in hits[:top_n]:
            neighbors = list(G.successors(node)) + list(G.predecessors(node))
            results.append({"node": node, "neighbors": neighbors[:6]})
        return {"nodes": G.number_of_nodes(), "related": results}

    def _sync_status(self) -> dict[str, Any]:
        state_path = os.path.join(self.data_dir, "sync_state.json")
        try:
            with open(state_path) as f:
                state = json.load(f)
            if self._compute_sources_hash() != state.get("sourcesHash", ""):
                state["status"] = "OUT_OF_SYNC"
            return state
        except Exception:
            return {"status": "UNKNOWN", "lastSync": "never"}

    def _build_markdown(self, query: str, top_n: int, compact: bool) -> str:
        sync = self._sync_status()
        chunks = self._hybrid_search(query, n=top_n)
        graph = self._query_graph(query)

        lines = ["## Auto-Retrieved Memory Context"]
        if sync["status"] != "synced":
            lines.append(f"**Sync:** {sync['status']} · Last: {sync.get('lastSync', 'never')[:19]}")
        if chunks:
            lines.append(f"\n### Hybrid Search ({len(chunks)} results — BM25 + vector + RRF)")
            for r in chunks:
                snippet = r["text"][:150] if compact else r["text"][:300]
                lines.append(f"- **[{r['section']}]** {snippet}")
        else:
            lines.append("\n_No strong matches in memory for this query._")
        if graph["related"]:
            lines.append(f"\n### Knowledge Graph ({graph['nodes']} nodes)")
            for r in graph["related"]:
                lines.append(f"- **{r['node']}** -> {', '.join(r['neighbors'][:4])}")
        if sync["status"] == "OUT_OF_SYNC":
            lines.append("\n### WARNING: MEMORY OUT OF SYNC — index may be stale")
        return "\n".join(lines)

    async def retrieve_markdown(self, query: str, top_n: int = 5, compact: bool = True) -> str:
        if not query.strip():
            return ""
        async with self.lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                functools.partial(self._build_markdown, query, top_n, compact),
            )


async def reindex_loop(index: MemoryIndex, interval_s: int = 300) -> None:
    """Background reindex task. Runs forever until cancelled."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            result = await index.reindex_if_stale()
            if result.get("status") == "reindexed":
                log.info("background reindex: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background reindex failed")
