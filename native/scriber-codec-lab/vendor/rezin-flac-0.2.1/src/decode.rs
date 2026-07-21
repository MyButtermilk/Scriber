// decode.rs — FLAC decoding logic (single-process and parallel).
//
// Public API:
//   decode_to_file(input, output) — decode FLAC to a WAV file
//   decode_to_pcm(input)          — decode FLAC to an in-memory PcmBuffer

use std::env;
use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::process::{self, Command, Stdio};
use std::time::Instant;

use crate::bitstream::BitReader;
use crate::frame::read_frame_header;
use crate::metadata::{read_streaminfo, SeekPoint};
use crate::subframe::read_subframe;

const MAX_BLOCK: usize = 65535;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

pub struct PcmBuffer {
    pub samples:     Vec<i32>,
    pub channels:    usize,
    pub sample_rate: u32,
    pub bps:         u8,
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Decodes a FLAC file to a WAV file.
pub fn decode_to_file(input_path: &str, output_path: &str) {
    let in_file = File::open(input_path).unwrap_or_else(|e| {
        eprintln!("Error opening input: {}", e); process::exit(1);
    });
    let mut reader = BitReader::new(BufReader::new(in_file));
    let info = read_streaminfo_or_exit(&mut reader);

    let channels      = info.channels as usize;
    let bps           = info.bps;
    let sample_rate   = info.sample_rate;
    let total_samples = info.total_samples;
    let seektable     = info.seektable;
    let audio_start   = info.audio_start;

    eprintln!("Decoding: {} Hz, {} ch, {} bps, {} samples",
              sample_rate, channels, bps, total_samples);

    if seektable.len() > 1 {
        decode_parallel_to_file(
            input_path, output_path, &seektable,
            channels, bps, sample_rate, total_samples, audio_start,
        );
        return;
    }

    let (samples, n_samples) = decode_single(
        &mut reader, channels, bps, sample_rate, total_samples,
    );

    let out_file = OpenOptions::new()
        .write(true).create(true).truncate(true)
        .open(output_path)
        .unwrap_or_else(|e| { eprintln!("Error creating output: {}", e); process::exit(1); });

    let mut out = BufWriter::new(out_file);
    write_wav_header(&mut out, n_samples as u32,
                     channels as u16, sample_rate, bps).unwrap();
    write_wav_samples(&mut out, &samples[..n_samples], bps).unwrap();
    out.flush().unwrap();
}

/// Decodes a FLAC file to an in-memory PCM buffer for direct playback.
pub fn decode_to_pcm(input_path: &str) -> PcmBuffer {
    let in_file = File::open(input_path).unwrap_or_else(|e| {
        eprintln!("Error opening input: {}", e); process::exit(1);
    });
    let mut reader = BitReader::new(BufReader::new(in_file));
    let info = read_streaminfo_or_exit(&mut reader);

    let channels      = info.channels as usize;
    let bps           = info.bps;
    let sample_rate   = info.sample_rate;
    let total_samples = info.total_samples;

    eprintln!("Decoding: {} Hz, {} ch, {} bps, {} samples",
              sample_rate, channels, bps, total_samples);

    let (samples, n_samples) = decode_single(
        &mut reader, channels, bps, sample_rate, total_samples,
    );

    PcmBuffer {
        samples: samples[..n_samples].to_vec(),
        channels,
        sample_rate,
        bps,
    }
}

// ---------------------------------------------------------------------------
// Worker entry point (called when re-exec'd with --worker)
// ---------------------------------------------------------------------------

pub fn run_worker(args: &[String]) {
    let byte_offset:  u64   = args[2].parse().unwrap_or_else(|_| { eprintln!("bad byte_offset");  process::exit(1); });
    let n_samples:    u64   = args[3].parse().unwrap_or_else(|_| { eprintln!("bad n_samples");    process::exit(1); });
    let start_sample: u64   = args[4].parse().unwrap_or_else(|_| { eprintln!("bad start_sample"); process::exit(1); });
    let channels:     usize = args[5].parse().unwrap_or_else(|_| { eprintln!("bad channels");     process::exit(1); });
    let bps:          u8    = args[6].parse().unwrap_or_else(|_| { eprintln!("bad bps");           process::exit(1); });
    let _sample_rate: u32   = args[7].parse().unwrap_or_else(|_| { eprintln!("bad sample_rate");  process::exit(1); });
    let input_path          = &args[8];

    let mut in_file = File::open(input_path).unwrap_or_else(|e| {
        eprintln!("Worker: cannot open {}: {}", input_path, e); process::exit(1);
    });
    in_file.seek(SeekFrom::Start(byte_offset)).unwrap_or_else(|e| {
        eprintln!("Worker: seek failed: {}", e); process::exit(1);
    });

    let mut reader = BitReader::new(BufReader::new(in_file));
    let mut ch0 = vec![0i32; MAX_BLOCK];
    let mut ch1 = vec![0i32; MAX_BLOCK];

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());

    let mut samples_written = 0u64;
    let mut frame_pos       = 0u64;
    let mut first_frame     = true;

    // bps and sample_rate aren't stored in worker args beyond what's needed;
    // read them from the frame headers directly.
    let bps_hint        = bps;
    let sample_rate_hint = args[7].parse::<u32>().unwrap_or(44100);

    loop {
        if samples_written >= n_samples { break; }

        let hdr = match read_frame_header(&mut reader, bps_hint, sample_rate_hint) {
            Ok(Some(h)) => h,
            Ok(None)    => break,
            Err(e) => {
                eprintln!("Worker: frame header error at {}/{}: {}", samples_written, n_samples, e);
                process::exit(1);
            }
        };

        if first_frame {
            frame_pos   = hdr.frame_num as u64 * hdr.block_size as u64;
            first_frame = false;
        }

        let block_size = hdr.block_size as usize;
        let bps_ch0    = bps_for_channel(hdr.channel_assignment, 0, bps_hint);
        let bps_ch1    = bps_for_channel(hdr.channel_assignment, 1, bps_hint);

        read_subframe(&mut reader, &mut ch0[..block_size], bps_ch0).unwrap_or_else(|e| {
            eprintln!("Worker: subframe 0 error: {}", e); process::exit(1);
        });
        if channels > 1 {
            read_subframe(&mut reader, &mut ch1[..block_size], bps_ch1).unwrap_or_else(|e| {
                eprintln!("Worker: subframe 1 error: {}", e); process::exit(1);
            });
        }

        reader.align();
        let _ = reader.read_bits_nocrc(16);

        decode_joint_stereo(hdr.channel_assignment, &mut ch0[..block_size], &mut ch1[..block_size]);

        for i in 0..block_size {
            let abs_sample = frame_pos + i as u64;
            if abs_sample < start_sample    { continue; }
            if samples_written >= n_samples { break; }

            out.write_all(&ch0[i].to_le_bytes()).unwrap();
            if channels > 1 {
                out.write_all(&ch1[i].to_le_bytes()).unwrap();
            }
            samples_written += 1;
        }

        frame_pos += hdr.block_size as u64;
    }

    out.flush().unwrap();
}

// ---------------------------------------------------------------------------
// Internal: single-process decode
// ---------------------------------------------------------------------------

fn decode_single<R: Read + Seek>(
    reader:        &mut BitReader<BufReader<R>>,
    channels:      usize,
    bps:           u8,
    sample_rate:   u32,
    total_samples: u64,
) -> (Vec<i32>, usize) {
    let start    = Instant::now();
    let capacity = if total_samples > 0 {
        total_samples as usize * channels
    } else {
        65536
    };

    let mut all_samples: Vec<i32> = vec![0; capacity];
    let mut n_samples   = 0usize;
    let mut frame_count = 0usize;
    let mut ch0 = vec![0i32; MAX_BLOCK];
    let mut ch1 = vec![0i32; MAX_BLOCK];

    loop {
        if total_samples > 0 && n_samples >= total_samples as usize * channels {
            break;
        }
        let hdr = match read_frame_header(reader, bps, sample_rate) {
            Ok(Some(h)) => h,
            Ok(None)    => break,
            Err(e)      => { eprintln!("Frame header error: {}", e); process::exit(1); }
        };

        let block_size = hdr.block_size as usize;
        let bps_ch0    = bps_for_channel(hdr.channel_assignment, 0, bps);
        let bps_ch1    = bps_for_channel(hdr.channel_assignment, 1, bps);

        read_subframe(reader, &mut ch0[..block_size], bps_ch0).unwrap_or_else(|e| {
            eprintln!("Subframe 0 error: {}", e); process::exit(1);
        });
        if channels > 1 {
            read_subframe(reader, &mut ch1[..block_size], bps_ch1).unwrap_or_else(|e| {
                eprintln!("Subframe 1 error: {}", e); process::exit(1);
            });
        }

        reader.align();
        let _ = reader.read_bits_nocrc(16);

        decode_joint_stereo(hdr.channel_assignment, &mut ch0[..block_size], &mut ch1[..block_size]);

        let needed = n_samples + block_size * channels;
        if needed > all_samples.len() {
            all_samples.resize(needed * 2, 0);
        }

        for i in 0..block_size {
            all_samples[n_samples + i * channels]     = ch0[i];
            if channels > 1 {
                all_samples[n_samples + i * channels + 1] = ch1[i];
            }
        }
        n_samples   += block_size * channels;
        frame_count += 1;
    }

    eprintln!("Decoded {} frames in {:.3}s", frame_count, start.elapsed().as_secs_f64());
    (all_samples, n_samples)
}

// ---------------------------------------------------------------------------
// Internal: parallel decode to file
// ---------------------------------------------------------------------------

fn decode_parallel_to_file(
    input_path:    &str,
    output_path:   &str,
    seektable:     &[SeekPoint],
    channels:      usize,
    bps:           u8,
    sample_rate:   u32,
    total_samples: u64,
    audio_start:   u64,
) {
    let n_seek    = seektable.len();
    let n_workers = cpu_count().min(n_seek);

    let base = n_seek / n_workers;
    let rem  = n_seek % n_workers;
    let mut seek_starts = vec![0usize; n_workers];
    let mut seek_counts = vec![0usize; n_workers];
    for i in 0..n_workers {
        seek_counts[i] = base + if i < rem { 1 } else { 0 };
        seek_starts[i] = if i == 0 { 0 } else { seek_starts[i-1] + seek_counts[i-1] };
    }

    let self_path = env::current_exe().unwrap_or_else(|_| {
        eprintln!("Could not determine executable path"); process::exit(1);
    });

    let channels_s = channels.to_string();
    let bps_s      = bps.to_string();
    let rate_s     = sample_rate.to_string();

    eprintln!("Decoding with {} workers...", n_workers);
    let start = Instant::now();

    let mut children = Vec::with_capacity(n_workers);
    for i in 0..n_workers {
        let sp_first     = seek_starts[i];
        let byte_off     = resolve_byte_offset(seektable, sp_first, audio_start);
        let next_sp      = sp_first + seek_counts[i];
        let end_sample   = if next_sp < n_seek { seektable[next_sp].sample_number } else { total_samples };
        let n_samples    = end_sample - seektable[sp_first].sample_number;
        let start_sample = seektable[sp_first].sample_number;

        let child = Command::new(&self_path)
            .arg("--worker")
            .arg(byte_off.to_string())
            .arg(n_samples.to_string())
            .arg(start_sample.to_string())
            .arg(&channels_s)
            .arg(&bps_s)
            .arg(&rate_s)
            .arg(input_path)
            .stdout(Stdio::piped())
            .spawn()
            .unwrap_or_else(|e| { eprintln!("Spawn failed: {}", e); process::exit(1); });

        children.push(child);
    }

    // Drain all worker pipes concurrently — sequential drain deadlocks once
    // a worker's pipe buffer fills.
    let mut reader_threads = Vec::with_capacity(n_workers);
    for child in children.iter_mut() {
        let stdout = child.stdout.take().unwrap();
        reader_threads.push(std::thread::spawn(move || {
            let mut buf = Vec::new();
            BufReader::new(stdout).read_to_end(&mut buf).map(|_| buf)
        }));
    }

    let mut worker_outputs: Vec<Vec<u8>> = Vec::with_capacity(n_workers);
    for (i, t) in reader_threads.into_iter().enumerate() {
        let buf = t.join()
            .unwrap_or_else(|_| { eprintln!("Worker {} reader thread panicked", i); process::exit(1); })
            .unwrap_or_else(|e| { eprintln!("Worker {} read failed: {}", i, e); process::exit(1); });
        worker_outputs.push(buf);
    }

    for (i, mut child) in children.into_iter().enumerate() {
        let status = child.wait().unwrap_or_else(|e| {
            eprintln!("Worker {} wait failed: {}", i, e); process::exit(1);
        });
        if !status.success() {
            eprintln!("Worker {} failed: {}", i, status); process::exit(1);
        }
    }

    eprintln!("Decoding done in {:.3}s ({} workers)", start.elapsed().as_secs_f64(), n_workers);

    let out_file = OpenOptions::new()
        .write(true).create(true).truncate(true)
        .open(output_path)
        .unwrap_or_else(|e| { eprintln!("Error creating output: {}", e); process::exit(1); });

    let mut out = BufWriter::new(out_file);
    write_wav_header(
        &mut out,
        total_samples as u32 * channels as u32,
        channels as u16,
        sample_rate,
        bps,
    ).unwrap();

    for (i, raw) in worker_outputs.iter().enumerate() {
        let samples_i32: Vec<i32> = raw.chunks_exact(4)
            .map(|b| i32::from_le_bytes(b.try_into().unwrap()))
            .collect();
        write_wav_samples(&mut out, &samples_i32, bps).unwrap_or_else(|e| {
            eprintln!("Stitch write failed for worker {}: {}", i, e); process::exit(1);
        });
    }
    out.flush().unwrap();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn read_streaminfo_or_exit<R: Read + Seek>(reader: &mut BitReader<BufReader<R>>) -> crate::metadata::StreamInfo {
    read_streaminfo(reader).unwrap_or_else(|e| {
        eprintln!("Error reading STREAMINFO: {}", e); process::exit(1);
    }).unwrap_or_else(|| {
        eprintln!("Unexpected EOF reading STREAMINFO"); process::exit(1);
    })
}

fn cpu_count() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
}

fn resolve_byte_offset(seektable: &[SeekPoint], idx: usize, audio_start: u64) -> u64 {
    let raw = seektable[idx].byte_offset;
    if seektable[0].byte_offset >= audio_start {
        raw
    } else {
        audio_start + raw
    }
}

pub(crate) fn decode_joint_stereo(channel_assignment: u8, ch0: &mut [i32], ch1: &mut [i32]) {
    match channel_assignment {
        0b1010 => {
            for i in 0..ch0.len() {
                let mid  = ch0[i];
                let side = ch1[i];
                let m    = (mid << 1) | (side & 1);
                ch0[i]   = (m + side) >> 1;
                ch1[i]   = (m - side) >> 1;
            }
        }
        0b1000 => {
            for i in 0..ch0.len() { ch1[i] = ch0[i] - ch1[i]; }
        }
        0b1001 => {
            for i in 0..ch0.len() { ch0[i] = ch1[i] + ch0[i]; }
        }
        _ => {}
    }
}

fn bps_for_channel(channel_assignment: u8, ch: usize, bps: u8) -> u8 {
    match (channel_assignment, ch) {
        (0b1001, 0)             => bps + 1,
        (0b1000, 1) | (0b1010, 1) => bps + 1,
        _ => bps,
    }
}

fn write_wav_header<W: Write>(
    out:         &mut W,
    n_samples:   u32,
    channels:    u16,
    sample_rate: u32,
    bps:         u8,
) -> io::Result<()> {
    let bytes_per_sample = (bps / 8) as u32;
    let data_size        = n_samples * bytes_per_sample;
    let byte_rate        = sample_rate * channels as u32 * bytes_per_sample;
    let block_align      = channels * bytes_per_sample as u16;

    out.write_all(b"RIFF")?;
    out.write_all(&(36 + data_size).to_le_bytes())?;
    out.write_all(b"WAVE")?;
    out.write_all(b"fmt ")?;
    out.write_all(&16u32.to_le_bytes())?;
    out.write_all(&1u16.to_le_bytes())?;
    out.write_all(&channels.to_le_bytes())?;
    out.write_all(&sample_rate.to_le_bytes())?;
    out.write_all(&byte_rate.to_le_bytes())?;
    out.write_all(&block_align.to_le_bytes())?;
    out.write_all(&(bps as u16).to_le_bytes())?;
    out.write_all(b"data")?;
    out.write_all(&data_size.to_le_bytes())?;
    Ok(())
}

fn write_wav_samples<W: Write>(out: &mut W, samples: &[i32], bps: u8) -> io::Result<()> {
    match bps {
        16 => {
            for &s in samples { out.write_all(&(s as i16).to_le_bytes())?; }
        }
        24 => {
            for &s in samples {
                let b = s.to_le_bytes();
                out.write_all(&b[..3])?;
            }
        }
        _ => {
            for &s in samples { out.write_all(&s.to_le_bytes())?; }
        }
    }
    Ok(())
}
