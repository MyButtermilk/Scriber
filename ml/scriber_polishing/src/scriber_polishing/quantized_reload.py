"""Fresh-process, saved-config-only reload probe for bitsandbytes artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from .policy_artifact import verify_frozen_lexical_policy_artifact
from .sst_parser import parse_sst
from .tokenize import POLISHING_INSTRUCTION


def _write_exclusive(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def run_probe(artifact: Path, variant: str) -> dict[str, object]:
    """Reload without a caller-supplied quantization config and generate twice."""

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        artifact,
        local_files_only=True,
        trust_remote_code=False,
        fix_mistral_regex=False,
    )
    model = AutoModelForCausalLM.from_pretrained(
        artifact,
        local_files_only=True,
        trust_remote_code=False,
        device_map="auto",
        dtype="auto",
        low_cpu_mem_usage=True,
        attn_implementation="sdpa",
    )
    if variant == "bf16":
        floating_dtypes = {
            parameter.dtype
            for parameter in model.parameters()
            if bool(getattr(parameter, "is_floating_point", lambda: False)())
        }
        if floating_dtypes != {torch.bfloat16}:
            raise RuntimeError("reloaded BF16 model contains a non-BF16 floating parameter dtype")
    else:
        expected_flag = "is_loaded_in_8bit" if variant == "int8" else "is_loaded_in_4bit"
        if not bool(getattr(model, expected_flag, False)):
            raise RuntimeError(f"reloaded model does not report expected {variant} quantization")
    messages = [
        {
            "role": "user",
            "content": f"{POLISHING_INSTRUCTION}Hallo, das ist ein Test.",
        }
    ]
    encoded = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )
    device = getattr(model, "device", None)
    if device is not None and hasattr(encoded, "to"):
        encoded = encoded.to(device)
    generation = {
        "do_sample": False,
        "num_beams": 1,
        "max_new_tokens": 128,
        "pad_token_id": tokenizer.eos_token_id,
    }
    with torch.inference_mode():
        first = model.generate(**dict(encoded), **generation)
        second = model.generate(**dict(encoded), **generation)
    if not bool(torch.equal(first, second)):
        raise RuntimeError("freshly reloaded quantized model is not deterministic")
    prompt_tokens = int(encoded["input_ids"].shape[-1])
    completion_ids = first[0, prompt_tokens:]
    generated_tokens = int(completion_ids.shape[-1])
    if generated_tokens < 1:
        raise RuntimeError("freshly reloaded quantized model generated no completion tokens")
    completion = tokenizer.decode(completion_ids, skip_special_tokens=True).strip()
    if not completion:
        raise RuntimeError("freshly reloaded quantized model generated an empty completion")
    parse_sst(completion)
    protection_policy = verify_frozen_lexical_policy_artifact(
        artifact,
        tokenizer=tokenizer,
    )
    return {
        "status": "passed",
        "process_boundary": "fresh_subprocess",
        "subprocess_pid": os.getpid(),
        "configuration_source": "saved_artifact",
        "caller_quantization_config": False,
        "loaded_quantization": variant,
        "deterministic": True,
        "valid_sst": True,
        "generated_tokens": generated_tokens,
        "output_sha256": "sha256:" + hashlib.sha256(completion.encode("utf-8")).hexdigest(),
        "protection_policy": protection_policy,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument(
        "--variant",
        choices=("bf16", "int8", "int4_nf4"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite reload evidence: {args.output}")
    report = run_probe(args.artifact.resolve(), args.variant)
    _write_exclusive(args.output.resolve(), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
