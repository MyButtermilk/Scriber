use serde::Serialize;
use std::{
    collections::{HashMap, VecDeque},
    fs::OpenOptions,
    io::{self, Write},
    path::{Path, PathBuf},
    sync::Mutex,
};
use tauri::{AppHandle, State, WebviewWindow};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
use uuid::Uuid;

const MAX_EXPORT_BYTES: usize = 64 * 1024 * 1024;
const MAX_RECENT_EXPORTS: usize = 16;
const MAX_FILENAME_UTF8_BYTES: usize = 180;
const MAX_FILENAME_UTF16_UNITS: usize = 180;
const ALLOWED_EXTENSIONS: &[&str] = &["json", "md", "pdf", "docx", "eml"];

#[derive(Default)]
struct MeetingExportRegistryInner {
    paths: HashMap<String, PathBuf>,
    order: VecDeque<String>,
}

#[derive(Default)]
pub struct MeetingExportRegistry {
    inner: Mutex<MeetingExportRegistryInner>,
}

impl MeetingExportRegistry {
    fn remember(&self, path: PathBuf) -> Result<String, String> {
        let token = Uuid::new_v4().simple().to_string();
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "The saved-file list is temporarily unavailable.".to_string())?;
        inner.paths.insert(token.clone(), path);
        inner.order.push_back(token.clone());
        while inner.order.len() > MAX_RECENT_EXPORTS {
            if let Some(expired) = inner.order.pop_front() {
                inner.paths.remove(&expired);
            }
        }
        Ok(token)
    }

    fn resolve(&self, token: &str) -> Result<PathBuf, String> {
        if token.len() != 32 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(
                "This saved-file action has expired. Export the meeting again.".to_string(),
            );
        }
        let inner = self
            .inner
            .lock()
            .map_err(|_| "The saved-file list is temporarily unavailable.".to_string())?;
        inner.paths.get(token).cloned().ok_or_else(|| {
            "This saved-file action has expired. Export the meeting again.".to_string()
        })
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SavedMeetingExport {
    token: String,
    path: String,
    directory: String,
    filename: String,
}

fn normalize_extension(extension: &str) -> Result<String, String> {
    let normalized = extension
        .trim()
        .trim_start_matches('.')
        .to_ascii_lowercase();
    if ALLOWED_EXTENSIONS.contains(&normalized.as_str()) {
        Ok(normalized)
    } else {
        Err("That meeting export format is not supported.".to_string())
    }
}

fn sanitize_filename(filename: &str, extension: &str) -> String {
    let mut sanitized = filename
        .chars()
        .map(|character| {
            if character.is_control() || r#"<>:\"/\|?*"#.contains(character) {
                '_'
            } else {
                character
            }
        })
        .collect::<String>();
    sanitized = sanitized.trim().trim_end_matches(['.', ' ']).to_string();
    if sanitized.is_empty() {
        sanitized = "Meeting export".to_string();
    }
    let expected_suffix = format!(".{extension}");
    let has_expected_suffix = sanitized.to_ascii_lowercase().ends_with(&expected_suffix);
    let (mut stem, suffix) = if has_expected_suffix {
        let suffix_start = sanitized.len().saturating_sub(expected_suffix.len());
        (
            sanitized[..suffix_start].to_string(),
            sanitized[suffix_start..].to_string(),
        )
    } else {
        (sanitized, expected_suffix)
    };
    stem = stem.trim_end_matches(['.', ' ']).to_string();
    if stem.is_empty() {
        stem = "Meeting export".to_string();
    }
    if is_windows_reserved_stem(&stem) {
        stem.insert(0, '_');
    }
    let byte_budget = MAX_FILENAME_UTF8_BYTES.saturating_sub(suffix.len());
    let utf16_budget = MAX_FILENAME_UTF16_UNITS.saturating_sub(suffix.encode_utf16().count());
    let stem = truncate_component(&stem, byte_budget, utf16_budget)
        .trim_end_matches(['.', ' '])
        .to_string();
    format!(
        "{}{suffix}",
        if stem.is_empty() {
            "Meeting export"
        } else {
            &stem
        }
    )
}

fn truncate_component(value: &str, byte_budget: usize, utf16_budget: usize) -> String {
    let mut result = String::new();
    let mut utf16_units = 0usize;
    for character in value.chars() {
        let next_bytes = result.len().saturating_add(character.len_utf8());
        let next_utf16 = utf16_units.saturating_add(character.len_utf16());
        if next_bytes > byte_budget || next_utf16 > utf16_budget {
            break;
        }
        result.push(character);
        utf16_units = next_utf16;
    }
    result
}

fn is_windows_reserved_stem(stem: &str) -> bool {
    let stem = stem
        .trim_matches(['.', ' '])
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_uppercase();
    matches!(
        stem.as_str(),
        "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
    ) || (stem.len() == 4
        && (stem.starts_with("COM") || stem.starts_with("LPT"))
        && matches!(stem.as_bytes()[3], b'1'..=b'9'))
}

fn ensure_extension(mut path: PathBuf, extension: &str) -> PathBuf {
    let matches = path
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case(extension));
    if !matches {
        path.set_extension(extension);
    }
    path
}

fn export_filter_label(extension: &str) -> &'static str {
    match extension {
        "json" => "JSON data",
        "md" => "Markdown document",
        "pdf" => "PDF document",
        "docx" => "Word document",
        "eml" => "Email draft",
        _ => "Meeting export",
    }
}

fn write_export_atomically(destination: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = destination.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "export destination has no parent",
        )
    })?;
    let filename = destination
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("meeting-export");
    let temporary = parent.join(format!(
        ".{filename}.{}.scriber-export.tmp",
        Uuid::new_v4().simple()
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        replace_file(&temporary, destination)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

#[cfg(windows)]
fn replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let source_wide = source
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let destination_wide = destination
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let moved = unsafe {
        MoveFileExW(
            source_wide.as_ptr(),
            destination_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if moved == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    std::fs::rename(source, destination)
}

#[tauri::command]
pub async fn save_meeting_export(
    window: WebviewWindow,
    registry: State<'_, MeetingExportRegistry>,
    filename: String,
    extension: String,
    bytes: Vec<u8>,
) -> Result<Option<SavedMeetingExport>, String> {
    if bytes.is_empty() {
        return Err("The meeting export was empty. Please try again.".to_string());
    }
    if bytes.len() > MAX_EXPORT_BYTES {
        return Err("The meeting export is too large to save from this screen.".to_string());
    }
    let extension = normalize_extension(&extension)?;
    let filename = sanitize_filename(&filename, &extension);
    let selected = window
        .dialog()
        .file()
        .set_parent(&window)
        .set_title("Save meeting export")
        .set_file_name(&filename)
        .add_filter(export_filter_label(&extension), &[extension.as_str()])
        .blocking_save_file();
    let Some(selected) = selected else {
        return Ok(None);
    };
    let destination = ensure_extension(
        selected
            .into_path()
            .map_err(|_| "That save location cannot be used on this device.".to_string())?,
        &extension,
    );
    write_export_atomically(&destination, &bytes)
        .map_err(|error| format!("Scriber could not save the file ({error})."))?;
    let token = registry.remember(destination.clone())?;
    let filename = destination
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or(&filename)
        .to_string();
    let directory = destination
        .parent()
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_default();
    Ok(Some(SavedMeetingExport {
        token,
        path: destination.to_string_lossy().into_owned(),
        directory,
        filename,
    }))
}

fn saved_export_path(registry: &MeetingExportRegistry, token: &str) -> Result<PathBuf, String> {
    let path = registry.resolve(token)?;
    if !path.is_file() {
        return Err("The saved file is no longer at that location.".to_string());
    }
    Ok(path)
}

#[tauri::command]
pub fn open_meeting_export(
    app: AppHandle,
    registry: State<'_, MeetingExportRegistry>,
    token: String,
) -> Result<(), String> {
    let path = saved_export_path(&registry, &token)?;
    app.opener()
        .open_path(path.to_string_lossy(), None::<String>)
        .map_err(|error| format!("Scriber could not open the saved file ({error})."))
}

#[tauri::command]
pub fn reveal_meeting_export(
    app: AppHandle,
    registry: State<'_, MeetingExportRegistry>,
    token: String,
) -> Result<(), String> {
    let path = saved_export_path(&registry, &token)?;
    app.opener()
        .reveal_item_in_dir(path)
        .map_err(|error| format!("Scriber could not open the folder ({error})."))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filename_is_safe_and_keeps_the_requested_format() {
        assert_eq!(
            sanitize_filename("Quarterly: planning?.PDF", "pdf"),
            "Quarterly_ planning_.PDF"
        );
        assert_eq!(sanitize_filename("   ", "docx"), "Meeting export.docx");
        assert_eq!(sanitize_filename("CON.pdf", "pdf"), "_CON.pdf");
        assert_eq!(sanitize_filename("conout$.pdf", "pdf"), "_conout$.pdf");
        assert_eq!(sanitize_filename("Lpt9.notes", "md"), "_Lpt9.notes.md");
    }

    #[test]
    fn unicode_filename_limits_are_boundary_safe_and_preserve_extension() {
        let filename = sanitize_filename(&format!("{} report.pdf", "🪶".repeat(200)), "pdf");
        assert!(filename.ends_with(".pdf"));
        assert!(filename.len() <= MAX_FILENAME_UTF8_BYTES);
        assert!(filename.encode_utf16().count() <= MAX_FILENAME_UTF16_UNITS);
        assert!(filename.is_char_boundary(filename.len()));
    }

    #[test]
    fn unsupported_formats_are_rejected() {
        assert_eq!(normalize_extension(".PDF").unwrap(), "pdf");
        assert!(normalize_extension("exe").is_err());
    }

    #[test]
    fn selected_path_is_forced_to_the_requested_extension() {
        assert_eq!(
            ensure_extension(PathBuf::from("meeting.exe"), "pdf"),
            PathBuf::from("meeting.pdf")
        );
        assert_eq!(
            ensure_extension(PathBuf::from("meeting.PDF"), "pdf"),
            PathBuf::from("meeting.PDF")
        );
    }

    #[test]
    fn atomic_writer_replaces_an_existing_export() {
        let root = std::env::temp_dir().join(format!(
            "scriber-export-dialog-test-{}",
            Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let destination = root.join("meeting.md");
        std::fs::write(&destination, b"old").unwrap();
        write_export_atomically(&destination, b"new").unwrap();
        assert_eq!(std::fs::read(&destination).unwrap(), b"new");
        assert!(root.read_dir().unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .contains(".tmp")));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn atomic_writer_keeps_existing_destination_when_replace_fails() {
        let root = std::env::temp_dir().join(format!(
            "scriber-export-dialog-failure-test-{}",
            Uuid::new_v4().simple()
        ));
        let destination = root.join("occupied.md");
        std::fs::create_dir_all(&destination).unwrap();

        assert!(write_export_atomically(&destination, b"new").is_err());
        assert!(destination.is_dir());
        assert!(root.read_dir().unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .contains(".tmp")));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn tauri_json_byte_array_deserializes_as_bounded_bytes() {
        assert_eq!(
            serde_json::from_str::<Vec<u8>>("[0,127,255]").unwrap(),
            vec![0, 127, 255]
        );
        assert!(serde_json::from_str::<Vec<u8>>("[256]").is_err());
    }

    #[test]
    fn registry_expires_old_actions() {
        let registry = MeetingExportRegistry::default();
        let mut first = String::new();
        for index in 0..=MAX_RECENT_EXPORTS {
            let token = registry
                .remember(PathBuf::from(format!("meeting-{index}.pdf")))
                .unwrap();
            if index == 0 {
                first = token;
            }
        }
        assert!(registry.resolve(&first).is_err());
    }
}
