use std::process::ExitCode;

fn main() -> ExitCode {
    let exit_code = scriber_diarization_sidecar::run_cli();
    exit_code
}
