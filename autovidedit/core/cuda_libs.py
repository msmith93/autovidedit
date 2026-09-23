"""Make pip-installed NVIDIA libraries visible to CTranslate2 (faster-whisper).

CTranslate2 dlopens libcublas.so.12 and libcudnn*.so.9 by name. The
nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels put them under
site-packages/nvidia/*/lib, which is not on the loader path. torch used to
preload them as a side effect; we do it explicitly so torch isn't needed.
"""

import ctypes
import importlib.util
from pathlib import Path

# Load order matters: cublasLt before cublas, core cudnn after its sub-libs'
# dependencies are resolvable. RTLD_GLOBAL makes later dlopen-by-name calls
# resolve to these already-loaded copies.
_LIBS = [
    ("nvidia.cublas", ["libcublasLt.so.12", "libcublas.so.12"]),
    ("nvidia.cudnn", [
        "libcudnn_graph.so.9", "libcudnn_engines_precompiled.so.9",
        "libcudnn_engines_runtime_compiled.so.9", "libcudnn_heuristic.so.9",
        "libcudnn_ops.so.9", "libcudnn_cnn.so.9", "libcudnn_adv.so.9",
        "libcudnn.so.9",
    ]),
]

_done = False


def preload_cuda_libs() -> bool:
    """Best-effort preload. Returns True if every library was loaded."""
    global _done
    if _done:
        return True
    ok = True
    for package, names in _LIBS:
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            ok = False
            continue
        lib_dir = Path(list(spec.submodule_search_locations)[0]) / "lib"
        for name in names:
            path = lib_dir / name
            if not path.exists():
                ok = False
                continue
            try:
                ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                ok = False
    _done = ok
    return ok
