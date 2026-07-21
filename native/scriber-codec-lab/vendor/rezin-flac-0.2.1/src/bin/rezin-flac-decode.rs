// rezin-flac-decode — Multi-process parallel FLAC decoder.
//
// Orchestrator mode:  rezin-flac-decode <input.flac> <output.wav>
// Worker mode:        rezin-flac-decode --worker <byte_offset> <n_samples> <start_sample>
//                                       <channels> <bps> <sample_rate> <input_path>

use std::env;
use std::process;

use rezin_flac::decode;

fn main() {
    let args: Vec<String> = env::args().collect();

    if args.len() >= 2 && args[1] == "--worker" {
        if args.len() < 9 {
            eprintln!(
                "Usage: rezin-flac-decode --worker <byte_offset> <n_samples> <start_sample> \
                 <channels> <bps> <sample_rate> <input_path>"
            );
            process::exit(1);
        }
        decode::run_worker(&args);
        return;
    }

    if args.len() < 3 {
        eprintln!("Usage: rezin-flac-decode <input.flac> <output.wav>");
        process::exit(1);
    }

    decode::decode_to_file(&args[1], &args[2]);
}
