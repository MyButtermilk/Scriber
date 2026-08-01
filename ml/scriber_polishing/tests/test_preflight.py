from scriber_polishing.preflight import detect_forbidden_api_key_names, parse_nvidia_smi_csv


def test_preflight_reports_forbidden_key_names_without_values() -> None:
    environment = {
        "HF_TOKEN": "allowed-secret",
        "OPENAI_API_KEY": "must-not-leak",
        "ANTHROPIC_API_KEY": "",
        "AZURE_OPENAI_API_KEY": "also-must-not-leak",
    }

    assert detect_forbidden_api_key_names(environment) == (
        "AZURE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    )


def test_nvidia_smi_measurement_parses_actual_gpu_and_vram() -> None:
    row = "NVIDIA GeForce RTX 4070 Laptop GPU, 591.86, 13110, 8188, 7539, 410"

    assert parse_nvidia_smi_csv(row) == {
        "name": "NVIDIA GeForce RTX 4070 Laptop GPU",
        "driver_version": "591.86",
        "cuda_driver_version": "13110",
        "memory_total_mib": 8188,
        "memory_free_mib": 7539,
        "memory_used_mib": 410,
    }
