// entropy.rs — ZigZag encoding and Rice coding.

use std::io::{self, Read, Write};

use crate::bitstream::{BitReader, BitWriter};

#[inline]
pub fn zig_zag(n: i32) -> u32 {
    ((n << 1) ^ (n >> 31)) as u32
}

#[inline]
fn un_zig_zag(u: u32) -> i32 {
    ((u >> 1) as i32) ^ -((u & 1) as i32)
}

/// Calculates the optimal Rice parameter `s` for a block of residuals.
pub fn optimize_rice(res: &[i32]) -> u8 {
    if res.is_empty() {
        return 0;
    }
    let sum: u64 = res.iter().map(|&v| zig_zag(v) as u64).sum();
    let avg = sum as f64 / res.len() as f64;
    if avg <= 0.0 {
        return 0;
    }
    let s = ((avg / 1.3863).log2() + 0.5) as i32;
    s.clamp(0, 14) as u8
}

/// Writes a Rice-coded signed integer with parameter `s`.
pub fn write_rice<W: Write>(w: &mut BitWriter<W>, val: i32, s: u8) -> io::Result<usize> {
    let u = zig_zag(val);
    let q = u >> s;
    let r = u & ((1u32 << s) - 1);

    assert!(q <= 30, "Rice quotient overflow — caller must use higher rice_k or verbatim");

    let mut bits = 0usize;

    // Unary: q zeros + terminator 1. Encoded as value=1 in (q+1) bits.
    if q > 0 {
        bits += w.write_bits(1u64, (q + 1) as u8)?;
    } else {
        bits += w.write_bits(1u64, 1)?;
    }

    if s > 0 {
        bits += w.write_bits(r as u64, s)?;
    }

    Ok(bits)
}

/// Reads a Rice-coded signed integer with parameter `s`.
/// Returns `None` on EOF.
pub fn read_rice<R: Read>(r: &mut BitReader<R>, s: u8) -> io::Result<Option<i32>> {
    let q = match r.read_unary()? {
        Some(v) => v,
        None => return Ok(None),
    };
    let rem = if s > 0 {
        match r.read_bits(s)? {
            Some(v) => v as u32,
            None => return Ok(None),
        }
    } else {
        0u32
    };
    let u = (q << (s as u32)) | rem;
    Ok(Some(un_zig_zag(u)))
}

// Expose zig_zag publicly for subframe.rs partition analysis.

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn test_rice_roundtrip() {
        // For each (s, vals) pair, all values must satisfy zig_zag(v) >> s <= 30.
        // s=0: max |v| where zig_zag fits in 30 unary zeros → |v| <= 15
        // s=2: zig_zag(v) >> 2 <= 30 → zig_zag(v) <= 120 → |v| <= 60
        // s=4: zig_zag(v) >> 4 <= 30 → zig_zag(v) <= 480 → |v| <= 240
        let cases: &[(u8, &[i32])] = &[
            (0, &[0, 1, -1, 7, -7]),
            (2, &[0, 1, -1, 7, -7, 50, -50]),
            (4, &[0, 1, -1, 7, -7, 100, -100]),
        ];

        for &(s, vals) in cases {
            let mut buf = Vec::new();
            {
                let mut w = BitWriter::new(&mut buf);
                for &v in vals {
                    write_rice(&mut w, v, s).unwrap();
                }
                w.flush().unwrap();
            }
            let mut r = BitReader::new(Cursor::new(&buf));
            for &v in vals {
                assert_eq!(read_rice(&mut r, s).unwrap(), Some(v),
                           "roundtrip failed for val={} s={}", v, s);
            }
        }
    }

    #[test]
    fn test_rice_encoding() {
        // ZigZag(-1) = 1. q=0, rem=1 with s=2. Bits: '1' '01' = 101xxxxx = 0xA0
        let mut buf = Vec::new();
        let mut w = BitWriter::new(&mut buf);
        write_rice(&mut w, -1, 2).unwrap();
        w.flush().unwrap();
        assert_eq!(buf[0] & 0xe0, 0xa0);
    }
}
