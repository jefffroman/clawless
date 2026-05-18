"""In-process memory retrieval (lifted from former memory_server.py).

int8 vector store + BM25 + NetworkX hybrid retrieval with RRF fusion. Source
markdown lives under ``${WORKSPACE_DIR}/memory/`` and is **authoritative**; the
index (``${MEMORY_DATA_DIR}`` → ``$WORKSPACE_DIR/.index``) is a *persisted
cache* that rides inside the single workspace archive across sleep/wake. It is
not rebuilt on every boot — reindex is consolidated at the SIGTERM shutdown
handler (the one chokepoint every sleep funnels through). The index is
reconciled to the markdown by per-file SHA whenever a reindex does run.

Embeddings come from a vendored all-MiniLM-L6-v2 ONNX wrapper
(``app.embedder.MiniLMEmbedder``: onnxruntime + tokenizers, the *same* model
artifact chromadb shipped) — no torch / sentence-transformers, and chromadb
itself is no longer a dependency (~167 MB / 47 packages dropped). Vectors are
persisted ourselves as a compact int8 matrix (per-vector absmax quantization,
~4× smaller than float32, ~1/127 distance perturbation) queried by
brute-force squared-L2; there is no external vector DB.

Reindex is per-file SHA-mapped: each source file's hash is stored individually
in ``sync_state.json`` so a single daily-note append re-embeds one source's
chunks rather than the whole corpus. Chunk IDs are enumerated per-source
(``{source}:{i}``) so surviving chunk IDs stay stable across incremental
reindexes; the int8 store reuses prior rows **by id** and is rebuilt from the
same ``chunks``/``all_ids`` list as the BM25 corpus in one locked pass so the
two can never disagree on the id set.
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

import networkx as nx
import numpy as np
from rank_bm25 import BM25Okapi

from .embedder import MiniLMEmbedder

log = logging.getLogger("clawless.memory")

MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384  # all-MiniLM-L6-v2 output dimensionality
RRF_K = 60

# Calibrated against MiniLM-L6-v2 distance distributions: topical hits cluster
# at 1.41-1.44, weak/gibberish queries plateau at 1.50+.
VECTOR_DISTANCE_MAX = 1.50

EXCLUDED_SECTION_PATTERNS = re.compile(
    r"^(Auto-Retrieved Memory Context|Conversation Summary|Hybrid Search|Knowledge Graph)\b"
)

DAILY_NOTE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

ROOT_SOURCES = (
    "MEMORY.md", "SOUL.md", "USER.md",
)


class MemoryIndex:
    def __init__(self, source_dir: str, data_dir: str, slug_safe: str) -> None:
        self.source_dir = source_dir
        self.data_dir = data_dir
        self.slug_safe = slug_safe or "default"
        self.embedder: Any | None = None
        # In-memory handle on the persisted int8 store:
        # {"ids": list[str], "q": int8[N,384], "scale": float32[N]}.
        # None until warmup()/do_reindex populate it.
        self._store: dict[str, Any] | None = None
        # Lock prevents reindex from racing against concurrent retrieval reads
        # of the same sidecars (vstore.npz, bm25_corpus.json, memory_graph.json).
        self.lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------

    def warmup(self) -> None:
        log.info("loading embedding function (%s via ONNX)", MODEL_NAME)
        self.embedder = MiniLMEmbedder()
        os.makedirs(self.data_dir, exist_ok=True)
        self._store = self._load_store()
        if self._store is not None:
            log.info("vector store loaded: %d vectors from %s",
                     len(self._store["ids"]), self._store_path())
        else:
            log.info("vector store absent; will build on next reindex")

    async def warmup_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.warmup)

    # --- source collection --------------------------------------------------

    def _daily_notes(self) -> list[str]:
        return [
            p for p in sorted(globmod.glob(os.path.join(self.source_dir, "*.md")))
            if DAILY_NOTE_PATTERN.match(os.path.basename(p))
        ]

    def _source_paths(self) -> list[str]:
        # Both top-level docs (MEMORY.md, SOUL.md, etc.) and daily notes
        # (YYYY-MM-DD.md) live flat under source_dir (WORKSPACE_DIR/memory).
        # The basename pattern distinguishes them: ROOT_SOURCES are named
        # files; daily notes match DAILY_NOTE_PATTERN.
        paths: list[str] = []
        for name in ROOT_SOURCES:
            p = os.path.join(self.source_dir, name)
            if os.path.exists(p):
                paths.append(p)
        paths.extend(self._daily_notes())
        return paths

    def _source_key(self, path: str) -> str:
        """Stable key for a source path. Top-level docs use just the basename
        ('MEMORY.md'), daily notes are namespaced under 'memory/' to match
        the chunk metadata convention. The keys must match the strings
        written into chunk metadata (see _collect_sources) so the
        changed/removed source-key sets line up with chunk membership."""
        bn = os.path.basename(path)
        if bn in ROOT_SOURCES:
            return bn
        return f"memory/{bn}"

    def _compute_source_hashes(self) -> dict[str, str]:
        """Per-file SHA1 over current source bytes. Per-file (not global)
        so a single daily-note append doesn't invalidate every other file."""
        out: dict[str, str] = {}
        for path in self._source_paths():
            try:
                with open(path, "rb") as f:
                    out[self._source_key(path)] = hashlib.sha1(f.read()).hexdigest()
            except FileNotFoundError:
                pass
        return out

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

    # --- int8 vector store --------------------------------------------------
    #
    # Replaces chromadb's PersistentClient. Vectors are stored as a per-vector
    # absmax-quantized int8 matrix + float32 scales, parallel to an id list.
    # At our corpus scale (hundreds–low-thousands of chunks; the O(n^2) graph
    # build bounds n) brute-force squared-L2 in numpy is sub-millisecond and
    # has no HNSW to persist. ~4x smaller than float32, ~1/127 distance
    # perturbation — well inside the 1.41–1.50 VECTOR_DISTANCE_MAX band.

    def _store_path(self) -> str:
        return os.path.join(self.data_dir, "vstore.npz")

    @staticmethod
    def _quantize(mat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Per-row symmetric absmax int8. ``mat`` is float32 (M,384);
        returns ``(q int8 (M,384), scale float32 (M,))``."""
        mat = np.asarray(mat, dtype=np.float32)
        scale = np.maximum(np.abs(mat).max(axis=1), 1e-12) / 127.0
        q = np.clip(np.round(mat / scale[:, None]), -127, 127).astype(np.int8)
        return q, scale.astype(np.float32)

    @staticmethod
    def _dequantize(q: np.ndarray, scale: np.ndarray) -> np.ndarray:
        return q.astype(np.float32) * scale[:, None]

    def _load_store(self) -> dict[str, Any] | None:
        """Load the persisted int8 store, or None if missing/corrupt. None is
        treated as "no store" — the next reindex rebuilds it (mirrors the
        _load_prev_hashes None-means-rebuild contract). The .index dir is
        restored from the agent's own versioned S3 archive, the same trust
        boundary as the JSON sidecars, so allow_pickle for the str id array
        is acceptable."""
        path = self._store_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                npz = np.load(f, allow_pickle=True)
                ids = [str(s) for s in npz["ids"].tolist()]
                q = npz["q"]
                scale = npz["scale"]
            if len(ids) != q.shape[0] or len(ids) != scale.shape[0]:
                log.warning("vector store id/row mismatch; treating as absent")
                return None
            return {"ids": ids, "q": q, "scale": scale}
        except Exception:
            log.exception("vector store load failed; treating as absent")
            return None

    def _save_store(self, ids: list[str], q: np.ndarray,
                    scale: np.ndarray) -> None:
        """Atomic single-file write (.tmp + os.replace, like
        _atomic_write_json). Written into an explicit file object so np.savez
        cannot append its own .npz to the temp name."""
        tmp = f"{self._store_path()}.tmp"
        with open(tmp, "wb") as f:
            np.savez(f,
                     ids=np.array(ids, dtype=object),
                     q=np.asarray(q, dtype=np.int8),
                     scale=np.asarray(scale, dtype=np.float32))
        os.replace(tmp, self._store_path())

    def has_persisted_index(self) -> bool:
        """For main.py: is there a usable persisted index to trust on wake, or
        is this a true first boot (or a corrupt store) that must build
        synchronously? Keys off the store warmup() already loaded (so a
        corrupt/unreadable vstore.npz — which _load_store turns into None —
        forces a rebuild instead of a permanent BM25-only degradation) plus
        the sync_state commit token. Call after warmup(); cheap (no extra
        load, one small JSON read) — not an O(corpus) SHA scan."""
        return (self._store is not None
                and self._load_prev_hashes() is not None)

    def _load_prev_hashes(self) -> dict[str, str] | None:
        """Returns the per-file hash map from sync_state.json, or None if
        the state file is missing, malformed, or in the legacy single-
        ``sourcesHash`` format. Caller treats None as "everything changed"
        and triggers a full rebuild that upgrades the file."""
        state_path = os.path.join(self.data_dir, "sync_state.json")
        if not os.path.exists(state_path):
            return None
        try:
            with open(state_path) as f:
                state = json.load(f)
        except Exception:
            return None
        prev = state.get("sourceHashes")
        if not isinstance(prev, dict):
            return None
        return prev

    def needs_reindex(self) -> tuple[list[str], list[str]]:
        """Returns ``(changed, removed)`` source-key lists. Empty tuple
        ``([], [])`` means in-sync. A ``None`` ``prev_hashes`` (missing/legacy
        state) returns ``(all_current_sources, [])`` so the caller takes the
        full-rebuild path and upgrades the state file."""
        current = self._compute_source_hashes()
        if not current:
            return ([], [])
        prev = self._load_prev_hashes()
        if prev is None:
            return (sorted(current.keys()), [])
        changed = sorted(k for k, v in current.items() if prev.get(k) != v)
        removed = sorted(k for k in prev.keys() if k not in current)
        return (changed, removed)

    @staticmethod
    def _assign_chunk_ids(chunks: list[dict[str, Any]]) -> list[str]:
        """Per-source enumeration: ``{source}:{i}`` where i is local to that
        source. Stable across reindexes — adding a section to one file
        doesn't shift any other file's ids — which makes the incremental
        path's surviving-chunk ids match the freshly-built BM25 corpus ids."""
        ids: list[str] = []
        per_source_idx: dict[str, int] = {}
        for c in chunks:
            src = c["metadata"]["source"]
            i = per_source_idx.get(src, 0)
            ids.append(f"{src}:{i}")
            per_source_idx[src] = i + 1
        return ids

    def do_reindex(self, changed: list[str], removed: list[str]) -> dict[str, Any]:
        assert self.embedder is not None, "warmup() not called"
        chunks = self._collect_sources()
        if not chunks:
            return {"status": "skipped", "reason": "no sources"}

        bm25_path = os.path.join(self.data_dir, "bm25_corpus.json")
        graph_path = os.path.join(self.data_dir, "memory_graph.json")
        state_path = os.path.join(self.data_dir, "sync_state.json")

        current_sources = sorted({c["metadata"]["source"] for c in chunks})
        all_ids = self._assign_chunk_ids(chunks)
        prev_hashes = self._load_prev_hashes()
        # Full rebuild path: missing/legacy state, or every current source
        # is in the changed set with no surviving entries.
        full_rebuild = prev_hashes is None or (
            set(changed) == set(current_sources) and not removed
        )

        N = len(chunks)
        q_out = np.zeros((N, EMBED_DIM), dtype=np.int8)
        scale_out = np.zeros((N,), dtype=np.float32)

        if full_rebuild:
            log.info("full reindex: %d chunks from %d sources",
                     N, len(current_sources))
            vecs = np.asarray(
                self.embedder([c["content"] for c in chunks]), dtype=np.float32
            )
            q_out, scale_out = self._quantize(vecs)
        else:
            log.info("incremental reindex: +%d changed -%d removed",
                     len(changed), len(removed))
            prev = self._store or self._load_store() or {
                "ids": [],
                "q": np.zeros((0, EMBED_DIM), np.int8),
                "scale": np.zeros((0,), np.float32),
            }
            prev_by_id = {sid: i for i, sid in enumerate(prev["ids"])}
            changed_set = set(changed)
            # Reuse prior int8 rows by chunk-id (never by position — chunk
            # counts shift). Re-embed only chunks whose source changed (or,
            # defensively, an unchanged chunk missing from a partial prior
            # store). Removed sources' chunks are simply absent from `chunks`.
            embed_pos: list[int] = []
            embed_txt: list[str] = []
            for p, (cid, c) in enumerate(zip(all_ids, chunks)):
                if c["metadata"]["source"] not in changed_set and cid in prev_by_id:
                    j = prev_by_id[cid]
                    q_out[p] = prev["q"][j]
                    scale_out[p] = prev["scale"][j]
                else:
                    embed_pos.append(p)
                    embed_txt.append(c["content"])
            if embed_txt:
                vecs = np.asarray(
                    self.embedder(embed_txt), dtype=np.float32
                )
                q_new, s_new = self._quantize(vecs)
                for k, p in enumerate(embed_pos):
                    q_out[p] = q_new[k]
                    scale_out[p] = s_new[k]

        # Persist the int8 store FIRST. Write order is the commit protocol:
        # vstore -> bm25 -> graph -> sync_state.json (the commit token, last).
        # A crash between writes leaves sync_state.json stale, so the next
        # reindex's SHA reconciliation rebuilds — never "newer state, stale
        # vectors". Single-file .tmp+os.replace = one atomic unit.
        self._save_store(all_ids, q_out, scale_out)
        self._store = {"ids": all_ids, "q": q_out, "scale": scale_out}

        # BM25 corpus rebuilt from all current chunks every time — the SAME
        # chunks/all_ids list as the store above, in one locked pass, so the
        # store and the corpus can never disagree on the id set.
        corpus = [
            {"id": all_ids[i], "text": chunks[i]["content"],
             "section": chunks[i]["metadata"]["section"]}
            for i in range(len(chunks))
        ]
        self._atomic_write_json(bm25_path, corpus)

        # Graph also rebuilt fully — cross-file "mentions" edges scan the
        # global node set (see _build_graph), so any change can ripple.
        G = self._build_graph(chunks)
        self._atomic_write_json(graph_path, nx.node_link_data(G))

        state = {
            "agent_slug": self.slug_safe,
            "sourceHashes": self._compute_source_hashes(),
            "sources": current_sources,
            "vectorChunks": N,
            "vectorStore": "int8-absmax-v1",
            "vectorDim": EMBED_DIM,
            "graphNodes": G.number_of_nodes(),
            "graphEdges": G.number_of_edges(),
            "lastSync": datetime.now(timezone.utc).isoformat(),
            "status": "synced",
        }
        self._atomic_write_json(state_path, state)
        return {
            "status": "reindexed",
            "chunks": N,
            "graphNodes": G.number_of_nodes(),
            "changed": changed if not full_rebuild else current_sources,
            "removed": removed,
        }

    async def reindex_if_stale(self, force: bool = False) -> dict[str, Any]:
        async with self.lock:
            loop = asyncio.get_running_loop()
            if force:
                current = await loop.run_in_executor(
                    None, lambda: sorted(self._compute_source_hashes().keys())
                )
                return await loop.run_in_executor(
                    None, self.do_reindex, current, []
                )
            changed, removed = await loop.run_in_executor(None, self.needs_reindex)
            if not changed and not removed:
                return {"status": "in_sync"}
            return await loop.run_in_executor(None, self.do_reindex, changed, removed)

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

        store = self._store or self._load_store()
        if not store or not store["ids"] or self.embedder is None:
            vector_ranked: list[str] = []
        else:
            qv = np.asarray(self.embedder([query]), dtype=np.float32)[0]
            deq = self._dequantize(store["q"], store["scale"])
            # Full squared-L2, NOT the 2-2·dot shortcut: dequantized vectors
            # are no longer exactly unit-norm, and VECTOR_DISTANCE_MAX is
            # calibrated on squared-L2. Brute force is sub-ms at our scale.
            dist = ((deq - qv) ** 2).sum(axis=1)
            order = np.argsort(dist)[: min(n * 2, len(store["ids"]))]
            vector_ranked = [
                store["ids"][int(i)] for i in order
                if dist[i] < VECTOR_DISTANCE_MAX
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
            if self._compute_source_hashes() != state.get("sourceHashes", {}):
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
