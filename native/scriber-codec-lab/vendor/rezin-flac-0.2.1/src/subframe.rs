// subframe.rs — FLAC subframe encoding and decoding.

use std::io::{self, Read, Write};

use rezin_lpc::fixed::{predict_fixed, restore_fixed};
use rezin_lpc::quantize::{predict_lpc, restore_lpc};

use crate::bitstream::{BitReader, BitWriter};
use crate::entropy::{optimize_rice, read_rice, write_rice, zig_zag};

const MAX_BLOCK: usize = 4096;

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

/// Writes a subframe. If `q_lpc` is empty, selects the best fixed predictor.
/// Otherwise uses the supplied LPC coefficients.
pub fn write_subframe_lpc<W: Write>(
    w:       &mut BitWriter<W>,
    samples: &[i32],
    q_lpc:   &[i32],
    shift:   u8,
    bps:     u8,
) -> io::Result<usize> {
    let n = samples.len();

    if q_lpc.is_empty() {
        // No LPC coefficients — find the best fixed predictor order.
        let max_order = n.min(4) as u8;
        let mut best_order  = 0u8;
        let mut best_sum    = f64::INFINITY;
        let mut best_res    = vec![0i32; n];
        let mut fixed_res   = vec![0i32; n];

        for order in 0..=max_order {
            predict_fixed(samples, order, &mut fixed_res);
            let sum: u64 = fixed_res[order as usize..].iter()
                .map(|&v| if v >= 0 { v as u64 } else { -(v as i64) as u64 })
                .sum();
            if (sum as f64) < best_sum {
                best_sum   = sum as f64;
                best_order = order;
                best_res.copy_from_slice(&fixed_res);
            }
        }
        return write_subframe_fixed(w, samples, &best_res, best_order, bps);
    }

    // LPC subframe.
    let p = q_lpc.len();
    let mut lpc_res = vec![0i32; n];
    predict_lpc(samples, q_lpc, shift, &mut lpc_res);

    let mut total = 0usize;
    let order = p as u8;
    let subframe_type = (0b00100000u64 | (order - 1) as u64) << 1;
    total += w.write_bits(subframe_type, 8)?;

    // Warmup samples
    for i in 0..p {
        total += w.write_bits(samples[i] as i64 as u64, bps)?;
    }

    let coeff_precision: u8 = 15;
    total += w.write_bits((coeff_precision - 1) as u64, 4)?;
    total += w.write_bits(shift as u64, 5)?;
    for i in 0..p {
        total += w.write_bits(
            (q_lpc[i] as u64) & ((1u64 << coeff_precision) - 1),
            coeff_precision,
        )?;
    }

    total += write_residuals(w, &lpc_res[p..], order)?;
    Ok(total)
}

fn write_subframe_fixed<W: Write>(
    w:         &mut BitWriter<W>,
    samples:   &[i32],
    residuals: &[i32],
    order:     u8,
    bps:       u8,
) -> io::Result<usize> {
    let mut total = 0usize;
    let subframe_type = ((0b001000u64 | order as u64) << 1) as u64;
    total += w.write_bits(subframe_type, 8)?;

    for i in 0..order as usize {
        total += w.write_bits(samples[i] as i64 as u64, bps)?;
    }

    total += write_residuals(w, &residuals[order as usize..], order)?;
    Ok(total)
}

pub fn write_subframe_constant<W: Write>(
    w:     &mut BitWriter<W>,
    value: i32,
    bps:   u8,
) -> io::Result<usize> {
    let mut total = 0usize;
    total += w.write_bits(0x00, 8)?; // type 0b000000, wasted=0
    total += w.write_bits(value as i64 as u64, bps)?;
    Ok(total)
}

fn best_partition_order(residuals: &[i32], predictor_order: u8) -> u8 {
    let n = residuals.len();
    let block_size = n + predictor_order as usize;
    let mut best_order = 0u8;
    let mut best_bits  = u64::MAX;

    for order in 0u8..=4 {
        let num_partitions = 1u32 << order;
        if block_size as u32 % num_partitions != 0 { continue; }
        let part_size = block_size as u32 / num_partitions;
        if part_size <= predictor_order as u32 { continue; }

        let mut use_rice2 = false;
        let mut pos = 0usize;
        for p in 0..num_partitions {
            let count = if p == 0 {
                (part_size - predictor_order as u32) as usize
            } else {
                part_size as usize
            };
            let partition = &residuals[pos..pos + count];
            let mut rice_k = optimize_rice(partition);
            let max_zz = partition.iter().map(|&v| zig_zag(v)).max().unwrap_or(0);
            while max_zz >> rice_k > 30 && rice_k < 30 { rice_k += 1; }
            if rice_k > 14 { use_rice2 = true; }
            pos += count;
        }

        let k_bits: u64 = if use_rice2 { 5 } else { 4 };
        let mut estimated_bits: u64 = 0;
        pos = 0;
        for p in 0..num_partitions {
            let count = if p == 0 {
                (part_size - predictor_order as u32) as usize
            } else {
                part_size as usize
            };
            let partition = &residuals[pos..pos + count];
            let mut rice_k = optimize_rice(partition);
            let max_zz = partition.iter().map(|&v| zig_zag(v)).max().unwrap_or(0);
            while max_zz >> rice_k > 30 && rice_k < 30 { rice_k += 1; }

            estimated_bits += k_bits;
            for &v in partition {
                let zz = zig_zag(v);
                estimated_bits += (zz >> rice_k) as u64 + 1 + rice_k as u64;
            }
            pos += count;
        }
        if estimated_bits < best_bits {
            best_bits  = estimated_bits;
            best_order = order;
        }
    }

    best_order
}

fn write_residuals<W: Write>(
    w:               &mut BitWriter<W>,
    residuals:       &[i32],
    predictor_order: u8,
) -> io::Result<usize> {
    let mut total = 0usize;
    let partition_order = best_partition_order(residuals, predictor_order);
    let block_size      = residuals.len() + predictor_order as usize;
    let num_partitions  = 1u32 << partition_order;
    let part_size       = block_size as u32 / num_partitions;

    // First pass: determine if any partition needs Rice2 (k > 14).
    let mut use_rice2 = false;
    let mut pos = 0usize;
    for p in 0..num_partitions {
        let count = if p == 0 {
            (part_size - predictor_order as u32) as usize
        } else {
            part_size as usize
        };
        let end = pos + count;
        let mut rice_k = optimize_rice(&residuals[pos..end]);
        let max_zz = residuals[pos..end].iter().map(|&v| zig_zag(v)).max().unwrap_or(0);
        while max_zz >> rice_k > 30 && rice_k < 30 { rice_k += 1; }
        if rice_k > 14 { use_rice2 = true; }
        pos += count;
    }

    total += w.write_bits(if use_rice2 { 1 } else { 0 }, 2)?;
    total += w.write_bits(partition_order as u64, 4)?;

    // Second pass: write parameters and samples.
    pos = 0;
    for p in 0..num_partitions {
        let count = if p == 0 {
            (part_size - predictor_order as u32) as usize
        } else {
            part_size as usize
        };
        let end = pos + count;
        let mut rice_k = optimize_rice(&residuals[pos..end]);
        let max_zz = residuals[pos..end].iter().map(|&v| zig_zag(v)).max().unwrap_or(0);
        while max_zz >> rice_k > 30 && rice_k < 30 { rice_k += 1; }

        total += w.write_bits(rice_k as u64, if use_rice2 { 5 } else { 4 })?;
        for i in pos..end {
            total += write_rice(w, residuals[i], rice_k)?;
        }
        pos += count;
    }

    Ok(total)
}

// ---------------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------------

/// Reads and decodes a single FLAC subframe into `out`.
/// `bps` is the effective bits-per-sample (may be bps+1 for side channel).
pub fn read_subframe<R: Read>(
    r:   &mut BitReader<R>,
    out: &mut [i32],
    bps: u8,
) -> io::Result<()> {
    let _n = out.len();

    let hdr = match r.read_bits(8)? {
        Some(v) => v,
        None    => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "EOF in subframe header")),
    };

    // Wasted bits flag
    let mut wasted = 0u8;
    if hdr & 1 == 1 {
        wasted = match r.read_unary()? {
            Some(v) => (v + 1) as u8,
            None    => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "EOF in wasted bits")),
        };
    }
    let effective_bps = bps - wasted;
    let subframe_type = ((hdr >> 1) & 0x3F) as u8;

    if subframe_type == 0b000000 {
        read_subframe_constant(r, out, effective_bps, wasted)?;
    } else if subframe_type == 0b000010 {
        read_subframe_verbatim(r, out, effective_bps, wasted)?;
    } else if subframe_type & 0b111000 == 0b001000 {
        let order = subframe_type & 0b111;
        read_subframe_fixed(r, out, effective_bps, order, wasted)?;
    } else if subframe_type & 0b100000 == 0b100000 {
        let order = (subframe_type & 0b011111) + 1;
        read_subframe_lpc(r, out, effective_bps, order, wasted)?;
    } else {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "unknown subframe type"));
    }

    Ok(())
}

fn read_subframe_constant<R: Read>(
    r:      &mut BitReader<R>,
    out:    &mut [i32],
    bps:    u8,
    wasted: u8,
) -> io::Result<()> {
    let raw = match r.read_bits(bps)? {
        Some(v) => v,
        None    => return Err(eof()),
    };
    let value = (sign_extend(raw, bps) as u32).wrapping_shl(wasted as u32) as i32;
    out.fill(value);
    Ok(())
}

fn read_subframe_verbatim<R: Read>(
    r:      &mut BitReader<R>,
    out:    &mut [i32],
    bps:    u8,
    wasted: u8,
) -> io::Result<()> {
    for s in out.iter_mut() {
        let raw = match r.read_bits(bps)? {
            Some(v) => v,
            None    => return Err(eof()),
        };
        *s = (sign_extend(raw, bps) as u32).wrapping_shl(wasted as u32) as i32;
    }
    Ok(())
}

fn read_subframe_fixed<R: Read>(
    r:      &mut BitReader<R>,
    out:    &mut [i32],
    bps:    u8,
    order:  u8,
    wasted: u8,
) -> io::Result<()> {
    let n = out.len();
    for i in 0..order as usize {
        let raw = match r.read_bits(bps)? {
            Some(v) => v,
            None    => return Err(eof()),
        };
        out[i] = (sign_extend(raw, bps) as u32).wrapping_shl(wasted as u32) as i32;
    }

    read_residuals(r, &mut out[order as usize..], order)?;

    // Reconstruct in-place using a temporary buffer.
    let mut tmp = vec![0i32; n];
    restore_fixed(out, order, &mut tmp);
    out.copy_from_slice(&tmp);

    if wasted > 0 {
        for s in out[order as usize..].iter_mut() {
            *s = (*s as u32).wrapping_shl(wasted as u32) as i32;
        }
    }
    Ok(())
}

fn read_subframe_lpc<R: Read>(
    r:      &mut BitReader<R>,
    out:    &mut [i32],
    bps:    u8,
    order:  u8,
    wasted: u8,
) -> io::Result<()> {
    let n = out.len();
    for i in 0..order as usize {
        let raw = match r.read_bits(bps)? {
            Some(v) => v,
            None    => return Err(eof()),
        };
        out[i] = (sign_extend(raw, bps) as u32).wrapping_shl(wasted as u32) as i32;
    }

    let prec_raw = match r.read_bits(4)? {
        Some(v) => v,
        None    => return Err(eof()),
    };
    let precision = (prec_raw + 1) as u8;

    let shift_raw = match r.read_bits(5)? {
        Some(v) => v,
        None    => return Err(eof()),
    };
    let shift = shift_raw as u8;

    let mut q_lpc = [0i32; 12];
    for i in 0..order as usize {
        let raw = match r.read_bits(precision)? {
            Some(v) => v,
            None    => return Err(eof()),
        };
        q_lpc[i] = sign_extend(raw, precision);
    }

    read_residuals(r, &mut out[order as usize..], order)?;

    let mut tmp = vec![0i32; n];
    restore_lpc(out, &q_lpc[..order as usize], shift, &mut tmp);
    out.copy_from_slice(&tmp);

    if wasted > 0 {
        for s in out[order as usize..].iter_mut() {
            *s = (*s as u32).wrapping_shl(wasted as u32) as i32;
        }
    }
    Ok(())
}

fn read_residuals<R: Read>(
    r:               &mut BitReader<R>,
    out:             &mut [i32],
    predictor_order: u8,
) -> io::Result<()> {
    let method = match r.read_bits(2)? {
        Some(v) => v,
        None    => return Err(eof()),
    };
    let param_bits: u8 = if method == 0 { 4 } else { 5 };

    let partition_order = match r.read_bits(4)? {
        Some(v) => v as u8,
        None    => return Err(eof()),
    };

    let block_size      = out.len() + predictor_order as usize;
    let num_partitions  = 1u32 << partition_order;
    let part_size       = block_size as u32 / num_partitions;
    let mut pos         = 0usize;

    for p in 0..num_partitions {
        let count = if p == 0 {
            (part_size - predictor_order as u32) as usize
        } else {
            part_size as usize
        };
        let rice_k = match r.read_bits(param_bits)? {
            Some(v) => v as u8,
            None    => return Err(eof()),
        };
        for i in 0..count {
            out[pos + i] = match read_rice(r, rice_k)? {
                Some(v) => v,
                None    => return Err(eof()),
            };
        }
        pos += count;
    }
    Ok(())
}

fn sign_extend(val: u64, bits: u8) -> i32 {
    if bits == 0 { return 0; }
    let sign_bit = 1u64 << (bits - 1);
    if val & sign_bit != 0 {
        let mask = !((sign_bit << 1).wrapping_sub(1));
        (val | mask) as i64 as i32
    } else {
        val as i32
    }
}

#[inline]
fn eof() -> io::Error {
    io::Error::new(io::ErrorKind::UnexpectedEof, "unexpected EOF in subframe")
}
