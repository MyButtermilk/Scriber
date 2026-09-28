# Third-Party Notices

This file records third-party components whose notices must accompany Scriber
binary distributions. The dependency lockfiles remain the authoritative list of
all resolved package versions.

## QuickJS-ng 0.15.0

- Project: `quickjs-ng/quickjs`
- Source: https://github.com/quickjs-ng/quickjs/tree/v0.15.0
- Use in Scriber: JavaScript engine behind the bounded, offline YouTube EJS wrapper
- License: MIT
- Installed license: `backend/tools/ffmpeg/LICENSE.quickjs-ng.txt`

Copyright (c) 2017-2026 Fabrice Bellard
Copyright (c) 2017-2024 Charlie Gordon
Copyright (c) 2023-2026 Ben Noordhuis
Copyright (c) 2023-2026 Saúl Ibarra Corretgé

The complete byte-locked MIT text is bundled beside the QuickJS engine in
every installer and updater payload.

## llama.cpp b10158

- Project: `ggml-org/llama.cpp`
- Source: https://github.com/ggml-org/llama.cpp/tree/f87067841bac583bc089a225382248d857791ca8
- Use in Scriber: local GGUF transcript polishing through a loopback-only
  `llama-server`; Vulkan is preferred and the same package provides CPU fallback
- License: MIT
- Installed license: `backend/tools/local-polishing/LICENSE.llama.cpp.txt`

The complete byte-locked MIT text is bundled beside the locked llama.cpp
runtime in every installer and updater payload. Polishing model repositories
are data-only download channels and never provide this executable runtime.

## Optional Scriber Gemma 3 270M transcript-polishing model

- Model: `Buttermilk03/scriber-gemma3-270m-polishing-de-v1`
- Source: https://huggingface.co/Buttermilk03/scriber-gemma3-270m-polishing-de-v1
- Base model: `google/gemma-3-270m-it`
- Base revision: `ac82b4e820549b854eebf28ce6dedaf9fdfa17b3`
- Terms: Gemma Terms of Use and Gemma Prohibited Use Policy
- Use in Scriber: optional public Q8_0 or BF16 GGUF download for local
  live-microphone transcript polishing

The model is not embedded in the standard installer. Scriber downloads a
commit-pinned, checksum-verified variant anonymously after explicit user action;
no Hugging Face account is required. The model repository must publish the
applicable Gemma terms, prohibited-use policy, model notice, and modification
notice beside the model artifacts. Use and redistribution remain subject to
those terms.

## LocalVQE 1.3

- Project: LocalVQE by LocalAI
- Source: https://github.com/localai-org/LocalVQE
- Source revision: `f53063c9eb2a85f96479867d1dd911dc3bf6319b`
- Model: `LocalAI-io/LocalVQE`, `localvqe-v1.3-4.8M-f32.gguf`
- Model revision: `29ca38495cba9d6393a92a4dd890f28dd81f758d`
- License: Apache-2.0 (code and model)
- Use: CPU meeting echo cancellation, noise suppression, and dereverberation
- Modifications: static CPU-only build, bounded Rust streaming adapter,
  UTF-8 Windows file access, mandatory integrity checking, quiet initialization,
  and disabled runtime backend discovery. Weights are embedded unchanged.

Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to the Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by the Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding any notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   Copyright 2024-2026 Richard Sherwood Palethorpe

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

## GGML (LocalVQE runtime)

- Source: https://github.com/ggml-org/ggml
- Revision: `c044a8eeae2591faa0950c8b5e514cbc4bbfc4ca`
- Use: statically linked CPU inference for LocalVQE v1.3

MIT License

Copyright (c) 2023-2026 The ggml authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

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
