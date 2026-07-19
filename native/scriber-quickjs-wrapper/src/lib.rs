use serde_json::Value;
use sha2::{Digest, Sha256};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const PROTOCOL: &str = "ScriberYtDlpQuickJsFileV1";
pub const QUICKJS_VERSION: &str = "0.15.0";
pub const ENGINE_FILE_NAME: &str = "qjs-engine.exe";
pub const ENGINE_LENGTH: u64 = 1_800_265;
pub const ENGINE_SHA256: &str = "f76c7df5a1153b7b8baf5befe3d2621e4a5508c739f9e9eee51a32988d62547e";
pub const MAX_SCRIPT_BYTES: usize = 32 * 1024 * 1024;
pub const MAX_STDOUT_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_STDERR_BYTES: usize = 256 * 1024;
pub const ENGINE_TIMEOUT: Duration = Duration::from_secs(45);
pub const TEST_ENGINE_TIMEOUT: Duration = Duration::from_millis(250);

const ENGINE_MEMORY_LIMIT_KIB: &str = "262144";
const ENGINE_STACK_LIMIT_KIB: &str = "4096";
const ENGINE_TIMEOUT_CLEANED_MESSAGE: &str = "engine timeout exceeded after child cleanup";
const CAPABILITY_SELF_TEST_SCRIPT: &[u8] = br#"
const keyword = "im" + "port";
const denied = async (loader) => {
  try {
    await loader();
    return false;
  } catch (_) {
    return true;
  }
};
(async () => {
  const globalsAbsent =
    typeof globalThis.std === "undefined" &&
    typeof globalThis.os === "undefined" &&
    typeof globalThis.bjson === "undefined" &&
    typeof globalThis.loadScript === "undefined" &&
    typeof globalThis.process === "undefined" &&
    typeof globalThis.require === "undefined";
  const importsDenied =
    await denied(() => import("qjs:std")) &&
    await denied(() => import("qjs:os")) &&
    await denied(() => import("./missing.js")) &&
    await denied(() => (0, eval)(keyword + "('qjs:std')")) &&
    await denied(() => new Function("return " + keyword + "('qjs:os')")());
  console.log(JSON.stringify(
    globalsAbsent && importsDenied
      ? { type: "result", responses: [] }
      : { type: "error", error: "capability boundary unavailable" }
  ));
})();
"#;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WrapperError {
    code: u8,
    message: &'static str,
}

impl WrapperError {
    const fn new(code: u8, message: &'static str) -> Self {
        Self { code, message }
    }

    pub const fn exit_code(self) -> u8 {
        self.code
    }

    pub const fn message(self) -> &'static str {
        self.message
    }
}

fn input_error(message: &'static str) -> WrapperError {
    WrapperError::new(65, message)
}

fn engine_error(message: &'static str) -> WrapperError {
    WrapperError::new(70, message)
}

fn output_error(message: &'static str) -> WrapperError {
    WrapperError::new(66, message)
}

fn sha256_file(path: &Path) -> Result<(u64, String), WrapperError> {
    let mut file = File::open(path).map_err(|_| engine_error("engine is unavailable"))?;
    let mut hasher = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|_| engine_error("engine cannot be verified"))?;
        if count == 0 {
            break;
        }
        total = total
            .checked_add(count as u64)
            .ok_or_else(|| engine_error("engine length is invalid"))?;
        if total > ENGINE_LENGTH {
            return Err(engine_error("engine identity differs from the lock"));
        }
        hasher.update(&buffer[..count]);
    }
    Ok((total, format!("{:x}", hasher.finalize())))
}

pub fn verify_engine(path: &Path) -> Result<(), WrapperError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| engine_error("engine is unavailable"))?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(engine_error("engine path is not a plain file"));
    }
    let (length, sha256) = sha256_file(path)?;
    if length != ENGINE_LENGTH || sha256 != ENGINE_SHA256 {
        return Err(engine_error("engine identity differs from the lock"));
    }
    Ok(())
}

pub fn read_script(path: &Path) -> Result<Vec<u8>, WrapperError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| input_error("script is unavailable"))?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(input_error("script path is not a plain file"));
    }
    if metadata.len() > MAX_SCRIPT_BYTES as u64 {
        return Err(input_error("script limit exceeded"));
    }
    let file = File::open(path).map_err(|_| input_error("script cannot be read"))?;
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take((MAX_SCRIPT_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| input_error("script cannot be read"))?;
    if bytes.len() > MAX_SCRIPT_BYTES {
        return Err(input_error("script limit exceeded"));
    }
    std::str::from_utf8(&bytes).map_err(|_| input_error("script is not valid UTF-8"))?;
    Ok(bytes)
}

struct ScratchDirectory {
    path: PathBuf,
}

impl ScratchDirectory {
    fn create() -> Result<Self, WrapperError> {
        let base = env::temp_dir();
        let seed = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        for attempt in 0..64_u32 {
            let path = base.join(format!(
                "scriber-qjs-{}-{seed:032x}-{attempt:02x}",
                std::process::id()
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Ok(Self { path }),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(_) => return Err(input_error("private script workspace is unavailable")),
            }
        }
        Err(input_error("private script workspace is unavailable"))
    }
}

impl Drop for ScratchDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

#[cfg(windows)]
struct ChildJob {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
impl ChildJob {
    fn attach(child: &Child) -> Result<Self, WrapperError> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOB_OBJECT_LIMIT_PROCESS_MEMORY,
        };

        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err(engine_error("engine job boundary is unavailable"));
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | JOB_OBJECT_LIMIT_PROCESS_MEMORY;
            info.BasicLimitInformation.ActiveProcessLimit = 1;
            info.ProcessMemoryLimit = 384 * 1024 * 1024;
            let configured = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if configured == 0 {
                let _ = CloseHandle(handle);
                return Err(engine_error("engine job boundary cannot be configured"));
            }
            let process_handle = child.as_raw_handle() as HANDLE;
            if AssignProcessToJobObject(handle, process_handle) == 0 {
                let _ = CloseHandle(handle);
                return Err(engine_error("engine cannot enter its job boundary"));
            }
            Ok(Self { handle })
        }
    }

    fn close(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        unsafe {
            if !self.handle.is_null() {
                let _ = CloseHandle(self.handle);
                self.handle = std::ptr::null_mut();
            }
        }
    }

    fn terminate_and_close(&mut self) {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;
        unsafe {
            if !self.handle.is_null() {
                let _ = TerminateJobObject(self.handle, 70);
            }
        }
        self.close();
    }
}

#[cfg(windows)]
impl Drop for ChildJob {
    fn drop(&mut self) {
        self.close();
    }
}

#[cfg(not(windows))]
struct ChildJob;

#[cfg(not(windows))]
impl ChildJob {
    fn attach(_child: &Child) -> Result<Self, WrapperError> {
        Err(engine_error("QuickJS wrapper requires Windows"))
    }

    fn terminate_and_close(&mut self) {}
}

#[cfg(windows)]
fn hide_child_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
fn hide_child_window(_command: &mut Command) {}

type ReaderResult = Result<Vec<u8>, WrapperError>;

trait ChildProcess {
    fn try_wait_state(&mut self) -> io::Result<Option<bool>>;
    fn terminate(&mut self) -> io::Result<()>;
    fn wait_and_reap(&mut self) -> io::Result<()>;
}

impl ChildProcess for Child {
    fn try_wait_state(&mut self) -> io::Result<Option<bool>> {
        Child::try_wait(self).map(|status| status.map(|status| status.success()))
    }

    fn terminate(&mut self) -> io::Result<()> {
        Child::kill(self)
    }

    fn wait_and_reap(&mut self) -> io::Result<()> {
        Child::wait(self).map(|_| ())
    }
}

trait JobBoundary {
    fn terminate_and_close(&mut self);
}

impl JobBoundary for ChildJob {
    fn terminate_and_close(&mut self) {
        ChildJob::terminate_and_close(self);
    }
}

fn bounded_reader<R: Read + Send + 'static>(
    mut reader: R,
    limit: usize,
    message: &'static str,
) -> (Receiver<ReaderResult>, JoinHandle<()>) {
    let (sender, receiver) = mpsc::channel();
    let handle = thread::spawn(move || {
        let mut output = Vec::new();
        let result = loop {
            let mut chunk = [0_u8; 16 * 1024];
            match reader.read(&mut chunk) {
                Ok(0) => break Ok(output),
                Ok(count) => {
                    if output.len().saturating_add(count) > limit {
                        break Err(output_error(message));
                    }
                    output.extend_from_slice(&chunk[..count]);
                }
                Err(_) => break Err(output_error("engine output cannot be read")),
            }
        };
        let _ = sender.send(result);
    });
    (receiver, handle)
}

fn receive_reader(receiver: &Receiver<ReaderResult>, current: &mut Option<ReaderResult>) -> bool {
    if current.is_some() {
        return false;
    }
    match receiver.try_recv() {
        Ok(result) => {
            let failed = result.is_err();
            *current = Some(result);
            failed
        }
        Err(TryRecvError::Empty) => false,
        Err(TryRecvError::Disconnected) => {
            *current = Some(Err(output_error("engine output reader stopped")));
            true
        }
    }
}

fn wait_for_child<C: ChildProcess>(
    child: &mut C,
    stdout_receiver: &Receiver<ReaderResult>,
    stderr_receiver: &Receiver<ReaderResult>,
    timeout: Duration,
) -> Result<(bool, ReaderResult, ReaderResult), WrapperError> {
    let started = Instant::now();
    let mut stdout_result = None;
    let mut stderr_result = None;
    let status = loop {
        let output_failed = receive_reader(stdout_receiver, &mut stdout_result)
            | receive_reader(stderr_receiver, &mut stderr_result);
        if output_failed {
            return Err(output_error("engine output limit exceeded"));
        }
        match child.try_wait_state() {
            Ok(Some(succeeded)) => break succeeded,
            Ok(None) => {}
            Err(_) => return Err(engine_error("engine state is unavailable")),
        }
        if started.elapsed() >= timeout {
            return Err(engine_error(ENGINE_TIMEOUT_CLEANED_MESSAGE));
        }
        thread::sleep(Duration::from_millis(10));
    };

    let receive = |receiver: &Receiver<ReaderResult>, current: Option<ReaderResult>| {
        current.unwrap_or_else(|| {
            receiver
                .recv_timeout(Duration::from_secs(5))
                .unwrap_or_else(|_| Err(output_error("engine output reader stopped")))
        })
    };
    Ok((
        status,
        receive(stdout_receiver, stdout_result),
        receive(stderr_receiver, stderr_result),
    ))
}

fn supervise_child<C, J, F>(
    mut child: C,
    mut job: J,
    stdout_receiver: &Receiver<ReaderResult>,
    stderr_receiver: &Receiver<ReaderResult>,
    timeout: Duration,
    join_readers: F,
) -> Result<(bool, ReaderResult, ReaderResult), WrapperError>
where
    C: ChildProcess,
    J: JobBoundary,
    F: FnOnce(),
{
    let result = wait_for_child(&mut child, stdout_receiver, stderr_receiver, timeout);
    if let Err(error) = result {
        // Closing the configured job actively terminates every member and also
        // triggers KILL_ON_JOB_CLOSE. Do this before waiting or joining either
        // pipe reader so inherited write handles cannot keep a reader blocked.
        job.terminate_and_close();
        let _ = child.terminate();
        let reaped = child.wait_and_reap();

        // Child owns the process handle. Both process and job handles must be
        // closed before a potentially blocking pipe-thread join.
        drop(child);
        drop(job);
        join_readers();

        if reaped.is_err() {
            return Err(if error == engine_error(ENGINE_TIMEOUT_CLEANED_MESSAGE) {
                engine_error("engine timeout child cleanup failed")
            } else {
                engine_error("engine child cleanup failed")
            });
        }
        return Err(error);
    }

    // try_wait observed process completion, so closing these handles is safe
    // and ensures the reader join never precedes process/job handle closure.
    drop(child);
    drop(job);
    join_readers();
    result
}

pub fn validate_solver_output(stdout: &[u8]) -> Result<String, WrapperError> {
    let text = std::str::from_utf8(stdout)
        .map_err(|_| output_error("engine output is not valid UTF-8"))?;
    let value: Value = serde_json::from_str(text)
        .map_err(|_| output_error("engine output is not one JSON value"))?;
    let object = value
        .as_object()
        .ok_or_else(|| output_error("engine output is not a JSON object"))?;
    match object.get("type").and_then(Value::as_str) {
        Some("result") if object.get("responses").and_then(Value::as_array).is_some() => {}
        Some("error") if object.get("error").and_then(Value::as_str).is_some() => {}
        _ => {
            return Err(output_error(
                "engine output does not match the EJS contract",
            ))
        }
    }
    serde_json::to_string(&value).map_err(|_| output_error("engine output cannot be normalized"))
}

fn run_script_with_timeout(
    engine: &Path,
    script_bytes: &[u8],
    timeout: Duration,
) -> Result<String, WrapperError> {
    let scratch = ScratchDirectory::create()?;
    let script_path = scratch.path.join("solver.js");
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&script_path)
        .map_err(|_| input_error("private script copy cannot be created"))?;
    file.write_all(script_bytes)
        .and_then(|_| file.flush())
        .map_err(|_| input_error("private script copy cannot be written"))?;
    drop(file);

    let mut command = Command::new(engine);
    command
        .arg("--memory-limit")
        .arg(ENGINE_MEMORY_LIMIT_KIB)
        .arg("--stack-size")
        .arg(ENGINE_STACK_LIMIT_KIB)
        .arg("--script")
        .arg(&script_path)
        .current_dir(&scratch.path)
        .env_clear()
        .env("TEMP", &scratch.path)
        .env("TMP", &scratch.path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    hide_child_window(&mut command);
    let mut child = command
        .spawn()
        .map_err(|_| engine_error("engine cannot be started"))?;
    let job = match ChildJob::attach(&child) {
        Ok(job) => job,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| output_error("engine stdout is unavailable"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| output_error("engine stderr is unavailable"))?;
    let (stdout_receiver, stdout_thread) =
        bounded_reader(stdout, MAX_STDOUT_BYTES, "engine stdout limit exceeded");
    let (stderr_receiver, stderr_thread) =
        bounded_reader(stderr, MAX_STDERR_BYTES, "engine stderr limit exceeded");
    let result = supervise_child(
        child,
        job,
        &stdout_receiver,
        &stderr_receiver,
        timeout,
        || {
            let _ = stdout_thread.join();
            let _ = stderr_thread.join();
        },
    );
    let (succeeded, stdout, stderr) = result?;
    let stdout = stdout?;
    let stderr = stderr?;
    if !succeeded || !stderr.is_empty() {
        return Err(engine_error("engine execution failed"));
    }
    validate_solver_output(&stdout)
}

fn run_script(engine: &Path, script_bytes: &[u8]) -> Result<String, WrapperError> {
    run_script_with_timeout(engine, script_bytes, ENGINE_TIMEOUT)
}

fn verify_capability_boundary(engine: &Path) -> Result<(), WrapperError> {
    let output = run_script(engine, CAPABILITY_SELF_TEST_SCRIPT)?;
    if output != r#"{"responses":[],"type":"result"}"# {
        return Err(engine_error("engine capability boundary self-test failed"));
    }
    Ok(())
}

fn sibling_engine() -> Result<PathBuf, WrapperError> {
    let executable =
        env::current_exe().map_err(|_| engine_error("wrapper location is unavailable"))?;
    let parent = executable
        .parent()
        .ok_or_else(|| engine_error("wrapper location is unavailable"))?;
    Ok(parent.join(ENGINE_FILE_NAME))
}

pub fn execute(arguments: &[String]) -> Result<(u8, Option<String>), WrapperError> {
    if arguments == ["--help"] || arguments == ["-h"] {
        let output = format!(
            "QuickJS-ng version {QUICKJS_VERSION}\n\
usage: qjs --script FILE\n\
Scriber protocol: {PROTOCOL}\n"
        );
        return Ok((1, Some(output)));
    }
    let engine = sibling_engine()?;
    if arguments == ["--scriber-self-test"] {
        verify_engine(&engine)?;
        verify_capability_boundary(&engine)?;
        return Ok((
            0,
            Some(format!(
                "{{\"contract\":\"{PROTOCOL}\",\"ok\":true,\"quickjsVersion\":\"{QUICKJS_VERSION}\"}}\n"
            )),
        ));
    }
    if arguments == ["--scriber-test-error"] {
        verify_engine(&engine)?;
        let _ = run_script(&engine, b"throw new Error('bounded test failure');\n")?;
        return Err(engine_error(
            "engine error self-test unexpectedly succeeded",
        ));
    }
    if arguments == ["--scriber-test-timeout"] {
        verify_engine(&engine)?;
        match run_script_with_timeout(&engine, b"for (;;) {}\n", TEST_ENGINE_TIMEOUT) {
            Err(error) if error == engine_error(ENGINE_TIMEOUT_CLEANED_MESSAGE) => {
                return Ok((
                    0,
                    Some(format!(
                        "{{\"childReaped\":true,\"contract\":\"{PROTOCOL}\",\"ok\":true,\"productionTimeoutMilliseconds\":{},\"testTimeoutMilliseconds\":{}}}\n",
                        ENGINE_TIMEOUT.as_millis(),
                        TEST_ENGINE_TIMEOUT.as_millis(),
                    )),
                ));
            }
            Err(_) => return Err(engine_error("engine timeout self-test failed")),
            Ok(_) => {
                return Err(engine_error(
                    "engine timeout self-test unexpectedly succeeded",
                ))
            }
        }
    }
    if arguments == ["--scriber-test-hang"] {
        verify_engine(&engine)?;
        let _ = run_script(&engine, b"for (;;) {}\n")?;
        return Err(engine_error(
            "engine timeout self-test unexpectedly succeeded",
        ));
    }
    if arguments.len() != 2 || arguments[0] != "--script" {
        return Err(WrapperError::new(64, "unsupported invocation"));
    }
    verify_engine(&engine)?;
    let script = read_script(Path::new(&arguments[1]))?;
    let output = run_script(&engine, &script)?;
    Ok((0, Some(format!("{output}\n"))))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    struct FaultInjectedChild {
        events: Rc<RefCell<Vec<&'static str>>>,
    }

    impl ChildProcess for FaultInjectedChild {
        fn try_wait_state(&mut self) -> io::Result<Option<bool>> {
            self.events.borrow_mut().push("try_wait_error");
            Err(io::Error::other("deterministic try_wait fault"))
        }

        fn terminate(&mut self) -> io::Result<()> {
            self.events.borrow_mut().push("child_terminate");
            Ok(())
        }

        fn wait_and_reap(&mut self) -> io::Result<()> {
            self.events.borrow_mut().push("child_wait_and_reap");
            Ok(())
        }
    }

    impl Drop for FaultInjectedChild {
        fn drop(&mut self) {
            self.events.borrow_mut().push("child_handle_closed");
        }
    }

    struct FaultInjectedJob {
        events: Rc<RefCell<Vec<&'static str>>>,
    }

    impl JobBoundary for FaultInjectedJob {
        fn terminate_and_close(&mut self) {
            self.events.borrow_mut().push("job_terminated_and_closed");
        }
    }

    impl Drop for FaultInjectedJob {
        fn drop(&mut self) {
            self.events.borrow_mut().push("job_guard_dropped");
        }
    }

    #[test]
    fn accepts_result_and_error_contracts() {
        assert_eq!(
            validate_solver_output(br#"{"type":"result","responses":[]}"#).unwrap(),
            r#"{"responses":[],"type":"result"}"#
        );
        assert_eq!(
            validate_solver_output(br#"{"type":"error","error":"bounded"}"#).unwrap(),
            r#"{"error":"bounded","type":"error"}"#
        );
    }

    #[test]
    fn rejects_extra_output_and_wrong_schema() {
        assert!(validate_solver_output(b"log\n{\"type\":\"result\",\"responses\":[]}").is_err());
        assert!(validate_solver_output(br#"{"type":"result"}"#).is_err());
        assert!(validate_solver_output(br#"[]"#).is_err());
    }

    #[test]
    fn help_is_compatible_with_yt_dlp_detection() {
        let (code, output) = execute(&["--help".to_owned()]).unwrap();
        assert_eq!(code, 1);
        assert!(output.unwrap().starts_with("QuickJS-ng version 0.15.0\n"));
    }

    #[test]
    fn production_timeout_contract_is_not_shortened_by_the_self_test() {
        assert_eq!(ENGINE_TIMEOUT, Duration::from_secs(45));
        assert_eq!(TEST_ENGINE_TIMEOUT, Duration::from_millis(250));
        assert!(TEST_ENGINE_TIMEOUT < ENGINE_TIMEOUT);
    }

    #[test]
    fn try_wait_error_reaps_and_closes_process_and_job_before_reader_join() {
        let events = Rc::new(RefCell::new(Vec::new()));
        let child = FaultInjectedChild {
            events: Rc::clone(&events),
        };
        let job = FaultInjectedJob {
            events: Rc::clone(&events),
        };
        let (stdout_sender, stdout_receiver) = mpsc::channel::<ReaderResult>();
        let (stderr_sender, stderr_receiver) = mpsc::channel::<ReaderResult>();
        let join_events = Rc::clone(&events);

        let result = supervise_child(
            child,
            job,
            &stdout_receiver,
            &stderr_receiver,
            Duration::from_secs(1),
            move || join_events.borrow_mut().push("reader_threads_joined"),
        );

        // Keep both channels connected until after supervision so the injected
        // try_wait error, not a disconnected reader, selects the error path.
        drop(stdout_sender);
        drop(stderr_sender);
        assert_eq!(result, Err(engine_error("engine state is unavailable")));
        assert_eq!(
            events.borrow().as_slice(),
            [
                "try_wait_error",
                "job_terminated_and_closed",
                "child_terminate",
                "child_wait_and_reap",
                "child_handle_closed",
                "job_guard_dropped",
                "reader_threads_joined",
            ]
        );
    }
}
