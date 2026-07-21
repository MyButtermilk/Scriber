// stream.rs — Frame-by-frame FLAC decoder for streaming playback.
//
// FlacStream reads one FLAC frame at a time into a caller-provided buffer,
// suitable for feeding a ring buffer or audio callback without decoding the
// entire file into memory first.

use std::fs::File;
use std::io::{self, BufReader, Read, Seek};

use crate::bitstream::BitReader;
use crate::decode::decode_joint_stereo;
use crate::frame::read_frame_header;
use crate::metadata::read_streaminfo;
use crate::subframe::read_subframe;

const MAX_BLOCK: usize = 65535;

pub struct FlacStream<R: Read + Seek> {
    reader:            BitReader<BufReader<R>>,
    pub channels:      usize,
    pub sample_rate:   u32,
    pub bps:           u8,
    pub total_samples: u64,
    samples_decoded:   u64,
    ch0:               Vec<i32>,
    ch1:               Vec<i32>,
}

impl FlacStream<File> {
    /// Opens a FLAC file and reads the STREAMINFO block.
    pub fn open(path: &str) -> io::Result<Self> {
        let file   = File::open(path)?;
        let reader = BitReader::new(BufReader::new(file));
        Self::from_reader(reader)
    }
}

impl<R: Read + Seek> FlacStream<R> {
    /// Constructs a FlacStream from an existing reader.
    pub fn from_reader(mut reader: BitReader<BufReader<R>>) -> io::Result<Self> {
        let info = read_streaminfo(&mut reader)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e.to_string()))?
            .ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "missing STREAMINFO"))?;

        Ok(Self {
            reader,
            channels:        info.channels as usize,
            sample_rate:     info.sample_rate,
            bps:             info.bps,
            total_samples:   info.total_samples,
            samples_decoded: 0,
            ch0:             vec![0i32; MAX_BLOCK],
            ch1:             vec![0i32; MAX_BLOCK],
        })
    }

    /// Decodes the next FLAC frame into `dest` as interleaved i32 samples.
    ///
    /// Returns `Ok(true)` if samples were written, `Ok(false)` at EOF.
    /// `dest` is cleared before writing; its length after return reflects
    /// the number of samples written (block_size × channels).
    pub fn next_frame(&mut self, dest: &mut Vec<i32>) -> io::Result<bool> {
        dest.clear();

        if self.total_samples > 0 && self.samples_decoded >= self.total_samples {
            return Ok(false);
        }

        let hdr = match read_frame_header(&mut self.reader, self.bps, self.sample_rate)? {
            Some(h) => h,
            None    => return Ok(false),
        };

        let block_size = hdr.block_size as usize;
        let bps_ch0    = bps_for_channel(hdr.channel_assignment, 0, self.bps);
        let bps_ch1    = bps_for_channel(hdr.channel_assignment, 1, self.bps);

        read_subframe(&mut self.reader, &mut self.ch0[..block_size], bps_ch0)?;
        if self.channels > 1 {
            read_subframe(&mut self.reader, &mut self.ch1[..block_size], bps_ch1)?;
        }

        self.reader.align();
        let _ = self.reader.read_bits_nocrc(16); // frame CRC-16, discard

        decode_joint_stereo(
            hdr.channel_assignment,
            &mut self.ch0[..block_size],
            &mut self.ch1[..block_size],
        );

        // Interleave channels into dest.
        dest.reserve(block_size * self.channels);
        for i in 0..block_size {
            dest.push(self.ch0[i]);
            if self.channels > 1 {
                dest.push(self.ch1[i]);
            }
        }

        self.samples_decoded += block_size as u64;
        Ok(true)
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn bps_for_channel(channel_assignment: u8, ch: usize, bps: u8) -> u8 {
    match (channel_assignment, ch) {
        (0b1001, 0)               => bps + 1,
        (0b1000, 1) | (0b1010, 1) => bps + 1,
        _                         => bps,
    }
}
