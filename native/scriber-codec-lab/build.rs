use std::env;
use std::process::Command;

fn rustc_field(output: &str, key: &str) -> String {
    output
        .lines()
        .find_map(|line| line.strip_prefix(key))
        .map(str::trim)
        .unwrap_or("unknown")
        .to_owned()
}

fn emit(name: &str, value: &str) {
    println!("cargo:rustc-env={name}={value}");
}

fn main() {
    let rustc = env::var("RUSTC").expect("Cargo must provide RUSTC");
    let output = Command::new(&rustc)
        .arg("--version")
        .arg("--verbose")
        .output()
        .expect("failed to run rustc --version --verbose");
    assert!(output.status.success(), "rustc version query failed");
    let verbose = String::from_utf8(output.stdout).expect("rustc version output must be UTF-8");

    emit(
        "SCRIBER_CODEC_LAB_RUSTC_RELEASE",
        &rustc_field(&verbose, "release:"),
    );
    emit(
        "SCRIBER_CODEC_LAB_RUSTC_COMMIT_HASH",
        &rustc_field(&verbose, "commit-hash:"),
    );
    emit(
        "SCRIBER_CODEC_LAB_RUSTC_COMMIT_DATE",
        &rustc_field(&verbose, "commit-date:"),
    );
    emit(
        "SCRIBER_CODEC_LAB_RUSTC_HOST",
        &rustc_field(&verbose, "host:"),
    );
    emit(
        "SCRIBER_CODEC_LAB_LLVM_VERSION",
        &rustc_field(&verbose, "LLVM version:"),
    );
    emit(
        "SCRIBER_CODEC_LAB_BUILD_HOST",
        &env::var("HOST").unwrap_or_else(|_| "unknown".to_owned()),
    );
    emit(
        "SCRIBER_CODEC_LAB_BUILD_TARGET",
        &env::var("TARGET").unwrap_or_else(|_| "unknown".to_owned()),
    );
    emit(
        "SCRIBER_CODEC_LAB_TARGET_FEATURES",
        &env::var("CARGO_CFG_TARGET_FEATURE").unwrap_or_default(),
    );
    emit(
        "SCRIBER_CODEC_LAB_ENCODED_RUSTFLAGS",
        &env::var("CARGO_ENCODED_RUSTFLAGS")
            .unwrap_or_default()
            .replace('\u{1f}', " "),
    );
    emit(
        "SCRIBER_CODEC_LAB_CARGO_PROFILE",
        &env::var("PROFILE").unwrap_or_else(|_| "unknown".to_owned()),
    );
    emit(
        "SCRIBER_CODEC_LAB_REZIN_BUILD_VARIANT",
        &env::var("SCRIBER_CODEC_LAB_REZIN_BUILD_VARIANT")
            .unwrap_or_else(|_| "not_applicable".to_owned()),
    );

    println!("cargo:rerun-if-changed=build.rs");
}
