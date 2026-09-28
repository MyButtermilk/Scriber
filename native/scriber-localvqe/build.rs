fn main() {
    for path in ["build.rs", "inputs.json", "CMakeLists.txt", "bridge.cpp"] {
        println!("cargo:rerun-if-changed={path}");
    }
    let inputs: serde_json::Value =
        serde_json::from_str(include_str!("inputs.json")).expect("LocalVQE input lock");
    let mut build = cmake::Config::new(".");
    build.profile("Release");
    // All C++ allocations stay behind the bridge. Bundle its CRT/STL so adding
    // enhancement never introduces a Visual C++ redistributable prerequisite.
    build.static_crt(true);
    for key in ["source", "ggml", "model"] {
        for field in ["url", "sha256"] {
            build.define(
                format!("{}_{}", key.to_uppercase(), field.to_uppercase()),
                inputs[key][field].as_str().expect("locked input"),
            );
        }
    }
    let dst = build.build();
    println!("cargo:rustc-link-search=native={}/lib", dst.display());
    for name in ["scriber_localvqe", "ggml", "ggml-cpu", "ggml-base"] {
        println!("cargo:rustc-link-lib=static={name}");
    }
    let target = std::env::var("TARGET").unwrap_or_default();
    if target.contains("msvc") {
        // Resolve C++ exception support statically before Rust's existing
        // dynamic C-runtime defaults; do not add VCRUNTIME140_1.dll to shipping.
        println!("cargo:rustc-link-lib=static:-bundle=libvcruntime");
        println!("cargo:rustc-link-lib=advapi32");
    } else if target.contains("apple") {
        println!("cargo:rustc-link-lib=c++");
    } else if !target.contains("msvc") {
        println!("cargo:rustc-link-lib=stdc++");
    }
    println!(
        "cargo:rustc-env=SCRIBER_LOCALVQE_MODEL={}/model.gguf",
        dst.display()
    );
    println!(
        "cargo:rustc-env=SCRIBER_LOCALVQE_FIXTURES={}/fixtures",
        dst.display()
    );
}
