# rezin-flac

- A Rust implementation of the FLAC (Free Lossless Audio Codec) specification.
- Works with 16bit & 24bit audio. Supports various sample rates.
- Based on hare-flac

## Usage

You can use this crate by itself, though this will explain usage via *rezin-cli*.

Install *rezin-flac* & *rezin-cli*:

```bash
cargo install rezin-flac
cargo install rezin-cli
```

Call *rezin-flac* via *rezin-cli*:

```bash
rezin encode input.wav output.flac
rezin decode input.flac output.wav
```

Uninstall:

```bash
cargo uninstall rezin-flac
cargo uninstall rezin-cli
```

## Notes

Initial performance metrics are hugely improved from the hare-flac implementation.
With 32 workers (9950X CPU) encoding & decoding speeds exceed ffmpeg.
Compression rations are on average within ~2% of ffmpeg.

## 0.2.1

- stream.rs added to enable FLAC streaming (ultimately for use in rezin-play).
- Fix to encode.rs: FLAC file seeking was buggy on certain Android players. Fixed.
