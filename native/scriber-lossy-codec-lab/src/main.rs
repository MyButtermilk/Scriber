use mp3lame_encoder::{Bitrate, Builder, FlushGap, Mode, MonoPcm, Quality, VbrMode};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::num::NonZeroU32;
use std::path::{Path, PathBuf};
use std::time::Instant;

const ALLOWED_SAMPLE_RATES_HZ: [u32; 2] = [16_000, 48_000];
const ALLOWED_DURATIONS_SECONDS: [u32; 4] = [5, 15, 30, 60];
const MP3_OUTPUT_SAMPLE_RATE_HZ: u32 = 16_000;
const BITRATE_BPS: u32 = 64_000;

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum Candidate {
    Mp3LameInProcess,
    OpusRuopusInProcess,
}

impl Candidate {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "mp3-lame" => Ok(Self::Mp3LameInProcess),
            "opus-ruopus" => Ok(Self::OpusRuopusInProcess),
            _ => Err(format!("unsupported candidate: {value}")),
        }
    }

    const fn id(self) -> &'static str {
        match self {
            Self::Mp3LameInProcess => "mp3_lame_in_process_v1",
            Self::OpusRuopusInProcess => "opus_ruopus_ogg_v1",
        }
    }

    const fn codec(self) -> &'static str {
        match self {
            Self::Mp3LameInProcess => "mp3",
            Self::OpusRuopusInProcess => "opus",
        }
    }

    const fn container(self) -> &'static str {
        match self {
            Self::Mp3LameInProcess => "mp3",
            Self::OpusRuopusInProcess => "ogg",
        }
    }
}

#[derive(Debug)]
struct Args {
    candidate: Candidate,
    input_pcm: PathBuf,
    output: PathBuf,
    attestation: PathBuf,
    sample_rate_hz: u32,
    duration_seconds: u32,
    iteration: u32,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CompilerAttestation {
    release: &'static str,
    commit_hash: &'static str,
    commit_date: &'static str,
    host: &'static str,
    llvm_version: &'static str,
    build_target: &'static str,
    compile_target_features: Vec<&'static str>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CpuAttestation {
    architecture: &'static str,
    operating_system: &'static str,
    vendor: String,
    brand: String,
    logical_parallelism: usize,
    sse2: bool,
    sse4_1: bool,
    avx: bool,
    avx2: bool,
    fma: bool,
    ruopus_simd_route_eligible: &'static str,
    route_is_eligibility_not_execution_proof: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct HashAndBytes {
    sha256: String,
    byte_count: usize,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct EncodeTiming {
    input_prepare_nanos: u128,
    codec_and_container_nanos: u128,
    total_candidate_nanos: u128,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CodecConfiguration {
    bitrate_bps: u32,
    source_sample_rate_hz: u32,
    encoded_input_sample_rate_hz: u32,
    encoded_output_sample_rate_hz: u32,
    channels: u8,
    sample_format: &'static str,
    frame_duration_ms: Option<u8>,
    source_rate_preparation: &'static str,
    implementation: &'static str,
    implementation_version: &'static str,
    implementation_features: Vec<&'static str>,
    bundled_native_library: Option<&'static str>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Attestation {
    schema_version: u8,
    lab: &'static str,
    issue: u8,
    candidate_id: &'static str,
    codec: &'static str,
    container: &'static str,
    sample_rate_hz: u32,
    duration_seconds: u32,
    iteration: u32,
    expected_mono_samples: usize,
    input: HashAndBytes,
    output: HashAndBytes,
    timing: EncodeTiming,
    configuration: CodecConfiguration,
    compiler: CompilerAttestation,
    cpu: CpuAttestation,
    production_ready: bool,
    production_integrated: bool,
    production_promoted: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut values = env::args().skip(1);
    let mut candidate = None;
    let mut input_pcm = None;
    let mut output = None;
    let mut attestation = None;
    let mut sample_rate_hz = None;
    let mut duration_seconds = None;
    let mut iteration = None;
    while let Some(key) = values.next() {
        let value = values
            .next()
            .ok_or_else(|| format!("missing value for {key}"))?;
        match key.as_str() {
            "--candidate" => candidate = Some(Candidate::parse(&value)?),
            "--input-pcm" => input_pcm = Some(PathBuf::from(value)),
            "--output" => output = Some(PathBuf::from(value)),
            "--attestation" => attestation = Some(PathBuf::from(value)),
            "--sample-rate-hz" => {
                sample_rate_hz = Some(value.parse().map_err(|_| "invalid sample rate")?)
            }
            "--duration-seconds" => {
                duration_seconds = Some(value.parse().map_err(|_| "invalid duration")?)
            }
            "--iteration" => iteration = Some(value.parse().map_err(|_| "invalid iteration")?),
            _ => return Err(format!("unknown argument: {key}")),
        }
    }
    let args = Args {
        candidate: candidate.ok_or("--candidate is required")?,
        input_pcm: input_pcm.ok_or("--input-pcm is required")?,
        output: output.ok_or("--output is required")?,
        attestation: attestation.ok_or("--attestation is required")?,
        sample_rate_hz: sample_rate_hz.ok_or("--sample-rate-hz is required")?,
        duration_seconds: duration_seconds.ok_or("--duration-seconds is required")?,
        iteration: iteration.ok_or("--iteration is required")?,
    };
    validate_matrix(args.sample_rate_hz, args.duration_seconds)?;
    if args.iteration == 0 {
        return Err("iteration must be at least one".to_owned());
    }
    Ok(args)
}

fn validate_matrix(sample_rate_hz: u32, duration_seconds: u32) -> Result<(), String> {
    if !ALLOWED_SAMPLE_RATES_HZ.contains(&sample_rate_hz) {
        return Err("sample rate must be exactly 16000 or 48000 Hz".to_owned());
    }
    if !ALLOWED_DURATIONS_SECONDS.contains(&duration_seconds) {
        return Err("duration must be exactly 5, 15, 30, or 60 seconds".to_owned());
    }
    Ok(())
}

fn pcm16_le(bytes: &[u8]) -> Result<Vec<i16>, String> {
    if !bytes.len().is_multiple_of(2) {
        return Err("PCM16 input has an odd byte count".to_owned());
    }
    Ok(bytes
        .chunks_exact(2)
        .map(|pair| i16::from_le_bytes([pair[0], pair[1]]))
        .collect())
}

fn upsample_16k_to_48k_linear(samples: &[i16]) -> Vec<f32> {
    let mut output = Vec::with_capacity(samples.len() * 3);
    for (index, &left) in samples.iter().enumerate() {
        let right = samples.get(index + 1).copied().unwrap_or(left);
        let left = f32::from(left) / 32_768.0;
        let right = f32::from(right) / 32_768.0;
        output.push(left);
        output.push(left + (right - left) / 3.0);
        output.push(left + (right - left) * (2.0 / 3.0));
    }
    output
}

fn pcm_to_opus_f32(samples: &[i16], sample_rate_hz: u32) -> Vec<f32> {
    if sample_rate_hz == 16_000 {
        upsample_16k_to_48k_linear(samples)
    } else {
        samples
            .iter()
            .map(|&sample| f32::from(sample) / 32_768.0)
            .collect()
    }
}

fn encode_mp3_lame(samples: &[i16], sample_rate_hz: u32) -> Result<Vec<u8>, String> {
    let mut encoder = Builder::new()
        .ok_or("LAME builder allocation failed")?
        .with_num_channels(1)
        .map_err(|error| format!("set LAME channels: {error}"))?
        .with_sample_rate(sample_rate_hz)
        .map_err(|error| format!("set LAME input rate: {error}"))?
        .with_output_sample_rate(NonZeroU32::new(MP3_OUTPUT_SAMPLE_RATE_HZ))
        .map_err(|error| format!("set LAME output rate: {error}"))?
        .with_brate(Bitrate::Kbps64)
        .map_err(|error| format!("set LAME bitrate: {error}"))?
        .with_mode(Mode::Mono)
        .map_err(|error| format!("set LAME mode: {error}"))?
        .with_quality(Quality::Good)
        .map_err(|error| format!("set LAME quality: {error}"))?
        .with_vbr_mode(VbrMode::Off)
        .map_err(|error| format!("set LAME CBR mode: {error}"))?
        .with_to_write_vbr_tag(true)
        .map_err(|error| format!("enable LAME gapless tag: {error}"))?
        .build()
        .map_err(|error| format!("initialize LAME encoder: {error}"))?;

    let mut payload = Vec::with_capacity(mp3lame_encoder::max_required_buffer_size(samples.len()));
    encoder
        .encode_to_vec(MonoPcm(samples), &mut payload)
        .map_err(|error| format!("encode LAME PCM: {error}"))?;
    payload.reserve(7_200);
    encoder
        .flush_to_vec::<FlushGap>(&mut payload)
        .map_err(|error| format!("flush LAME stream: {error}"))?;

    let tag_size = encoder.lame_tag_size();
    if tag_size == 0 {
        return Ok(payload);
    }
    let boundary = encoder.id3v2_tag_size().min(payload.len());
    let mut tag = Vec::with_capacity(tag_size);
    encoder
        .lame_tag_encode_to_vec(&mut tag)
        .ok_or("LAME tag buffer size calculation failed")?;
    let tag_end = boundary
        .checked_add(tag.len())
        .ok_or("LAME tag boundary overflow")?;
    if tag_end > payload.len() {
        return Err("LAME tag is larger than the reserved first MPEG frame".to_owned());
    }
    payload[boundary..tag_end].copy_from_slice(&tag);
    Ok(payload)
}

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn compiler_attestation() -> CompilerAttestation {
    CompilerAttestation {
        release: env!("SCRIBER_LAB_RUSTC_RELEASE"),
        commit_hash: env!("SCRIBER_LAB_RUSTC_COMMIT"),
        commit_date: env!("SCRIBER_LAB_RUSTC_COMMIT_DATE"),
        host: env!("SCRIBER_LAB_RUSTC_HOST"),
        llvm_version: env!("SCRIBER_LAB_RUSTC_LLVM"),
        build_target: env!("SCRIBER_LAB_BUILD_TARGET"),
        compile_target_features: env!("SCRIBER_LAB_COMPILE_TARGET_FEATURES")
            .split(',')
            .filter(|feature| !feature.is_empty())
            .collect(),
    }
}

#[cfg(target_arch = "x86_64")]
fn cpu_identity() -> (String, String) {
    use std::arch::x86_64::__cpuid;
    // SAFETY: CPUID is supported on every x86_64 CPU and the queried leaves
    // are checked against the maximum extended leaf before use.
    unsafe {
        let vendor_leaf = __cpuid(0);
        let mut vendor = Vec::with_capacity(12);
        vendor.extend_from_slice(&vendor_leaf.ebx.to_le_bytes());
        vendor.extend_from_slice(&vendor_leaf.edx.to_le_bytes());
        vendor.extend_from_slice(&vendor_leaf.ecx.to_le_bytes());
        let max_extended = __cpuid(0x8000_0000).eax;
        let mut brand = Vec::new();
        if max_extended >= 0x8000_0004 {
            for leaf in 0x8000_0002..=0x8000_0004 {
                let value = __cpuid(leaf);
                brand.extend_from_slice(&value.eax.to_le_bytes());
                brand.extend_from_slice(&value.ebx.to_le_bytes());
                brand.extend_from_slice(&value.ecx.to_le_bytes());
                brand.extend_from_slice(&value.edx.to_le_bytes());
            }
        }
        (
            String::from_utf8_lossy(&vendor)
                .trim_matches('\0')
                .to_owned(),
            String::from_utf8_lossy(&brand)
                .trim_matches(['\0', ' '])
                .to_owned(),
        )
    }
}

#[cfg(not(target_arch = "x86_64"))]
fn cpu_identity() -> (String, String) {
    ("not-x86_64".to_owned(), "not-x86_64".to_owned())
}

fn cpu_attestation() -> CpuAttestation {
    let (vendor, brand) = cpu_identity();
    #[cfg(target_arch = "x86_64")]
    let (sse2, sse4_1, avx, avx2, fma) = (
        std::arch::is_x86_feature_detected!("sse2"),
        std::arch::is_x86_feature_detected!("sse4.1"),
        std::arch::is_x86_feature_detected!("avx"),
        std::arch::is_x86_feature_detected!("avx2"),
        std::arch::is_x86_feature_detected!("fma"),
    );
    #[cfg(not(target_arch = "x86_64"))]
    let (sse2, sse4_1, avx, avx2, fma) = (false, false, false, false, false);
    let route = if avx2 && fma {
        "avx2_fma_runtime_dispatch_eligible"
    } else if sse2 {
        "sse2_runtime_dispatch_eligible"
    } else {
        "scalar_runtime_dispatch_eligible"
    };
    CpuAttestation {
        architecture: env::consts::ARCH,
        operating_system: env::consts::OS,
        vendor,
        brand,
        logical_parallelism: std::thread::available_parallelism()
            .map(usize::from)
            .unwrap_or(0),
        sse2,
        sse4_1,
        avx,
        avx2,
        fma,
        ruopus_simd_route_eligible: route,
        route_is_eligibility_not_execution_proof: true,
    }
}

fn configuration(candidate: Candidate, source_rate: u32) -> CodecConfiguration {
    match candidate {
        Candidate::Mp3LameInProcess => CodecConfiguration {
            bitrate_bps: BITRATE_BPS,
            source_sample_rate_hz: source_rate,
            encoded_input_sample_rate_hz: source_rate,
            encoded_output_sample_rate_hz: MP3_OUTPUT_SAMPLE_RATE_HZ,
            channels: 1,
            sample_format: "signed_16_le",
            frame_duration_ms: None,
            source_rate_preparation: "lame_internal_resampler_when_source_is_48000_hz",
            implementation: "mp3lame-encoder/mp3lame-sys/LAME",
            implementation_version: "0.2.4/0.1.11/3.100",
            implementation_features: vec!["std", "cbr_64k", "gapless_lame_tag"],
            bundled_native_library: Some("LAME 3.100 static lab link"),
        },
        Candidate::OpusRuopusInProcess => CodecConfiguration {
            bitrate_bps: BITRATE_BPS,
            source_sample_rate_hz: source_rate,
            encoded_input_sample_rate_hz: 48_000,
            encoded_output_sample_rate_hz: 48_000,
            channels: 1,
            sample_format: "signed_16_le_to_f32",
            frame_duration_ms: Some(20),
            source_rate_preparation: if source_rate == 16_000 {
                "deterministic_linear_3x_upsample_to_48000_hz"
            } else {
                "exact_i16_to_f32_scaling_at_48000_hz"
            },
            implementation: "ruopus pure Rust with RFC 7845 Ogg writer",
            implementation_version: "0.1.2",
            implementation_features: vec!["std", "default_features_disabled", "runtime_x86_simd"],
            bundled_native_library: None,
        },
    }
}

fn write_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("create output directory: {error}"))?;
    }
    let json =
        serde_json::to_vec_pretty(value).map_err(|error| format!("serialize JSON: {error}"))?;
    fs::write(path, [json.as_slice(), b"\n"].concat())
        .map_err(|error| format!("write {}: {error}", path.display()))
}

fn run(args: Args) -> Result<(), String> {
    let input = fs::read(&args.input_pcm)
        .map_err(|error| format!("read {}: {error}", args.input_pcm.display()))?;
    let expected_samples = args.sample_rate_hz as usize * args.duration_seconds as usize;
    let expected_bytes = expected_samples * 2;
    if input.len() != expected_bytes {
        return Err(format!(
            "input byte count {} does not match exact mono PCM16 matrix byte count {expected_bytes}",
            input.len()
        ));
    }

    let total_started = Instant::now();
    let prepare_started = Instant::now();
    let pcm = pcm16_le(&input)?;
    let opus_pcm = if matches!(args.candidate, Candidate::OpusRuopusInProcess) {
        Some(pcm_to_opus_f32(&pcm, args.sample_rate_hz))
    } else {
        None
    };
    let input_prepare_nanos = prepare_started.elapsed().as_nanos();
    let codec_started = Instant::now();
    let output = match args.candidate {
        Candidate::Mp3LameInProcess => encode_mp3_lame(&pcm, args.sample_rate_hz)?,
        Candidate::OpusRuopusInProcess => {
            ruopus::encode_ogg_opus(opus_pcm.as_deref().unwrap_or_default(), 1, BITRATE_BPS)
        }
    };
    let codec_and_container_nanos = codec_started.elapsed().as_nanos();
    let total_candidate_nanos = total_started.elapsed().as_nanos();
    if output.is_empty() {
        return Err("encoder produced an empty artifact".to_owned());
    }
    if let Some(parent) = args.output.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("create output directory: {error}"))?;
    }
    fs::write(&args.output, &output)
        .map_err(|error| format!("write {}: {error}", args.output.display()))?;
    let attestation = Attestation {
        schema_version: 1,
        lab: "scriber-lossy-codec-lab",
        issue: 18,
        candidate_id: args.candidate.id(),
        codec: args.candidate.codec(),
        container: args.candidate.container(),
        sample_rate_hz: args.sample_rate_hz,
        duration_seconds: args.duration_seconds,
        iteration: args.iteration,
        expected_mono_samples: expected_samples,
        input: HashAndBytes {
            sha256: sha256(&input),
            byte_count: input.len(),
        },
        output: HashAndBytes {
            sha256: sha256(&output),
            byte_count: output.len(),
        },
        timing: EncodeTiming {
            input_prepare_nanos,
            codec_and_container_nanos,
            total_candidate_nanos,
        },
        configuration: configuration(args.candidate, args.sample_rate_hz),
        compiler: compiler_attestation(),
        cpu: cpu_attestation(),
        production_ready: false,
        production_integrated: false,
        production_promoted: false,
    };
    write_json(&args.attestation, &attestation)
}

fn main() {
    if env::args().nth(1).as_deref() == Some("--system-json") {
        let value = serde_json::json!({
            "schemaVersion": 1,
            "lab": "scriber-lossy-codec-lab",
            "compiler": compiler_attestation(),
            "cpu": cpu_attestation(),
            "codecBuild": {
                "mp3": {
                    "wrapper": "mp3lame-encoder 0.2.4",
                    "sys": "mp3lame-sys 0.1.11",
                    "bundledLame": "3.100",
                    "features": ["std"]
                },
                "opus": {
                    "implementation": "ruopus 0.1.2",
                    "features": ["std"],
                    "defaultFeatures": false,
                    "simd": "stable x86 runtime dispatch; AVX2+FMA when eligible, otherwise SSE2"
                }
            },
            "productionReady": false,
            "productionIntegrated": false,
            "productionPromoted": false
        });
        println!(
            "{}",
            serde_json::to_string_pretty(&value).expect("serialize system JSON")
        );
        return;
    }
    if let Err(error) = parse_args().and_then(run) {
        eprintln!("scriber-lossy-codec-lab: {error}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matrix_is_fail_closed() {
        assert!(validate_matrix(16_000, 5).is_ok());
        assert!(validate_matrix(48_000, 60).is_ok());
        assert!(validate_matrix(44_100, 5).is_err());
        assert!(validate_matrix(16_000, 10).is_err());
    }

    #[test]
    fn linear_upsample_has_exact_three_x_shape() {
        let result = upsample_16k_to_48k_linear(&[0, 3_000]);
        assert_eq!(result.len(), 6);
        assert!((result[1] - 1_000.0 / 32_768.0).abs() < 1e-6);
        assert!((result[2] - 2_000.0 / 32_768.0).abs() < 1e-6);
        assert_eq!(result[3], 3_000.0 / 32_768.0);
        assert_eq!(result[5], 3_000.0 / 32_768.0);
    }

    #[test]
    fn ruopus_writes_ogg_opus_headers() {
        let pcm = vec![0_i16; 16_000 * 5];
        let prepared = pcm_to_opus_f32(&pcm, 16_000);
        let encoded = ruopus::encode_ogg_opus(&prepared, 1, BITRATE_BPS);
        assert!(encoded.starts_with(b"OggS"));
        assert!(encoded.windows(8).any(|window| window == b"OpusHead"));
        assert!(encoded.windows(8).any(|window| window == b"OpusTags"));
    }

    #[test]
    fn lame_writes_decodable_looking_mp3() {
        let pcm = vec![0_i16; 16_000 * 5];
        let encoded = encode_mp3_lame(&pcm, 16_000).expect("encode five-second fixture");
        assert!(encoded.len() > 10_000);
        assert!(
            encoded.starts_with(b"ID3")
                || encoded
                    .windows(2)
                    .any(|v| v[0] == 0xff && v[1] & 0xe0 == 0xe0)
        );
    }
}
