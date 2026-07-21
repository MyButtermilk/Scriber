# Scriber codec-lab patch provenance

This directory is the published `rezin-flac` 0.2.1 crates.io source, used only
by the isolated Issue #18 codec lab.

- Upstream repository: `https://gitlab.com/calebjdt/rezin`
- Upstream VCS commit: `ed8beb27de8aa56fdaabc06c210ee96e7769fbcc`
- Crates.io archive SHA-256:
  `61d993f51d1e4c2300573baf6226ba832f43d8b197584f5adf24be3c45482ed2`
- Original `src/encode.rs` SHA-256:
  `4cfe2a4281d24d17d661a2f43a4c92706970d53b4f1165a9d34858d6b7093e17`
- Patched `src/encode.rs` SHA-256:
  `ad054beaa097d5b9547269f92746bf92affcd1a64442dcd9c1b68d421e8ee848`
- License: MIT; the complete upstream license is retained as `LICENSE`.

## Why vendoring is required

Version 0.2.1 chooses `available_parallelism()` workers before distributing
FLAC frames. On the validation host it observes 24 workers. A 5-second, 16-kHz
mono fixture contains only 20 4096-sample frames. Four workers therefore
receive zero frames, but their calculated sample starts exceed the 80,000
sample input and the parent panics while slicing the worker chunk.

The lab changes one expression after calculating `total_frames`:

```rust
let n_workers = cpu_count().min(total_frames.max(1));
```

This preserves Rezin's official multi-process encoder and changes no codec
algorithm or bitstream logic. It only prevents zero-work child processes. The
patch is not production integration or a proposed upstream release.
