#![allow(dead_code)]

use serde_json::{json, Map, Value};
use std::fmt;
use std::io::{self, Write};
use std::sync::Arc;

pub type SharedPcmSegment = Arc<Vec<u8>>;

pub const AUDIO_PREPARATION_SCHEMA_VERSION: &str = "1";
pub const WAV_PCM16_HEADER_LEN: usize = 44;
pub const MIN_ENCODER_QUEUE_CAPACITY_FRAMES: usize = 1;
pub const MAX_ENCODER_QUEUE_CAPACITY_FRAMES: usize = 128;
pub const MIN_VIRTUAL_WAV_PCM_BYTES: u64 = 2;
pub const MAX_VIRTUAL_WAV_PCM_BYTES: u64 = 64 * 1024 * 1024;

const WAV_BITS_PER_SAMPLE: u16 = 16;
const WAV_FORMAT_PCM: u16 = 1;
const WAV_RIFF_MAX_PCM_BYTES: u64 = u32::MAX as u64 - 36;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodedAudioFormat {
    WavPcm16,
}

impl EncodedAudioFormat {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::WavPcm16 => "wav_pcm16",
        }
    }

    fn parse(value: &Value) -> Result<Self, AudioCodecError> {
        match value.as_str() {
            Some("wav_pcm16") => Ok(Self::WavPcm16),
            _ => Err(AudioCodecError::UnsupportedFormat),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncoderImplementation {
    WavPcm16Virtual,
    WavPcm16FileV1,
}

impl EncoderImplementation {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::WavPcm16Virtual => "wav_pcm16_virtual",
            Self::WavPcm16FileV1 => "wav_pcm16_file_v1",
        }
    }

    fn parse(value: &Value) -> Result<Self, AudioCodecError> {
        match value.as_str() {
            Some("wav_pcm16_virtual") => Ok(Self::WavPcm16Virtual),
            Some("wav_pcm16_file_v1") => Ok(Self::WavPcm16FileV1),
            _ => Err(AudioCodecError::UnsupportedImplementation),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncoderPlan {
    pub format: EncodedAudioFormat,
    pub implementation: EncoderImplementation,
    pub sample_rate: u32,
    pub channels: u16,
    pub bits_per_sample: u16,
    pub queue_capacity_frames: usize,
    pub max_pcm_bytes: u64,
}

impl EncoderPlan {
    pub fn parse_optional(
        capture_payload: &Value,
        capture_sample_rate: u32,
        capture_channels: u16,
    ) -> Result<Option<Self>, AudioCodecError> {
        let Some(value) = capture_payload.get("audioPreparation") else {
            return Ok(None);
        };
        if value.is_null() {
            return Ok(None);
        }
        Self::parse(value, capture_sample_rate, capture_channels).map(Some)
    }

    pub fn parse(
        value: &Value,
        capture_sample_rate: u32,
        capture_channels: u16,
    ) -> Result<Self, AudioCodecError> {
        let object = value
            .as_object()
            .ok_or(AudioCodecError::InvalidPreparationObject)?;
        reject_unknown_plan_fields(object)?;

        if object.get("schemaVersion").and_then(Value::as_str)
            != Some(AUDIO_PREPARATION_SCHEMA_VERSION)
        {
            return Err(AudioCodecError::UnsupportedSchemaVersion);
        }
        let format = EncodedAudioFormat::parse(
            object
                .get("format")
                .ok_or(AudioCodecError::MissingPlanField("format"))?,
        )?;
        let implementation = EncoderImplementation::parse(
            object
                .get("implementation")
                .ok_or(AudioCodecError::MissingPlanField("implementation"))?,
        )?;
        if !matches!(
            (format, implementation),
            (
                EncodedAudioFormat::WavPcm16,
                EncoderImplementation::WavPcm16Virtual | EncoderImplementation::WavPcm16FileV1
            )
        ) {
            return Err(AudioCodecError::ImplementationFormatMismatch);
        }

        let sample_rate = required_u32(object, "sampleRate")?;
        let channels = required_u16(object, "channels")?;
        let bits_per_sample = required_u16(object, "bitsPerSample")?;
        let queue_capacity_frames = required_usize(object, "queueCapacityFrames")?;
        let max_pcm_bytes = required_u64(object, "maxPcmBytes")?;

        if sample_rate != capture_sample_rate || channels != capture_channels {
            return Err(AudioCodecError::CaptureFormatMismatch);
        }
        if !(8_000..=192_000).contains(&sample_rate) || !(1..=16).contains(&channels) {
            return Err(AudioCodecError::UnsupportedPcmFormat);
        }
        if bits_per_sample != WAV_BITS_PER_SAMPLE {
            return Err(AudioCodecError::UnsupportedPcmFormat);
        }
        if !(MIN_ENCODER_QUEUE_CAPACITY_FRAMES..=MAX_ENCODER_QUEUE_CAPACITY_FRAMES)
            .contains(&queue_capacity_frames)
        {
            return Err(AudioCodecError::QueueCapacityOutOfRange);
        }
        if !(MIN_VIRTUAL_WAV_PCM_BYTES..=MAX_VIRTUAL_WAV_PCM_BYTES).contains(&max_pcm_bytes)
            || max_pcm_bytes > WAV_RIFF_MAX_PCM_BYTES
        {
            return Err(AudioCodecError::PcmLimitOutOfRange);
        }
        let block_align = u64::from(channels) * u64::from(bits_per_sample / 8);
        if max_pcm_bytes % block_align != 0 {
            return Err(AudioCodecError::PcmLimitNotFrameAligned);
        }

        Ok(Self {
            format,
            implementation,
            sample_rate,
            channels,
            bits_per_sample,
            queue_capacity_frames,
            max_pcm_bytes,
        })
    }

    pub fn to_payload(&self) -> Value {
        json!({
            "schemaVersion": AUDIO_PREPARATION_SCHEMA_VERSION,
            "format": self.format.as_str(),
            "implementation": self.implementation.as_str(),
            "sampleRate": self.sample_rate,
            "channels": self.channels,
            "bitsPerSample": self.bits_per_sample,
            "queueCapacityFrames": self.queue_capacity_frames,
            "maxPcmBytes": self.max_pcm_bytes,
        })
    }
}

fn reject_unknown_plan_fields(object: &Map<String, Value>) -> Result<(), AudioCodecError> {
    const ALLOWED: [&str; 8] = [
        "schemaVersion",
        "format",
        "implementation",
        "sampleRate",
        "channels",
        "bitsPerSample",
        "queueCapacityFrames",
        "maxPcmBytes",
    ];
    if object.keys().any(|key| !ALLOWED.contains(&key.as_str())) {
        return Err(AudioCodecError::UnknownPlanField);
    }
    Ok(())
}

fn required_u64(object: &Map<String, Value>, field: &'static str) -> Result<u64, AudioCodecError> {
    object
        .get(field)
        .ok_or(AudioCodecError::MissingPlanField(field))?
        .as_u64()
        .ok_or(AudioCodecError::InvalidPlanField(field))
}

fn required_u32(object: &Map<String, Value>, field: &'static str) -> Result<u32, AudioCodecError> {
    required_u64(object, field)?
        .try_into()
        .map_err(|_| AudioCodecError::InvalidPlanField(field))
}

fn required_u16(object: &Map<String, Value>, field: &'static str) -> Result<u16, AudioCodecError> {
    required_u64(object, field)?
        .try_into()
        .map_err(|_| AudioCodecError::InvalidPlanField(field))
}

fn required_usize(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<usize, AudioCodecError> {
    required_u64(object, field)?
        .try_into()
        .map_err(|_| AudioCodecError::InvalidPlanField(field))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AudioCodecError {
    InvalidPreparationObject,
    UnsupportedSchemaVersion,
    MissingPlanField(&'static str),
    InvalidPlanField(&'static str),
    UnknownPlanField,
    UnsupportedFormat,
    UnsupportedImplementation,
    ImplementationFormatMismatch,
    CaptureFormatMismatch,
    UnsupportedPcmFormat,
    QueueCapacityOutOfRange,
    PcmLimitOutOfRange,
    PcmLimitNotFrameAligned,
    PcmLimitExceeded,
    PcmDataNotFrameAligned,
    RiffSizeOverflow,
}

impl AudioCodecError {
    pub const fn code(&self) -> &'static str {
        match self {
            Self::InvalidPreparationObject => "audioPreparationInvalidObject",
            Self::UnsupportedSchemaVersion => "audioPreparationUnsupportedSchemaVersion",
            Self::MissingPlanField(_) => "audioPreparationMissingField",
            Self::InvalidPlanField(_) => "audioPreparationInvalidField",
            Self::UnknownPlanField => "audioPreparationUnknownField",
            Self::UnsupportedFormat => "audioPreparationUnsupportedFormat",
            Self::UnsupportedImplementation => "audioPreparationUnsupportedImplementation",
            Self::ImplementationFormatMismatch => "audioPreparationImplementationFormatMismatch",
            Self::CaptureFormatMismatch => "audioPreparationCaptureFormatMismatch",
            Self::UnsupportedPcmFormat => "audioPreparationUnsupportedPcmFormat",
            Self::QueueCapacityOutOfRange => "audioPreparationQueueCapacityOutOfRange",
            Self::PcmLimitOutOfRange => "audioPreparationPcmLimitOutOfRange",
            Self::PcmLimitNotFrameAligned => "audioPreparationPcmLimitNotFrameAligned",
            Self::PcmLimitExceeded => "audioPreparationPcmLimitExceeded",
            Self::PcmDataNotFrameAligned => "audioPreparationPcmDataNotFrameAligned",
            Self::RiffSizeOverflow => "audioPreparationRiffSizeOverflow",
        }
    }
}

impl fmt::Display for AudioCodecError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for AudioCodecError {}

pub trait StreamingAudioEncoder: Send {
    type Output;

    fn plan(&self) -> &EncoderPlan;
    fn push_pcm_segment(&mut self, segment: SharedPcmSegment) -> Result<(), AudioCodecError>;
    fn finish(self) -> Result<Self::Output, AudioCodecError>;
}

#[derive(Debug)]
pub struct WavPcm16VirtualEncoder {
    plan: EncoderPlan,
    segments: Vec<SharedPcmSegment>,
    pcm_bytes: u64,
}

impl WavPcm16VirtualEncoder {
    pub fn new(plan: EncoderPlan) -> Result<Self, AudioCodecError> {
        if plan.format != EncodedAudioFormat::WavPcm16
            || plan.implementation != EncoderImplementation::WavPcm16Virtual
            || plan.bits_per_sample != WAV_BITS_PER_SAMPLE
        {
            return Err(AudioCodecError::ImplementationFormatMismatch);
        }
        Ok(Self {
            plan,
            segments: Vec::new(),
            pcm_bytes: 0,
        })
    }
}

impl StreamingAudioEncoder for WavPcm16VirtualEncoder {
    type Output = VirtualWavPcm16;

    fn plan(&self) -> &EncoderPlan {
        &self.plan
    }

    fn push_pcm_segment(&mut self, segment: SharedPcmSegment) -> Result<(), AudioCodecError> {
        let new_total = self
            .pcm_bytes
            .checked_add(segment.len() as u64)
            .ok_or(AudioCodecError::PcmLimitExceeded)?;
        if new_total > self.plan.max_pcm_bytes || new_total > WAV_RIFF_MAX_PCM_BYTES {
            return Err(AudioCodecError::PcmLimitExceeded);
        }
        self.segments.push(segment);
        self.pcm_bytes = new_total;
        Ok(())
    }

    fn finish(self) -> Result<Self::Output, AudioCodecError> {
        let block_align = u64::from(self.plan.channels) * 2;
        if !self.pcm_bytes.is_multiple_of(block_align) {
            return Err(AudioCodecError::PcmDataNotFrameAligned);
        }
        let header = wav_pcm16_header(self.plan.sample_rate, self.plan.channels, self.pcm_bytes)?;
        Ok(VirtualWavPcm16 {
            plan: self.plan,
            header,
            segments: self.segments,
            pcm_bytes: self.pcm_bytes,
        })
    }
}

#[derive(Debug)]
pub struct VirtualWavPcm16 {
    plan: EncoderPlan,
    header: [u8; WAV_PCM16_HEADER_LEN],
    segments: Vec<SharedPcmSegment>,
    pcm_bytes: u64,
}

impl VirtualWavPcm16 {
    pub fn header(&self) -> &[u8; WAV_PCM16_HEADER_LEN] {
        &self.header
    }

    pub fn segments(&self) -> &[SharedPcmSegment] {
        &self.segments
    }

    pub const fn pcm_bytes(&self) -> u64 {
        self.pcm_bytes
    }

    pub fn total_bytes(&self) -> u64 {
        WAV_PCM16_HEADER_LEN as u64 + self.pcm_bytes
    }

    pub fn plan(&self) -> &EncoderPlan {
        &self.plan
    }

    pub fn write_to<W: Write>(&self, writer: &mut W) -> io::Result<u64> {
        writer.write_all(&self.header)?;
        for segment in &self.segments {
            writer.write_all(segment.as_slice())?;
        }
        Ok(self.total_bytes())
    }
}

pub fn wav_pcm16_header(
    sample_rate: u32,
    channels: u16,
    pcm_bytes: u64,
) -> Result<[u8; WAV_PCM16_HEADER_LEN], AudioCodecError> {
    if !(8_000..=192_000).contains(&sample_rate) || !(1..=16).contains(&channels) {
        return Err(AudioCodecError::UnsupportedPcmFormat);
    }
    if pcm_bytes > WAV_RIFF_MAX_PCM_BYTES {
        return Err(AudioCodecError::RiffSizeOverflow);
    }
    let block_align = channels
        .checked_mul(WAV_BITS_PER_SAMPLE / 8)
        .ok_or(AudioCodecError::RiffSizeOverflow)?;
    if !pcm_bytes.is_multiple_of(u64::from(block_align)) {
        return Err(AudioCodecError::PcmDataNotFrameAligned);
    }
    let byte_rate = sample_rate
        .checked_mul(u32::from(block_align))
        .ok_or(AudioCodecError::RiffSizeOverflow)?;
    let data_size: u32 = pcm_bytes
        .try_into()
        .map_err(|_| AudioCodecError::RiffSizeOverflow)?;
    let riff_size = data_size
        .checked_add(36)
        .ok_or(AudioCodecError::RiffSizeOverflow)?;

    let mut header = [0_u8; WAV_PCM16_HEADER_LEN];
    header[0..4].copy_from_slice(b"RIFF");
    header[4..8].copy_from_slice(&riff_size.to_le_bytes());
    header[8..12].copy_from_slice(b"WAVE");
    header[12..16].copy_from_slice(b"fmt ");
    header[16..20].copy_from_slice(&16_u32.to_le_bytes());
    header[20..22].copy_from_slice(&WAV_FORMAT_PCM.to_le_bytes());
    header[22..24].copy_from_slice(&channels.to_le_bytes());
    header[24..28].copy_from_slice(&sample_rate.to_le_bytes());
    header[28..32].copy_from_slice(&byte_rate.to_le_bytes());
    header[32..34].copy_from_slice(&block_align.to_le_bytes());
    header[34..36].copy_from_slice(&WAV_BITS_PER_SAMPLE.to_le_bytes());
    header[36..40].copy_from_slice(b"data");
    header[40..44].copy_from_slice(&data_size.to_le_bytes());
    Ok(header)
}

pub fn codec_attestation_payload() -> Value {
    json!({
        "schemaVersion": AUDIO_PREPARATION_SCHEMA_VERSION,
        "implementations": [
            {
                "id": EncoderImplementation::WavPcm16Virtual.as_str(),
                "format": EncodedAudioFormat::WavPcm16.as_str(),
                "lossless": true,
                "productionReady": false,
                "lifecycleState": "captureLabGated",
                "streamingInput": true,
                "virtualHeaderBytes": WAV_PCM16_HEADER_LEN,
                "maxPcmBytes": MAX_VIRTUAL_WAV_PCM_BYTES,
                "artifactExposed": false,
            },
            {
                "id": EncoderImplementation::WavPcm16FileV1.as_str(),
                "format": EncodedAudioFormat::WavPcm16.as_str(),
                "lossless": true,
                "productionReady": true,
                "lifecycleState": "production",
                "streamingInput": true,
                "headerPatchOnFinish": true,
                "maxPcmBytes": MAX_VIRTUAL_WAV_PCM_BYTES,
                "artifactExposed": true,
                "artifactOwnership": "tauriLeaseToPythonBackend",
            }
        ],
        "experimentalBuildFeatures": {
            "flacencStable": cfg!(feature = "codec-experimental-flacenc-stable"),
            "flacencNightly": cfg!(feature = "codec-experimental-flacenc-nightly"),
            "rezinFlac": cfg!(feature = "codec-experimental-rezin-flac"),
            "ruopus": cfg!(feature = "codec-experimental-ruopus"),
            "libopusenc": cfg!(feature = "codec-experimental-libopusenc"),
            "mp3lame": cfg!(feature = "codec-experimental-mp3lame"),
            "shine": cfg!(feature = "codec-experimental-shine"),
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_plan(max_pcm_bytes: u64) -> EncoderPlan {
        EncoderPlan {
            format: EncodedAudioFormat::WavPcm16,
            implementation: EncoderImplementation::WavPcm16Virtual,
            sample_rate: 16_000,
            channels: 1,
            bits_per_sample: 16,
            queue_capacity_frames: 8,
            max_pcm_bytes,
        }
    }

    fn read_u16(bytes: &[u8], offset: usize) -> u16 {
        u16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap())
    }

    fn read_u32(bytes: &[u8], offset: usize) -> u32 {
        u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap())
    }

    #[test]
    fn wav_header_has_exact_pcm16_riff_sizes() {
        let header = wav_pcm16_header(16_000, 1, 32_000).unwrap();

        assert_eq!(&header[0..4], b"RIFF");
        assert_eq!(read_u32(&header, 4), 32_036);
        assert_eq!(&header[8..12], b"WAVE");
        assert_eq!(&header[12..16], b"fmt ");
        assert_eq!(read_u32(&header, 16), 16);
        assert_eq!(read_u16(&header, 20), WAV_FORMAT_PCM);
        assert_eq!(read_u16(&header, 22), 1);
        assert_eq!(read_u32(&header, 24), 16_000);
        assert_eq!(read_u32(&header, 28), 32_000);
        assert_eq!(read_u16(&header, 32), 2);
        assert_eq!(read_u16(&header, 34), 16);
        assert_eq!(&header[36..40], b"data");
        assert_eq!(read_u32(&header, 40), 32_000);
    }

    #[test]
    fn virtual_wav_preserves_fragmented_segments_without_pcm_reassembly() {
        let first = Arc::new(vec![1_u8, 2, 3]);
        let second = Arc::new(vec![4_u8]);
        let first_identity = first.as_slice().as_ptr();
        let second_identity = second.as_slice().as_ptr();
        let mut encoder = WavPcm16VirtualEncoder::new(test_plan(16)).unwrap();

        encoder.push_pcm_segment(Arc::clone(&first)).unwrap();
        encoder.push_pcm_segment(Arc::clone(&second)).unwrap();
        let wav = encoder.finish().unwrap();

        assert_eq!(wav.pcm_bytes(), 4);
        assert_eq!(wav.total_bytes(), 48);
        assert_eq!(wav.segments().len(), 2);
        assert_eq!(wav.segments()[0].as_slice().as_ptr(), first_identity);
        assert_eq!(wav.segments()[1].as_slice().as_ptr(), second_identity);
        let mut emitted = Vec::new();
        assert_eq!(wav.write_to(&mut emitted).unwrap(), 48);
        assert_eq!(&emitted[44..], &[1, 2, 3, 4]);
    }

    #[test]
    fn virtual_wav_rejects_overflow_before_mutating_segments() {
        let mut encoder = WavPcm16VirtualEncoder::new(test_plan(4)).unwrap();
        encoder
            .push_pcm_segment(Arc::new(vec![1_u8, 2, 3, 4]))
            .unwrap();

        assert_eq!(
            encoder.push_pcm_segment(Arc::new(vec![5_u8, 6])),
            Err(AudioCodecError::PcmLimitExceeded)
        );
        let wav = encoder.finish().unwrap();
        assert_eq!(wav.pcm_bytes(), 4);
        assert_eq!(wav.segments().len(), 1);
    }

    #[test]
    fn plan_parse_is_exact_and_fail_closed() {
        let value = json!({
            "schemaVersion": "1",
            "format": "wav_pcm16",
            "implementation": "wav_pcm16_virtual",
            "sampleRate": 16_000,
            "channels": 1,
            "bitsPerSample": 16,
            "queueCapacityFrames": 8,
            "maxPcmBytes": 32_000,
        });
        let plan = EncoderPlan::parse(&value, 16_000, 1).unwrap();
        assert_eq!(plan, test_plan(32_000));
        assert_eq!(
            EncoderPlan::parse(&plan.to_payload(), 16_000, 1).unwrap(),
            plan
        );

        let mut unknown = value.clone();
        unknown["provider"] = Value::String("must-not-cross-boundary".to_string());
        assert_eq!(
            EncoderPlan::parse(&unknown, 16_000, 1),
            Err(AudioCodecError::UnknownPlanField)
        );

        let mut unsupported = value;
        unsupported["implementation"] = Value::String("mp3lame".to_string());
        assert_eq!(
            EncoderPlan::parse(&unsupported, 16_000, 1),
            Err(AudioCodecError::UnsupportedImplementation)
        );
    }

    #[test]
    fn codec_attestation_never_promotes_experimental_features() {
        let payload = codec_attestation_payload();
        assert_eq!(payload["implementations"][0]["id"], "wav_pcm16_virtual");
        assert_eq!(payload["implementations"][0]["lossless"], true);
        assert_eq!(payload["implementations"][0]["productionReady"], false);
        assert_eq!(
            payload["implementations"][0]["lifecycleState"],
            "captureLabGated"
        );
        assert_eq!(payload["implementations"][0]["artifactExposed"], false);
        assert_eq!(payload["implementations"][1]["id"], "wav_pcm16_file_v1");
        assert_eq!(payload["implementations"][1]["productionReady"], true);
        assert_eq!(payload["implementations"][1]["artifactExposed"], true);
    }
}
