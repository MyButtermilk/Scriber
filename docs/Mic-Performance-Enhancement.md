# Microphone Performance Enhancement

## Implementation Status ✅

**Implemented Solutions:**
- ✅ **Solution 1 (Ready Signal)**: Added `on_ready` callback to `MicrophoneInput` that fires when the stream is actually started
- ✅ **Solution 4 (Visual Feedback)**: Added "Preparing..." overlay with pulsing animation shown during initialization
- ✅ **Solution 5 (Hybrid)**: Combines Solution 1 + 4 for best user experience

**Not Implemented:**
- ❌ **Solution 2 (Pre-warming)**: Available via `MIC_ALWAYS_ON` setting (existing feature)
- ❌ **Solution 3 (Pre-buffer)**: Planned for future if needed

---

## Problem Description

When the user presses the hotkey to start recording, the microphone is sometimes not ready yet, causing the first few seconds of speech to be cut off.

## Root Cause Analysis

### Current Flow

When the hotkey is pressed, the following sequence occurs:

1. **`ScriberController.start_listening()`** is called (`web_api.py`, line 655-684)
2. Inside `start_listening()`:
   - A new `TranscriptRecord` is created
   - The `ScriberPipeline` is created
   - `self._pipeline.start()` is launched as an async task (line 678)
   - **Immediately after**, the recording overlay is shown (line 683)

3. **`ScriberPipeline.start()`** (`pipeline.py`, line 516-587):
   - An aiohttp session is created
   - The STT service is instantiated (may involve network connection)
   - SmartTurn and VAD analyzers are initialized
   - **`MicrophoneInput`** is created (line 536-544)
   - The pipeline is assembled and started

4. **`MicrophoneInput.start()`** (`microphone.py`, line 62-128):
   - Device detection is performed
   - `sd.InputStream` is created
   - **The stream only starts after all initialization** (line 121)

### The Issue

The overlay is shown **immediately** after calling `_pipeline.start()`, but the actual audio stream only starts after the entire pipeline initialization completes (STT service connection, VAD initialization, device queries, etc.). This can take several hundred milliseconds to a few seconds.

**Result**: The user sees the recording indicator and starts speaking, but the microphone isn't actually capturing audio yet.

---

## Proposed Solutions

### Solution 1: Ready Signal from Microphone (Recommended)

**Concept**: Add a callback or event that fires only when the microphone is actually recording. Show the overlay only after this signal.

**Implementation**:
1. Add an `on_ready` callback to `MicrophoneInput`
2. Fire it after `self.stream.start()` succeeds in `MicrophoneInput.start()`
3. In `ScriberController`, wait for this signal before showing the overlay or add a short delay

**Pros**:
- Clean, accurate synchronization
- Minimal changes required
- No wasted resources

**Cons**:
- Small additional latency before visual feedback appears

**Files to modify**:
- `src/microphone.py`: Add `on_ready` callback
- `src/pipeline.py`: Pass callback through to MicrophoneInput
- `src/web_api.py`: Use callback to trigger overlay

---

### Solution 2: Pre-warming the Microphone Stream

**Concept**: Keep the microphone in standby mode so it's instantly ready when the hotkey is pressed.

**Implementation**:
1. When `MIC_ALWAYS_ON` is enabled, keep the stream open
2. On hotkey press, immediately start processing the already-flowing audio
3. Use the existing `keep_alive` parameter in `MicrophoneInput`

**Pros**:
- Near-instant response
- No lost audio

**Cons**:
- Continuous system resource usage
- Battery impact on laptops
- Privacy concerns (mic always active)

**Files to modify**:
- `src/pipeline.py`: Refactor to support pre-warmed mic
- `src/web_api.py`: Manage persistent mic instance

---

### Solution 3: Pre-Recording Audio Buffer (Best for No Audio Loss)

**Concept**: Maintain a rolling buffer of the last N seconds of audio. When recording starts, include the buffered audio.

**Implementation**:
1. Create a `MicrophonePreBuffer` class that continuously records to a ring buffer
2. On hotkey press, flush the buffer into the pipeline before live audio
3. Configure buffer duration (e.g., 2-3 seconds)

**Pros**:
- Guarantees no audio loss, even during slow initialization
- Catches speech that started before the hotkey press

**Cons**:
- Continuous mic usage and memory allocation
- More complex implementation
- Privacy concerns

**Files to modify**:
- New file: `src/mic_buffer.py`
- `src/pipeline.py`: Integrate buffer with pipeline
- `src/web_api.py`: Manage buffer lifecycle

---

### Solution 4: Visual Feedback During Initialization

**Concept**: Show the overlay in an "initializing" state (e.g., pulsing indicator, "Preparing..." text) until the microphone is ready.

**Implementation**:
1. Add a new overlay state: "initializing"
2. Show this state immediately on hotkey press
3. Transition to "recording" state when mic is ready
4. User knows to wait for the transition before speaking

**Pros**:
- No changes to audio pipeline
- Clear user feedback
- Simple implementation

**Cons**:
- Doesn't solve the audio loss issue, just informs the user
- User still needs to wait

**Files to modify**:
- `src/overlay.py`: Add initializing state
- `src/web_api.py`: Signal state transitions

---

### Solution 5: Hybrid Approach (Recommended for Production)

**Concept**: Combine Solution 1 and Solution 4 for best user experience.

**Implementation**:
1. Show overlay immediately in "initializing" state (Solution 4)
2. Transition to "recording" state when mic signals ready (Solution 1)
3. Optionally, add a small pre-buffer for the initialization period (partial Solution 3)

**User Experience**:
- Press hotkey → See "Preparing..." overlay immediately
- ~500ms later → Overlay transitions to "Recording" with waveform
- User knows exactly when to start speaking

**Files to modify**:
- `src/microphone.py`: Add `on_ready` callback
- `src/overlay.py`: Add initializing/preparing state
- `src/pipeline.py`: Wire up callback
- `src/web_api.py`: Orchestrate state transitions

---

## Implementation Priority

1. **Quick Win**: Solution 4 (Visual feedback) - Can be implemented in ~1 hour
2. **Proper Fix**: Solution 1 (Ready signal) - ~2-3 hours  
3. **Best UX**: Solution 5 (Hybrid) - ~4-6 hours
4. **Maximum Reliability**: Solution 3 (Pre-buffer) - ~8+ hours

## Recommendation

Start with **Solution 5 (Hybrid)** as it provides the best user experience:
- Immediate visual feedback
- Accurate recording state indication
- Minimal resource overhead
- Clear user guidance on when to start speaking

If time is limited, implement **Solution 4** first as a quick improvement, then add **Solution 1** later.
