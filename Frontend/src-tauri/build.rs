fn main() {
    println!("cargo:rerun-if-env-changed=SCRIBER_OUTLOOK_CLIENT_ID");
    tauri_build::build();
}
