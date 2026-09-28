//! LocalVQE v1.3 streaming adapter. Transport is 10 ms; inference is 16 ms.
//!
//! Drop the native one-hop analysis pre-roll, retain original timestamps in the
//! relay, and flush one zero hop at EOF. This avoids shifting clean speech or
//! dropping the final partial hop. The model estimates acoustic delay itself.
use scriber_localvqe::{Model, HOP};
use std::collections::VecDeque;

pub const MEETING_AEC_FRAME_SAMPLES: usize = 480;
pub const MEETING_OUTPUT_SAMPLES: usize = 160;

pub struct MeetingEnhancer {
    model: Model,
    stream: HopAdapter,
}

impl MeetingEnhancer {
    pub fn new() -> Result<Self, String> {
        Ok(Self {
            model: Model::new()?,
            stream: HopAdapter::default(),
        })
    }

    pub fn push(&mut self, render: &[i16], mic: &[i16]) -> Result<(), String> {
        self.stream.push(render, mic, |mic, render, out| {
            self.model.process(mic, render, out)
        })
    }

    pub fn pop(&mut self, out: &mut Vec<i16>) -> bool {
        self.stream.pop(out)
    }

    pub fn finish(&mut self) -> Result<(), String> {
        self.stream
            .finish(|mic, render, out| self.model.process(mic, render, out))
    }
}

struct HopAdapter {
    mic: [f32; HOP],
    render: [f32; HOP],
    used: usize,
    primed: bool,
    accepted: u64,
    produced: u64,
    output: VecDeque<i16>,
    finished: bool,
}

impl Default for HopAdapter {
    fn default() -> Self {
        Self {
            mic: [0.0; HOP],
            render: [0.0; HOP],
            used: 0,
            primed: false,
            accepted: 0,
            produced: 0,
            output: VecDeque::with_capacity(HOP * 3),
            finished: false,
        }
    }
}

impl HopAdapter {
    fn hop(
        &mut self,
        process: &mut impl FnMut(&[f32; HOP], &[f32; HOP], &mut [f32; HOP]) -> Result<(), String>,
    ) -> Result<(), String> {
        let mut output = [0.0; HOP];
        process(&self.mic, &self.render, &mut output)?;
        if self.primed {
            let count = (self.accepted - self.produced).min(HOP as u64) as usize;
            self.output.extend(output[..count].iter().map(|s| {
                (s.clamp(-1.0, 1.0) * 32768.0)
                    .round()
                    .clamp(-32768.0, 32767.0) as i16
            }));
            self.produced += count as u64;
        } else {
            self.primed = true;
        }
        self.used = 0;
        Ok(())
    }

    fn push(
        &mut self,
        render: &[i16],
        mic: &[i16],
        mut process: impl FnMut(&[f32; HOP], &[f32; HOP], &mut [f32; HOP]) -> Result<(), String>,
    ) -> Result<(), String> {
        if self.finished
            || render.len() != MEETING_OUTPUT_SAMPLES
            || mic.len() != MEETING_OUTPUT_SAMPLES
        {
            return Err(
                "LocalVQE requires an active stream and 160 samples per 10 ms frame".into(),
            );
        }
        // A consumer must drain each push. Never grow with meeting duration.
        if self.output.len() >= MEETING_OUTPUT_SAMPLES {
            return Err("LocalVQE output was not drained".into());
        }
        self.accepted += mic.len() as u64;
        for (&mic, &render) in mic.iter().zip(render) {
            self.mic[self.used] = f32::from(mic) / 32768.0;
            self.render[self.used] = f32::from(render) / 32768.0;
            self.used += 1;
            if self.used == HOP {
                self.hop(&mut process)?;
            }
        }
        Ok(())
    }

    fn pop(&mut self, out: &mut Vec<i16>) -> bool {
        if self.output.len() < MEETING_OUTPUT_SAMPLES {
            return false;
        }
        out.clear();
        out.extend(self.output.drain(..MEETING_OUTPUT_SAMPLES));
        true
    }

    fn finish(
        &mut self,
        mut process: impl FnMut(&[f32; HOP], &[f32; HOP], &mut [f32; HOP]) -> Result<(), String>,
    ) -> Result<(), String> {
        if self.finished {
            return Ok(());
        }
        if self.used > 0 {
            self.mic[self.used..].fill(0.0);
            self.render[self.used..].fill(0.0);
            self.hop(&mut process)?;
        }
        if self.produced < self.accepted {
            self.mic.fill(0.0);
            self.render.fill(0.0);
            self.hop(&mut process)?;
        }
        self.finished = true;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reblocking_preserves_every_sample_at_all_eof_offsets() {
        // A one-hop delayed identity emulates native analysis/synthesis latency.
        // Test every 10/16ms alignment, short streams, and a long recording.
        for frames in (0..24).chain([10_003]) {
            let mut stream = HopAdapter::default();
            let mut previous = [0.0; HOP];
            let mut process = |mic: &[f32; HOP], _: &[f32; HOP], out: &mut [f32; HOP]| {
                *out = previous;
                previous = *mic;
                Ok(())
            };
            let mut expected = Vec::new();
            let mut actual = Vec::new();
            let mut output = Vec::new();
            for frame in 0..frames {
                let mic: Vec<i16> = (0..MEETING_OUTPUT_SAMPLES)
                    .map(|i| ((frame * 160 + i) % 60001) as i32 - 30000)
                    .map(|i| i as i16)
                    .collect();
                expected.extend_from_slice(&mic);
                stream
                    .push(&[0; MEETING_OUTPUT_SAMPLES], &mic, &mut process)
                    .unwrap();
                while stream.pop(&mut output) {
                    actual.extend_from_slice(&output);
                }
                assert!(stream.output.len() < MEETING_OUTPUT_SAMPLES);
                assert!(stream.accepted - stream.produced <= (HOP * 2) as u64);
            }
            stream.finish(&mut process).unwrap();
            stream.finish(&mut process).unwrap();
            while stream.pop(&mut output) {
                actual.extend_from_slice(&output);
            }
            assert_eq!(actual, expected, "length/alignment at {frames} frames");
            assert!(stream.output.is_empty());
            assert!(stream.push(&[0; 160], &[0; 160], &mut process).is_err());
        }
    }

    #[test]
    fn malformed_frames_and_processing_errors_are_visible() {
        let mut stream = HopAdapter::default();
        assert!(stream.push(&[0; 10], &[0; 160], |_, _, _| Ok(())).is_err());
        stream.push(&[0; 160], &[0; 160], |_, _, _| Ok(())).unwrap();
        assert!(stream
            .push(&[0; 160], &[0; 160], |_, _, _| Err(
                "inference failed".into()
            ))
            .is_err());
    }

    #[test]
    fn actual_model_streams_and_flushes_short_capture() {
        let mut enhancer = MeetingEnhancer::new().unwrap();
        let mut output = Vec::new();
        enhancer.push(&[0; 160], &[0; 160]).unwrap();
        assert!(!enhancer.pop(&mut output));
        enhancer.finish().unwrap();
        assert!(enhancer.pop(&mut output));
        assert_eq!(output.len(), 160);
        assert!(output.iter().all(|s| s.abs() <= 2));
        assert!(!enhancer.pop(&mut output));
    }
}
