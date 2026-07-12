# Third-Party Notices

This file records third-party components whose notices must accompany Scriber
binary distributions. The dependency lockfiles remain the authoritative list of
all resolved package versions.

## aec3 0.2.0

- Project: `aec3-rs`
- Source: https://github.com/RubyBit/aec3-rs
- Use in Scriber: WebRTC AEC3 processing in the crash-isolated meeting audio sidecar
- License: MIT OR BSD-3-Clause; portions are derived from the WebRTC project
- Pinned package: `aec3 = "=0.2.0"`

Copyright (c) 2025 Angelos-Ermis Mangos.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

WebRTC-derived portions:

Copyright (c) 2011, The WebRTC project authors. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of Google nor the names of its contributors may be used to
   endorse or promote products derived from this software without specific
   prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The upstream patent grant is published at:
https://github.com/RubyBit/aec3-rs/blob/v0.2.0/PATENT

## Optional WeSpeaker speaker-embedding model

- Model: `talatapp/wespeaker-voxceleb-resnet34-LM-onnx`
- Pinned revision: `abea38bae76873d0842509a54f8fbe6c8b5b5fe6`
- Source: https://huggingface.co/talatapp/wespeaker-voxceleb-resnet34-LM-onnx
- Upstream project: https://github.com/wenet-e2e/wespeaker
- License declared by the model repository: Apache-2.0
- Use in Scriber: optional, explicit-opt-in local speaker embeddings; downloaded
  after installation and not included in the standard installer

The model repository states that it is derived from WeSpeaker and trained on
VoxCeleb data. The VoxCeleb datasets are published under Creative Commons
Attribution 4.0 and their maintainers describe the datasets as available for
non-commercial research purposes. Consequently, distributing or enabling this
optional model in a commercial release requires a separate legal review; the
standard Scriber installer does not bundle it.

## Sherpa-ONNX speaker diarization worker and optional models

- Runtime: `k2-fsa/sherpa-onnx` 1.13.3
- Source: https://github.com/k2-fsa/sherpa-onnx
- Runtime license: Apache-2.0
- Segmentation model: `sherpa-onnx-pyannote-segmentation-3-0`, INT8 ONNX
- Segmentation source: https://huggingface.co/pyannote/segmentation-3.0
- Segmentation license: MIT, Copyright (c) 2022 CNRS
- Embedding model: `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx`
- Embedding upstream: https://github.com/modelscope/3D-Speaker
- Embedding upstream license: Apache-2.0
- Use in Scriber: optional offline speaker separation for File, YouTube,
  Meeting finalization, and imported meeting recordings when the selected STT
  model has no native diarization

Scriber's statically linked Rust worker is a versioned resource of the signed
standard installer/updater and remains a separate process from both Tauri and
the live-audio sidecar. The installer does not download an executable from the
model channel. Both models remain an explicit optional download and are pinned
by SHA-256. The installed component manifest verifies the signed-build worker
digest, both models, the Pyannote MIT license, the complete Apache-2.0 text,
the exact 3D-Speaker ModelScope provenance record, and the Scriber worker MIT
license.
