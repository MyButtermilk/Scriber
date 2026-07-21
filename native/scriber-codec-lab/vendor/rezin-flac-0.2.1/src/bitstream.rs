// bitstream.rs — MSB-first bit writer and bit reader.

use std::io::{self, Read, Write};

use crate::crc::{crc8_update, crc16_update};

const OUTBUF_SIZE: usize = 65536;
const INBUF_SIZE:  usize = 65536;

// ---------------------------------------------------------------------------
// BitWriter
// ---------------------------------------------------------------------------

pub struct BitWriter<W: Write> {
    handle:       W,
    /// Partial-byte accumulator; bits are packed MSB-first into the low bits.
    /// Only the low `bits_used` bits are valid.
    buffer:       u8,
    bits_used:    u8,
    pub crc8:     u8,
    pub crc16:    u16,
    outbuf:       Box<[u8; OUTBUF_SIZE]>,
    outbuf_len:   usize,
    bytes_written: u64,
}

impl<W: Write> BitWriter<W> {
    pub fn new(handle: W) -> Self {
        Self {
            handle,
            buffer:        0,
            bits_used:     0,
            crc8:          0,
            crc16:         0,
            outbuf:        Box::new([0u8; OUTBUF_SIZE]),
            outbuf_len:    0,
            bytes_written: 0,
        }
    }

    fn flush_outbuf(&mut self) -> io::Result<()> {
        if self.outbuf_len == 0 {
            return Ok(());
        }
        self.handle.write_all(&self.outbuf[..self.outbuf_len])?;
        self.outbuf_len = 0;
        Ok(())
    }

    fn emit_byte(&mut self, b: u8) -> io::Result<()> {
        self.crc8  = crc8_update(self.crc8, b);
        self.crc16 = crc16_update(self.crc16, b);
        self.outbuf[self.outbuf_len] = b;
        self.outbuf_len += 1;
        self.bytes_written += 1;
        if self.outbuf_len == OUTBUF_SIZE {
            self.flush_outbuf()?;
        }
        Ok(())
    }

    /// Writes `count` bits of `value`, MSB-first. `count` must be ≤ 64.
    pub fn write_bits(&mut self, value: u64, count: u8) -> io::Result<usize> {
        if count == 0 {
            return Ok(0);
        }
        let mut bytes_written = 0usize;
        let mut remaining = count;
        let clean = if count == 64 { value } else { value & ((1u64 << count) - 1) };

        while remaining > 0 {
            let space = 8 - self.bits_used;
            if remaining >= space {
                let shift = remaining - space;
                let bits = ((clean >> shift) & ((1u64 << space) - 1)) as u8;
                let byte = (self.buffer << (space % 8)) | bits;
                self.emit_byte(byte)?;
                bytes_written += 1;
                remaining -= space;
                self.bits_used = 0;
                self.buffer = 0;
            } else {
                self.buffer = (self.buffer << remaining)
                    | (clean as u8 & ((1u8 << remaining) - 1));
                self.bits_used += remaining;
                remaining = 0;
            }
        }
        Ok(bytes_written)
    }

    /// Writes `count` zero bits. Optimised for unary Rice coding.
    pub fn write_zeros(&mut self, count: u32) -> io::Result<usize> {
        if count == 0 {
            return Ok(0);
        }
        let mut total_written = 0usize;
        let mut remaining = count;

        // Fill any partial byte first.
        if self.bits_used > 0 {
            let fill = (8 - self.bits_used) as u32;
            if remaining < fill {
                self.buffer <<= remaining as u8;
                self.bits_used += remaining as u8;
                return Ok(0);
            }
            self.buffer <<= fill as u8;
            self.emit_byte(self.buffer)?;
            total_written += 1;
            self.buffer = 0;
            self.bits_used = 0;
            remaining -= fill;
        }

        // Emit whole zero bytes.
        let zero_bytes = remaining / 8;
        for _ in 0..zero_bytes {
            self.emit_byte(0)?;
            total_written += 1;
        }
        remaining %= 8;

        // Queue leftover bits.
        if remaining > 0 {
            self.buffer = 0;
            self.bits_used = remaining as u8;
        }

        Ok(total_written)
    }

    /// Writes raw bytes, flushing any pending partial byte first.
    pub fn write_bytes(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        if self.bits_used > 0 {
            let final_byte = self.buffer << (8 - self.bits_used);
            self.emit_byte(final_byte)?;
            self.buffer = 0;
            self.bits_used = 0;
        }
        for &b in bytes {
            self.emit_byte(b)?;
        }
        self.flush_outbuf()?;
        Ok(bytes.len())
    }

    /// Writes raw bytes directly to the output buffer without updating CRC accumulators.
    /// Use this when stitching pre-encoded frame data where CRC was already finalized.
    pub fn write_bytes_raw(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        // Must be byte-aligned — caller is responsible for ensuring this.
        debug_assert_eq!(self.bits_used, 0, "write_bytes_raw called with pending bits");
        self.bytes_written += bytes.len() as u64;

        let mut pos = 0;
        while pos < bytes.len() {
            let space = OUTBUF_SIZE - self.outbuf_len;
            let chunk = (bytes.len() - pos).min(space);
            self.outbuf[self.outbuf_len..self.outbuf_len + chunk]
                .copy_from_slice(&bytes[pos..pos + chunk]);
            self.outbuf_len += chunk;
            pos += chunk;
            if self.outbuf_len == OUTBUF_SIZE {
                self.handle.write_all(&self.outbuf[..self.outbuf_len])?;
                self.outbuf_len = 0;
            }
        }
        Ok(bytes.len())
    }

    /// Pads to next byte boundary with zero bits (padding excluded from CRCs).
    /// Flushes the output buffer.
    pub fn flush(&mut self) -> io::Result<usize> {
        let mut n = 0;
        if self.bits_used > 0 {
            let final_byte = self.buffer << (8 - self.bits_used);
            // Write directly to outbuf without updating CRCs (per FLAC spec).
            self.outbuf[self.outbuf_len] = final_byte;
            self.outbuf_len += 1;
            n += 1;
            self.buffer = 0;
            self.bits_used = 0;
        }
        self.flush_outbuf()?;
        Ok(n)
    }

    /// Frame header flush: the final partial byte IS included in CRC-8.
    pub fn flush_header(&mut self) -> io::Result<()> {
        if self.bits_used > 0 {
            self.buffer <<= 8 - self.bits_used;
            let b = self.buffer;
            self.emit_byte(b)?;
            self.buffer = 0;
            self.bits_used = 0;
        }
        self.flush_outbuf()
    }

    /// Finalizes a frame: flushes the last partial byte into CRC-16,
    /// flushes the output buffer, and returns the CRC-16.
    pub fn finalize_frame(&mut self) -> io::Result<u16> {
        if self.bits_used > 0 {
            self.buffer <<= 8 - self.bits_used;
            let b = self.buffer;
            self.emit_byte(b)?;
            self.buffer = 0;
            self.bits_used = 0;
        }
        self.flush_outbuf()?;
        Ok(self.crc16)
    }

    pub fn reset_crc8(&mut self)  { self.crc8  = 0; }
    pub fn reset_crc16(&mut self) { self.crc16 = 0; }
    pub fn bits_pending(&self) -> u8  { self.bits_used }
    pub fn bytes_written(&self) -> u64 { self.bytes_written }
}

// ---------------------------------------------------------------------------
// BitReader
// ---------------------------------------------------------------------------

pub struct BitReader<R: Read> {
    handle:     R,
    /// Staging word: bits held MSB-first in the high `bits_avail` bits.
    bits:       u64,
    bits_avail: u8,
    inbuf:      Box<[u8; INBUF_SIZE]>,
    inbuf_pos:  usize,
    inbuf_len:  usize,
    pub crc8:   u8,
    pub crc16:  u16,
}

impl<R: Read> BitReader<R> {
    pub fn new(handle: R) -> Self {
        Self {
            handle,
            bits:       0,
            bits_avail: 0,
            inbuf:      Box::new([0u8; INBUF_SIZE]),
            inbuf_pos:  0,
            inbuf_len:  0,
            crc8:       0,
            crc16:      0,
        }
    }

    pub fn reset_crc8(&mut self)  { self.crc8  = 0; }
    pub fn reset_crc16(&mut self) { self.crc16 = 0; }
    pub fn bits_avail(&self) -> u8 { self.bits_avail }

    fn refill_byte(&mut self) -> io::Result<bool> {
        if self.inbuf_pos >= self.inbuf_len {
            let n = self.handle.read(&mut self.inbuf[..])?;
            if n == 0 {
                return Ok(false); // EOF
            }
            self.inbuf_pos = 0;
            self.inbuf_len = n;
        }
        let b = self.inbuf[self.inbuf_pos];
        self.inbuf_pos += 1;
        self.crc8  = crc8_update(self.crc8, b);
        self.crc16 = crc16_update(self.crc16, b);
        self.bits |= (b as u64) << (56 - self.bits_avail);
        self.bits_avail += 8;
        Ok(true)
    }

    /// Reads exactly `count` bits (1–57), MSB-first. Returns `None` on EOF.
    pub fn read_bits(&mut self, count: u8) -> io::Result<Option<u64>> {
        debug_assert!(count > 0 && count <= 57, "read_bits: count must be 1–57");
        while self.bits_avail < count {
            if !self.refill_byte()? {
                return Ok(None);
            }
        }
        let val = self.bits >> (64 - count);
        self.bits <<= count;
        self.bits_avail -= count;
        Ok(Some(val))
    }

    /// Reads `count` bits without updating CRC accumulators.
    /// Used when reading checksum fields.
    pub fn read_bits_nocrc(&mut self, count: u8) -> io::Result<Option<u64>> {
        debug_assert!(count > 0 && count <= 57, "read_bits_nocrc: count must be 1–57");
        while self.bits_avail < count {
            let crc8_save  = self.crc8;
            let crc16_save = self.crc16;
            if !self.refill_byte()? {
                return Ok(None);
            }
            self.crc8  = crc8_save;
            self.crc16 = crc16_save;
        }
        let val = self.bits >> (64 - count);
        self.bits <<= count;
        self.bits_avail -= count;
        Ok(Some(val))
    }

    /// Reads a unary-coded non-negative integer (counts leading zeros
    /// up to the terminating 1). Returns the zero count (Rice quotient).
    pub fn read_unary(&mut self) -> io::Result<Option<u32>> {
        let mut q = 0u32;
        loop {
            while self.bits_avail == 0 {
                if !self.refill_byte()? {
                    return Ok(None);
                }
            }
            let leading = leading_zeros_in_staging(self.bits, self.bits_avail);
            if leading < self.bits_avail {
                q += leading as u32;
                self.bits <<= leading + 1;
                self.bits_avail -= leading + 1;
                return Ok(Some(q));
            }
            q += self.bits_avail as u32;
            self.bits = 0;
            self.bits_avail = 0;
        }
    }

    /// Discards bits until the reader is byte-aligned.
    pub fn align(&mut self) {
        let leftover = self.bits_avail % 8;
        if leftover != 0 {
            self.bits <<= leftover;
            self.bits_avail -= leftover;
        }
    }
}

/// Counts leading zero bits in the top `avail` bits of a u64 staging word.
fn leading_zeros_in_staging(bits: u64, avail: u8) -> u8 {
    let mut n = 0u8;
    let mut mask = 1u64 << 63;
    while n < avail {
        if bits & mask != 0 {
            break;
        }
        n += 1;
        mask >>= 1;
    }
    n
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn test_bit_writer_basic() {
        let mut buf = Vec::new();
        let mut w = BitWriter::new(&mut buf);
        w.write_bits(0b1011, 4).unwrap();
        w.write_bits(0b0001, 4).unwrap();
        w.write_bits(0b1, 1).unwrap();
        w.flush().unwrap();
        assert_eq!(buf[0], 0xb1);
        assert_eq!(buf[1], 0x80);
    }

    #[test]
    fn test_bit_reader_roundtrip() {
        let mut buf = Vec::new();
        {
            let mut w = BitWriter::new(&mut buf);
            w.write_bits(0b1011, 4).unwrap();
            w.write_bits(0b01,   2).unwrap();
            w.write_bits(0b1,    1).unwrap();
            w.write_bits(0b0,    1).unwrap();
            w.flush().unwrap();
        }
        let mut r = BitReader::new(Cursor::new(&buf));
        assert_eq!(r.read_bits(4).unwrap(), Some(0b1011));
        assert_eq!(r.read_bits(2).unwrap(), Some(0b01));
        assert_eq!(r.read_bits(1).unwrap(), Some(0b1));
        assert_eq!(r.read_bits(1).unwrap(), Some(0b0));
    }

    #[test]
    fn test_read_unary() {
        let mut buf = Vec::new();
        {
            let mut w = BitWriter::new(&mut buf);
            w.write_bits(1u64, 1).unwrap();   // q=0
            w.write_zeros(3).unwrap();         // q=3: '000'
            w.write_bits(1u64, 1).unwrap();    //       '1'
            w.flush().unwrap();
        }
        let mut r = BitReader::new(Cursor::new(&buf));
        assert_eq!(r.read_unary().unwrap(), Some(0));
        assert_eq!(r.read_unary().unwrap(), Some(3));
    }
}
