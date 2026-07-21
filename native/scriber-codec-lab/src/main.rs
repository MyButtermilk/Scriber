use flacenc::bitsink::ByteSink;
use flacenc::component::BitRepr;
use flacenc::config;
use flacenc::error::Verify;
use flacenc::source::MemSource;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

const SCHEMA_VERSION: u32 = 1;
const ALLOWED_DURATIONS_SECONDS: [u32; 4] = [5, 15, 30, 60];
const ALLOWED_SAMPLE_RATES_HZ: [u32; 2] = [16_000, 48_000];
const CHANNELS: usize = 1;
const BITS_PER_SAMPLE: usize = 16;

#[derive(Debug)]
struct Args {
    input: PathBuf,
    output: PathBuf,
    attestation: PathBuf,
    sample_rate_hz: u32,
    duration_seconds: u32,
    iteration: u32,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Attestation {
    schema_version: u32,
    candidate_id: &'static str,
    codec: &'static str,
    production_ready: bool,
    production_integrated: bool,
    production_promoted: bool,
    input: InputAttestation,
    output: OutputAttestation,
    encoder: EncoderAttestation,
    compiler: CompilerAttestation,
    cpu_path: CpuPathAttestation,
    timings_ms: TimingAttestation,
    iteration: u32,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct InputAttestation {
    format: &'static str,
    sample_rate_hz: u32,
    channels: usize,
    bits_per_sample: usize,
    duration_seconds: u32,
    sample_count: usize,
    byte_count: usize,
    sha256: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct OutputAttestation {
    container: &'static str,
    codec: &'static str,
    byte_count: usize,
    sha256: String,
    encoded_to_pcm_ratio: f64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct EncoderAttestation {
    crate_name: &'static str,
    crate_version: &'static str,
    crate_rustc_version: &'static str,
    crate_features: &'static str,
    crate_build_profile: &'static str,
    block_size_samples: usize,
    multithread: bool,
    lab_feature_nightly_simd: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CompilerAttestation {
    release: &'static str,
    commit_hash: &'static str,
    commit_date: &'static str,
    llvm_version: &'static str,
    rustc_host: &'static str,
    build_host: &'static str,
    build_target: &'static str,
    encoded_rustflags: &'static str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CpuPathAttestation {
    implementation: &'static str,
    upstream_feature: Option<&'static str>,
    target_arch: &'static str,
    target_os: &'static str,
    compile_target_features: Vec<&'static str>,
    runtime_detected_features: BTreeMap<&'static str, bool>,
    dispatch_claim: &'static str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct TimingAttestation {
    core_encode: f64,
    bitstream_serialize: f64,
    output_write: f64,
    encode_serialize_and_write: f64,
}

fn parse_args() -> Result<Args, String> {
    let mut values: BTreeMap<String, String> = BTreeMap::new();
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
    let input = PathBuf::from(required("--input-pcm")?);
    let output = PathBuf::from(required("--output-flac")?);
    let attestation = PathBuf::from(required("--attestation")?);
    let sample_rate_hz = required("--sample-rate-hz")?
        .parse::<u32>()
        .map_err(|_| "--sample-rate-hz must be an integer".to_owned())?;
    let duration_seconds = required("--duration-seconds")?
        .parse::<u32>()
        .map_err(|_| "--duration-seconds must be an integer".to_owned())?;
    let iteration = required("--iteration")?
        .parse::<u32>()
        .map_err(|_| "--iteration must be an integer".to_owned())?;

    let known = [
        "--input-pcm",
        "--output-flac",
        "--attestation",
        "--sample-rate-hz",
        "--duration-seconds",
        "--iteration",
    ];
    if let Some(unknown) = values.keys().find(|key| !known.contains(&key.as_str())) {
        return Err(format!("unknown argument: {unknown}"));
    }
    if !ALLOWED_SAMPLE_RATES_HZ.contains(&sample_rate_hz) {
        return Err("sample rate must be exactly 16000 or 48000 Hz".to_owned());
    }
    if !ALLOWED_DURATIONS_SECONDS.contains(&duration_seconds) {
        return Err("duration must be exactly 5, 15, 30, or 60 seconds".to_owned());
    }
    if iteration == 0 {
        return Err("iteration must be at least 1".to_owned());
    }

    Ok(Args {
        input,
        output,
        attestation,
        sample_rate_hz,
        duration_seconds,
        iteration,
    })
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn parse_pcm16_le(
    bytes: &[u8],
    sample_rate_hz: u32,
    duration_seconds: u32,
) -> Result<Vec<i32>, String> {
    let expected_samples = sample_rate_hz as usize * duration_seconds as usize * CHANNELS;
    let expected_bytes = expected_samples * (BITS_PER_SAMPLE / 8);
    if bytes.len() != expected_bytes {
        return Err(format!(
            "PCM byte length mismatch: expected {expected_bytes}, got {}",
            bytes.len()
        ));
    }
    Ok(bytes
        .chunks_exact(2)
        .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]) as i32)
        .collect())
}

fn runtime_features() -> BTreeMap<&'static str, bool> {
    let mut features = BTreeMap::new();
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    {
        features.insert("avx", std::is_x86_feature_detected!("avx"));
        features.insert("avx2", std::is_x86_feature_detected!("avx2"));
        features.insert("fma", std::is_x86_feature_detected!("fma"));
        features.insert("sse2", std::is_x86_feature_detected!("sse2"));
        features.insert("sse4.1", std::is_x86_feature_detected!("sse4.1"));
        features.insert("sse4.2", std::is_x86_feature_detected!("sse4.2"));
    }
    #[cfg(target_arch = "aarch64")]
    {
        features.insert("neon", std::arch::is_aarch64_feature_detected!("neon"));
    }
    features
}

fn ensure_parent(path: &Path) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create {}: {error}", parent.display()))?;
    }
    Ok(())
}

fn run(args: Args) -> Result<(), String> {
    let input_bytes =
        fs::read(&args.input).map_err(|error| format!("failed to read input PCM: {error}"))?;
    let samples = parse_pcm16_le(&input_bytes, args.sample_rate_hz, args.duration_seconds)?;

    // Single-threaded mode deliberately isolates the stable fake-SIMD and
    // nightly portable-SIMD implementations instead of mixing in scheduler
    // variance from flacenc's optional frame workers.
    let mut encoder_config = config::Encoder::default();
    encoder_config.multithread = false;
    let block_size = encoder_config.block_size;
    let verified_config = encoder_config
        .into_verified()
        .map_err(|(_, error)| format!("invalid encoder configuration: {error:?}"))?;
    let source = MemSource::from_samples(
        &samples,
        CHANNELS,
        BITS_PER_SAMPLE,
        args.sample_rate_hz as usize,
    );

    let total_started = Instant::now();
    let core_started = Instant::now();
    let stream = flacenc::encode_with_fixed_block_size(&verified_config, source, block_size)
        .map_err(|error| format!("FLAC encoding failed: {error}"))?;
    let core_ms = core_started.elapsed().as_secs_f64() * 1000.0;

    let serialize_started = Instant::now();
    let mut sink = ByteSink::new();
    stream
        .write(&mut sink)
        .map_err(|error| format!("FLAC bitstream serialization failed: {error:?}"))?;
    let flac_bytes = sink.as_slice();
    let serialize_ms = serialize_started.elapsed().as_secs_f64() * 1000.0;

    ensure_parent(&args.output)?;
    let write_started = Instant::now();
    fs::write(&args.output, flac_bytes)
        .map_err(|error| format!("failed to write FLAC output: {error}"))?;
    let write_ms = write_started.elapsed().as_secs_f64() * 1000.0;
    let total_ms = total_started.elapsed().as_secs_f64() * 1000.0;

    let nightly_simd = cfg!(feature = "nightly-simd");
    let attestation = Attestation {
        schema_version: SCHEMA_VERSION,
        candidate_id: if nightly_simd {
            "flacenc_0_5_1_nightly_portable_simd"
        } else {
            "flacenc_0_5_1_stable_fake_simd"
        },
        codec: "flac",
        production_ready: false,
        production_integrated: false,
        production_promoted: false,
        input: InputAttestation {
            format: "pcm_s16le",
            sample_rate_hz: args.sample_rate_hz,
            channels: CHANNELS,
            bits_per_sample: BITS_PER_SAMPLE,
            duration_seconds: args.duration_seconds,
            sample_count: samples.len(),
            byte_count: input_bytes.len(),
            sha256: sha256_hex(&input_bytes),
        },
        output: OutputAttestation {
            container: "native_flac",
            codec: "flac",
            byte_count: flac_bytes.len(),
            sha256: sha256_hex(flac_bytes),
            encoded_to_pcm_ratio: flac_bytes.len() as f64 / input_bytes.len() as f64,
        },
        encoder: EncoderAttestation {
            crate_name: "flacenc",
            crate_version: flacenc::constant::build_info::CRATE_VERSION,
            crate_rustc_version: flacenc::constant::build_info::RUSTC_VERSION,
            crate_features: flacenc::constant::build_info::FEATURES,
            crate_build_profile: flacenc::constant::build_info::BUILD_PROFILE,
            block_size_samples: block_size,
            multithread: false,
            lab_feature_nightly_simd: nightly_simd,
        },
        compiler: CompilerAttestation {
            release: env!("SCRIBER_CODEC_LAB_RUSTC_RELEASE"),
            commit_hash: env!("SCRIBER_CODEC_LAB_RUSTC_COMMIT_HASH"),
            commit_date: env!("SCRIBER_CODEC_LAB_RUSTC_COMMIT_DATE"),
            llvm_version: env!("SCRIBER_CODEC_LAB_LLVM_VERSION"),
            rustc_host: env!("SCRIBER_CODEC_LAB_RUSTC_HOST"),
            build_host: env!("SCRIBER_CODEC_LAB_BUILD_HOST"),
            build_target: env!("SCRIBER_CODEC_LAB_BUILD_TARGET"),
            encoded_rustflags: env!("SCRIBER_CODEC_LAB_ENCODED_RUSTFLAGS"),
        },
        cpu_path: CpuPathAttestation {
            implementation: if nightly_simd {
                "flacenc_portable_simd_nightly"
            } else {
                "flacenc_fake_simd_stable"
            },
            upstream_feature: nightly_simd.then_some("simd-nightly"),
            target_arch: env::consts::ARCH,
            target_os: env::consts::OS,
            compile_target_features: env!("SCRIBER_CODEC_LAB_TARGET_FEATURES")
                .split(',')
                .filter(|feature| !feature.is_empty())
                .collect(),
            runtime_detected_features: runtime_features(),
            dispatch_claim: if nightly_simd {
                "upstream portable_simd enabled; exact machine instructions remain compiler-selected"
            } else {
                "upstream stable fake-SIMD compatibility implementation"
            },
        },
        timings_ms: TimingAttestation {
            core_encode: core_ms,
            bitstream_serialize: serialize_ms,
            output_write: write_ms,
            encode_serialize_and_write: total_ms,
        },
        iteration: args.iteration,
    };

    ensure_parent(&args.attestation)?;
    let json = serde_json::to_vec_pretty(&attestation)
        .map_err(|error| format!("failed to serialize attestation: {error}"))?;
    fs::write(&args.attestation, json)
        .map_err(|error| format!("failed to write attestation: {error}"))?;
    println!(
        "{}",
        serde_json::to_string(&attestation)
            .map_err(|error| format!("failed to serialize result: {error}"))?
    );
    Ok(())
}

fn main() {
    if let Err(error) = parse_args().and_then(run) {
        eprintln!("scriber-codec-lab: {error}");
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

    #[test]
    fn pcm_shape_requires_exact_attested_duration() {
        let bytes = vec![0_u8; 16_000 * 5 * 2];
        assert_eq!(parse_pcm16_le(&bytes, 16_000, 5).unwrap().len(), 80_000);
        assert!(parse_pcm16_le(&bytes[..bytes.len() - 2], 16_000, 5).is_err());
    }
}
