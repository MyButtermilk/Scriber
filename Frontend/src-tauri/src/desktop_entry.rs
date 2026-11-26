//! Thin versioned desktop shell entry point.
//!
//! All context-independent desktop shell logic lives in `scriber-shell-core` so
//! UI/version changes only recompile and relink this tiny wrapper instead of
//! recompiling the full shell crate.

pub fn run() {
    scriber_shell_core::run_with_context(tauri::generate_context!());
}
