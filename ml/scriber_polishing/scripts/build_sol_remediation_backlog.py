"""Create a leakage-safe remediation backlog from blinded Sol verdicts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        nargs=5,
        action="append",
        required=True,
        metavar=("NAME", "VERDICTS", "BUNDLE", "MAPPING", "MANIFEST"),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise ValueError(f"refusing to overwrite {args.output_dir}")

    grouped: dict[tuple[str, str], dict[str, object]] = {}
    source_index: dict[tuple[str, str], set[str]] = defaultdict(set)
    source_ids: dict[tuple[str, str], str] = {}
    input_bindings = []
    package_results = []
    for name, verdict_path, bundle_path, mapping_path, manifest_path in args.package:
        verdict_file, bundle_file = Path(verdict_path), Path(bundle_path)
        mapping_file, manifest_file = Path(mapping_path), Path(manifest_path)
        verdicts = _load_jsonl(verdict_file)
        bundle = {row["case_id"]: row for row in _load_jsonl(bundle_file)}
        mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        actual = hashlib.sha256(verdict_file.read_bytes()).hexdigest()
        if manifest.get("verdict_output_sha256") != actual:
            raise ValueError(f"verdict manifest hash mismatch for {name}")
        cases = mapping["cases"]
        package_base_ids: set[str] = set()
        fine_tune_preferred_count = 0
        baseline_preferred_count = 0
        selected_fine_tune_acceptable_count = 0
        selected_fine_tune_unacceptable_count = 0
        selected_fine_tune_critical_count = 0
        selected_fine_tune_flag_counts: Counter[str] = Counter()
        for verdict in verdicts:
            public_id = verdict["case_id"]
            private = cases[public_id]
            case = bundle[public_id]
            base_id = private["base_case_id"]
            package_base_ids.add(base_id)
            key = (name, base_id)
            source_ids[key] = base_id
            source_index[key].add(public_id)
            chosen = private["candidate_a_label"] if verdict["candidate"] == "A" else private["candidate_b_label"]
            if chosen == "final":
                fine_tune_preferred_count += 1
                selected_fine_tune_acceptable_count += int(verdict["acceptable"])
                selected_fine_tune_unacceptable_count += int(not verdict["acceptable"])
                selected_fine_tune_critical_count += int(verdict["critical_error"])
                selected_fine_tune_flag_counts.update(verdict["flags"])
            else:
                baseline_preferred_count += 1
            ft_side = "A" if private["candidate_a_label"] == "final" else "B"
            ft_flags = set(case["deterministic_critical_flags"][ft_side])
            attributed_flags = set(ft_flags)
            if chosen == "final":
                attributed_flags.update(verdict["flags"])
            signals = set()
            if chosen != "final":
                signals.add("pairwise_preference_loss")
            if chosen == "final" and not verdict["acceptable"]:
                signals.add("selected_fine_tune_unacceptable")
            if chosen == "final" and verdict["critical_error"]:
                signals.add("selected_fine_tune_critical")
            if ft_flags:
                signals.add("deterministic_fine_tune_error")
            if not signals:
                continue
            item = grouped.setdefault(
                key,
                {
                    "schema_version": 1,
                    "package": name,
                    "remediation_case_key": _sha(name + "\0" + base_id),
                    "failure_signals": set(),
                    "error_flags": set(),
                    "public_position_variants": 0,
                    "fine_tune_selected_count": 0,
                    "fine_tune_rejected_count": 0,
                    "fine_tune_critical_count": 0,
                    "pairwise_loss_count": 0,
                    "minimum_selected_fine_tune_scores": {},
                    "future_training_policy": "generate_novel_sibling_cases_only",
                    "direct_test_case_training_use_allowed": False,
                },
            )
            item["failure_signals"].update(signals)
            item["error_flags"].update(attributed_flags)
            item["public_position_variants"] += 1
            if chosen == "final":
                item["fine_tune_selected_count"] += 1
                item["fine_tune_rejected_count"] += int(not verdict["acceptable"])
                item["fine_tune_critical_count"] += int(verdict["critical_error"])
                minima = item["minimum_selected_fine_tune_scores"]
                for score, value in verdict["scores"].items():
                    minima[score] = min(minima.get(score, value), value)
            else:
                item["pairwise_loss_count"] += 1
        input_bindings.append(
            {
                "package": name,
                "verdict_count": len(verdicts),
                "verdict_sha256": "sha256:" + actual,
                "bundle_sha256": "sha256:" + hashlib.sha256(bundle_file.read_bytes()).hexdigest(),
                "private_mapping_sha256": "sha256:" + hashlib.sha256(mapping_file.read_bytes()).hexdigest(),
            }
        )
        package_results.append(
            {
                "package": name,
                "public_verdict_count": len(verdicts),
                "base_case_count": len(package_base_ids),
                "fine_tune_preferred_count": fine_tune_preferred_count,
                "fine_tune_preferred_rate": fine_tune_preferred_count / len(verdicts),
                "baseline_preferred_count": baseline_preferred_count,
                "baseline_preferred_rate": baseline_preferred_count / len(verdicts),
                "selected_fine_tune_acceptable_count": selected_fine_tune_acceptable_count,
                "selected_fine_tune_unacceptable_count": selected_fine_tune_unacceptable_count,
                "selected_fine_tune_critical_count": selected_fine_tune_critical_count,
                "selected_fine_tune_flag_counts": dict(
                    sorted(
                        selected_fine_tune_flag_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ),
            }
        )

    records = []
    for key in sorted(grouped):
        item = grouped[key]
        item["failure_signals"] = sorted(item["failure_signals"])
        item["error_flags"] = sorted(item["error_flags"])
        item["minimum_selected_fine_tune_scores"] = dict(sorted(item["minimum_selected_fine_tune_scores"].items()))
        records.append(item)
    flag_counts = Counter(flag for item in records for flag in item["error_flags"])
    signal_counts = Counter(signal for item in records for signal in item["failure_signals"])
    output = args.output_dir
    output.mkdir(parents=True)
    backlog_payload = b"".join(_canonical(item) for item in records)
    (output / "sol-remediation-backlog.jsonl").write_bytes(backlog_payload)
    private_rows = []
    for key in sorted(grouped):
        private_rows.append(
            {
                "schema_version": 1,
                "package": key[0],
                "remediation_case_key": grouped[key]["remediation_case_key"],
                "source_evaluation_case_id": source_ids[key],
                "public_judge_case_ids": sorted(source_index[key]),
                "classification": "private_evaluation_index_do_not_train",
            }
        )
    private_payload = b"".join(_canonical(item) for item in private_rows)
    (output / "sol-remediation-private-index.jsonl").write_bytes(private_payload)
    summary = {
        "schema_version": 1,
        "kind": "scriber_sol_remediation_backlog",
        "unique_error_case_count": len(records),
        "flag_counts": dict(sorted(flag_counts.items(), key=lambda item: (-item[1], item[0]))),
        "failure_signal_counts": dict(sorted(signal_counts.items(), key=lambda item: (-item[1], item[0]))),
        "inputs": input_bindings,
        "diagnostic_results": {
            "status": "complete_diagnostic_only",
            "release_evidence": False,
            "public_verdict_count": sum(item["public_verdict_count"] for item in package_results),
            "base_case_count": sum(item["base_case_count"] for item in package_results),
            "packages": package_results,
        },
        "backlog_sha256": "sha256:" + hashlib.sha256(backlog_payload).hexdigest(),
        "private_index_sha256": "sha256:" + hashlib.sha256(private_payload).hexdigest(),
        "new_training_examples_created": 0,
        "policy": {
            "direct_test_or_challenge_reuse": "prohibited",
            "allowed_followup": "derive generator requirements and create independently seeded novel siblings after this goal",
        },
    }
    (output / "sol-remediation-summary.json").write_bytes(_canonical(summary))
    top = list(summary["flag_counts"].items())[:15]
    lines = [
        "# Sol-Fehlerbacklog für eine spätere Verbesserungsrunde",
        "",
        f"Dokumentierte eindeutige Fehlerfälle: **{len(records)}**.",
        "Es wurden keine Testtexte und keine neuen Trainingsbeispiele übernommen.",
        "Die Fall-IDs liegen getrennt im privaten Index und dürfen nicht direkt trainiert werden.",
        "",
        "## Häufigste Fehlerklassen",
        "",
        *[f"- `{flag}`: {count} Fälle" for flag, count in top],
        "",
        "## Zulässige spätere Nutzung",
        "",
        "- Aus den Fehlerklassen neue Generatoranforderungen ableiten.",
        "- Mit neuen Seeds unabhängige Schwesterfälle erzeugen.",
        "- Neue Fälle vor Training gegen Test/Challenge deduplizieren.",
        "- Die ursprünglichen Test-/Challenge-Fälle ausschließlich zur erneuten Evaluation verwenden.",
    ]
    (output / "SOL_REMEDIATION_BACKLOG.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
