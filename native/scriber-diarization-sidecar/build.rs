use std::env;

fn main() {
    println!("cargo:rerun-if-env-changed=CARGO_CFG_TARGET_FEATURE");

    let target = env::var("TARGET").unwrap_or_default();
    let profile = env::var("PROFILE").unwrap_or_default();
    let target_features = env::var("CARGO_CFG_TARGET_FEATURE").unwrap_or_default();
    let has_static_crt = target_features
        .split(',')
        .any(|feature| feature == "crt-static");

    if target.ends_with("pc-windows-msvc") && profile == "release" && !has_static_crt {
        panic!(
            "Windows release requires crt-static; run Cargo from native/scriber-diarization-sidecar"
        );
    }
}
