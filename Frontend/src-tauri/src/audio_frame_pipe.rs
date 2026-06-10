#![allow(dead_code)]

use std::fmt;

pub const AUDIO_FRAME_MAGIC: [u8; 4] = *b"SAF1";
pub const AUDIO_FRAME_VERSION: u16 = 1;
pub const AUDIO_FRAME_HEADER_LEN: usize = 36;
pub const AUDIO_FRAME_MAX_PAYLOAD_BYTES: u32 = 1024 * 1024;
pub const AUDIO_FRAME_FLAG_PREBUFFER: u16 = 0x0001;
pub const AUDIO_FRAME_FLAG_END_OF_STREAM: u16 = 0x0002;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioFrameHeader {
    pub payload_len: u32,
    pub sequence: u64,
    pub timestamp_micros: u64,
    pub frame_count: u32,
    pub channels: u16,
    pub flags: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AudioFrameProtocolError {
    HeaderTooShort { expected: usize, actual: usize },
    BadMagic { actual: [u8; 4] },
    UnsupportedVersion { version: u16 },
    InvalidHeaderLength { actual: u16 },
    InvalidChannels { channels: u16 },
    InvalidFrameCount { frame_count: u32 },
    PayloadTooLarge { payload_len: u32 },
    PayloadLengthMismatch { expected: usize, actual: usize },
    SequenceOutOfOrder { expected: u64, actual: u64 },
}

impl fmt::Display for AudioFrameProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::HeaderTooShort { expected, actual } => {
                write!(
                    formatter,
                    "audio frame header too short: expected {expected}, got {actual}"
                )
            }
            Self::BadMagic { actual } => write!(formatter, "invalid audio frame magic: {actual:?}"),
            Self::UnsupportedVersion { version } => {
                write!(formatter, "unsupported audio frame version: {version}")
            }
            Self::InvalidHeaderLength { actual } => {
                write!(formatter, "invalid audio frame header length: {actual}")
            }
            Self::InvalidChannels { channels } => {
                write!(formatter, "invalid audio frame channel count: {channels}")
            }
            Self::InvalidFrameCount { frame_count } => {
                write!(formatter, "invalid audio frame count: {frame_count}")
            }
            Self::PayloadTooLarge { payload_len } => {
                write!(formatter, "audio frame payload too large: {payload_len}")
            }
            Self::PayloadLengthMismatch { expected, actual } => write!(
                formatter,
                "audio frame payload length mismatch: expected {expected}, got {actual}"
            ),
            Self::SequenceOutOfOrder { expected, actual } => write!(
                formatter,
                "audio frame sequence out of order: expected {expected}, got {actual}"
            ),
        }
    }
}

impl std::error::Error for AudioFrameProtocolError {}

impl AudioFrameHeader {
    pub fn new(
        payload_len: u32,
        sequence: u64,
        timestamp_micros: u64,
        frame_count: u32,
        channels: u16,
        flags: u16,
    ) -> Result<Self, AudioFrameProtocolError> {
        let header = Self {
            payload_len,
            sequence,
            timestamp_micros,
            frame_count,
            channels,
            flags,
        };
        header.validate()?;
        Ok(header)
    }

    pub fn expected_payload_len(&self) -> usize {
        usize::from(self.channels) * self.frame_count as usize * 2
    }

    pub fn validate(&self) -> Result<(), AudioFrameProtocolError> {
        if self.channels == 0 || self.channels > 16 {
            return Err(AudioFrameProtocolError::InvalidChannels {
                channels: self.channels,
            });
        }
        if self.frame_count == 0 {
            return Err(AudioFrameProtocolError::InvalidFrameCount {
                frame_count: self.frame_count,
            });
        }
        if self.payload_len > AUDIO_FRAME_MAX_PAYLOAD_BYTES {
            return Err(AudioFrameProtocolError::PayloadTooLarge {
                payload_len: self.payload_len,
            });
        }
        let expected = self.expected_payload_len();
        if self.payload_len as usize != expected {
            return Err(AudioFrameProtocolError::PayloadLengthMismatch {
                expected,
                actual: self.payload_len as usize,
            });
        }
        Ok(())
    }

    pub fn encode(&self) -> Result<[u8; AUDIO_FRAME_HEADER_LEN], AudioFrameProtocolError> {
        self.validate()?;
        let mut bytes = [0_u8; AUDIO_FRAME_HEADER_LEN];
        bytes[0..4].copy_from_slice(&AUDIO_FRAME_MAGIC);
        bytes[4..6].copy_from_slice(&(AUDIO_FRAME_HEADER_LEN as u16).to_le_bytes());
        bytes[6..8].copy_from_slice(&AUDIO_FRAME_VERSION.to_le_bytes());
        bytes[8..12].copy_from_slice(&self.payload_len.to_le_bytes());
        bytes[12..20].copy_from_slice(&self.sequence.to_le_bytes());
        bytes[20..28].copy_from_slice(&self.timestamp_micros.to_le_bytes());
        bytes[28..32].copy_from_slice(&self.frame_count.to_le_bytes());
        bytes[32..34].copy_from_slice(&self.channels.to_le_bytes());
        bytes[34..36].copy_from_slice(&self.flags.to_le_bytes());
        Ok(bytes)
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, AudioFrameProtocolError> {
        if bytes.len() < AUDIO_FRAME_HEADER_LEN {
            return Err(AudioFrameProtocolError::HeaderTooShort {
                expected: AUDIO_FRAME_HEADER_LEN,
                actual: bytes.len(),
            });
        }
        let mut magic = [0_u8; 4];
        magic.copy_from_slice(&bytes[0..4]);
        if magic != AUDIO_FRAME_MAGIC {
            return Err(AudioFrameProtocolError::BadMagic { actual: magic });
        }
        let header_len = u16::from_le_bytes([bytes[4], bytes[5]]);
        if header_len != AUDIO_FRAME_HEADER_LEN as u16 {
            return Err(AudioFrameProtocolError::InvalidHeaderLength { actual: header_len });
        }
        let version = u16::from_le_bytes([bytes[6], bytes[7]]);
        if version != AUDIO_FRAME_VERSION {
            return Err(AudioFrameProtocolError::UnsupportedVersion { version });
        }
        let header = Self {
            payload_len: u32::from_le_bytes(bytes[8..12].try_into().unwrap()),
            sequence: u64::from_le_bytes(bytes[12..20].try_into().unwrap()),
            timestamp_micros: u64::from_le_bytes(bytes[20..28].try_into().unwrap()),
            frame_count: u32::from_le_bytes(bytes[28..32].try_into().unwrap()),
            channels: u16::from_le_bytes(bytes[32..34].try_into().unwrap()),
            flags: u16::from_le_bytes(bytes[34..36].try_into().unwrap()),
        };
        header.validate()?;
        Ok(header)
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct AudioFrameSequenceGuard {
    next_sequence: u64,
}

impl AudioFrameSequenceGuard {
    pub fn new() -> Self {
        Self { next_sequence: 0 }
    }

    pub fn next_sequence(&self) -> u64 {
        self.next_sequence
    }

    pub fn verify_and_advance(
        &mut self,
        header: &AudioFrameHeader,
    ) -> Result<(), AudioFrameProtocolError> {
        if header.sequence != self.next_sequence {
            return Err(AudioFrameProtocolError::SequenceOutOfOrder {
                expected: self.next_sequence,
                actual: header.sequence,
            });
        }
        self.next_sequence = self.next_sequence.saturating_add(1);
        Ok(())
    }
}

pub fn encode_audio_frame(
    header: &AudioFrameHeader,
    payload: &[u8],
) -> Result<Vec<u8>, AudioFrameProtocolError> {
    header.validate()?;
    if payload.len() != header.payload_len as usize {
        return Err(AudioFrameProtocolError::PayloadLengthMismatch {
            expected: header.payload_len as usize,
            actual: payload.len(),
        });
    }
    let mut frame = Vec::with_capacity(AUDIO_FRAME_HEADER_LEN + payload.len());
    frame.extend_from_slice(&header.encode()?);
    frame.extend_from_slice(payload);
    Ok(frame)
}

pub fn decode_audio_frame(
    frame: &[u8],
) -> Result<(AudioFrameHeader, &[u8]), AudioFrameProtocolError> {
    let header = AudioFrameHeader::decode(frame)?;
    let expected_len = AUDIO_FRAME_HEADER_LEN + header.payload_len as usize;
    if frame.len() != expected_len {
        return Err(AudioFrameProtocolError::PayloadLengthMismatch {
            expected: expected_len,
            actual: frame.len(),
        });
    }
    Ok((header, &frame[AUDIO_FRAME_HEADER_LEN..]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    const DOCUMENTED_HEADER_HEX: &str =
        "5341463124000100000800002a0000000000000015cd5b07000000000002000002000100";

    fn documented_header() -> AudioFrameHeader {
        AudioFrameHeader::new(2048, 42, 123_456_789, 512, 2, AUDIO_FRAME_FLAG_PREBUFFER).unwrap()
    }

    fn hex(bytes: &[u8]) -> String {
        let mut output = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            write!(&mut output, "{byte:02x}").unwrap();
        }
        output
    }

    #[test]
    fn audio_frame_header_round_trips_with_documented_layout() {
        let header = documented_header();
        let encoded = header.encode().unwrap();

        assert_eq!(encoded.len(), AUDIO_FRAME_HEADER_LEN);
        assert_eq!(hex(&encoded), DOCUMENTED_HEADER_HEX);
        assert_eq!(AudioFrameHeader::decode(&encoded).unwrap(), header);
    }

    #[test]
    fn audio_frame_round_trip_preserves_payload() {
        let header = documented_header();
        let payload = vec![7_u8; header.payload_len as usize];

        let frame = encode_audio_frame(&header, &payload).unwrap();
        let (decoded_header, decoded_payload) = decode_audio_frame(&frame).unwrap();

        assert_eq!(decoded_header, header);
        assert_eq!(decoded_payload, payload.as_slice());
    }

    #[test]
    fn audio_frame_decode_rejects_bad_magic() {
        let mut encoded = documented_header().encode().unwrap();
        encoded[0] = b'X';

        assert!(matches!(
            AudioFrameHeader::decode(&encoded),
            Err(AudioFrameProtocolError::BadMagic { .. })
        ));
    }

    #[test]
    fn audio_frame_rejects_payload_length_mismatch() {
        let header = documented_header();

        assert!(matches!(
            encode_audio_frame(&header, &[0; 12]),
            Err(AudioFrameProtocolError::PayloadLengthMismatch { .. })
        ));
    }

    #[test]
    fn audio_frame_sequence_guard_rejects_out_of_order_frame() {
        let mut guard = AudioFrameSequenceGuard::new();
        let first = AudioFrameHeader::new(1024, 0, 1, 512, 1, 0).unwrap();
        let skipped = AudioFrameHeader::new(1024, 2, 2, 512, 1, 0).unwrap();

        guard.verify_and_advance(&first).unwrap();

        assert!(matches!(
            guard.verify_and_advance(&skipped),
            Err(AudioFrameProtocolError::SequenceOutOfOrder {
                expected: 1,
                actual: 2
            })
        ));
    }
}
