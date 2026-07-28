fn main() {
    println!("cargo:rerun-if-env-changed=SCRIBER_OUTLOOK_CLIENT_ID");
    // tauri_build resolves the product version through tauri.conf.json from
    // ../package.json and embeds it in every Windows binary produced by this
    // package. Make version-only releases invalidate those PE resources.
    println!("cargo:rerun-if-changed=../package.json");
    println!("cargo:rerun-if-changed=tauri.conf.json");
    // Tauri embeds this file into the Windows PE resource. Keep Cargo aware of
    // icon-only changes so an incremental installer build cannot reuse the old
    // low-contrast executable icon.
    println!("cargo:rerun-if-changed=icons/icon.ico");
    tauri_build::build();
}
