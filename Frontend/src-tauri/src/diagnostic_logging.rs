//! Runtime opt-out shared by shell diagnostics and managed-backend output.
//!
//! The gate covers the write itself. Once disabling returns, no admitted
//! writer can append later. Enabled writers rotate bounded files under the
//! same gate; disabling alone retains existing files without truncation.

use std::{
    collections::HashMap,
    env,
    fs::{self, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    thread::{self, JoinHandle},
};

pub(crate) const ENABLED_ENV: &str = "SCRIBER_DIAGNOSTIC_LOGGING_ENABLED";
const MAX_LOG_BYTES: usize = 5 * 1024 * 1024;
const LOG_ARCHIVE_COUNT: usize = 3;

#[derive(Default)]
struct RotationFailure {
    notified: bool,
    dropped_bytes: u64,
    dropped_writes: u64,
    write_failures: u64,
    os_error: Option<i32>,
}

impl RotationFailure {
    fn marker(&self, recovered: bool) -> String {
        let outcome = if recovered { "recovered" } else { "blocked" };
        // Fixed fields and numeric counters only. Never echo a failed record,
        // path or OS exception; do not recurse through the diagnostic writer.
        format!(
            "\n{{\"event\":\"diagnostic.rotation.{outcome}\",\"level\":\"WARNING\",\"message\":\"Diagnostic log rotation {outcome}\",\"meta\":{{\"dropped_bytes\":{},\"dropped_writes\":{},\"write_failures\":{},\"os_error\":{}}}}}\n",
            self.dropped_bytes,
            self.dropped_writes,
            self.write_failures,
            self.os_error.unwrap_or(0)
        )
    }
}

struct DiagnosticState {
    enabled: bool,
    rotation_failures: HashMap<PathBuf, RotationFailure>,
}

struct DiagnosticGate(Mutex<DiagnosticState>);

impl DiagnosticGate {
    fn new(enabled: bool) -> Self {
        Self(Mutex::new(DiagnosticState {
            enabled,
            rotation_failures: HashMap::new(),
        }))
    }

    fn enabled(&self) -> bool {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .enabled
    }

    fn set_enabled(&self, enabled: bool) {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .enabled = enabled;
    }

    fn append(&self, path: &Path, bytes: &[u8]) -> io::Result<()> {
        self.append_with_limit(path, bytes, MAX_LOG_BYTES)
    }

    fn append_with_limit(&self, path: &Path, bytes: &[u8], max_bytes: usize) -> io::Result<()> {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if !state.enabled || bytes.is_empty() {
            return Ok(());
        }
        let max_bytes = max_bytes.max(1);
        let hard_limit = max_bytes.saturating_mul(2) as u64;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        for (index, chunk) in bytes.chunks(max_bytes).enumerate() {
            let mut current_size = match fs::metadata(path) {
                Ok(metadata) => metadata.len(),
                Err(error) if error.kind() == io::ErrorKind::NotFound => 0,
                Err(error) => return Err(error),
            };
            if current_size > 0
                && current_size.saturating_add(chunk.len() as u64) > max_bytes as u64
            {
                // A viewer can deny delete sharing on either the active file
                // or the oldest archive. Keep recent evidence up to a hard
                // ceiling while it is locked, then count rejected writes.
                if let Err(error) = rotate_log(path) {
                    let failure = state.rotation_failures.entry(path.to_owned()).or_default();
                    failure.os_error = error.raw_os_error();
                    let marker = failure.marker(false);
                    if !failure.notified
                        && current_size
                            .saturating_add(marker.len() as u64)
                            .saturating_add(chunk.len() as u64)
                            <= hard_limit
                        && append_bytes(path, marker.as_bytes()).is_ok()
                    {
                        failure.notified = true;
                        current_size += marker.len() as u64;
                    }
                    if current_size.saturating_add(chunk.len() as u64) > hard_limit {
                        failure.dropped_bytes = failure
                            .dropped_bytes
                            .saturating_add((bytes.len() - index * max_bytes) as u64);
                        failure.dropped_writes = failure.dropped_writes.saturating_add(1);
                        return Err(error);
                    }
                    if let Err(error) = append_bytes(path, chunk) {
                        // write_all may have written a prefix, so only count
                        // the failed call instead of inventing a byte count.
                        failure.write_failures = failure.write_failures.saturating_add(1);
                        return Err(error);
                    }
                    continue;
                }
                current_size = 0;
            }
            if let Some(failure) = state.rotation_failures.get(path) {
                let marker = failure.marker(true);
                if current_size
                    .saturating_add(marker.len() as u64)
                    .saturating_add(chunk.len() as u64)
                    <= hard_limit
                    && append_bytes(path, marker.as_bytes()).is_ok()
                {
                    state.rotation_failures.remove(path);
                }
            }
            append_bytes(path, chunk)?;
        }
        Ok(())
    }
}

fn append_bytes(path: &Path, bytes: &[u8]) -> io::Result<()> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?
        .write_all(bytes)
}

fn archive_path(path: &Path, index: usize) -> PathBuf {
    let mut extension = std::ffi::OsString::from(index.to_string());
    if let Some(original_extension) = path.extension() {
        extension.push(".");
        extension.push(original_extension);
    }
    // Keep the final suffix discoverable by debug logs and support bundles.
    path.with_extension(extension)
}

fn rotate_log(path: &Path) -> io::Result<()> {
    // Retain an oversized legacy file whole until normal archive eviction;
    // migrating to bounded logs must not truncate its existing history.
    let mut target = archive_path(path, 1);
    let mut oldest_modified = None;
    for index in 1..=LOG_ARCHIVE_COUNT {
        let archive = archive_path(path, index);
        let metadata = match fs::metadata(&archive) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return fs::rename(path, archive);
            }
            Err(error) => return Err(error),
        };
        let modified = metadata.modified()?;
        if oldest_modified.is_none_or(|oldest| modified < oldest) {
            oldest_modified = Some(modified);
            target = archive;
        }
    }
    // Replace the oldest slot in the rename itself, without deleting it first.
    // std::fs::rename replaces existing files (MoveFileExW with
    // MOVEFILE_REPLACE_EXISTING on Windows). If a reader denies delete sharing
    // on the active file, the failed rename leaves every archive intact.
    fs::rename(path, target)
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
    use super::{
        archive_path, drain_output, rotate_log, startup_enabled, DiagnosticGate, LOG_ARCHIVE_COUNT,
    };
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
    fn rotation_keeps_active_paths_and_discoverable_archive_suffixes() {
        let root =
            std::env::temp_dir().join(format!("scriber-log-rotation-{}", uuid::Uuid::new_v4()));
        let gate = DiagnosticGate::new(true);
        for name in ["tauri-backend.log", "backend-crash-metadata.jsonl"] {
            let path = root.join(name);
            gate.append_with_limit(&path, b"1234", 4).unwrap();
            assert!(!archive_path(&path, 1).exists());
            gate.append_with_limit(&path, b"5", 4).unwrap();
            assert_eq!(fs::read(&path).unwrap(), b"5");
            let archive = archive_path(&path, 1);
            assert_eq!(archive.extension(), path.extension());
            assert_eq!(fs::read(archive).unwrap(), b"1234");
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rotation_reuses_the_oldest_archive_without_shifting_other_slots() {
        let root =
            std::env::temp_dir().join(format!("scriber-log-retention-{}", uuid::Uuid::new_v4()));
        let path = root.join("tauri-shell.log");
        let gate = DiagnosticGate::new(true);
        gate.append_with_limit(&path, b"full", 4).unwrap();
        for (index, seconds) in [(1, 300), (2, 100), (3, 200)] {
            let archive = archive_path(&path, index);
            fs::write(&archive, format!("old{index}")).unwrap();
            let file = fs::OpenOptions::new().write(true).open(archive).unwrap();
            file.set_times(
                fs::FileTimes::new()
                    .set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(seconds)),
            )
            .unwrap();
        }
        gate.append_with_limit(&path, b"new", 4).unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"new");
        assert_eq!(fs::read(archive_path(&path, 1)).unwrap(), b"old1");
        assert_eq!(fs::read(archive_path(&path, 2)).unwrap(), b"full");
        assert_eq!(fs::read(archive_path(&path, 3)).unwrap(), b"old3");
        assert_eq!(fs::read_dir(&root).unwrap().count(), LOG_ARCHIVE_COUNT + 1);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rotation_preserves_every_archive_when_the_active_file_is_missing() {
        let root =
            std::env::temp_dir().join(format!("scriber-log-missing-{}", uuid::Uuid::new_v4()));
        let path = root.join("tauri-shell.log");
        fs::create_dir_all(&root).unwrap();
        for index in 1..=LOG_ARCHIVE_COUNT {
            fs::write(archive_path(&path, index), format!("old{index}")).unwrap();
        }
        for _ in 0..4 {
            assert_eq!(
                rotate_log(&path).unwrap_err().kind(),
                std::io::ErrorKind::NotFound
            );
        }
        for index in 1..=LOG_ARCHIVE_COUNT {
            assert_eq!(
                fs::read(archive_path(&path, index)).unwrap(),
                format!("old{index}").as_bytes()
            );
        }
        assert_eq!(fs::read_dir(&root).unwrap().count(), LOG_ARCHIVE_COUNT);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn oversized_writes_are_bounded_but_legacy_logs_are_preserved_whole() {
        let root =
            std::env::temp_dir().join(format!("scriber-log-limits-{}", uuid::Uuid::new_v4()));
        let path = root.join("tauri-backend.log");
        let gate = DiagnosticGate::new(true);
        gate.append_with_limit(&path, b"abcdefghijklm", 4).unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"m");
        assert_eq!(fs::read(archive_path(&path, 1)).unwrap(), b"abcd");
        assert_eq!(fs::read(archive_path(&path, 2)).unwrap(), b"efgh");
        assert_eq!(fs::read(archive_path(&path, 3)).unwrap(), b"ijkl");

        let legacy = root.join("legacy.log");
        fs::write(&legacy, b"oversized existing history").unwrap();
        gate.set_enabled(false);
        gate.append_with_limit(&legacy, b"ignored", 4).unwrap();
        assert!(!archive_path(&legacy, 1).exists());
        assert_eq!(fs::read(&legacy).unwrap(), b"oversized existing history");
        gate.set_enabled(true);
        gate.append_with_limit(&legacy, b"new", 4).unwrap();
        assert_eq!(fs::read(&legacy).unwrap(), b"new");
        assert_eq!(
            fs::read(archive_path(&legacy, 1)).unwrap(),
            b"oversized existing history"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn concurrent_writers_keep_rotation_and_append_in_the_same_gate() {
        let root =
            std::env::temp_dir().join(format!("scriber-log-writers-{}", uuid::Uuid::new_v4()));
        let path = root.join("tauri-backend.log");
        let gate = Arc::new(DiagnosticGate::new(true));
        let writers: Vec<_> = (0..8)
            .map(|_| {
                let gate = Arc::clone(&gate);
                let path = path.clone();
                std::thread::spawn(move || {
                    for _ in 0..8 {
                        gate.append_with_limit(&path, b"record\n", 7).unwrap();
                    }
                })
            })
            .collect();
        for writer in writers {
            writer.join().unwrap();
        }
        assert_eq!(fs::read_dir(&root).unwrap().count(), LOG_ARCHIVE_COUNT + 1);
        for entry in fs::read_dir(&root).unwrap() {
            assert_eq!(fs::read(entry.unwrap().path()).unwrap(), b"record\n");
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn active_reader_keeps_recent_records_until_the_hard_limit_and_reports_recovery() {
        assert_blocked_rotation_is_bounded_and_observable(true);
    }

    #[cfg(windows)]
    #[test]
    fn archive_reader_keeps_recent_records_until_the_hard_limit_and_reports_recovery() {
        assert_blocked_rotation_is_bounded_and_observable(false);
    }

    #[cfg(windows)]
    fn assert_blocked_rotation_is_bounded_and_observable(lock_active: bool) {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION};
        use windows_sys::Win32::Storage::FileSystem::{FILE_SHARE_READ, FILE_SHARE_WRITE};

        let root =
            std::env::temp_dir().join(format!("scriber-log-locked-{}", uuid::Uuid::new_v4()));
        let path = root.join("tauri-backend.log");
        let gate = DiagnosticGate::new(true);
        let limit = 512;
        gate.append_with_limit(&path, &vec![b'f'; limit], limit)
            .unwrap();
        for index in 1..=LOG_ARCHIVE_COUNT {
            let archive = archive_path(&path, index);
            fs::write(&archive, format!("old{index}")).unwrap();
            fs::OpenOptions::new()
                .write(true)
                .open(archive)
                .unwrap()
                .set_times(fs::FileTimes::new().set_modified(
                    std::time::UNIX_EPOCH + std::time::Duration::from_secs(index as u64),
                ))
                .unwrap();
        }
        let locked_path = if lock_active {
            path.clone()
        } else {
            archive_path(&path, 1)
        };
        let reader = fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .open(locked_path)
            .unwrap();
        gate.append_with_limit(&path, b"crash-marker", limit)
            .unwrap();
        let mut rejected = 0;
        for _ in 0..12 {
            if let Err(error) = gate.append_with_limit(&path, &[b'x'; 128], limit) {
                // MoveFileExW/its readonly fallback can report AccessDenied
                // for a locked replacement target, or SharingViolation.
                assert!(matches!(
                    error.raw_os_error(),
                    Some(code) if code == ERROR_SHARING_VIOLATION as i32
                        || code == ERROR_ACCESS_DENIED as i32
                ));
                rejected += 1;
            }
        }
        assert!(rejected > 0);
        let active = fs::read_to_string(&path).unwrap();
        assert!(active.len() <= limit * 2);
        assert!(active.starts_with(&"f".repeat(limit)));
        assert!(active.contains("crash-marker"));
        assert_eq!(
            active
                .matches("\"event\":\"diagnostic.rotation.blocked\"")
                .count(),
            1
        );
        for index in 1..=LOG_ARCHIVE_COUNT {
            assert_eq!(
                fs::read(archive_path(&path, index)).unwrap(),
                format!("old{index}").as_bytes()
            );
        }
        assert_eq!(fs::read_dir(&root).unwrap().count(), LOG_ARCHIVE_COUNT + 1);
        drop(reader);
        // A full-sized record still fits: the optional recovery marker must
        // not consume the space reserved for the caller's payload.
        gate.append_with_limit(&path, &vec![b'n'; limit], limit)
            .unwrap();
        let recovered = fs::read_to_string(&path).unwrap();
        assert!(recovered.ends_with(&"n".repeat(limit)));
        assert!(recovered.len() <= limit * 2);
        assert!(recovered.contains("\"event\":\"diagnostic.rotation.recovered\""));
        assert!(recovered.contains(&format!("\"dropped_bytes\":{}", rejected * 128)));
        assert!(recovered.contains(&format!("\"dropped_writes\":{rejected}")));
        assert_eq!(fs::read_to_string(archive_path(&path, 1)).unwrap(), active);
        assert!(gate.0.lock().unwrap().rotation_failures.is_empty());
        assert_eq!(fs::read_dir(&root).unwrap().count(), LOG_ARCHIVE_COUNT + 1);
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
