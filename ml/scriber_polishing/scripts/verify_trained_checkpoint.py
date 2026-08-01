"""Load a saved Scriber polishing checkpoint in a fresh process and generate SST."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.metrics import EvaluationCase, evaluate_predictions  # noqa: E402
from scriber_polishing.protected_spans import protect_spans, restore_spans  # noqa: E402
from scriber_polishing.sst_parser import SSTParseError, parse_sst  # noqa: E402
from scriber_polishing.tokenize import POLISHING_INSTRUCTION  # noqa: E402
from scriber_polishing.training_runtime import load_pair_records  # noqa: E402


def _write_report(report: dict[str, object], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--test", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cases", type=int, default=1)
    parser.add_argument("--case-index", type=int, default=0)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    args = parser.parse_args()
    if args.cases <= 0:
        raise ValueError("--cases must be positive")
    if args.case_index < 0:
        raise ValueError("--case-index must not be negative")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; refusing a silent CPU verification")
    checkpoint = args.checkpoint.resolve()
    if not checkpoint.is_dir():
        raise ValueError(f"checkpoint directory does not exist: {checkpoint}")

    load_started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(
        checkpoint,
        local_files_only=True,
        fix_mistral_regex=False,
    )
    model = AutoModelForCausalLM.from_pretrained(
        checkpoint,
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        local_files_only=True,
    ).to("cuda")
    model.eval()
    model.generation_config.top_p = None
    model.generation_config.top_k = None
    load_seconds = time.perf_counter() - load_started
    torch.cuda.reset_peak_memory_stats()

    loaded_records = load_pair_records(args.test, limit=args.case_index + args.cases)
    records = loaded_records[args.case_index : args.case_index + args.cases]
    if len(records) != args.cases:
        raise ValueError("test file has too few records for the requested case range")
    cases: list[EvaluationCase] = []
    evidence: list[dict[str, object]] = []
    generation_started = time.perf_counter()
    for record in records:
        protected = protect_spans(record["source_text"])
        messages = [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{protected.text}"}]
        inputs = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        ).to("cuda")
        with torch.inference_mode():
            generated = model.generate(
                **inputs,
                do_sample=False,
                num_beams=1,
                max_new_tokens=args.max_new_tokens,
                pad_token_id=tokenizer.eos_token_id,
            )
        completion_tokens = generated[0, inputs["input_ids"].shape[-1] :]
        completion = tokenizer.decode(completion_tokens, skip_special_tokens=True)
        restoration_error: str | None = None
        try:
            completion = restore_spans(protected, completion)
        except ValueError as error:
            restoration_error = str(error)
        parse_error: str | None = None
        try:
            parse_sst(completion)
        except SSTParseError as error:
            parse_error = str(error)
        protected_values = tuple(span.value for span in protected.spans)
        cases.append(
            EvaluationCase(
                case_id=record["example_id"],
                source_text=record["source_text"],
                target_sst=record["target_sst"],
                prediction_sst=completion,
                protected_values=protected_values,
            )
        )
        evidence.append(
            {
                "case_id": record["example_id"],
                "input_sha256": hashlib.sha256(record["source_text"].encode()).hexdigest(),
                "target_sha256": hashlib.sha256(record["target_sst"].encode()).hexdigest(),
                "prediction_sha256": hashlib.sha256(completion.encode()).hexdigest(),
                "input_tokens": int(inputs["input_ids"].shape[-1]),
                "generated_tokens": int(completion_tokens.shape[-1]),
                "sst_valid": parse_error is None and restoration_error is None,
                "parse_error": parse_error,
                "restoration_error": restoration_error,
            }
        )
    torch.cuda.synchronize()
    generation_seconds = time.perf_counter() - generation_started
    metrics = evaluate_predictions(cases)
    report: dict[str, object] = {
        "schema_version": 1,
        "checkpoint": str(checkpoint),
        "fresh_process": True,
        "gpu": torch.cuda.get_device_name(0),
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "dtype": "bfloat16",
        "attn_implementation": "sdpa",
        "cases": len(cases),
        "load_seconds": load_seconds,
        "generation_seconds": generation_seconds,
        "peak_vram_mib": round(torch.cuda.max_memory_allocated() / 1024**2),
        "metrics": asdict(metrics),
        "case_evidence": evidence,
    }
    _write_report(report, args.report)
    if not all(item["sst_valid"] for item in evidence):
        raise RuntimeError("fresh-process checkpoint generation did not produce valid SST")
    print(
        f"checkpoint_verification=pass cases={len(cases)} "
        f"parse_rate={metrics.sst_parse_rate:.4f} peak_vram_mib={report['peak_vram_mib']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
