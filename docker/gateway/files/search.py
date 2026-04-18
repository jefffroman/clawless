import os, json, chromadb
from chromadb.utils import embedding_functions
from rank_bm25 import BM25Okapi

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
VECTOR_DB   = os.path.join(SCRIPT_DIR, "chroma_db")
BM25_CORPUS = os.path.join(SCRIPT_DIR, "bm25_corpus.json")

RRF_K         = 60
BM25_WEIGHT   = 1.0
VECTOR_WEIGHT = 1.0
N_RESULTS     = 5


def rrf_fuse(bm25_ranked, vector_ranked):
    scores = {}
    for rank, doc_id in enumerate(bm25_ranked):
        scores[doc_id] = scores.get(doc_id, 0) + BM25_WEIGHT / (RRF_K + rank + 1)
    for rank, doc_id in enumerate(vector_ranked):
        scores[doc_id] = scores.get(doc_id, 0) + VECTOR_WEIGHT / (RRF_K + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


def hybrid_search(query, n=N_RESULTS):
    with open(BM25_CORPUS) as f:
        corpus = json.load(f)
    tokenized   = [doc["text"].lower().split() for doc in corpus]
    bm25        = BM25Okapi(tokenized)
    bm25_scores = bm25.get_scores(query.lower().split())
    bm25_ranked = [corpus[i]["id"] for i in
                   sorted(range(len(bm25_scores)), key=lambda x: bm25_scores[x], reverse=True)]

    embedder      = embedding_functions.DefaultEmbeddingFunction()
    client        = chromadb.PersistentClient(path=VECTOR_DB)
    col           = client.get_collection("memory_chunks", embedding_function=embedder)
    results       = col.query(query_texts=[query], n_results=min(n * 2, col.count()),
                              include=["documents", "metadatas", "distances"])
    vector_ranked = [f"mem_{i}" for i in
                     sorted(range(len(results["ids"][0])),
                            key=lambda x: results["distances"][0][x])]

    fused     = rrf_fuse(bm25_ranked, vector_ranked)[:n]
    id_to_doc = {doc["id"]: doc for doc in corpus}
    return [id_to_doc[fid] for fid in fused if fid in id_to_doc]


if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "project status"
    for r in hybrid_search(query):
        print(f"[{r['section']}]\n  {r['text'][:200]}\n")
