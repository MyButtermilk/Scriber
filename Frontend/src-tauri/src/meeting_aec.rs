//! Pinned pure-Rust WebRTC AEC3 adapter for 10 ms meeting frames.

use aec3::{
    nodes::audio::AudioFormat,
    pipelines::linear::{self, LinearPipeline},
};

pub const MEETING_AEC_SAMPLE_RATE: u32 = 48_000;
pub const MEETING_AEC_FRAME_SAMPLES: usize = 480;

pub struct MeetingAec3 {
    pipeline: LinearPipeline,
    render: Vec<f32>,
    capture: Vec<f32>,
    output: Vec<f32>,
}

impl MeetingAec3 {
    pub fn new(initial_delay_ms: i32) -> Result<Self, String> {
        let format = AudioFormat::ten_ms(MEETING_AEC_SAMPLE_RATE, 1);
        let pipeline = linear::builder(format, format)
            .initial_delay_ms(initial_delay_ms.clamp(0, 500))
            .enable_high_pass_filter(false)
            .enable_noise_suppression(false)
            .enable_gain_controller2(false)
            .build()
            .map_err(|error| format!("meeting AEC3 initialization failed: {error}"))?;
        Ok(Self {
            pipeline,
            render: vec![0.0; MEETING_AEC_FRAME_SAMPLES],
            capture: vec![0.0; MEETING_AEC_FRAME_SAMPLES],
            output: vec![0.0; MEETING_AEC_FRAME_SAMPLES],
        })
    }

    #[cfg(test)]
    pub fn process(&mut self, render_pcm: &[i16], capture_pcm: &[i16]) -> Result<Vec<i16>, String> {
        let mut output = Vec::with_capacity(MEETING_AEC_FRAME_SAMPLES);
        self.process_into(render_pcm, capture_pcm, &mut output)?;
        Ok(output)
    }

    pub fn process_into(
        &mut self,
        render_pcm: &[i16],
        capture_pcm: &[i16],
        output_pcm: &mut Vec<i16>,
    ) -> Result<(), String> {
        if render_pcm.len() != MEETING_AEC_FRAME_SAMPLES
            || capture_pcm.len() != MEETING_AEC_FRAME_SAMPLES
        {
            return Err(format!(
                "meeting AEC3 requires {MEETING_AEC_FRAME_SAMPLES} samples per 10 ms frame"
            ));
        }
        for (target, sample) in self.render.iter_mut().zip(render_pcm) {
            *target = f32::from(*sample) / 32768.0;
        }
        for (target, sample) in self.capture.iter_mut().zip(capture_pcm) {
            *target = f32::from(*sample) / 32768.0;
        }
        self.pipeline
            .handle_render_frame(&self.render)
            .map_err(|error| format!("meeting AEC3 render processing failed: {error}"))?;
        let produced = self
            .pipeline
            .process_capture_frame(&self.capture, &mut self.output)
            .map_err(|error| format!("meeting AEC3 capture processing failed: {error}"))?;
        if !produced {
            return Err("meeting AEC3 did not produce a capture frame".to_string());
        }
        output_pcm.clear();
        output_pcm.extend(
            self.output
                .iter()
                .map(|sample| (sample.clamp(-1.0, 1.0) * 32767.0).round() as i16),
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    #[test]
    fn aec3_adapter_processes_exact_ten_ms_frames() {
        let mut processor = MeetingAec3::new(80).expect("AEC3 should initialize");
        let render = vec![0i16; MEETING_AEC_FRAME_SAMPLES];
        let capture = vec![1_000i16; MEETING_AEC_FRAME_SAMPLES];
        let output = processor
            .process(&render, &capture)
            .expect("AEC3 should produce output");
        assert_eq!(output.len(), MEETING_AEC_FRAME_SAMPLES);
    }

    #[test]
    fn aec3_adapter_rejects_non_ten_ms_frames() {
        let mut processor = MeetingAec3::new(80).expect("AEC3 should initialize");
        assert!(processor.process(&[0; 10], &[0; 10]).is_err());
    }

    #[test]
    fn aec3_process_into_reuses_the_caller_buffer() {
        let mut processor = MeetingAec3::new(80).expect("AEC3 should initialize");
        let render = vec![0i16; MEETING_AEC_FRAME_SAMPLES];
        let capture = vec![1_000i16; MEETING_AEC_FRAME_SAMPLES];
        let mut output = Vec::with_capacity(MEETING_AEC_FRAME_SAMPLES);
        processor
            .process_into(&render, &capture, &mut output)
            .expect("first AEC3 frame");
        let allocation = output.as_ptr();
        for _ in 0..1_000 {
            processor
                .process_into(&render, &capture, &mut output)
                .expect("reused AEC3 frame");
            assert_eq!(output.as_ptr(), allocation);
            assert_eq!(output.len(), MEETING_AEC_FRAME_SAMPLES);
        }
    }

    fn deterministic_render(seed: &mut u32) -> Vec<i16> {
        (0..MEETING_AEC_FRAME_SAMPLES)
            .map(|_| {
                *seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                ((*seed >> 16) as i16) / 3
            })
            .collect()
    }

    fn energy(samples: &[i16]) -> f64 {
        samples
            .iter()
            .map(|sample| f64::from(*sample).powi(2))
            .sum::<f64>()
            / samples.len().max(1) as f64
    }

    #[test]
    fn aec3_measurably_attenuates_delayed_render_echo() {
        let mut processor = MeetingAec3::new(80).expect("AEC3 should initialize");
        let mut history = VecDeque::from(vec![vec![0i16; MEETING_AEC_FRAME_SAMPLES]; 8]);
        let mut seed = 7u32;
        let mut input_energy = 0.0;
        let mut output_energy = 0.0;
        for frame_index in 0..900 {
            let render = deterministic_render(&mut seed);
            let delayed = history.pop_front().unwrap();
            history.push_back(render.clone());
            let capture: Vec<i16> = delayed
                .iter()
                .map(|sample| (*sample as f32 * 0.55) as i16)
                .collect();
            let output = processor.process(&render, &capture).expect("AEC3 frame");
            if frame_index >= 700 {
                input_energy += energy(&capture);
                output_energy += energy(&output);
            }
        }
        assert!(
            output_energy < input_energy * 0.35,
            "AEC3 residual ratio was {:.3}",
            output_energy / input_energy
        );
    }

    #[test]
    fn aec3_preserves_near_end_voice_during_double_talk() {
        let mut processor = MeetingAec3::new(80).expect("AEC3 should initialize");
        let mut history = VecDeque::from(vec![vec![0i16; MEETING_AEC_FRAME_SAMPLES]; 8]);
        let mut seed = 11u32;
        let mut local_energy = 0.0;
        let mut output_energy = 0.0;
        for frame_index in 0..900 {
            let render = deterministic_render(&mut seed);
            let delayed = history.pop_front().unwrap();
            history.push_back(render.clone());
            let local: Vec<i16> = (0..MEETING_AEC_FRAME_SAMPLES)
                .map(|sample| {
                    let phase = ((frame_index * MEETING_AEC_FRAME_SAMPLES + sample) as f32)
                        * 2.0
                        * std::f32::consts::PI
                        * 220.0
                        / MEETING_AEC_SAMPLE_RATE as f32;
                    (phase.sin() * 4_000.0) as i16
                })
                .collect();
            let capture: Vec<i16> = delayed
                .iter()
                .zip(&local)
                .map(|(echo, voice)| ((*echo as f32 * 0.55) as i16).saturating_add(*voice))
                .collect();
            let output = processor.process(&render, &capture).expect("AEC3 frame");
            if frame_index >= 700 {
                local_energy += energy(&local);
                output_energy += energy(&output);
            }
        }
        assert!(
            output_energy > local_energy * 0.25,
            "AEC3 removed too much near-end energy: {:.3}",
            output_energy / local_energy
        );
    }
}
