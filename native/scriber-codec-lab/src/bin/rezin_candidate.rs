use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

const ALLOWED_DURATIONS_SECONDS: [u32; 4] = [5, 15, 30, 60];
const ALLOWED_SAMPLE_RATES_HZ: [u32; 2] = [16_000, 48_000];

struct Args {
    input_wav: PathBuf,
    output_flac: PathBuf,
    attestation: PathBuf,
    sample_rate_hz: u32,
    duration_seconds: u32,
    iteration: u32,
    build_variant: String,
}

fn parse_args() -> Result<Args, String> {
    let mut values = BTreeMap::new();
    let mut args = env::args().skip(1);
    while let Some(flag) = args.next() {
        if !flag.starts_with("--") {
            return Err(format!("unexpected positional argument: {flag}"));
        }
        let value = args
            .next()
            .ok_or_else(|| format!("missing value for {flag}"))?;
        if values.insert(flag.clone(), value).is_some() {
            return Err(format!("duplicate argument: {flag}"));
        }
    }
    let required = |name: &str| {
        values
            .get(name)
            .cloned()
            .ok_or_else(|| format!("missing required argument: {name}"))
    };
    let sample_rate_hz = required("--sample-rate-hz")?
        .parse::<u32>()
        .map_err(|_| "--sample-rate-hz must be an integer".to_owned())?;
    let duration_seconds = required("--duration-seconds")?
        .parse::<u32>()
        .map_err(|_| "--duration-seconds must be an integer".to_owned())?;
    let iteration = required("--iteration")?
        .parse::<u32>()
        .map_err(|_| "--iteration must be an integer".to_owned())?;
    if !ALLOWED_SAMPLE_RATES_HZ.contains(&sample_rate_hz) {
        return Err("sample rate must be exactly 16000 or 48000 Hz".to_owned());
    }
    if !ALLOWED_DURATIONS_SECONDS.contains(&duration_seconds) {
        return Err("duration must be exactly 5, 15, 30, or 60 seconds".to_owned());
    }
    if iteration == 0 {
        return Err("iteration must be at least 1".to_owned());
    }
    let known = [
        "--input-wav",
        "--output-flac",
        "--attestation",
        "--sample-rate-hz",
        "--duration-seconds",
        "--iteration",
        "--build-variant",
    ];
    if let Some(unknown) = values.keys().find(|key| !known.contains(&key.as_str())) {
        return Err(format!("unknown argument: {unknown}"));
    }
    Ok(Args {
        input_wav: PathBuf::from(required("--input-wav")?),
        output_flac: PathBuf::from(required("--output-flac")?),
        attestation: PathBuf::from(required("--attestation")?),
        sample_rate_hz,
        duration_seconds,
        iteration,
        build_variant: required("--build-variant")?,
    })
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn ensure_parent(path: &Path) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create {}: {error}", parent.display()))?;
    }
    Ok(())
}

fn run(args: Args) -> Result<(), String> {
    let profile = env!("SCRIBER_CODEC_LAB_CARGO_PROFILE");
    let compiled_variant = env!("SCRIBER_CODEC_LAB_REZIN_BUILD_VARIANT");
    let (lto, codegen_units) = match args.build_variant.as_str() {
        "stable_release_default" => ("off", 16),
        "stable_release_lto_thin_cgu1" => ("thin", 1),
        other => return Err(format!("unknown build variant: {other}")),
    };
    if compiled_variant != args.build_variant {
        return Err(format!(
            "build variant mismatch: requested={}, compiled={compiled_variant}",
            args.build_variant
        ));
    }

    let wav_bytes =
        fs::read(&args.input_wav).map_err(|error| format!("failed to read input WAV: {error}"))?;
    let expected_pcm_bytes = args.sample_rate_hz as usize * args.duration_seconds as usize * 2;
    if wav_bytes.len() != expected_pcm_bytes + 44
        || wav_bytes.get(0..4) != Some(b"RIFF")
        || wav_bytes.get(8..12) != Some(b"WAVE")
    {
        return Err("input must be the canonical 44-byte mono PCM16 WAV fixture".to_owned());
    }
    ensure_parent(&args.output_flac)?;
    let input = args
        .input_wav
        .to_str()
        .ok_or_else(|| "input path is not UTF-8".to_owned())?;
    let output = args
        .output_flac
        .to_str()
        .ok_or_else(|| "output path is not UTF-8".to_owned())?;

    let started = Instant::now();
    rezin_flac::encode::encode_to_file(input, output);
    let encode_to_file_ms = started.elapsed().as_secs_f64() * 1000.0;
    let output_bytes = fs::read(&args.output_flac)
        .map_err(|error| format!("failed to read FLAC output: {error}"))?;
    if output_bytes.get(0..4) != Some(b"fLaC") {
        return Err("rezin output is not native FLAC".to_owned());
    }
    let workers = std::thread::available_parallelism()
        .map(|count| count.get())
        .unwrap_or(1);
    let total_frames =
        (args.sample_rate_hz as usize * args.duration_seconds as usize).div_ceil(4096);
    let effective_workers = workers.min(total_frames.max(1));
    let attestation = json!({
        "schemaVersion": 1,
        "candidateId": format!("rezin_flac_0_2_1_{}", args.build_variant),
        "codec": "flac",
        "productionReady": false,
        "productionIntegrated": false,
        "productionPromoted": false,
        "input": {
            "container": "wav",
            "codec": "pcm_s16le",
            "sampleRateHz": args.sample_rate_hz,
            "channels": 1,
            "bitsPerSample": 16,
            "durationSeconds": args.duration_seconds,
            "pcmByteCount": expected_pcm_bytes,
            "wavByteCount": wav_bytes.len(),
            "wavSha256": sha256_hex(&wav_bytes),
        },
        "output": {
            "container": "native_flac",
            "codec": "flac",
            "byteCount": output_bytes.len(),
            "sha256": sha256_hex(&output_bytes),
            "encodedToPcmRatio": output_bytes.len() as f64 / expected_pcm_bytes as f64,
        },
        "encoder": {
            "crateName": "rezin-flac",
            "crateVersion": "0.2.1",
            "cargoFeature": "rezin-candidate",
            "executionModel": "available_parallelism child processes",
            "availableParallelism": workers,
            "effectiveWorkerCount": effective_workers,
            "workerBoundPatchApplied": true,
        },
        "build": {
            "variant": args.build_variant,
            "compiledVariant": compiled_variant,
            "cargoProfile": profile,
            "lto": lto,
            "codegenUnits": codegen_units,
        },
        "compiler": {
            "release": env!("SCRIBER_CODEC_LAB_RUSTC_RELEASE"),
            "commitHash": env!("SCRIBER_CODEC_LAB_RUSTC_COMMIT_HASH"),
            "commitDate": env!("SCRIBER_CODEC_LAB_RUSTC_COMMIT_DATE"),
            "llvmVersion": env!("SCRIBER_CODEC_LAB_LLVM_VERSION"),
            "rustcHost": env!("SCRIBER_CODEC_LAB_RUSTC_HOST"),
            "buildTarget": env!("SCRIBER_CODEC_LAB_BUILD_TARGET"),
            "compileTargetFeatures": env!("SCRIBER_CODEC_LAB_TARGET_FEATURES"),
            "encodedRustflags": env!("SCRIBER_CODEC_LAB_ENCODED_RUSTFLAGS"),
        },
        "timingsMs": {
            "wavParseParallelEncodeAndWrite": encode_to_file_ms,
        },
        "iteration": args.iteration,
    });
    ensure_parent(&args.attestation)?;
    fs::write(
        &args.attestation,
        serde_json::to_vec_pretty(&attestation)
            .map_err(|error| format!("failed to serialize attestation: {error}"))?,
    )
    .map_err(|error| format!("failed to write attestation: {error}"))?;
    println!("{attestation}");
    Ok(())
}

fn main() {
    let process_args: Vec<String> = env::args().collect();
    if process_args.get(1).map(String::as_str) == Some("--worker") {
        rezin_flac::encode::run_worker(&process_args);
        return;
    }
    if let Err(error) = parse_args().and_then(run) {
        eprintln!("scriber-rezin-candidate: {error}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn supported_matrix_is_exact() {
        assert_eq!(ALLOWED_DURATIONS_SECONDS, [5, 15, 30, 60]);
        assert_eq!(ALLOWED_SAMPLE_RATES_HZ, [16_000, 48_000]);
    }
}
