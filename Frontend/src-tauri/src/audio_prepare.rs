#![allow(dead_code)]

use crate::audio_codec::{
    wav_pcm16_header, EncoderImplementation, EncoderPlan, SharedPcmSegment, StreamingAudioEncoder,
    VirtualWavPcm16, WavPcm16VirtualEncoder, WAV_PCM16_HEADER_LEN,
};
use serde_json::{json, Value};
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;
use std::sync::{
    mpsc::{self, Receiver, SyncSender, TrySendError},
    Arc, Mutex,
};
use std::thread::{self, JoinHandle};

const PREPARATION_THREAD_NAME: &str = "scriber-audio-wav-prepare";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AudioPreparationSubmit {
    Accepted,
    CandidateInvalidated,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AudioPreparationState {
    Ready,
    Invalidated,
    Failed,
}

impl AudioPreparationState {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::Invalidated => "invalidated",
            Self::Failed => "failed",
        }
    }
}

#[derive(Debug)]
pub struct AudioPreparationResult {
    pub state: AudioPreparationState,
    pub plan: EncoderPlan,
    pub submitted_segments: u64,
    pub submitted_pcm_bytes: u64,
    pub reason: Option<&'static str>,
    pub virtual_wav: Option<VirtualWavPcm16>,
    pub artifact: Option<PreparedAudioArtifact>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureArtifactTarget {
    pub lease_id: String,
    pub path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreparedAudioArtifact {
    pub lease_id: String,
    pub path: PathBuf,
    pub pcm_bytes: u64,
    pub byte_length: u64,
}

impl AudioPreparationResult {
    pub fn summary_payload(&self) -> Value {
        let virtual_wav_bytes = self.virtual_wav.as_ref().map(VirtualWavPcm16::total_bytes);
        let artifact = self.artifact.as_ref().map(|artifact| {
            json!({
                "schemaVersion": "1",
                "leaseId": artifact.lease_id,
                "path": artifact.path,
                "format": "wav_pcm16",
                "contentType": "audio/wav",
                "byteLength": artifact.byte_length,
                "pcmBytes": artifact.pcm_bytes,
                "sampleRate": self.plan.sample_rate,
                "channels": self.plan.channels,
                "bitsPerSample": self.plan.bits_per_sample,
                "owner": "tauriShellPendingHandoff",
                "cleanupCommand": "audioCaptureArtifactRelease",
            })
        });
        json!({
            "state": self.state.as_str(),
            "reason": self.reason,
            "plan": self.plan.to_payload(),
            "submittedSegments": self.submitted_segments,
            "submittedPcmBytes": self.submitted_pcm_bytes,
            "virtualWavBytes": virtual_wav_bytes,
            "artifactExposed": artifact.is_some(),
            "artifact": artifact,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AudioPreparationError {
    WorkerSpawnFailed,
    WorkerPanicked,
}

impl AudioPreparationError {
    pub const fn code(&self) -> &'static str {
        match self {
            Self::WorkerSpawnFailed => "audioPreparationWorkerSpawnFailed",
            Self::WorkerPanicked => "audioPreparationWorkerPanicked",
        }
    }
}

impl fmt::Display for AudioPreparationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for AudioPreparationError {}

#[derive(Debug)]
pub struct AudioPreparationWorker {
    plan: EncoderPlan,
    sender: Option<SyncSender<SharedPcmSegment>>,
    join_handle: Option<JoinHandle<AudioPreparationResult>>,
    invalidated_reason: Arc<Mutex<Option<&'static str>>>,
    submitted_segments: u64,
    submitted_pcm_bytes: u64,
}

impl AudioPreparationWorker {
    pub fn start(plan: EncoderPlan) -> Result<Self, AudioPreparationError> {
        Self::start_inner(plan, None, None)
    }

    pub fn start_file(
        plan: EncoderPlan,
        target: CaptureArtifactTarget,
    ) -> Result<Self, AudioPreparationError> {
        Self::start_inner(plan, Some(target), None)
    }

    fn start_inner(
        plan: EncoderPlan,
        artifact_target: Option<CaptureArtifactTarget>,
        start_gate: Option<Receiver<()>>,
    ) -> Result<Self, AudioPreparationError> {
        let (sender, receiver) = mpsc::sync_channel(plan.queue_capacity_frames);
        let invalidated_reason = Arc::new(Mutex::new(None));
        let worker_invalidated_reason = Arc::clone(&invalidated_reason);
        let worker_plan = plan.clone();
        let panic_plan = plan.clone();
        let panic_artifact_target = artifact_target.clone();
        let join_handle = thread::Builder::new()
            .name(PREPARATION_THREAD_NAME.to_string())
            .spawn(move || {
                match catch_unwind(AssertUnwindSafe(|| {
                    run_wav_preparation_worker(
                        worker_plan,
                        artifact_target,
                        receiver,
                        worker_invalidated_reason,
                        start_gate,
                    )
                })) {
                    Ok(result) => result,
                    Err(_) => {
                        if let Some(target) = panic_artifact_target {
                            let _ = fs::remove_file(target.path);
                        }
                        failed_result(panic_plan, "audioPreparationWorkerPanicked")
                    }
                }
            })
            .map_err(|_| AudioPreparationError::WorkerSpawnFailed)?;
        Ok(Self {
            plan,
            sender: Some(sender),
            join_handle: Some(join_handle),
            invalidated_reason,
            submitted_segments: 0,
            submitted_pcm_bytes: 0,
        })
    }

    pub fn plan(&self) -> &EncoderPlan {
        &self.plan
    }

    pub fn try_submit(&mut self, segment: SharedPcmSegment) -> AudioPreparationSubmit {
        let Some(sender) = self.sender.as_ref() else {
            return AudioPreparationSubmit::CandidateInvalidated;
        };
        let segment_len = segment.len() as u64;
        let Some(next_pcm_bytes) = self.submitted_pcm_bytes.checked_add(segment_len) else {
            self.invalidate("audioPreparationPcmLimitExceeded");
            return AudioPreparationSubmit::CandidateInvalidated;
        };
        if next_pcm_bytes > self.plan.max_pcm_bytes {
            self.invalidate("audioPreparationPcmLimitExceeded");
            return AudioPreparationSubmit::CandidateInvalidated;
        }
        match sender.try_send(segment) {
            Ok(()) => {
                self.submitted_segments = self.submitted_segments.saturating_add(1);
                self.submitted_pcm_bytes = next_pcm_bytes;
                AudioPreparationSubmit::Accepted
            }
            Err(TrySendError::Full(_)) => {
                self.invalidate("encoderQueueFull");
                AudioPreparationSubmit::CandidateInvalidated
            }
            Err(TrySendError::Disconnected(_)) => {
                self.invalidate("encoderWorkerDisconnected");
                AudioPreparationSubmit::CandidateInvalidated
            }
        }
    }

    pub fn invalidate(&mut self, reason: &'static str) {
        if let Ok(mut current) = self.invalidated_reason.lock() {
            if current.is_none() {
                *current = Some(reason);
            }
        }
        // Disconnecting is nonblocking. The worker owns only bounded in-memory
        // work and will discard its partial candidate after observing the flag.
        self.sender.take();
    }

    pub fn finish(mut self) -> Result<AudioPreparationResult, AudioPreparationError> {
        self.sender.take();
        self.join_worker()
    }

    fn join_worker(&mut self) -> Result<AudioPreparationResult, AudioPreparationError> {
        let mut result = self
            .join_handle
            .take()
            .ok_or(AudioPreparationError::WorkerPanicked)?
            .join()
            .map_err(|_| AudioPreparationError::WorkerPanicked)?;
        result.submitted_segments = self.submitted_segments;
        result.submitted_pcm_bytes = self.submitted_pcm_bytes;
        Ok(result)
    }
}

impl Drop for AudioPreparationWorker {
    fn drop(&mut self) {
        if self.join_handle.is_some() {
            if let Ok(mut reason) = self.invalidated_reason.lock() {
                if reason.is_none() {
                    *reason = Some("audioPreparationWorkerDropped");
                }
            }
        }
        self.sender.take();
        if let Some(join_handle) = self.join_handle.take() {
            let _ = join_handle.join();
        }
    }
}

fn run_wav_preparation_worker(
    plan: EncoderPlan,
    artifact_target: Option<CaptureArtifactTarget>,
    receiver: Receiver<SharedPcmSegment>,
    invalidated_reason: Arc<Mutex<Option<&'static str>>>,
    start_gate: Option<Receiver<()>>,
) -> AudioPreparationResult {
    if let Some(gate) = start_gate {
        let _ = gate.recv();
    }
    if plan.implementation == EncoderImplementation::WavPcm16FileV1 {
        let Some(target) = artifact_target else {
            return failed_result(plan, "audioPreparationArtifactLeaseMissing");
        };
        return run_wav_file_preparation_worker(plan, target, receiver, invalidated_reason);
    }
    if artifact_target.is_some() {
        return failed_result(plan, "audioPreparationArtifactLeaseUnexpected");
    }
    let mut encoder = match WavPcm16VirtualEncoder::new(plan.clone()) {
        Ok(encoder) => encoder,
        Err(error) => return failed_result(plan, error.code()),
    };
    while let Ok(segment) = receiver.recv() {
        if let Some(reason) = current_invalidation(&invalidated_reason) {
            return invalidated_result(plan, reason);
        }
        if let Err(error) = encoder.push_pcm_segment(segment) {
            return invalidated_result(plan, error.code());
        }
    }
    if let Some(reason) = current_invalidation(&invalidated_reason) {
        return invalidated_result(plan, reason);
    }
    match encoder.finish() {
        Ok(virtual_wav) => AudioPreparationResult {
            state: AudioPreparationState::Ready,
            plan,
            submitted_segments: 0,
            submitted_pcm_bytes: 0,
            reason: None,
            virtual_wav: Some(virtual_wav),
            artifact: None,
        },
        Err(error) => invalidated_result(plan, error.code()),
    }
}

fn run_wav_file_preparation_worker(
    plan: EncoderPlan,
    target: CaptureArtifactTarget,
    receiver: Receiver<SharedPcmSegment>,
    invalidated_reason: Arc<Mutex<Option<&'static str>>>,
) -> AudioPreparationResult {
    let run = || -> Result<PreparedAudioArtifact, &'static str> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            .open(&target.path)
            .map_err(|_| "audioPreparationArtifactCreateFailed")?;
        file.write_all(&[0_u8; WAV_PCM16_HEADER_LEN])
            .map_err(|_| "audioPreparationArtifactWriteFailed")?;
        let mut pcm_bytes = 0_u64;
        while let Ok(segment) = receiver.recv() {
            if let Some(reason) = current_invalidation(&invalidated_reason) {
                return Err(reason);
            }
            pcm_bytes = pcm_bytes
                .checked_add(segment.len() as u64)
                .ok_or("audioPreparationPcmLimitExceeded")?;
            if pcm_bytes > plan.max_pcm_bytes {
                return Err("audioPreparationPcmLimitExceeded");
            }
            file.write_all(segment.as_slice())
                .map_err(|_| "audioPreparationArtifactWriteFailed")?;
        }
        if let Some(reason) = current_invalidation(&invalidated_reason) {
            return Err(reason);
        }
        let header = wav_pcm16_header(plan.sample_rate, plan.channels, pcm_bytes)
            .map_err(|error| error.code())?;
        file.seek(SeekFrom::Start(0))
            .map_err(|_| "audioPreparationArtifactSeekFailed")?;
        file.write_all(&header)
            .map_err(|_| "audioPreparationArtifactHeaderWriteFailed")?;
        file.flush()
            .map_err(|_| "audioPreparationArtifactFlushFailed")?;
        let byte_length = WAV_PCM16_HEADER_LEN as u64 + pcm_bytes;
        let observed_length = file
            .metadata()
            .map_err(|_| "audioPreparationArtifactMetadataFailed")?
            .len();
        if observed_length != byte_length {
            return Err("audioPreparationArtifactLengthMismatch");
        }
        drop(file);
        Ok(PreparedAudioArtifact {
            lease_id: target.lease_id.clone(),
            path: target.path.clone(),
            pcm_bytes,
            byte_length,
        })
    };

    match run() {
        Ok(artifact) => AudioPreparationResult {
            state: AudioPreparationState::Ready,
            plan,
            submitted_segments: 0,
            submitted_pcm_bytes: artifact.pcm_bytes,
            reason: None,
            virtual_wav: None,
            artifact: Some(artifact),
        },
        Err(reason) => {
            let _ = fs::remove_file(&target.path);
            invalidated_result(plan, reason)
        }
    }
}

fn current_invalidation(
    invalidated_reason: &Arc<Mutex<Option<&'static str>>>,
) -> Option<&'static str> {
    invalidated_reason.lock().ok().and_then(|reason| *reason)
}

fn invalidated_result(plan: EncoderPlan, reason: &'static str) -> AudioPreparationResult {
    AudioPreparationResult {
        state: AudioPreparationState::Invalidated,
        plan,
        submitted_segments: 0,
        submitted_pcm_bytes: 0,
        reason: Some(reason),
        virtual_wav: None,
        artifact: None,
    }
}

fn failed_result(plan: EncoderPlan, reason: &'static str) -> AudioPreparationResult {
    AudioPreparationResult {
        state: AudioPreparationState::Failed,
        plan,
        submitted_segments: 0,
        submitted_pcm_bytes: 0,
        reason: Some(reason),
        virtual_wav: None,
        artifact: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio_codec::{EncodedAudioFormat, EncoderImplementation};

    fn test_plan(queue_capacity_frames: usize, max_pcm_bytes: u64) -> EncoderPlan {
        EncoderPlan {
            format: EncodedAudioFormat::WavPcm16,
            implementation: EncoderImplementation::WavPcm16Virtual,
            sample_rate: 16_000,
            channels: 1,
            bits_per_sample: 16,
            queue_capacity_frames,
            max_pcm_bytes,
        }
    }

    fn file_plan(queue_capacity_frames: usize, max_pcm_bytes: u64) -> EncoderPlan {
        EncoderPlan {
            implementation: EncoderImplementation::WavPcm16FileV1,
            ..test_plan(queue_capacity_frames, max_pcm_bytes)
        }
    }

    fn test_artifact_target() -> CaptureArtifactTarget {
        let lease_id = uuid::Uuid::new_v4().simple().to_string();
        CaptureArtifactTarget {
            path: std::env::temp_dir().join(format!("{lease_id}.wav")),
            lease_id,
        }
    }

    #[test]
    fn bounded_worker_preserves_fragmented_pcm() {
        let mut worker = AudioPreparationWorker::start(test_plan(8, 32)).unwrap();
        assert_eq!(
            worker.try_submit(Arc::new(vec![1_u8, 2, 3])),
            AudioPreparationSubmit::Accepted
        );
        assert_eq!(
            worker.try_submit(Arc::new(vec![4_u8])),
            AudioPreparationSubmit::Accepted
        );

        let result = worker.finish().unwrap();
        assert_eq!(result.state, AudioPreparationState::Ready);
        assert_eq!(result.submitted_segments, 2);
        assert_eq!(result.submitted_pcm_bytes, 4);
        assert_eq!(result.virtual_wav.as_ref().unwrap().pcm_bytes(), 4);
    }

    #[test]
    fn bounded_worker_queue_overflow_invalidates_only_candidate() {
        let (gate_tx, gate_rx) = mpsc::channel();
        let mut worker =
            AudioPreparationWorker::start_inner(test_plan(1, 32), None, Some(gate_rx)).unwrap();
        assert_eq!(
            worker.try_submit(Arc::new(vec![1_u8, 2])),
            AudioPreparationSubmit::Accepted
        );
        assert_eq!(
            worker.try_submit(Arc::new(vec![3_u8, 4])),
            AudioPreparationSubmit::CandidateInvalidated
        );
        gate_tx.send(()).unwrap();

        let result = worker.finish().unwrap();
        assert_eq!(result.state, AudioPreparationState::Invalidated);
        assert_eq!(result.reason, Some("encoderQueueFull"));
        assert!(result.virtual_wav.is_none());
        assert_eq!(result.submitted_segments, 1);
        assert_eq!(result.submitted_pcm_bytes, 2);
    }

    #[test]
    fn pcm_limit_overflow_is_local_candidate_failure() {
        let mut worker = AudioPreparationWorker::start(test_plan(8, 4)).unwrap();
        assert_eq!(
            worker.try_submit(Arc::new(vec![0_u8; 6])),
            AudioPreparationSubmit::CandidateInvalidated
        );

        let result = worker.finish().unwrap();
        assert_eq!(result.state, AudioPreparationState::Invalidated);
        assert_eq!(result.reason, Some("audioPreparationPcmLimitExceeded"));
        assert!(result.virtual_wav.is_none());
        assert_eq!(result.submitted_pcm_bytes, 0);
    }

    #[test]
    fn production_file_worker_streams_pcm_and_patches_header() {
        let target = test_artifact_target();
        let mut worker =
            AudioPreparationWorker::start_file(file_plan(8, 32), target.clone()).unwrap();
        assert_eq!(
            worker.try_submit(Arc::new(vec![1_u8, 2, 3, 4])),
            AudioPreparationSubmit::Accepted
        );
        let result = worker.finish().unwrap();

        assert_eq!(result.state, AudioPreparationState::Ready);
        let artifact = result.artifact.as_ref().unwrap();
        assert_eq!(artifact.lease_id, target.lease_id);
        assert_eq!(artifact.pcm_bytes, 4);
        assert_eq!(artifact.byte_length, 48);
        let bytes = fs::read(&target.path).unwrap();
        assert_eq!(&bytes[0..4], b"RIFF");
        assert_eq!(u32::from_le_bytes(bytes[4..8].try_into().unwrap()), 40);
        assert_eq!(u32::from_le_bytes(bytes[40..44].try_into().unwrap()), 4);
        assert_eq!(&bytes[44..], &[1, 2, 3, 4]);
        fs::remove_file(target.path).unwrap();
    }

    #[test]
    fn production_file_worker_removes_partial_artifact_on_invalidation() {
        let target = test_artifact_target();
        let mut worker =
            AudioPreparationWorker::start_file(file_plan(8, 4), target.clone()).unwrap();
        assert_eq!(
            worker.try_submit(Arc::new(vec![0_u8; 6])),
            AudioPreparationSubmit::CandidateInvalidated
        );
        let result = worker.finish().unwrap();

        assert_eq!(result.state, AudioPreparationState::Invalidated);
        assert!(!target.path.exists());
    }
}
