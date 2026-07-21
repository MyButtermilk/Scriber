// encode.rs — FLAC encoding logic (parallel multi-process).
//
// Public API:
//   encode_to_file(input, output) — encode a WAV file to FLAC

use std::env;
use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, Read, Write};
use std::process::{self, Command, Stdio};
use std::time::Instant;

use crate::bitstream::BitWriter;
use crate::frame::write_frame_header_ext;
use crate::metadata::{write_seektable, write_stream_header, SeekPoint};
use crate::subframe::{write_subframe_constant, write_subframe_lpc};
use rezin_lpc::analysis::{levinson_durbin, window_hann, BLOCK_SIZE};
use rezin_lpc::autocorrelation::autocorrelation;
use rezin_lpc::fixed::predict_fixed;
use rezin_lpc::quantize::{predict_lpc, quantize};
use rezin_wav::parse::parse;
use rezin_wav::stream::Stream;

const MAX_BLOCK: usize = BLOCK_SIZE; // 4096

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Encodes a WAV file to FLAC.
pub fn encode_to_file(input_path: &str, output_path: &str) {
    let mut in_file = File::open(input_path).unwrap_or_else(|e| {
        eprintln!("Error opening input: {}", e); process::exit(1);
    });

    let dec = parse(&mut in_file).unwrap_or_else(|e| {
        eprintln!("WAV error: {}", e); process::exit(1);
    });

    let channels         = dec.meta.channels as usize;
    let bps              = dec.meta.bit_depth as u8;
    let bytes_per_sample = (dec.meta.bit_depth / 8) as u32;
    let total_samples    = dec.data_size / (dec.meta.channels as u32 * bytes_per_sample);

    // Read all PCM samples upfront.
    let mut all_samples = vec![0i32; total_samples as usize * channels];
    let mut stream = Stream::new(in_file, &dec);
    let mut n_read = 0usize;
    loop {
        match stream.read_samples(&mut all_samples[n_read..]) {
            Ok(0) => break,
            Ok(n) => n_read += n,
            Err(e) => { eprintln!("Read error: {}", e); process::exit(1); }
        }
    }

    let total_frames = (n_read + MAX_BLOCK * channels - 1) / (MAX_BLOCK * channels);
    // Scriber codec-lab patch: upstream 0.2.1 creates zero-frame workers when
    // available_parallelism exceeds total_frames, then slices beyond n_read.
    // Keep the official parallel process model while bounding workers to work.
    let n_workers    = cpu_count().min(total_frames.max(1));

    let base_frames = total_frames / n_workers;
    let remainder   = total_frames % n_workers;
    let mut start_frames = vec![0u32; n_workers];
    let mut frame_counts = vec![0u32; n_workers];
    for i in 0..n_workers {
        let extra = if i < remainder { 1 } else { 0 };
        frame_counts[i] = (base_frames + extra) as u32;
        start_frames[i] = if i == 0 { 0 }
                          else { start_frames[i - 1] + frame_counts[i - 1] };
    }

    let self_path = env::current_exe().unwrap_or_else(|_| {
        eprintln!("Could not determine executable path"); process::exit(1);
    });

    let channels_s = channels.to_string();
    let bps_s      = bps.to_string();
    let rate_s     = dec.meta.rate.to_string();

    eprintln!("Encoding with {} workers...", n_workers);
    let start = Instant::now();

    let mut children = Vec::with_capacity(n_workers);
    for i in 0..n_workers {
        let sample_start = start_frames[i] as usize * MAX_BLOCK * channels;
        let sample_end   = {
            let end_frame = (start_frames[i] + frame_counts[i]) as usize;
            (end_frame * MAX_BLOCK * channels).min(n_read)
        };

        let mut child = Command::new(&self_path)
            .arg("--worker")
            .arg(start_frames[i].to_string())
            .arg(&channels_s)
            .arg(&bps_s)
            .arg(&rate_s)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap_or_else(|e| { eprintln!("Spawn failed: {}", e); process::exit(1); });

        {
            let mut stdin = child.stdin.take().unwrap();
            let chunk    = &all_samples[sample_start..sample_end];
            let byte_len = chunk.len() * 4;
            // Safety: i32 slice reinterpreted as u8; LE byte order matches worker expectation.
            let bytes = unsafe {
                std::slice::from_raw_parts(chunk.as_ptr() as *const u8, byte_len)
            };
            stdin.write_all(bytes).unwrap_or_else(|e| {
                eprintln!("Worker stdin write failed: {}", e); process::exit(1);
            });
            // stdin drops here, closing the pipe and signalling EOF to the worker.
        }

        children.push(child);
    }

    let mut worker_outputs: Vec<(Vec<u8>, Vec<u8>)> = Vec::with_capacity(n_workers);
    for (i, child) in children.iter_mut().enumerate() {
        let stdout = child.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);

        let mut len_buf = [0u8; 8];
        reader.read_exact(&mut len_buf).unwrap_or_else(|e| {
            eprintln!("Worker {} length read failed: {}", i, e); process::exit(1);
        });
        let flac_len = u64::from_le_bytes(len_buf) as usize;

        let mut flac_data = vec![0u8; flac_len];
        reader.read_exact(&mut flac_data).unwrap_or_else(|e| {
            eprintln!("Worker {} FLAC data read failed: {}", i, e); process::exit(1);
        });

        let mut map_data = Vec::new();
        reader.read_to_end(&mut map_data).unwrap_or_else(|e| {
            eprintln!("Worker {} map read failed: {}", i, e); process::exit(1);
        });

        worker_outputs.push((flac_data, map_data));
    }

    for (i, mut child) in children.into_iter().enumerate() {
        let status = child.wait().unwrap_or_else(|e| {
            eprintln!("Worker {} wait failed: {}", i, e); process::exit(1);
        });
        if !status.success() {
            eprintln!("Worker {} exited with: {}", i, status); process::exit(1);
        }
    }

    eprintln!("Encoding done in {:.2}s", start.elapsed().as_secs_f64());

    let n_seekpoints             = n_workers;
    let seektable_payload_offset = 42u64 + 4;

    let out_file = OpenOptions::new()
        .write(true).create(true).truncate(true)
        .open(output_path)
        .unwrap_or_else(|e| { eprintln!("Error creating output: {}", e); process::exit(1); });

    let mut writer = BitWriter::new(out_file);
    write_stream_header(&mut writer, &dec).unwrap();

    let payload_bytes = n_seekpoints as u64 * 18;
    writer.write_bits(1, 1).unwrap();
    writer.write_bits(3, 7).unwrap();
    writer.write_bits(payload_bytes, 24).unwrap();
    for _ in 0..n_seekpoints {
        writer.write_bits(0, 64).unwrap();
        writer.write_bits(0, 64).unwrap();
        writer.write_bits(0, 16).unwrap();
        writer.flush().unwrap();
    }
    writer.flush().unwrap();

    let mut seek_entries: Vec<SeekPoint> = Vec::with_capacity(n_seekpoints);
    let mut cumulative_bytes: u64 = 0;

    for (i, (flac_data, map_data)) in worker_outputs.iter().enumerate() {
        if map_data.len() >= 12 && seek_entries.len() < n_seekpoints {
            let frame_num     = u32::from_be_bytes(map_data[0..4].try_into().unwrap());
            let worker_offset = u64::from_be_bytes(map_data[4..12].try_into().unwrap());
            let abs_offset    = cumulative_bytes + worker_offset;
            let frame_sample  = frame_num as u64 * MAX_BLOCK as u64;
            seek_entries.push(SeekPoint {
                sample_number: frame_sample,
                byte_offset:   abs_offset,
                frame_samples: MAX_BLOCK as u16,
            });
        }
        cumulative_bytes += flac_data.len() as u64;

        writer.write_bytes_raw(flac_data).unwrap_or_else(|e| {
            eprintln!("Stitch write failed for worker {}: {}", i, e); process::exit(1);
        });
    }
    writer.flush().unwrap();
    drop(writer);

    if !seek_entries.is_empty() {
        let mut patch = OpenOptions::new()
            .write(true)
            .open(output_path)
            .unwrap_or_else(|e| { eprintln!("Seektable reopen failed: {}", e); process::exit(1); });
        write_seektable(&mut patch, seektable_payload_offset, &seek_entries)
            .unwrap_or_else(|e| { eprintln!("Seektable write failed: {}", e); process::exit(1); });
    }

    eprintln!("Output: {}", output_path);
}

// ---------------------------------------------------------------------------
// Worker entry point (called when re-exec'd with --worker)
// ---------------------------------------------------------------------------

pub fn run_worker(args: &[String]) {
    let start_frame: u32 = args[2].parse().unwrap_or_else(|_| {
        eprintln!("Invalid start_frame"); process::exit(1);
    });
    let channels: usize = args[3].parse().unwrap_or_else(|_| {
        eprintln!("Invalid channels"); process::exit(1);
    });
    let bps: u8 = args[4].parse().unwrap_or_else(|_| {
        eprintln!("Invalid bps"); process::exit(1);
    });
    let sample_rate: u32 = args[5].parse().unwrap_or_else(|_| {
        eprintln!("Invalid sample_rate"); process::exit(1);
    });

    let mut raw_bytes = Vec::new();
    io::stdin().read_to_end(&mut raw_bytes).unwrap_or_else(|e| {
        eprintln!("Worker stdin read failed: {}", e); process::exit(1);
    });
    let n_samples = raw_bytes.len() / 4;
    let mut samples = vec![0i32; n_samples];
    for (i, chunk) in raw_bytes.chunks_exact(4).enumerate() {
        samples[i] = i32::from_le_bytes(chunk.try_into().unwrap());
    }

    let mut flac_buf: Vec<u8> = Vec::new();
    let mut map_buf:  Vec<u8> = Vec::new();
    let mut writer = BitWriter::new(&mut flac_buf);
    let mut bufs   = EncodeBuffers::new();

    encode_chunk(
        &mut writer,
        &mut map_buf,
        &samples,
        channels,
        bps,
        sample_rate,
        start_frame,
        &mut bufs,
    );
    writer.flush().unwrap();
    drop(writer);

    let stdout = io::stdout();
    let mut out = stdout.lock();
    out.write_all(&(flac_buf.len() as u64).to_le_bytes()).unwrap();
    out.write_all(&flac_buf).unwrap();
    out.write_all(&map_buf).unwrap();
    out.flush().unwrap();
}

// ---------------------------------------------------------------------------
// EncodeBuffers
// ---------------------------------------------------------------------------

struct EncodeBuffers {
    left:       Box<[i32; MAX_BLOCK]>,
    right:      Box<[i32; MAX_BLOCK]>,
    mid:        Box<[i32; MAX_BLOCK]>,
    side:       Box<[i32; MAX_BLOCK]>,
    clean:      Box<[i32; MAX_BLOCK]>,
    residual:   Box<[i32; MAX_BLOCK]>,
    windowed:   Box<[f64; MAX_BLOCK]>,
    r_buf:      [f64; 13],
    lpc_coeffs: [f64; 12],
    test_q_lpc: [i32; 12],
    best_q_lpc: [i32; 12],
}

impl EncodeBuffers {
    fn new() -> Self {
        Self {
            left:       Box::new([0i32; MAX_BLOCK]),
            right:      Box::new([0i32; MAX_BLOCK]),
            mid:        Box::new([0i32; MAX_BLOCK]),
            side:       Box::new([0i32; MAX_BLOCK]),
            clean:      Box::new([0i32; MAX_BLOCK]),
            residual:   Box::new([0i32; MAX_BLOCK]),
            windowed:   Box::new([0.0f64; MAX_BLOCK]),
            r_buf:      [0.0f64; 13],
            lpc_coeffs: [0.0f64; 12],
            test_q_lpc: [0i32; 12],
            best_q_lpc: [0i32; 12],
        }
    }
}

// ---------------------------------------------------------------------------
// Core encoding
// ---------------------------------------------------------------------------

fn encode_chunk(
    writer:      &mut BitWriter<impl Write>,
    map_buf:     &mut Vec<u8>,
    all_samples: &[i32],
    channels:    usize,
    bps:         u8,
    sample_rate: u32,
    start_frame: u32,
    bufs:        &mut EncodeBuffers,
) {
    let mut frame_count = start_frame;
    let mut pos         = 0usize;

    while pos < all_samples.len() {
        let remaining     = all_samples.len() - pos;
        let block_samples = remaining.min(MAX_BLOCK * channels);
        let block_size    = (block_samples / channels) as u16;
        let n             = block_size as usize;

        for i in 0..n {
            bufs.left[i] = all_samples[pos + i * channels];
            if channels > 1 {
                bufs.right[i] = all_samples[pos + i * channels + 1];
            }
        }

        let (channel_assignment, bps_ch0, bps_ch1, use_midside) =
            choose_channel_mode(bufs, n, channels, bps);

        let frame_byte_offset = writer.bytes_written();
        map_buf.extend_from_slice(&frame_count.to_be_bytes());
        map_buf.extend_from_slice(&frame_byte_offset.to_be_bytes());

        writer.reset_crc16();
        write_frame_header_ext(
            writer, block_size, sample_rate, bps, channel_assignment, frame_count,
        ).unwrap();

        if channels == 1 {
            bufs.clean[..n].copy_from_slice(&bufs.left[..n]);
            encode_channel(writer, n, bps_ch0, bufs).unwrap();
        } else if use_midside {
            bufs.clean[..n].copy_from_slice(&bufs.mid[..n]);
            encode_channel(writer, n, bps_ch0, bufs).unwrap();
            bufs.clean[..n].copy_from_slice(&bufs.side[..n]);
            encode_channel(writer, n, bps_ch1, bufs).unwrap();
        } else {
            bufs.clean[..n].copy_from_slice(&bufs.left[..n]);
            encode_channel(writer, n, bps_ch0, bufs).unwrap();
            bufs.clean[..n].copy_from_slice(&bufs.right[..n]);
            encode_channel(writer, n, bps_ch1, bufs).unwrap();
        }

        let frame_crc = writer.finalize_frame().unwrap();
        writer.write_bits(frame_crc as u64, 16).unwrap();

        frame_count += 1;
        pos         += block_samples;
    }
}

// ---------------------------------------------------------------------------
// Channel helpers
// ---------------------------------------------------------------------------

fn choose_channel_mode(
    bufs:     &mut EncodeBuffers,
    n:        usize,
    channels: usize,
    bps:      u8,
) -> (u8, u8, u8, bool) {
    if channels == 1 {
        return (0b0000, bps, bps, false);
    }

    let mut left_score  = 0u64;
    let mut right_score = 0u64;
    let mut mid_score   = 0u64;
    let mut side_score  = 0u64;

    for i in 0..n {
        let l = bufs.left[i];
        let r = bufs.right[i];
        bufs.mid[i]  = (l + r) >> 1;
        bufs.side[i] = l - r;
        left_score  += l.unsigned_abs() as u64;
        right_score += r.unsigned_abs() as u64;
        mid_score   += bufs.mid[i].unsigned_abs() as u64;
        side_score  += bufs.side[i].unsigned_abs() as u64;
    }

    if bps <= 24 && mid_score + side_score < left_score + right_score {
        (0b1010, bps, bps + 1, true)
    } else {
        (0b0001, bps, bps, false)
    }
}

fn encode_channel<W: Write>(
    w:    &mut BitWriter<W>,
    n:    usize,
    bps:  u8,
    bufs: &mut EncodeBuffers,
) -> io::Result<usize> {
    let shift_bits = 32i32 - bps as i32;
    for i in 0..n {
        bufs.clean[i] = (bufs.clean[i] << shift_bits) >> shift_bits;
    }
    let clean = &bufs.clean[..n];

    if clean.iter().all(|&v| v == clean[0]) {
        return write_subframe_constant(w, clean[0], bps);
    }

    let mut best_score = f64::INFINITY;
    let mut best_shift = 0u8;
    let mut is_fixed   = true;
    let mut best_order = 0usize;
    let total_f        = n as f64;

    let max_fixed = n.min(4);
    for order in 0..=max_fixed {
        predict_fixed(clean, order as u8, &mut bufs.residual[..n]);
        let abs_sum = bufs.residual[order..n].iter()
            .map(|&v| v.abs() as f64).sum::<f64>().max(1.0);
        let remaining = total_f - order as f64;
        let score = remaining * (abs_sum / remaining).log2() + order as f64 * bps as f64;
        if score < best_score {
            best_score = score;
            best_order = order;
            is_fixed   = true;
        }
    }

    window_hann(clean, &mut bufs.windowed[..n]);
    autocorrelation(&bufs.windowed[..n], &mut bufs.r_buf);
    levinson_durbin(&bufs.r_buf, &mut bufs.lpc_coeffs);

    let precision = 12u8;
    for order in 1..=12usize {
        let shift = quantize(
            &bufs.lpc_coeffs[..order],
            &mut bufs.test_q_lpc[..order],
            precision,
        );
        predict_lpc(clean, &bufs.test_q_lpc[..order], shift, &mut bufs.residual[..n]);

        let abs_sum = bufs.residual[order..n].iter()
            .map(|&v| v.abs() as f64).sum::<f64>().max(1.0);
        let remaining = total_f - order as f64;
        let score = remaining * (abs_sum / remaining).log2()
                    + order as f64 * bps as f64
                    + order as f64 * precision as f64
                    + 14.0;
        if score < best_score {
            best_score = score;
            best_order = order;
            best_shift = shift;
            bufs.best_q_lpc[..order].copy_from_slice(&bufs.test_q_lpc[..order]);
            is_fixed   = false;
        }
    }

    if is_fixed {
        write_subframe_lpc(w, clean, &[], 0, bps)
    } else {
        write_subframe_lpc(w, clean, &bufs.best_q_lpc[..best_order], best_shift, bps)
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn cpu_count() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
}
