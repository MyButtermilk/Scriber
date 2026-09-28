// Scriber bridge: no exception may cross the C/Rust boundary.
#include "localvqe_api.h"
#include <memory>

extern "C" uintptr_t scriber_vqe_new(const char* path, int threads) noexcept {
    try {
        const auto options = localvqe_options_new();
        if (!options) return 0;
        struct cleanup {
            uintptr_t value;
            ~cleanup() { localvqe_options_free(value); }
        } guard{options};
        if (localvqe_options_set_model_path(options, path) != 0 ||
            localvqe_options_set_threads(options, threads) != 0) return 0;
        const auto ctx = localvqe_new_with_options(options);
        if (ctx && (localvqe_sample_rate(ctx) != 16000 ||
                    localvqe_hop_length(ctx) != 256 || localvqe_fft_size(ctx) != 512)) {
            localvqe_free(ctx);
            return 0;
        }
        return ctx;
    } catch (...) { return 0; }
}

extern "C" int scriber_vqe_process(uintptr_t ctx, const float* mic,
                                 const float* render, float* out) noexcept {
    try { return localvqe_process_frame_f32(ctx, mic, render, 256, out); }
    catch (...) { return -1; }
}

extern "C" void scriber_vqe_free(uintptr_t ctx) noexcept {
    try { localvqe_free(ctx); } catch (...) {}
}
