// rezin-flac-encode — Multi-process parallel FLAC encoder.
//
// Orchestrator mode:  rezin-flac-encode <input.wav> <output.flac>
// Worker mode:        rezin-flac-encode --worker <start_frame> <channels> <bps> <sample_rate>

use std::env;
use std::process;

use rezin_flac::encode;

fn main() {
    let args: Vec<String> = env::args().collect();

    if args.len() >= 2 && args[1] == "--worker" {
        if args.len() < 6 {
            eprintln!(
                "Usage: rezin-flac-encode --worker <start_frame> <channels> <bps> <sample_rate>"
            );
            process::exit(1);
        }
        encode::run_worker(&args);
        return;
    }

    if args.len() < 3 {
        eprintln!("Usage: rezin-flac-encode <input.wav> <output.flac>");
        process::exit(1);
    }

    encode::encode_to_file(&args[1], &args[2]);
}
