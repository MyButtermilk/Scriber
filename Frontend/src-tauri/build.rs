fn main() {
    println!("cargo:rerun-if-env-changed=SCRIBER_OUTLOOK_CLIENT_ID");
    // Tauri embeds this file into the Windows PE resource. Keep Cargo aware of
    // icon-only changes so an incremental installer build cannot reuse the old
    // low-contrast executable icon.
    println!("cargo:rerun-if-changed=icons/icon.ico");
    tauri_build::build();
}
