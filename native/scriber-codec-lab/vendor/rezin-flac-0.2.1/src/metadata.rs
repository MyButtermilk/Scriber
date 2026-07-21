// metadata.rs — FLAC stream header, STREAMINFO, and SEEKTABLE.

use std::io::{self, Read, Seek, SeekFrom, Write};

use crate::bitstream::{BitReader, BitWriter};
use rezin_wav::types::Decoder;

#[derive(Debug, Clone, Copy)]
pub struct SeekPoint {
    pub sample_number: u64,
    pub byte_offset:   u64,
    pub frame_samples: u16,
}

/// Parsed contents of a FLAC STREAMINFO block.
#[derive(Debug, Clone)]
pub struct StreamInfo {
    pub min_block_size: u16,
    pub max_block_size: u16,
    pub min_frame_size: u32,
    pub max_frame_size: u32,
    pub sample_rate:    u32,
    pub channels:       u8,
    pub bps:            u8,
    pub total_samples:  u64,
    pub seektable:      Vec<SeekPoint>,
    /// Byte offset of the first audio frame in the file.
    pub audio_start:    u64,
}

/// Writes the "fLaC" marker and STREAMINFO metadata block.
pub fn write_stream_header<W: Write>(w: &mut BitWriter<W>, dec: &Decoder) -> io::Result<usize> {
    let mut total = 0usize;

    // "fLaC" magic (32 bits)
    total += w.write_bits(0x664C6143, 32)?;

    // Metadata block header: not-last(1) | type=0(7) | length=34(24)
    total += w.write_bits(0, 1)?;
    total += w.write_bits(0, 7)?;
    total += w.write_bits(34, 24)?;

    // STREAMINFO payload
    total += w.write_bits(4096, 16)?; // min block size
    total += w.write_bits(4096, 16)?; // max block size
    total += w.write_bits(0,    24)?; // min frame size (unknown)
    total += w.write_bits(0,    24)?; // max frame size (unknown)
    total += w.write_bits(dec.meta.rate as u64,            20)?;
    total += w.write_bits((dec.meta.channels - 1) as u64,  3)?;
    total += w.write_bits((dec.meta.bit_depth - 1) as u64, 5)?;

    let bytes_per_sample = (dec.meta.bit_depth / 8) as u32;
    let total_samples    = dec.data_size / (dec.meta.channels as u32 * bytes_per_sample);
    total += w.write_bits(total_samples as u64, 36)?;

    // MD5 placeholder (128 bits)
    total += w.write_bits(0, 64)?;
    total += w.write_bits(0, 64)?;

    Ok(total)
}

/// Writes a zeroed seektable block as a placeholder.
/// Returns the byte offset of the seektable payload start so the caller
/// can seek back and overwrite it after encoding.
pub fn write_seektable_placeholder<W: Write + Seek>(
    w:         &mut BitWriter<W>,
    fd:        &mut W,
    n_entries: u32,
) -> io::Result<u64> {
    w.flush()?;

    let payload_bytes = n_entries as u64 * 18;
    w.write_bits(1, 1)?;               // last metadata block
    w.write_bits(3, 7)?;               // type 3 = SEEKTABLE
    w.write_bits(payload_bytes, 24)?;
    w.flush()?;

    let payload_offset = fd.seek(SeekFrom::Current(0))?;

    for _ in 0..n_entries {
        w.write_bits(0, 64)?; // sample_number
        w.write_bits(0, 64)?; // byte_offset
        w.write_bits(0, 16)?; // frame_samples
        w.flush()?;
    }

    Ok(payload_offset)
}

/// Seeks back to the seektable payload and overwrites it with real data,
/// then seeks back to EOF.
pub fn write_seektable<F: Write + Seek>(
    fd:             &mut F,
    payload_offset: u64,
    entries:        &[SeekPoint],
) -> io::Result<()> {
    fd.seek(SeekFrom::Start(payload_offset))?;
    let mut buf = [0u8; 18];
    for e in entries {
        buf[0]  = ((e.sample_number >> 56) & 0xFF) as u8;
        buf[1]  = ((e.sample_number >> 48) & 0xFF) as u8;
        buf[2]  = ((e.sample_number >> 40) & 0xFF) as u8;
        buf[3]  = ((e.sample_number >> 32) & 0xFF) as u8;
        buf[4]  = ((e.sample_number >> 24) & 0xFF) as u8;
        buf[5]  = ((e.sample_number >> 16) & 0xFF) as u8;
        buf[6]  = ((e.sample_number >>  8) & 0xFF) as u8;
        buf[7]  = ( e.sample_number        & 0xFF) as u8;
        buf[8]  = ((e.byte_offset >> 56) & 0xFF) as u8;
        buf[9]  = ((e.byte_offset >> 48) & 0xFF) as u8;
        buf[10] = ((e.byte_offset >> 40) & 0xFF) as u8;
        buf[11] = ((e.byte_offset >> 32) & 0xFF) as u8;
        buf[12] = ((e.byte_offset >> 24) & 0xFF) as u8;
        buf[13] = ((e.byte_offset >> 16) & 0xFF) as u8;
        buf[14] = ((e.byte_offset >>  8) & 0xFF) as u8;
        buf[15] = ( e.byte_offset        & 0xFF) as u8;
        buf[16] = ((e.frame_samples >> 8) & 0xFF) as u8;
        buf[17] = ( e.frame_samples       & 0xFF) as u8;
        fd.write_all(&buf)?;
    }
    fd.seek(SeekFrom::End(0))?;
    Ok(())
}

/// Reads and parses FLAC metadata blocks, returning a populated `StreamInfo`.
/// Returns `None` on EOF or malformed stream.
pub fn read_streaminfo<R: Read>(r: &mut BitReader<R>) -> io::Result<Option<StreamInfo>> {
    // Verify "fLaC" magic.
    let magic = match r.read_bits(32)? {
        Some(v) => v,
        None => return Ok(None),
    };
    if magic != 0x664C6143 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid fLaC magic"));
    }

    let mut audio_pos: u64 = 4;
    let mut pre_seektable: Vec<SeekPoint> = Vec::new();

    loop {
        let header = match r.read_bits(32)? {
            Some(v) => v,
            None => return Ok(None),
        };
        let is_last    = (header >> 31) & 1;
        let block_type = (header >> 24) & 0x7F;
        let length     = header & 0x00FFFFFF;
        audio_pos += 4 + length;

        if block_type == 0 {
            // STREAMINFO
            macro_rules! rb {
                ($n:expr) => {
                    match r.read_bits($n)? {
                        Some(v) => v,
                        None    => return Ok(None),
                    }
                };
            }
            let min_block_size = rb!(16) as u16;
            let max_block_size = rb!(16) as u16;
            let min_frame_size = rb!(24) as u32;
            let max_frame_size = rb!(24) as u32;
            let sample_rate    = rb!(20) as u32;
            let channels_raw   = rb!(3)  as u8;
            let bps_raw        = rb!(5)  as u8;
            let total_samples  = rb!(36);
            // MD5: 128 bits — discard.
            for _ in 0..4 { rb!(32); }

            // Walk any remaining metadata blocks.
            let mut last = is_last;
            let mut seektable: Vec<SeekPoint> = Vec::new();
            while last == 0 {
                let h = match r.read_bits(32)? {
                    Some(v) => v,
                    None    => return Ok(None),
                };
                last = (h >> 31) & 1;
                let blk_type = (h >> 24) & 0x7F;
                let blk_len  = h & 0x00FFFFFF;
                audio_pos += 4 + blk_len;
                if blk_type == 3 {
                    seektable = match read_seektable_payload(r, blk_len)? {
                        Some(v) => v,
                        None    => return Ok(None),
                    };
                } else {
                    for _ in 0..blk_len {
                        if r.read_bits(8)?.is_none() {
                            return Ok(None);
                        }
                    }
                }
            }

            let final_seektable = if !seektable.is_empty() {
                seektable
            } else {
                pre_seektable
            };

            return Ok(Some(StreamInfo {
                min_block_size,
                max_block_size,
                min_frame_size,
                max_frame_size,
                sample_rate,
                channels:      channels_raw + 1,
                bps:           bps_raw + 1,
                total_samples,
                seektable:     final_seektable,
                audio_start:   audio_pos,
            }));

        } else if block_type == 3 {
            pre_seektable = match read_seektable_payload(r, length)? {
                Some(v) => v,
                None    => return Ok(None),
            };
        } else {
            for _ in 0..length {
                if r.read_bits(8)?.is_none() {
                    return Ok(None);
                }
            }
        }

        if is_last == 1 && block_type != 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "no STREAMINFO found"));
        }
    }
}

fn read_seektable_payload<R: Read>(
    r:      &mut BitReader<R>,
    length: u64,
) -> io::Result<Option<Vec<SeekPoint>>> {
    let n_entries = (length / 18) as usize;
    let mut entries = Vec::with_capacity(n_entries);

    for _ in 0..n_entries {
        macro_rules! rb {
            ($n:expr) => {
                match r.read_bits($n)? {
                    Some(v) => v,
                    None    => return Ok(None),
                }
            };
        }
        let sn_hi = rb!(32);
        let sn_lo = rb!(32);
        let bo_hi = rb!(32);
        let bo_lo = rb!(32);
        let fs    = rb!(16) as u16;
        entries.push(SeekPoint {
            sample_number: (sn_hi << 32) | sn_lo,
            byte_offset:   (bo_hi << 32) | bo_lo,
            frame_samples: fs,
        });
    }
    Ok(Some(entries))
}
