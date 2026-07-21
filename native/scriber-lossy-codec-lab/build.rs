use std::env;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=Cargo.toml");
    println!("cargo:rerun-if-env-changed=RUSTC");
    let rustc = env::var("RUSTC").unwrap_or_else(|_| "rustc".to_owned());
    let output = Command::new(&rustc)
        .arg("--version")
        .arg("--verbose")
        .output()
        .expect("run rustc --version --verbose");
    assert!(output.status.success(), "rustc identity probe failed");
    let stdout = String::from_utf8(output.stdout).expect("rustc identity is UTF-8");
    for (key, label) in [
        ("SCRIBER_LAB_RUSTC_RELEASE", "release"),
        ("SCRIBER_LAB_RUSTC_COMMIT", "commit-hash"),
        ("SCRIBER_LAB_RUSTC_COMMIT_DATE", "commit-date"),
        ("SCRIBER_LAB_RUSTC_HOST", "host"),
        ("SCRIBER_LAB_RUSTC_LLVM", "LLVM version"),
    ] {
        let value = stdout
            .lines()
            .find_map(|line| line.strip_prefix(&format!("{label}: ")))
            .unwrap_or("unknown");
        println!("cargo:rustc-env={key}={value}");
    }
    let target = env::var("TARGET").unwrap_or_else(|_| "unknown".to_owned());
    let features = env::var("CARGO_CFG_TARGET_FEATURE").unwrap_or_default();
    println!("cargo:rustc-env=SCRIBER_LAB_BUILD_TARGET={target}");
    println!("cargo:rustc-env=SCRIBER_LAB_COMPILE_TARGET_FEATURES={features}");
}
