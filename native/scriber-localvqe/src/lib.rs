//! The single product model: LocalVQE v1.3, CPU only, 16 kHz / 256 samples.
use std::{ffi::CString, io::Write, marker::PhantomData, rc::Rc};

pub const HOP: usize = 256;
pub const MODEL_VERSION: &str = "1.3";
pub const MODEL_SHA256: &str = "c4f7912485c32cfc206c536f2f050b52513f2f613fdbc616391f6b26ab1d51ec";
const MODEL: &[u8] = include_bytes!(env!("SCRIBER_LOCALVQE_MODEL"));

pub fn cpu_supported() -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        std::is_x86_feature_detected!("avx2")
            && std::is_x86_feature_detected!("fma")
            && std::is_x86_feature_detected!("f16c")
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        true
    }
}

extern "C" {
    fn scriber_vqe_new(path: *const std::ffi::c_char, threads: i32) -> usize;
    fn scriber_vqe_process(ctx: usize, mic: *const f32, render: *const f32, out: *mut f32) -> i32;
    fn scriber_vqe_free(ctx: usize);
}

/// Owned and used by one relay thread. The native mutable graph is neither Send nor Sync.
pub struct Model {
    handle: usize,
    _thread_bound: PhantomData<Rc<()>>,
}

impl Model {
    pub fn new() -> Result<Self, String> {
        if !cpu_supported() {
            return Err("LocalVQE requires an AVX2/FMA/F16C-capable CPU".into());
        }
        // The file-only upstream loader copies every weight into its own CPU buffer.
        // Embed the verified bytes in the executable, materialize a private temporary
        // file only during construction, then delete it. No network/cache at runtime.
        let mut file = tempfile::Builder::new()
            .prefix("scriber-localvqe-")
            .suffix(".gguf")
            .tempfile()
            .map_err(|_| "LocalVQE model staging failed")?;
        file.write_all(MODEL)
            .map_err(|_| "LocalVQE model staging failed")?;
        file.flush().map_err(|_| "LocalVQE model staging failed")?;
        let path = CString::new(
            file.path()
                .to_str()
                .ok_or("LocalVQE model path is not UTF-8")?,
        )
        .map_err(|_| "LocalVQE invalid model path")?;
        let threads = std::thread::available_parallelism().map_or(1, |n| n.get().min(4)) as i32;
        // SAFETY: the NUL-terminated path lives throughout construction; the loader
        // owns the weights before returning. Only this wrapper owns the handle.
        let handle = unsafe { scriber_vqe_new(path.as_ptr(), threads) };
        if handle == 0 {
            return Err("LocalVQE v1.3 initialization failed".into());
        }
        Ok(Self {
            handle,
            _thread_bound: PhantomData,
        })
    }

    pub fn process(
        &mut self,
        mic: &[f32; HOP],
        render: &[f32; HOP],
        out: &mut [f32; HOP],
    ) -> Result<(), String> {
        if mic.iter().chain(render).any(|sample| !sample.is_finite()) {
            return Err("LocalVQE non-finite input".into());
        }
        // SAFETY: valid exclusive context and exact, live, non-overlapping hop buffers.
        let status = unsafe {
            scriber_vqe_process(self.handle, mic.as_ptr(), render.as_ptr(), out.as_mut_ptr())
        };
        if status != 0 || out.iter().any(|sample| !sample.is_finite()) {
            return Err("LocalVQE processing failed".into());
        }
        Ok(())
    }
}

impl Drop for Model {
    fn drop(&mut self) {
        // SAFETY: this is the sole owner and no native calls outlive this value.
        unsafe { scriber_vqe_free(self.handle) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn floats(bytes: &[u8]) -> Vec<f32> {
        bytes
            .chunks_exact(4)
            .map(|b| f32::from_le_bytes(b.try_into().unwrap()))
            .collect()
    }

    #[test]
    fn pinned_upstream_inference_parity_and_finite_output() {
        let inputs = floats(include_bytes!(concat!(
            env!("SCRIBER_LOCALVQE_FIXTURES"),
            "/regression_input.f32"
        )));
        let expected = floats(include_bytes!(concat!(
            env!("SCRIBER_LOCALVQE_FIXTURES"),
            "/localvqe-v1.3-4.8M-f32.out.f32"
        )));
        let n = expected.len();
        assert_eq!(inputs.len(), n * 2);
        let mut model = Model::new().unwrap();
        let started = std::time::Instant::now();
        let mut max_error = 0.0_f32;
        for start in (0..n - HOP).step_by(HOP) {
            let mic: &[f32; HOP] = inputs[start..start + HOP].try_into().unwrap();
            let render: &[f32; HOP] = inputs[n + start..n + start + HOP].try_into().unwrap();
            let mut out = [0.0; HOP];
            model.process(mic, render, &mut out).unwrap();
            for (&actual, &reference) in out.iter().zip(&expected[start..start + HOP]) {
                max_error = max_error.max((actual - reference).abs());
                assert!(
                    (actual - reference).abs() <= 1e-3 + reference.abs() * 1e-2,
                    "reference={reference}, actual={actual}"
                );
            }
        }
        eprintln!(
            "LocalVQE: 992 ms audio processed in {:?}; maximum reference error={max_error}",
            started.elapsed()
        );
        assert!(model
            .process(&[f32::NAN; HOP], &[0.0; HOP], &mut [0.0; HOP])
            .is_err());
    }
}
