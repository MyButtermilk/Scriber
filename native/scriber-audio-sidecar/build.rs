use std::{env, path::PathBuf};

fn main() {
    let icon =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../Frontend/src-tauri/icons/icon.ico");
    println!("cargo:rerun-if-changed={}", icon.display());
    println!("cargo:rerun-if-changed=windows-app-manifest.xml");

    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }

    let version = env::var("CARGO_PKG_VERSION").expect("worker package version");
    let parts: Vec<u16> = version
        .split('.')
        .map(|part| {
            part.parse()
                .expect("worker version requires numeric u16 parts")
        })
        .collect();
    assert_eq!(parts.len(), 3, "worker version requires major.minor.patch");
    let numeric =
        (u64::from(parts[0]) << 48) | (u64::from(parts[1]) << 32) | (u64::from(parts[2]) << 16);

    tauri_winres::WindowsResource::new()
        .set("ProductName", "Scriber audio worker")
        .set("FileDescription", "Scriber audio capture worker")
        .set("FileVersion", &version)
        .set("ProductVersion", &version)
        .set_version_info(tauri_winres::VersionInfo::FILEVERSION, numeric)
        .set_version_info(tauri_winres::VersionInfo::PRODUCTVERSION, numeric)
        .set_icon_with_id(&icon.to_string_lossy(), "32512")
        .set_manifest(include_str!("windows-app-manifest.xml"))
        .compile()
        .expect("compile standalone audio worker Windows resources");
}
