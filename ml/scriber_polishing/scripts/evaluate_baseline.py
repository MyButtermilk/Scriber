from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.corruption_engine import CorruptionSeverity, corrupt_text  # noqa: E402
from scriber_polishing.metrics import EvaluationCase, evaluate_predictions  # noqa: E402
from scriber_polishing.model_factory import BASE_MODEL_ID  # noqa: E402
from scriber_polishing.protected_spans import protect_spans, restore_spans  # noqa: E402
from scriber_polishing.semantic_generator import generate_example  # noqa: E402
from scriber_polishing.tokenize import POLISHING_INSTRUCTION  # noqa: E402

BASE_REVISION = "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3"


def _write(report: dict[str, object], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the untouched Gemma base before training.")
    parser.add_argument("--revision", default=BASE_REVISION)
    parser.add_argument("--cases", type=int, default=20)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "artifacts" / "baseline_evaluation.json")
    args = parser.parse_args()
    if args.cases < 5:
        raise ValueError("baseline evaluation requires at least five cases")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required; refusing a silent CPU baseline")

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_ID, revision=args.revision, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL_ID,
        revision=args.revision,
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        local_files_only=True,
    ).to("cuda")
    model.eval()
    model.generation_config.top_p = None
    model.generation_config.top_k = None
    torch.cuda.reset_peak_memory_stats()

    severities = (
        CorruptionSeverity.IDENTITY,
        CorruptionSeverity.LIGHT,
        CorruptionSeverity.MEDIUM,
        CorruptionSeverity.HEAVY,
        CorruptionSeverity.CHALLENGE,
    )
    cases: list[EvaluationCase] = []
    case_evidence: list[dict[str, object]] = []
    started = time.perf_counter()
    for index in range(args.cases):
        seed = index + 1
        canonical = generate_example(seed)
        severity = severities[index % len(severities)]
        corrupted = corrupt_text(canonical.target_plain_text, severity, seed=270_100 + seed)
        protected = protect_spans(corrupted.text)
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
        with suppress(ValueError):
            completion = restore_spans(protected, completion)
        protected_values = tuple(span.value for span in protected.spans)
        cases.append(
            EvaluationCase(
                case_id=f"baseline-{seed:04d}",
                source_text=corrupted.text,
                target_sst=canonical.target_sst,
                prediction_sst=completion,
                protected_values=protected_values,
            )
        )
        case_evidence.append(
            {
                "case_id": f"baseline-{seed:04d}",
                "seed": seed,
                "severity": severity.value,
                "input_sha256": hashlib.sha256(corrupted.text.encode()).hexdigest(),
                "target_sha256": hashlib.sha256(canonical.target_sst.encode()).hexdigest(),
                "prediction_sha256": hashlib.sha256(completion.encode()).hexdigest(),
                "input_tokens": int(inputs["input_ids"].shape[-1]),
                "generated_tokens": int(completion_tokens.shape[-1]),
            }
        )
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    metrics = evaluate_predictions(cases)
    report: dict[str, object] = {
        "schema_version": 1,
        "candidate": "unmodified_base_model",
        "base_model": BASE_MODEL_ID,
        "base_revision": args.revision,
        "gpu": torch.cuda.get_device_name(0),
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "cases": args.cases,
        "elapsed_seconds": elapsed,
        "mean_case_seconds": elapsed / args.cases,
        "peak_vram_mib": round(torch.cuda.max_memory_allocated() / 1024**2),
        "metrics": asdict(metrics),
        "case_evidence": case_evidence,
        "synthetic_input_only": True,
        "training_started": False,
    }
    _write(report, args.output)
    print(
        f"baseline=complete cases={args.cases} parse_rate={metrics.sst_parse_rate:.4f} "
        f"exact={metrics.normalized_exact_match:.4f} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
