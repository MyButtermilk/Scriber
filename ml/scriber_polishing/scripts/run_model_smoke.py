from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.model_factory import BASE_MODEL_ID  # noqa: E402
from scriber_polishing.sst_parser import SSTParseError, parse_sst  # noqa: E402
from scriber_polishing.tokenize import POLISHING_INSTRUCTION, tokenize_example  # noqa: E402

BASE_REVISION = "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3"
SOURCE = "sehr geehrte frau huber komma neue zeile bitte prüfen sie die unterlagen bis freitag punkt"
TARGET = (
    "[DOC]\n"
    "[SALUTATION]Sehr geehrte Frau Huber,[/SALUTATION]\n"
    "[P]Bitte prüfen Sie die Unterlagen bis Freitag.[/P]\n"
    "[/DOC]"
)


def _write_report(report: dict[str, object], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Gemma forward, backward, reload, and generation smoke.")
    parser.add_argument("--revision", default=BASE_REVISION)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "artifacts" / "model_smoke.json")
    parser.add_argument("--local-files-only", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; refusing a silent CPU fallback")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("BF16 is required for the primary smoke")

    torch.manual_seed(270_003)
    torch.cuda.manual_seed_all(270_003)
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(
        BASE_MODEL_ID,
        revision=args.revision,
        local_files_only=args.local_files_only,
    )
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL_ID,
        revision=args.revision,
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        local_files_only=args.local_files_only,
    ).to("cuda")
    torch.cuda.synchronize()
    load_seconds = time.perf_counter() - started

    encoded = tokenize_example(tokenizer, transcript=SOURCE, completion=TARGET, max_length=512)
    batch = {key: torch.tensor([value], device="cuda") for key, value in encoded.items()}
    model.train()
    forward_started = time.perf_counter()
    output = model(**batch)
    loss = output.loss
    if loss is None or not torch.isfinite(loss):
        raise RuntimeError("forward loss is not finite")
    torch.cuda.synchronize()
    forward_seconds = time.perf_counter() - forward_started

    backward_started = time.perf_counter()
    loss.backward()
    torch.cuda.synchronize()
    backward_seconds = time.perf_counter() - backward_started
    gradient_norm = math.sqrt(
        sum(
            float(parameter.grad.detach().float().pow(2).sum().item())
            for parameter in model.parameters()
            if parameter.grad is not None
        )
    )
    if not math.isfinite(gradient_norm) or gradient_norm <= 0:
        raise RuntimeError("backward gradients are not finite and nonzero")
    model.zero_grad(set_to_none=True)

    messages = [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{SOURCE}"}]
    generation_inputs = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    ).to("cuda")
    model.eval()
    generation_started = time.perf_counter()
    with torch.inference_mode():
        generated = model.generate(
            **generation_inputs,
            do_sample=False,
            num_beams=1,
            max_new_tokens=64,
        )
    torch.cuda.synchronize()
    generation_seconds = time.perf_counter() - generation_started
    completion_tokens = generated[0, generation_inputs["input_ids"].shape[-1] :]
    completion = tokenizer.decode(completion_tokens, skip_special_tokens=True)
    try:
        parse_sst(completion)
        parseable = True
    except (SSTParseError, ValueError):
        parseable = False

    report: dict[str, object] = {
        "schema_version": 1,
        "base_model": BASE_MODEL_ID,
        "base_revision": args.revision,
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0),
        "bf16_supported": torch.cuda.is_bf16_supported(),
        "sdpa": True,
        "load_seconds": load_seconds,
        "forward_seconds": forward_seconds,
        "backward_seconds": backward_seconds,
        "generation_seconds": generation_seconds,
        "loss": float(loss.detach().float().item()),
        "gradient_norm": gradient_norm,
        "peak_vram_mib": round(torch.cuda.max_memory_allocated() / 1024**2),
        "input_tokens": int(generation_inputs["input_ids"].shape[-1]),
        "generated_tokens": int(completion_tokens.shape[-1]),
        "baseline_sst_parseable": parseable,
        "baseline_output_sha256": hashlib.sha256(completion.encode()).hexdigest(),
        "synthetic_input_only": True,
    }
    _write_report(report, args.output)
    print(f"model_smoke=pass loss={report['loss']:.6f} peak_vram_mib={report['peak_vram_mib']} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
