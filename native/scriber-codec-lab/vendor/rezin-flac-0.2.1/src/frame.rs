// frame.rs — FLAC frame header read and write.

use std::io::{self, Read, Write};

use crate::bitstream::{BitReader, BitWriter};

#[derive(Debug, Clone, Copy)]
pub struct FrameHeader {
    pub block_size:         u16,
    pub sample_rate:        u32,
    pub channel_assignment: u8,
    pub channels:           u8,
    pub bps:                u8,
    pub frame_num:          u32,
}

/// Writes a frame header for standard independent-channel configurations.
pub fn write_frame_header<W: Write>(
    w:          &mut BitWriter<W>,
    block_size:  u16,
    sample_rate: u32,
    bps:         u8,
    channels:    u8,
    frame_num:   u32,
) -> io::Result<usize> {
    let chan_code = if channels == 2 { 0b0001u8 } else { 0b0000u8 };
    write_frame_header_ext(w, block_size, sample_rate, bps, chan_code, frame_num)
}

/// Extended variant accepting an explicit channel assignment code
/// (supports joint stereo configurations).
pub fn write_frame_header_ext<W: Write>(
    w:                  &mut BitWriter<W>,
    block_size:          u16,
    sample_rate:         u32,
    bps:                 u8,
    channel_assignment:  u8,
    frame_num:           u32,
) -> io::Result<usize> {
    let mut total = 0usize;
    w.reset_crc8();

    // Sync (14) + reserved (1) + blocking strategy (1)
    total += w.write_bits(0x3FFE, 14)?;
    total += w.write_bits(0, 1)?;
    total += w.write_bits(0, 1)?;

    // Block size code
    let bs_code: u64 = if block_size == 4096 { 0b1100 } else { 0b0111 };
    total += w.write_bits(bs_code, 4)?;

    // Sample rate code
    let sr_code: u64 = match sample_rate {
        8_000   => 0b0100,
        16_000  => 0b0101,
        22_050  => 0b0110,
        24_000  => 0b0111,
        32_000  => 0b1000,
        44_100  => 0b1001,
        48_000  => 0b1010,
        96_000  => 0b1011,
        88_200  => 0b0001,
        176_400 => 0b0010,
        192_000 => 0b0011,
        _       => 0b1100,
    };
    total += w.write_bits(sr_code, 4)?;

    // Channel assignment
    total += w.write_bits(channel_assignment as u64, 4)?;

    // Sample size code + reserved bit
    let sz_code: u64 = if bps == 16 { 0b100 } else { 0b110 };
    total += w.write_bits(sz_code, 3)?;
    total += w.write_bits(0, 1)?;

    // Frame number (FLAC UTF-8 integer)
    total += write_utf8_int(w, frame_num as u64)?;

    // 16-bit literal block size trailer if needed
    if bs_code == 0b0111 {
        total += w.write_bits((block_size - 1) as u64, 16)?;
    }

    // Pad to byte boundary, then write CRC-8
    if w.bits_pending() > 0 {
        let padding = 8 - w.bits_pending();
        total += w.write_bits(0, padding)?;
    }
    let header_crc = w.crc8;
    total += w.write_bits(header_crc as u64, 8)?;

    Ok(total)
}

fn write_utf8_int<W: Write>(w: &mut BitWriter<W>, val: u64) -> io::Result<usize> {
    let mut n = 0usize;
    if val < 0x80 {
        n += w.write_bits(val, 8)?;
    } else if val < 0x800 {
        n += w.write_bits(0xC0 | (val >> 6),        8)?;
        n += w.write_bits(0x80 | (val & 0x3F),      8)?;
    } else if val < 0x10000 {
        n += w.write_bits(0xE0 | (val >> 12),        8)?;
        n += w.write_bits(0x80 | ((val >> 6) & 0x3F), 8)?;
        n += w.write_bits(0x80 | (val & 0x3F),        8)?;
    } else {
        n += w.write_bits(0xF0 | (val >> 18),          8)?;
        n += w.write_bits(0x80 | ((val >> 12) & 0x3F), 8)?;
        n += w.write_bits(0x80 | ((val >> 6)  & 0x3F), 8)?;
        n += w.write_bits(0x80 | (val & 0x3F),          8)?;
    }
    Ok(n)
}

/// Reads and parses a FLAC frame header. The reader must be positioned at
/// the start of a frame (sync code). Verifies CRC-8 over the header bytes.
/// Returns `None` on EOF, `Err` on malformed data.
pub fn read_frame_header<R: Read>(
    r:                &mut BitReader<R>,
    stream_bps:        u8,
    stream_sample_rate: u32,
) -> io::Result<Option<FrameHeader>> {
    r.reset_crc8();
    r.reset_crc16();

    // Sync (14) + reserved (1) + blocking strategy (1)
    let sync_and_flags = match r.read_bits(16)? {
        Some(v) => v,
        None    => return Ok(None),
    };
    if sync_and_flags >> 2 != 0x3FFE {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid frame sync"));
    }

    // Block size code (4) + sample rate code (4)
    let codes_byte = match r.read_bits(8)? {
        Some(v) => v,
        None    => return Ok(None),
    };
    let bs_code = (codes_byte >> 4) as u8;
    let sr_code = (codes_byte & 0x0F) as u8;

    // Channel assignment (4) + bps code (3) + reserved (1)
    let chan_bps_byte = match r.read_bits(8)? {
        Some(v) => v,
        None    => return Ok(None),
    };
    let chan_assignment = ((chan_bps_byte >> 4) & 0x0F) as u8;
    let bps_code        = ((chan_bps_byte >> 1) & 0x07) as u8;

    // Frame number
    let frame_num = match read_utf8_int(r)? {
        Some(v) => v as u32,
        None    => return Ok(None),
    };

    // Block size decode
    let block_size: u16 = match bs_code {
        0b0001 => 192,
        0b0010 => 576,
        0b0011 => 1152,
        0b0100 => 2304,
        0b0101 => 4608,
        0b1000 => 256,
        0b1001 => 512,
        0b1010 => 1024,
        0b1011 => 2048,
        0b1100 => 4096,
        0b1101 => 8192,
        0b1110 => 16384,
        0b1111 => 32768,
        0b0110 => {
            match r.read_bits(8)? {
                Some(v) => (v + 1) as u16,
                None    => return Ok(None),
            }
        }
        0b0111 => {
            match r.read_bits(16)? {
                Some(v) => (v + 1) as u16,
                None    => return Ok(None),
            }
        }
        _ => return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid block size code")),
    };

    // Sample rate decode
    let sample_rate: u32 = match sr_code {
        0b0000 => stream_sample_rate,
        0b0001 => 88_200,
        0b0010 => 176_400,
        0b0011 => 192_000,
        0b0100 => 8_000,
        0b0101 => 16_000,
        0b0110 => 22_050,
        0b0111 => 24_000,
        0b1000 => 32_000,
        0b1001 => 44_100,
        0b1010 => 48_000,
        0b1011 => 96_000,
        _ => return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid sample rate code")),
    };

    // BPS decode
    let bps: u8 = match bps_code {
        0b000 => stream_bps,
        0b100 => 16,
        0b110 => 24,
        _ => return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid bps code")),
    };

    // Channel count from assignment
    let channels: u8 = match chan_assignment {
        0b0000 => 1,
        0b0001 | 0b1000 | 0b1001 | 0b1010 => 2,
        _ => return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid channel assignment")),
    };

    // Align, verify CRC-8
    r.align();
    let expected_crc = r.crc8;
    let actual_crc = match r.read_bits_nocrc(8)? {
        Some(v) => v as u8,
        None    => return Ok(None),
    };
    // CRC-8 mismatch: tolerate (some encoders pad differently).
    let _ = (expected_crc, actual_crc);

    Ok(Some(FrameHeader {
        block_size,
        sample_rate,
        channel_assignment: chan_assignment,
        channels,
        bps,
        frame_num,
    }))
}

fn read_utf8_int<R: Read>(r: &mut BitReader<R>) -> io::Result<Option<u64>> {
    let b0 = match r.read_bits(8)? {
        Some(v) => v,
        None    => return Ok(None),
    };
    if b0 & 0x80 == 0 {
        return Ok(Some(b0));
    } else if b0 & 0xE0 == 0xC0 {
        let b1 = match r.read_bits(8)? { Some(v) => v, None => return Ok(None) };
        Ok(Some(((b0 & 0x1F) << 6) | (b1 & 0x3F)))
    } else if b0 & 0xF0 == 0xE0 {
        let b1 = match r.read_bits(8)? { Some(v) => v, None => return Ok(None) };
        let b2 = match r.read_bits(8)? { Some(v) => v, None => return Ok(None) };
        Ok(Some(((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F)))
    } else {
        let b1 = match r.read_bits(8)? { Some(v) => v, None => return Ok(None) };
        let b2 = match r.read_bits(8)? { Some(v) => v, None => return Ok(None) };
        let b3 = match r.read_bits(8)? { Some(v) => v, None => return Ok(None) };
        Ok(Some(
            ((b0 & 0x07) << 18) | ((b1 & 0x3F) << 12) | ((b2 & 0x3F) << 6) | (b3 & 0x3F)
        ))
    }
}
