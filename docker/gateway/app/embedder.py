"""all-MiniLM-L6-v2 ONNX embedder — vendored, chromadb-free.

This replaces `chromadb.utils.embedding_functions.DefaultEmbeddingFunction`
(`ONNXMiniLM_L6_V2`). It pulls the **same** model artifact chromadb uses (same
URL + pinned SHA256) and reproduces its tokenization / mean-pooling /
L2-normalization byte-for-byte, so the calibrated `VECTOR_DISTANCE_MAX`
threshold and recall are unchanged — while dropping the entire chromadb
dependency (~167 MB / 47 packages: grpc, kubernetes, pydantic, uvicorn,
opentelemetry, …). Only deps: onnxruntime + tokenizers + numpy (the
irreducible floor for ONNX MiniLM embeddings).

Algorithm and constants are derived from chroma-core/chromadb's
`onnx_mini_lm_l6_v2.py` (Apache-2.0). Differences are dependency-only: the
download uses stdlib `urllib` (not httpx), there is no tqdm progress bar, and
the model cache defaults to an image-baked path outside the workspace so
`restore()` can never wipe it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tarfile
import time
import urllib.request
from functools import cached_property
from typing import Any

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

log = logging.getLogger("clawless.embedder")

_MODEL_URL = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"
_MODEL_SHA256 = "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"
_ARCHIVE = "onnx.tar.gz"
_EXTRACTED = "onnx"
_ONNX_FILES = (
    "config.json", "model.onnx", "special_tokens_map.json",
    "tokenizer_config.json", "tokenizer.json", "vocab.txt",
)
# Image-baked by the Dockerfile, outside $WORKSPACE_DIR so the workspace
# restore (which clears WORKSPACE_DIR children) can never delete it.
_DEFAULT_MODEL_DIR = "/opt/clawless/models/all-MiniLM-L6-v2"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


class MiniLMEmbedder:
    """Callable: ``embedder([texts]) -> np.ndarray(M, 384) float32``,
    L2-normalized. Drop-in for the prior chromadb DefaultEmbeddingFunction
    (memory.py wraps the result in ``np.asarray``)."""

    def __init__(self, model_dir: str | None = None) -> None:
        self.model_dir = (
            model_dir
            or os.environ.get("CLAWLESS_MODEL_DIR", "").strip()
            or _DEFAULT_MODEL_DIR
        )

    # --- model artifact ----------------------------------------------------

    def _onnx_dir(self) -> str:
        return os.path.join(self.model_dir, _EXTRACTED)

    def _ensure_model(self) -> None:
        onnx_dir = self._onnx_dir()
        if all(os.path.exists(os.path.join(onnx_dir, f)) for f in _ONNX_FILES):
            return
        os.makedirs(self.model_dir, exist_ok=True)
        archive = os.path.join(self.model_dir, _ARCHIVE)
        if not os.path.exists(archive) or _sha256(archive) != _MODEL_SHA256:
            self._download(archive)
        with tarfile.open(name=archive, mode="r:gz") as tar:
            if sys.version_info >= (3, 12):
                tar.extractall(path=self.model_dir, filter="data")
            else:
                tar.extractall(path=self.model_dir)

    def _download(self, dest: str) -> None:
        last: Exception | None = None
        for attempt in range(3):
            try:
                log.info("downloading MiniLM ONNX model (attempt %d)", attempt + 1)
                tmp = f"{dest}.tmp"
                with urllib.request.urlopen(_MODEL_URL) as resp, open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                if _sha256(tmp) != _MODEL_SHA256:
                    os.remove(tmp)
                    raise ValueError("model archive SHA256 mismatch")
                os.replace(tmp, dest)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("model download failed: %s", e)
                time.sleep(1 + attempt)
        raise RuntimeError(f"could not download MiniLM model: {last}")

    # --- tokenizer + session (lazy, cached) --------------------------------

    @cached_property
    def tokenizer(self) -> Any:
        tok = Tokenizer.from_file(os.path.join(self._onnx_dir(), "tokenizer.json"))
        # sentence-transformers uses 256 even though the HF config says 128.
        tok.enable_truncation(max_length=256)
        tok.enable_padding(pad_id=0, pad_token="[PAD]", length=256)
        return tok

    @cached_property
    def session(self) -> ort.InferenceSession:
        providers = list(ort.get_available_providers())
        if "CoreMLExecutionProvider" in providers:
            providers.remove("CoreMLExecutionProvider")
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            os.path.join(self._onnx_dir(), "model.onnx"),
            providers=providers,
            sess_options=so,
        )

    # --- forward -----------------------------------------------------------

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(v, axis=1)
        norm[norm == 0] = 1e-12
        return v / norm[:, np.newaxis]

    def _forward(self, documents: list[str], batch_size: int = 32) -> np.ndarray:
        out = []
        for i in range(0, len(documents), batch_size):
            batch = documents[i : i + batch_size]
            encoded = [self.tokenizer.encode(d) for d in batch]
            input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
            attention_mask = np.array(
                [e.attention_mask for e in encoded], dtype=np.int64
            )
            model_output = self.session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": np.zeros_like(input_ids),
                },
            )
            last_hidden_state = model_output[0]
            mask = np.broadcast_to(
                np.expand_dims(attention_mask, -1), last_hidden_state.shape
            )
            emb = np.sum(last_hidden_state * mask, 1) / np.clip(
                mask.sum(1), a_min=1e-9, a_max=None
            )
            out.append(self._normalize(emb).astype(np.float32))
        return np.concatenate(out)

    def __call__(self, input: list[str]) -> np.ndarray:
        self._ensure_model()
        return self._forward(list(input))
