"""Local text embeddings for meaning-based recall: all-MiniLM-L6-v2 (ONNX) on the CPU.

No server and no background service: the model loads inside JARVIS on first use and
frees itself after IDLE seconds without a call. It never downloads anything at run time
(the files were fetched once into the Hugging Face cache); when they or onnxruntime are
missing, embed() returns None and callers keep their keyword search.

    vectors = embed(["my thesis draft", "dissertation chapter 2"])   # 384-d, unit length
"""

import logging
import threading
import time

log = logging.getLogger("jarvis.embed")

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_TOKENS = 256
IDLE = 600  # seconds without a call before the model is freed
RETRY_MISSING = 600  # seconds before looking again for model files that were not there
RETRY_ERROR = 60     # ... after a transient failure (a locked file, a busy disk)

_lock = threading.Lock()
_state = {"session": None, "tokenizer": None, "last": 0.0, "timer": None, "missing": False,
          "retry_at": 0.0, "paths": None, "logged": None}


def _files():
    """Resolved (model, tokenizer) paths, looked up once while they stay on disk."""
    import os
    from huggingface_hub import hf_hub_download
    cached = _state.get("paths")  # (resolver, paths): a different resolver means a fresh lookup
    if cached and cached[0] is hf_hub_download and all(os.path.exists(p) for p in cached[1]):
        return cached[1]
    paths = (hf_hub_download(MODEL, "onnx/model.onnx", local_files_only=True),
             hf_hub_download(MODEL, "tokenizer.json", local_files_only=True))
    _state["paths"] = (hf_hub_download, paths)
    return paths


def _is_missing(exc):
    """Model files or onnxruntime absent (worth a slow retry) vs. a transient failure."""
    if isinstance(exc, (FileNotFoundError, ImportError)):
        return True
    return type(exc).__name__ in ("LocalEntryNotFoundError", "EntryNotFoundError",
                                  "OfflineModeIsEnabled")


def _fail(exc):
    """Turn semantic recall off for a cooldown, saying why once per distinct reason."""
    missing = _is_missing(exc)
    _state["missing"] = True
    _state["paths"] = None
    _state["session"] = _state["tokenizer"] = None
    _state["retry_at"] = time.monotonic() + (RETRY_MISSING if missing else RETRY_ERROR)
    reason = f"{type(exc).__name__}: {exc}"
    if _state.get("logged") != reason:
        _state["logged"] = reason
        log.warning("semantic recall off (%s): %s; retrying in %d s",
                    "model files missing" if missing else "embedding failed", reason,
                    RETRY_MISSING if missing else RETRY_ERROR)


def _load():
    import onnxruntime
    from tokenizers import Tokenizer
    model, tokenizer_file = _files()
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2  # stay light beside voice and the console
    options.enable_cpu_mem_arena = False  # indexing batches would pin their peak for good
    session = onnxruntime.InferenceSession(model, options, providers=["CPUExecutionProvider"])
    tokenizer = Tokenizer.from_file(tokenizer_file)
    tokenizer.enable_truncation(MAX_TOKENS)
    tokenizer.enable_padding()
    return session, tokenizer


def _unload_when_idle():
    with _lock:
        if time.monotonic() - _state["last"] >= IDLE:
            _state.update(session=None, tokenizer=None, timer=None)
            return
        timer = threading.Timer(IDLE, _unload_when_idle)
        timer.daemon = True
        _state["timer"] = timer
        timer.start()


def available():
    """True when the model files and onnxruntime are present (no download is attempted)."""
    if _state["missing"]:
        if time.monotonic() < _state.get("retry_at", 0.0):
            return False
        _state["missing"] = False  # cooldown over: look again
    try:
        import importlib.util
        import sys
        if ("onnxruntime" not in sys.modules  # no import here: it costs ~100 MB
                and importlib.util.find_spec("onnxruntime") is None):
            raise ImportError("onnxruntime")
        _files()
        return True
    except Exception as exc:
        _fail(exc)
        return False


def embed(texts):
    """Unit-length float32 vectors (numpy, shape [n, 384]) for `texts`, or None."""
    texts = [str(t or "") for t in texts]
    if not texts or not available():
        return None
    import numpy as np
    with _lock:
        try:
            if _state["session"] is None:
                _state["session"], _state["tokenizer"] = _load()
        except Exception as exc:
            _fail(exc)
            return None
        session, tokenizer = _state["session"], _state["tokenizer"]
        _state["last"] = time.monotonic()
        if _state["timer"] is None:
            timer = threading.Timer(IDLE, _unload_when_idle)
            timer.daemon = True
            _state["timer"] = timer
            timer.start()
    encoded = tokenizer.encode_batch(texts)
    ids = np.array([e.ids for e in encoded], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": mask}
    if any(i.name == "token_type_ids" for i in session.get_inputs()):
        feeds["token_type_ids"] = np.zeros_like(ids)
    hidden = session.run(None, feeds)[0]                        # [n, tokens, 384]
    weights = mask[..., None].astype(np.float32)
    pooled = (hidden * weights).sum(1) / np.clip(weights.sum(1), 1e-9, None)
    return (pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)).astype(np.float32)
