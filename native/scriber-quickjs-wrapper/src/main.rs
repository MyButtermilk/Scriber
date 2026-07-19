use scriber_quickjs_wrapper::{execute, PROTOCOL};
use std::io::{self, Write};
use std::process::ExitCode;

fn main() -> ExitCode {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    match execute(&arguments) {
        Ok((code, output)) => {
            if let Some(output) = output {
                let mut stdout = io::stdout().lock();
                if stdout
                    .write_all(output.as_bytes())
                    .and_then(|_| stdout.flush())
                    .is_err()
                {
                    return ExitCode::from(74);
                }
            }
            ExitCode::from(code)
        }
        Err(error) => {
            let _ = writeln!(io::stderr().lock(), "{PROTOCOL}: {}", error.message());
            ExitCode::from(error.exit_code())
        }
    }
}
