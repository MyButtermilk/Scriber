use serde::Serialize;
use std::{
    collections::{HashMap, VecDeque},
    fs::OpenOptions,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::Mutex,
    time::Duration,
};
use tauri::{AppHandle, Manager, State, WebviewWindow};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
use uuid::Uuid;

use crate::{ui_locale_for_app, BackendManager, UiLocale};

const MAX_EXPORT_BYTES: usize = 64 * 1024 * 1024;
const MAX_STREAMED_AUDIO_EXPORT_BYTES: u64 = 512 * 1024 * 1024;
const MAX_RECENT_EXPORTS: usize = 16;
const MAX_FILENAME_UTF8_BYTES: usize = 180;
const MAX_FILENAME_UTF16_UNITS: usize = 180;
const ALLOWED_EXTENSIONS: &[&str] = &["json", "md", "pdf", "docx", "eml", "opus"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExportError {
    RegistryUnavailable,
    SavedActionExpired,
    UnsupportedFormat,
    EmptyExport,
    ExportTooLarge,
    InvalidSaveLocation,
    SaveFailed,
    InvalidAudioAddress,
    AudioPrepareFailed,
    AudioReadFailed,
    AudioNotReady,
    AudioExportFailed,
    AudioTooLarge,
    AudioUnexpectedType,
    AudioEmpty,
    AudioSaveFailed,
    AudioStopped,
    SavedFileMissing,
    OpenFailed,
    RevealFailed,
}

impl ExportError {
    fn code(self) -> &'static str {
        match self {
            Self::RegistryUnavailable => "meeting_export_registry_unavailable",
            Self::SavedActionExpired => "meeting_export_action_expired",
            Self::UnsupportedFormat => "meeting_export_unsupported_format",
            Self::EmptyExport => "meeting_export_empty",
            Self::ExportTooLarge => "meeting_export_too_large",
            Self::InvalidSaveLocation => "meeting_export_invalid_save_location",
            Self::SaveFailed => "meeting_export_save_failed",
            Self::InvalidAudioAddress => "meeting_export_invalid_audio_address",
            Self::AudioPrepareFailed => "meeting_export_audio_prepare_failed",
            Self::AudioReadFailed => "meeting_export_audio_read_failed",
            Self::AudioNotReady => "meeting_export_audio_not_ready",
            Self::AudioExportFailed => "meeting_export_audio_failed",
            Self::AudioTooLarge => "meeting_export_audio_too_large",
            Self::AudioUnexpectedType => "meeting_export_audio_unexpected_type",
            Self::AudioEmpty => "meeting_export_audio_empty",
            Self::AudioSaveFailed => "meeting_export_audio_save_failed",
            Self::AudioStopped => "meeting_export_audio_stopped",
            Self::SavedFileMissing => "meeting_export_saved_file_missing",
            Self::OpenFailed => "meeting_export_open_failed",
            Self::RevealFailed => "meeting_export_reveal_failed",
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MeetingExportCommandError {
    code: &'static str,
    message: String,
}

#[derive(Debug, Clone, Copy)]
struct ExportLocale {
    locale: UiLocale,
}

impl ExportLocale {
    fn new(locale: UiLocale) -> Self {
        Self { locale }
    }

    fn save_dialog_title(self) -> &'static str {
        match self.locale {
            UiLocale::De => "Meeting-Export speichern",
            UiLocale::En => "Save meeting export",
        }
    }

    fn save_audio_dialog_title(self) -> &'static str {
        match self.locale {
            UiLocale::De => "Komprimiertes Meeting-Audio speichern",
            UiLocale::En => "Save compressed meeting audio",
        }
    }

    fn fallback_filename_stem(self, extension: &str) -> &'static str {
        match (self.locale, extension) {
            (UiLocale::De, "opus") => "Komprimiertes Meeting-Audio",
            (UiLocale::En, "opus") => "Compressed meeting audio",
            (UiLocale::De, "eml") => "Meeting-E-Mail-Entwurf",
            (UiLocale::En, "eml") => "Meeting email draft",
            (UiLocale::De, _) => "Meeting-Export",
            (UiLocale::En, _) => "Meeting export",
        }
    }

    fn filter_label(self, extension: &str) -> &'static str {
        match (self.locale, extension) {
            (UiLocale::De, "json") => "JSON-Daten",
            (UiLocale::En, "json") => "JSON data",
            (UiLocale::De, "md") => "Markdown-Dokument",
            (UiLocale::En, "md") => "Markdown document",
            (UiLocale::De, "pdf") => "PDF-Dokument",
            (UiLocale::En, "pdf") => "PDF document",
            (UiLocale::De, "docx") => "Word-Dokument",
            (UiLocale::En, "docx") => "Word document",
            (UiLocale::De, "eml") => "E-Mail-Entwurf",
            (UiLocale::En, "eml") => "Email draft",
            (UiLocale::De, "opus") => "Komprimiertes Meeting-Audio",
            (UiLocale::En, "opus") => "Compressed meeting audio",
            (UiLocale::De, _) => "Meeting-Export",
            (UiLocale::En, _) => "Meeting export",
        }
    }

    fn error(self, error: ExportError) -> MeetingExportCommandError {
        let message = match (self.locale, error) {
            (UiLocale::De, ExportError::RegistryUnavailable) => {
                "Aktionen für gespeicherte Dateien sind vorübergehend nicht verfügbar. Versuche es erneut."
            }
            (UiLocale::En, ExportError::RegistryUnavailable) => {
                "Saved-file actions are temporarily unavailable. Please try again."
            }
            (UiLocale::De, ExportError::SavedActionExpired) => {
                "Diese Aktion für die gespeicherte Datei ist abgelaufen. Exportiere das Meeting erneut."
            }
            (UiLocale::En, ExportError::SavedActionExpired) => {
                "This saved-file action has expired. Export the meeting again."
            }
            (UiLocale::De, ExportError::UnsupportedFormat) => {
                "Dieses Meeting-Exportformat wird nicht unterstützt."
            }
            (UiLocale::En, ExportError::UnsupportedFormat) => {
                "That meeting export format is not supported."
            }
            (UiLocale::De, ExportError::EmptyExport) => {
                "Der Meeting-Export war leer. Versuche es erneut."
            }
            (UiLocale::En, ExportError::EmptyExport) => {
                "The meeting export was empty. Please try again."
            }
            (UiLocale::De, ExportError::ExportTooLarge) => {
                "Der Meeting-Export ist zu groß, um ihn von dieser Ansicht aus zu speichern."
            }
            (UiLocale::En, ExportError::ExportTooLarge) => {
                "The meeting export is too large to save from this screen."
            }
            (UiLocale::De, ExportError::InvalidSaveLocation) => {
                "Dieser Speicherort kann auf diesem Gerät nicht verwendet werden."
            }
            (UiLocale::En, ExportError::InvalidSaveLocation) => {
                "That save location cannot be used on this device."
            }
            (UiLocale::De, ExportError::SaveFailed) => {
                "Scriber konnte den Meeting-Export nicht speichern. Versuche es erneut."
            }
            (UiLocale::En, ExportError::SaveFailed) => {
                "Scriber could not save the meeting export. Please try again."
            }
            (UiLocale::De, ExportError::InvalidAudioAddress) => {
                "Diese Adresse für den Meeting-Audioexport ist nicht zulässig."
            }
            (UiLocale::En, ExportError::InvalidAudioAddress) => {
                "That meeting audio export address is not allowed."
            }
            (UiLocale::De, ExportError::AudioPrepareFailed) => {
                "Scriber konnte den Audioexport nicht vorbereiten. Versuche es erneut."
            }
            (UiLocale::En, ExportError::AudioPrepareFailed) => {
                "Scriber could not prepare the audio export. Please try again."
            }
            (UiLocale::De, ExportError::AudioReadFailed) => {
                "Scriber konnte das komprimierte Meeting-Audio nicht lesen. Versuche es erneut."
            }
            (UiLocale::En, ExportError::AudioReadFailed) => {
                "Scriber could not read the compressed meeting audio. Please try again."
            }
            (UiLocale::De, ExportError::AudioNotReady) => {
                "Das komprimierte Meeting-Audio ist noch nicht verfügbar."
            }
            (UiLocale::En, ExportError::AudioNotReady) => {
                "Compressed meeting audio is not ready yet."
            }
            (UiLocale::De, ExportError::AudioExportFailed) => {
                "Scriber konnte das komprimierte Meeting-Audio nicht exportieren. Versuche es erneut."
            }
            (UiLocale::En, ExportError::AudioExportFailed) => {
                "Scriber could not export the compressed meeting audio. Please try again."
            }
            (UiLocale::De, ExportError::AudioTooLarge) => {
                "Das komprimierte Meeting-Audio ist für den Export zu groß."
            }
            (UiLocale::En, ExportError::AudioTooLarge) => {
                "The compressed meeting audio is too large to export."
            }
            (UiLocale::De, ExportError::AudioUnexpectedType) => {
                "Der Meeting-Audioexport hat einen unerwarteten Dateityp zurückgegeben."
            }
            (UiLocale::En, ExportError::AudioUnexpectedType) => {
                "The meeting audio export returned an unexpected file type."
            }
            (UiLocale::De, ExportError::AudioEmpty) => {
                "Das komprimierte Meeting-Audio war leer. Versuche es erneut."
            }
            (UiLocale::En, ExportError::AudioEmpty) => {
                "The compressed meeting audio was empty. Please try again."
            }
            (UiLocale::De, ExportError::AudioSaveFailed) => {
                "Scriber konnte die Audiodatei nicht speichern. Versuche es erneut."
            }
            (UiLocale::En, ExportError::AudioSaveFailed) => {
                "Scriber could not save the audio file. Please try again."
            }
            (UiLocale::De, ExportError::AudioStopped) => {
                "Der Meeting-Audioexport wurde unerwartet beendet. Versuche es erneut."
            }
            (UiLocale::En, ExportError::AudioStopped) => {
                "The meeting audio export stopped unexpectedly. Please try again."
            }
            (UiLocale::De, ExportError::SavedFileMissing) => {
                "Die gespeicherte Datei befindet sich nicht mehr an diesem Speicherort."
            }
            (UiLocale::En, ExportError::SavedFileMissing) => {
                "The saved file is no longer at that location."
            }
            (UiLocale::De, ExportError::OpenFailed) => {
                "Scriber konnte die gespeicherte Datei nicht öffnen."
            }
            (UiLocale::En, ExportError::OpenFailed) => {
                "Scriber could not open the saved file."
            }
            (UiLocale::De, ExportError::RevealFailed) => {
                "Scriber konnte den Ordner nicht öffnen."
            }
            (UiLocale::En, ExportError::RevealFailed) => {
                "Scriber could not open the folder."
            }
        };
        MeetingExportCommandError {
            code: error.code(),
            message: message.to_string(),
        }
    }
}

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
    fn remember(&self, path: PathBuf) -> Result<String, ExportError> {
        let token = Uuid::new_v4().simple().to_string();
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| ExportError::RegistryUnavailable)?;
        inner.paths.insert(token.clone(), path);
        inner.order.push_back(token.clone());
        while inner.order.len() > MAX_RECENT_EXPORTS {
            if let Some(expired) = inner.order.pop_front() {
                inner.paths.remove(&expired);
            }
        }
        Ok(token)
    }

    fn resolve(&self, token: &str) -> Result<PathBuf, ExportError> {
        if token.len() != 32 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(ExportError::SavedActionExpired);
        }
        let inner = self
            .inner
            .lock()
            .map_err(|_| ExportError::RegistryUnavailable)?;
        inner
            .paths
            .get(token)
            .cloned()
            .ok_or(ExportError::SavedActionExpired)
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

fn normalize_extension(extension: &str) -> Result<String, ExportError> {
    let normalized = extension
        .trim()
        .trim_start_matches('.')
        .to_ascii_lowercase();
    if ALLOWED_EXTENSIONS.contains(&normalized.as_str()) {
        Ok(normalized)
    } else {
        Err(ExportError::UnsupportedFormat)
    }
}

fn sanitize_filename(filename: &str, extension: &str, text: ExportLocale) -> String {
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
        sanitized = text.fallback_filename_stem(extension).to_string();
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
        stem = text.fallback_filename_stem(extension).to_string();
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
            text.fallback_filename_stem(extension)
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

fn write_export_stream_atomically(
    destination: &Path,
    reader: &mut impl Read,
    max_bytes: u64,
) -> io::Result<u64> {
    let parent = destination.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "export destination has no parent",
        )
    })?;
    let filename = destination
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("meeting-audio");
    let temporary = parent.join(format!(
        ".{filename}.{}.scriber-export.tmp",
        Uuid::new_v4().simple()
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        let mut total = 0u64;
        let mut buffer = [0u8; 64 * 1024];
        loop {
            let read = reader.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            total = total.saturating_add(read as u64);
            if total > max_bytes {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "compressed meeting audio exceeds the export limit",
                ));
            }
            file.write_all(&buffer[..read])?;
        }
        if total == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "compressed meeting audio was empty",
            ));
        }
        file.sync_all()?;
        drop(file);
        replace_file(&temporary, destination)?;
        Ok(total)
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
) -> Result<Option<SavedMeetingExport>, MeetingExportCommandError> {
    let text = ExportLocale::new(ui_locale_for_app(window.app_handle()));
    if bytes.is_empty() {
        return Err(text.error(ExportError::EmptyExport));
    }
    if bytes.len() > MAX_EXPORT_BYTES {
        return Err(text.error(ExportError::ExportTooLarge));
    }
    let extension = normalize_extension(&extension).map_err(|error| text.error(error))?;
    let filename = sanitize_filename(&filename, &extension, text);
    let selected = window
        .dialog()
        .file()
        .set_parent(&window)
        .set_title(text.save_dialog_title())
        .set_file_name(&filename)
        .add_filter(text.filter_label(&extension), &[extension.as_str()])
        .blocking_save_file();
    let Some(selected) = selected else {
        return Ok(None);
    };
    let destination = ensure_extension(
        selected
            .into_path()
            .map_err(|_| text.error(ExportError::InvalidSaveLocation))?,
        &extension,
    );
    write_export_atomically(&destination, &bytes)
        .map_err(|_| text.error(ExportError::SaveFailed))?;
    let token = registry
        .remember(destination.clone())
        .map_err(|error| text.error(error))?;
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

fn valid_meeting_export_id(meeting_id: &str) -> bool {
    !meeting_id.is_empty()
        && meeting_id.len() <= 128
        && meeting_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

#[tauri::command]
pub async fn save_meeting_audio_export(
    window: WebviewWindow,
    manager: State<'_, BackendManager>,
    registry: State<'_, MeetingExportRegistry>,
    meeting_id: String,
    filename: String,
) -> Result<Option<SavedMeetingExport>, MeetingExportCommandError> {
    let text = ExportLocale::new(ui_locale_for_app(window.app_handle()));
    if !valid_meeting_export_id(&meeting_id) {
        return Err(text.error(ExportError::InvalidAudioAddress));
    }
    let extension = "opus";
    let filename = sanitize_filename(&filename, extension, text);
    let selected = window
        .dialog()
        .file()
        .set_parent(&window)
        .set_title(text.save_audio_dialog_title())
        .set_file_name(&filename)
        .add_filter(text.filter_label(extension), &[extension])
        .blocking_save_file();
    let Some(selected) = selected else {
        return Ok(None);
    };
    let destination = ensure_extension(
        selected
            .into_path()
            .map_err(|_| text.error(ExportError::InvalidSaveLocation))?,
        extension,
    );
    let access = manager.access();
    let url = format!(
        "{}/api/meetings/{meeting_id}/export/audio",
        access.base_url.trim_end_matches('/')
    );
    let session_token = access.session_token;
    let destination_for_write = destination.clone();
    let export_result = tauri::async_runtime::spawn_blocking(move || -> Result<(), ExportError> {
        let client = reqwest::blocking::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(300))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| ExportError::AudioPrepareFailed)?;
        let mut request = client.get(url);
        if !session_token.is_empty() {
            request = request.header("X-Scriber-Token", session_token);
        }
        let mut response = request.send().map_err(|_| ExportError::AudioReadFailed)?;
        if !response.status().is_success() {
            return Err(if response.status().as_u16() == 404 {
                ExportError::AudioNotReady
            } else {
                ExportError::AudioExportFailed
            });
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_STREAMED_AUDIO_EXPORT_BYTES)
        {
            return Err(ExportError::AudioTooLarge);
        }
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if !content_type.starts_with("audio/") {
            return Err(ExportError::AudioUnexpectedType);
        }
        write_export_stream_atomically(
            &destination_for_write,
            &mut response,
            MAX_STREAMED_AUDIO_EXPORT_BYTES,
        )
        .map_err(|error| match error.kind() {
            io::ErrorKind::InvalidData => ExportError::AudioTooLarge,
            io::ErrorKind::UnexpectedEof => ExportError::AudioEmpty,
            _ => ExportError::AudioSaveFailed,
        })?;
        Ok(())
    })
    .await
    .map_err(|_| ExportError::AudioStopped)
    .and_then(|result| result);
    export_result.map_err(|error| text.error(error))?;

    let token = registry
        .remember(destination.clone())
        .map_err(|error| text.error(error))?;
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

fn saved_export_path(
    registry: &MeetingExportRegistry,
    token: &str,
) -> Result<PathBuf, ExportError> {
    let path = registry.resolve(token)?;
    if !path.is_file() {
        return Err(ExportError::SavedFileMissing);
    }
    Ok(path)
}

#[tauri::command]
pub fn open_meeting_export(
    app: AppHandle,
    registry: State<'_, MeetingExportRegistry>,
    token: String,
) -> Result<(), MeetingExportCommandError> {
    let text = ExportLocale::new(ui_locale_for_app(&app));
    let path = saved_export_path(&registry, &token).map_err(|error| text.error(error))?;
    app.opener()
        .open_path(path.to_string_lossy(), None::<String>)
        .map_err(|_| text.error(ExportError::OpenFailed))
}

#[tauri::command]
pub fn reveal_meeting_export(
    app: AppHandle,
    registry: State<'_, MeetingExportRegistry>,
    token: String,
) -> Result<(), MeetingExportCommandError> {
    let text = ExportLocale::new(ui_locale_for_app(&app));
    let path = saved_export_path(&registry, &token).map_err(|error| text.error(error))?;
    app.opener()
        .reveal_item_in_dir(path)
        .map_err(|_| text.error(ExportError::RevealFailed))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filename_is_safe_and_keeps_the_requested_format() {
        let english = ExportLocale::new(UiLocale::En);
        assert_eq!(
            sanitize_filename("Quarterly: planning?.PDF", "pdf", english),
            "Quarterly_ planning_.PDF"
        );
        assert_eq!(
            sanitize_filename("   ", "docx", english),
            "Meeting export.docx"
        );
        assert_eq!(sanitize_filename("CON.pdf", "pdf", english), "_CON.pdf");
        assert_eq!(
            sanitize_filename("conout$.pdf", "pdf", english),
            "_conout$.pdf"
        );
        assert_eq!(
            sanitize_filename("Lpt9.notes", "md", english),
            "_Lpt9.notes.md"
        );
    }

    #[test]
    fn unicode_filename_limits_are_boundary_safe_and_preserve_extension() {
        let filename = sanitize_filename(
            &format!("{} report.pdf", "🪶".repeat(200)),
            "pdf",
            ExportLocale::new(UiLocale::En),
        );
        assert!(filename.ends_with(".pdf"));
        assert!(filename.len() <= MAX_FILENAME_UTF8_BYTES);
        assert!(filename.encode_utf16().count() <= MAX_FILENAME_UTF16_UNITS);
        assert!(filename.is_char_boundary(filename.len()));
    }

    #[test]
    fn unsupported_formats_are_rejected() {
        assert_eq!(normalize_extension(".PDF").unwrap(), "pdf");
        assert_eq!(normalize_extension(".OPUS").unwrap(), "opus");
        assert_eq!(
            normalize_extension("exe").unwrap_err(),
            ExportError::UnsupportedFormat
        );
    }

    #[test]
    fn dialog_copy_and_fallback_filenames_follow_the_interface_locale() {
        let german = ExportLocale::new(UiLocale::De);
        let english = ExportLocale::new(UiLocale::En);

        assert_eq!(german.save_dialog_title(), "Meeting-Export speichern");
        assert_eq!(english.save_dialog_title(), "Save meeting export");
        assert_eq!(
            german.save_audio_dialog_title(),
            "Komprimiertes Meeting-Audio speichern"
        );
        assert_eq!(german.filter_label("docx"), "Word-Dokument");
        assert_eq!(english.filter_label("docx"), "Word document");
        assert_eq!(sanitize_filename("", "pdf", german), "Meeting-Export.pdf");
        assert_eq!(
            sanitize_filename("", "opus", german),
            "Komprimiertes Meeting-Audio.opus"
        );
        assert_eq!(
            sanitize_filename("", "opus", english),
            "Compressed meeting audio.opus"
        );
    }

    #[test]
    fn native_export_errors_are_localized_and_do_not_leak_internal_details() {
        let german = ExportLocale::new(UiLocale::De);
        let english = ExportLocale::new(UiLocale::En);
        let errors = [
            ExportError::RegistryUnavailable,
            ExportError::SavedActionExpired,
            ExportError::UnsupportedFormat,
            ExportError::EmptyExport,
            ExportError::ExportTooLarge,
            ExportError::InvalidSaveLocation,
            ExportError::SaveFailed,
            ExportError::InvalidAudioAddress,
            ExportError::AudioPrepareFailed,
            ExportError::AudioReadFailed,
            ExportError::AudioNotReady,
            ExportError::AudioExportFailed,
            ExportError::AudioTooLarge,
            ExportError::AudioUnexpectedType,
            ExportError::AudioEmpty,
            ExportError::AudioSaveFailed,
            ExportError::AudioStopped,
            ExportError::SavedFileMissing,
            ExportError::OpenFailed,
            ExportError::RevealFailed,
        ];

        for error in errors {
            for localized in [german.error(error), english.error(error)] {
                assert_eq!(localized.code, error.code());
                assert!(localized.code.starts_with("meeting_export_"));
                let message = localized.message;
                assert!(!message.trim().is_empty());
                assert!(!message.contains("C:\\"));
                assert!(!message.contains("/Users/"));
                assert!(!message.contains("os error"));
                assert!(!message.contains("http://"));
                assert!(!message.contains("https://"));
            }
        }
        assert_eq!(
            german.error(ExportError::OpenFailed).message,
            "Scriber konnte die gespeicherte Datei nicht öffnen."
        );
        assert_eq!(
            english.error(ExportError::RevealFailed).message,
            "Scriber could not open the folder."
        );
    }

    #[test]
    fn meeting_audio_export_ids_are_strictly_scoped() {
        assert!(valid_meeting_export_id("meeting_123-abc"));
        assert!(!valid_meeting_export_id(""));
        assert!(!valid_meeting_export_id("../meeting"));
        assert!(!valid_meeting_export_id(&"a".repeat(129)));
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
    fn streamed_audio_writer_is_atomic_and_bounded() {
        let root = std::env::temp_dir().join(format!(
            "scriber-streamed-audio-export-test-{}",
            Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let destination = root.join("meeting.opus");
        let mut source = io::Cursor::new(b"OggS-opus-audio".to_vec());
        assert_eq!(
            write_export_stream_atomically(&destination, &mut source, 64).unwrap(),
            15
        );
        assert_eq!(std::fs::read(&destination).unwrap(), b"OggS-opus-audio");

        let mut oversized = io::Cursor::new(vec![1u8; 9]);
        assert!(write_export_stream_atomically(&destination, &mut oversized, 8).is_err());
        assert_eq!(std::fs::read(&destination).unwrap(), b"OggS-opus-audio");
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
