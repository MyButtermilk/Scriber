//! Runtime opt-out shared by shell diagnostics and managed-backend output.
//!
//! The gate covers the write itself. Once disabling returns, no admitted
//! writer can append later; existing files are retained without truncation.

use std::{
    env,
    fs::{self, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    thread::{self, JoinHandle},
};

pub(crate) const ENABLED_ENV: &str = "SCRIBER_DIAGNOSTIC_LOGGING_ENABLED";

struct DiagnosticGate(Mutex<bool>);

impl DiagnosticGate {
    fn new(enabled: bool) -> Self {
        Self(Mutex::new(enabled))
    }

    fn enabled(&self) -> bool {
        *self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn set_enabled(&self, enabled: bool) {
        *self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = enabled;
    }

    fn append(&self, path: &Path, bytes: &[u8]) -> io::Result<()> {
        let enabled = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if !*enabled {
            return Ok(());
        }
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?
            .write_all(bytes)
    }
}

fn gate() -> &'static DiagnosticGate {
    static GATE: OnceLock<DiagnosticGate> = OnceLock::new();
    GATE.get_or_init(|| {
        let process_override = env::var(ENABLED_ENV).ok();
        let settings = fs::read_to_string(super::scriber_data_dir().join(".env")).ok();
        DiagnosticGate::new(startup_enabled(
            process_override.as_deref(),
            settings.as_deref(),
        ))
    })
}

fn startup_enabled(process_override: Option<&str>, settings: Option<&str>) -> bool {
    let persisted = settings.and_then(|text| {
        text.lines().rev().find_map(|line| {
            let (key, value) = line.trim().split_once('=')?;
            (key.trim() == ENABLED_ENV).then_some(value.trim())
        })
    });
    let value = process_override
        .or(persisted)
        .unwrap_or("1")
        .trim()
        .trim_matches(['\'', '"']);
    !matches!(
        value.to_ascii_lowercase().as_str(),
        "0" | "false" | "off" | "no"
    )
}

pub(crate) fn enabled() -> bool {
    gate().enabled()
}

pub(crate) fn set_enabled(enabled: bool) {
    gate().set_enabled(enabled);
}

pub(crate) fn append(path: &Path, bytes: &[u8]) {
    let _ = gate().append(path, bytes);
}

pub(crate) fn forward_backend_output(
    reader: impl Read + Send + 'static,
    path: PathBuf,
) -> io::Result<JoinHandle<()>> {
    // The child owns the pipe lifetime. Always drain it, including while
    // disabled, so logging preferences cannot block backend work on a full pipe.
    thread::Builder::new()
        .name("scriber-backend-output".to_string())
        .spawn(move || {
            drain_output(reader, |bytes| append(&path, bytes));
        })
}

fn drain_output(mut reader: impl Read, mut sink: impl FnMut(&[u8])) {
    let mut buffer = [0u8; 8192];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => sink(&buffer[..count]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{drain_output, startup_enabled, DiagnosticGate};
    use std::{fs, sync::Arc};

    #[test]
    fn startup_uses_persisted_opt_out_before_any_log_is_opened() {
        assert!(startup_enabled(None, None));
        assert!(!startup_enabled(
            None,
            Some("OTHER=value\nSCRIBER_DIAGNOSTIC_LOGGING_ENABLED='0'\n")
        ));
        assert!(!startup_enabled(
            Some("false"),
            Some("SCRIBER_DIAGNOSTIC_LOGGING_ENABLED=1")
        ));
        assert!(startup_enabled(
            Some("1"),
            Some("SCRIBER_DIAGNOSTIC_LOGGING_ENABLED=0")
        ));
    }

    #[test]
    fn disabling_keeps_existing_logs_and_admits_no_later_writes() {
        let root = std::env::temp_dir().join(format!("scriber-log-gate-{}", uuid::Uuid::new_v4()));
        let path = root.join("logs").join("probe.log");
        let gate = Arc::new(DiagnosticGate::new(false));
        gate.append(&path, b"hidden").unwrap();
        assert!(!root.exists());
        gate.set_enabled(true);
        gate.append(&path, b"kept").unwrap();
        gate.set_enabled(false);
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let gate = Arc::clone(&gate);
                let path = path.clone();
                std::thread::spawn(move || gate.append(&path, b"must not be written").unwrap())
            })
            .collect();
        for thread in threads {
            thread.join().unwrap();
        }
        assert_eq!(fs::read(&path).unwrap(), b"kept");
        gate.set_enabled(true);
        gate.append(&path, b" resumed").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"kept resumed");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[ignore = "child process fixture; invoked only by the pipe lifecycle test"]
    fn backend_pipe_fixture() {
        use std::io::Write;
        assert_eq!(
            std::env::var("SCRIBER_LOG_PIPE_FIXTURE").as_deref(),
            Ok("1")
        );
        let mut output = std::io::stdout().lock();
        for _ in 0..2048 {
            output.write_all(&[b'x'; 1024]).unwrap();
        }
        output.flush().unwrap();
        // The owning test must terminate this exact child, then observe EOF.
        std::thread::sleep(std::time::Duration::from_secs(10));
    }

    #[test]
    fn disabled_backend_output_drains_full_pipe_and_exits_when_child_is_killed() {
        use std::{
            process::{Command, Stdio},
            sync::mpsc,
            time::Duration,
        };
        let root = std::env::temp_dir().join(format!("scriber-log-pipe-{}", uuid::Uuid::new_v4()));
        let path = root.join("must-not-exist.log");
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "diagnostic_logging::tests::backend_pipe_fixture",
                "--ignored",
                "--nocapture",
            ])
            .env("SCRIBER_LOG_PIPE_FIXTURE", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let reader = child.stdout.take().unwrap();
        let gate = DiagnosticGate::new(false);
        let (progress_send, progress_recv) = mpsc::channel();
        let (done_send, done_recv) = mpsc::channel();
        let output = std::thread::spawn(move || {
            let mut total = 0;
            drain_output(reader, |bytes| {
                gate.append(&path, bytes).unwrap();
                total += bytes.len();
                if total >= 2 * 1024 * 1024 {
                    let _ = progress_send.send(total);
                }
            });
            let _ = done_send.send(());
        });
        let progress = progress_recv.recv_timeout(Duration::from_secs(5));
        child.kill().unwrap();
        child.wait().unwrap();
        let completed = done_recv.recv_timeout(Duration::from_secs(2));
        assert!(
            progress.is_ok(),
            "disabled logging must continuously drain a full pipe"
        );
        assert!(
            completed.is_ok(),
            "child termination must release the output reader"
        );
        output.join().unwrap();
        assert!(!root.exists());
    }
}
