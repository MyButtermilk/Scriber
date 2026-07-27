//! Release-only Minisign verifier. This binary is never listed as a Tauri
//! sidecar/resource, so it cannot enter the installed application bundle.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use minisign_verify::{PublicKey, Signature};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug)]
struct Arguments {
    artifact: PathBuf,
    signature: PathBuf,
    public_key: String,
}

fn usage() -> &'static str {
    "usage: minisign-verify --artifact <path> --signature <path> --public-key <base64>"
}

fn parse_arguments<I>(arguments: I) -> Result<Arguments, String>
where
    I: IntoIterator<Item = String>,
{
    let mut artifact = None;
    let mut signature = None;
    let mut public_key = None;
    let mut values = arguments.into_iter();
    while let Some(argument) = values.next() {
        let target = match argument.as_str() {
            "--artifact" => &mut artifact,
            "--signature" => &mut signature,
            "--public-key" => &mut public_key,
            "-h" | "--help" => return Err(usage().to_string()),
            _ => return Err(format!("unknown argument: {argument}\n{}", usage())),
        };
        let value = values
            .next()
            .ok_or_else(|| format!("missing value for {argument}\n{}", usage()))?;
        if target.replace(value).is_some() {
            return Err(format!("duplicate argument: {argument}\n{}", usage()));
        }
    }
    Ok(Arguments {
        artifact: artifact
            .map(PathBuf::from)
            .ok_or_else(|| format!("missing --artifact\n{}", usage()))?,
        signature: signature
            .map(PathBuf::from)
            .ok_or_else(|| format!("missing --signature\n{}", usage()))?,
        public_key: public_key.ok_or_else(|| format!("missing --public-key\n{}", usage()))?,
    })
}

fn decode_tauri_minisign_text(encoded: &str, label: &str) -> Result<String, String> {
    let decoded = STANDARD
        .decode(encoded.trim())
        .map_err(|error| format!("invalid Base64-encoded {label}: {error}"))?;
    String::from_utf8(decoded).map_err(|_| format!("decoded {label} is not valid UTF-8"))
}

fn verify_bytes(
    data: &[u8],
    encoded_signature: &str,
    encoded_public_key: &str,
) -> Result<(), String> {
    // Tauri stores both values as Base64-wrapped complete Minisign text. Keep
    // this decoder byte-compatible with tauri-plugin-updater's verifier.
    let public_key_text = decode_tauri_minisign_text(encoded_public_key, "Minisign public key")?;
    let signature_text = decode_tauri_minisign_text(encoded_signature, "Minisign signature")?;
    let public_key = PublicKey::decode(&public_key_text)
        .map_err(|error| format!("invalid Minisign public key: {error}"))?;
    let signature = Signature::decode(&signature_text)
        .map_err(|error| format!("invalid Minisign signature: {error}"))?;
    public_key
        .verify(data, &signature, true)
        .map_err(|error| format!("Minisign verification failed: {error}"))
}

fn verify_files(
    artifact_path: &Path,
    signature_path: &Path,
    public_key_text: &str,
) -> Result<u64, String> {
    let data = fs::read(artifact_path).map_err(|error| {
        format!(
            "could not read artifact {}: {error}",
            artifact_path.display()
        )
    })?;
    let signature_text = fs::read_to_string(signature_path).map_err(|error| {
        format!(
            "could not read signature {}: {error}",
            signature_path.display()
        )
    })?;
    verify_bytes(&data, &signature_text, public_key_text)?;
    Ok(data.len() as u64)
}

fn run() -> Result<(), String> {
    let arguments = parse_arguments(env::args().skip(1))?;
    let size = verify_files(
        &arguments.artifact,
        &arguments.signature,
        &arguments.public_key,
    )?;
    println!("{{\"schemaVersion\":1,\"verified\":true,\"sizeBytes\":{size}}}");
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PUBLIC_KEY_TEXT: &str = concat!(
        "untrusted comment: minisign public key 73620F1852B49F1F\n",
        "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3\n",
    );
    const SIGNATURE: &str = concat!(
        "untrusted comment: signature from minisign secret key\n",
        "RUQf6LRCGA9i559r3g7V1qNyJDApGip8MfqcadIgT9CuhV3EMhHoN1mGTkUidF/",
        "z7SrlQgXdy8ofjb7bNJJylDOocrCo8KLzZwo=\n",
        "trusted comment: timestamp:1633700835\tfile:test\tprehashed\n",
        "wLMDjy9FLAuxZ3q4NlEvkgtyhrr0gtTu6KC4KBJdITbbOeAi1zBIYo0v4iTgt8jJ",
        "pIidRJnp94ABQkJAgAooBQ==",
    );

    #[test]
    fn verifies_known_minisign_fixture() {
        let signature = STANDARD.encode(SIGNATURE);
        let public_key = STANDARD.encode(PUBLIC_KEY_TEXT);
        verify_bytes(b"test", &signature, &public_key).expect("known signature must verify");
    }

    #[test]
    fn rejects_tampered_artifact() {
        let signature = STANDARD.encode(SIGNATURE);
        let public_key = STANDARD.encode(PUBLIC_KEY_TEXT);
        let error = verify_bytes(b"tampered", &signature, &public_key)
            .expect_err("tampered payload must not verify");
        assert!(error.contains("verification failed"));
    }

    #[test]
    fn rejects_tampered_signature() {
        let tampered = STANDARD.encode(SIGNATURE.replacen("RUQf", "RUQg", 1));
        let public_key = STANDARD.encode(PUBLIC_KEY_TEXT);
        assert!(verify_bytes(b"test", &tampered, &public_key).is_err());
    }

    #[test]
    fn rejects_unwrapped_minisign_text() {
        assert!(verify_bytes(b"test", SIGNATURE, PUBLIC_KEY_TEXT).is_err());
    }

    #[test]
    fn rejects_non_utf8_wrapped_text() {
        let invalid = STANDARD.encode([0xff, 0xfe]);
        let signature = STANDARD.encode(SIGNATURE);
        assert!(verify_bytes(b"test", &signature, &invalid).is_err());
    }

    #[test]
    fn rejects_duplicate_cli_arguments() {
        let error = parse_arguments([
            "--artifact".to_string(),
            "one".to_string(),
            "--artifact".to_string(),
            "two".to_string(),
        ])
        .expect_err("duplicates must fail closed");
        assert!(error.contains("duplicate argument"));
    }
}
