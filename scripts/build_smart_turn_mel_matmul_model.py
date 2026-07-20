from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from pipecat.audio.turn.smart_turn import _whisper_features


MODEL_NAME = "smart-turn-mel-matmul.onnx"
MODEL_OPSET = 17
MODEL_IR_VERSION = 10


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_model() -> tuple[bytes, str]:
    matrix = np.ascontiguousarray(_whisper_features._MEL_FILTERS.T, dtype=np.float64)
    if matrix.shape != (80, 201):
        raise ValueError(f"Unexpected Pipecat mel-filter shape: {matrix.shape}")

    graph = helper.make_graph(
        [helper.make_node("MatMul", ["mel_filters", "magnitudes"], ["mel_spec"])],
        "scriber-smart-turn-mel-matmul",
        [helper.make_tensor_value_info("magnitudes", TensorProto.DOUBLE, [201, "frames"])],
        [helper.make_tensor_value_info("mel_spec", TensorProto.DOUBLE, [80, "frames"])],
        [numpy_helper.from_array(matrix, name="mel_filters")],
    )
    model = helper.make_model(
        graph,
        producer_name="scriber-smart-turn-mel-builder",
        opset_imports=[helper.make_opsetid("", MODEL_OPSET)],
    )
    model.ir_version = MODEL_IR_VERSION
    onnx.checker.check_model(model)
    return model.SerializeToString(deterministic=True), _sha256_bytes(matrix.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    model_bytes, matrix_sha256 = build_model()
    output = args.output.resolve()
    if args.check:
        if not output.is_file():
            raise FileNotFoundError(output)
        if output.read_bytes() != model_bytes:
            raise RuntimeError(f"Generated model differs from {output.name}")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        temporary.write_bytes(model_bytes)
        temporary.replace(output)

    print(
        json.dumps(
            {
                "model": output.name,
                "modelBytes": len(model_bytes),
                "modelSha256": _sha256_bytes(model_bytes),
                "melFiltersSha256": matrix_sha256,
                "onnxVersion": onnx.__version__,
                "numpyVersion": np.__version__,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
