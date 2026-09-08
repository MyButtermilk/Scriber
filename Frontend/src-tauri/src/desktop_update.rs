//! One install admission gate shared by the main and tray WebViews.

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{AppHandle, State};

#[derive(Default)]
pub(crate) struct DesktopUpdateInstallGate(AtomicBool);

impl DesktopUpdateInstallGate {
    fn try_begin(&self) -> bool {
        self.0
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }

    pub(crate) fn is_active(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }

    fn finish(&self) {
        self.0.store(false, Ordering::Release);
    }
}

#[tauri::command]
pub(crate) fn begin_desktop_update_install(
    app: AppHandle,
    gate: State<'_, DesktopUpdateInstallGate>,
) -> bool {
    if !gate.try_begin() {
        return false;
    }
    super::update_tray_status_for_app(&app, |status| status.update_installing = true);
    super::write_shell_log("desktop update installation admitted (mode=quiet)");
    true
}

#[tauri::command]
pub(crate) fn finish_desktop_update_install(
    app: AppHandle,
    gate: State<'_, DesktopUpdateInstallGate>,
) {
    gate.finish();
    super::update_tray_status_for_app(&app, |status| status.update_installing = false);
}

#[cfg(test)]
mod tests {
    use super::DesktopUpdateInstallGate;
    use std::sync::{Arc, Barrier};

    #[test]
    fn concurrent_windows_admit_exactly_one_install_and_failure_can_retry() {
        let gate = Arc::new(DesktopUpdateInstallGate::default());
        let ready = Arc::new(Barrier::new(8));
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let gate = Arc::clone(&gate);
                let ready = Arc::clone(&ready);
                std::thread::spawn(move || {
                    ready.wait();
                    gate.try_begin()
                })
            })
            .collect();
        assert_eq!(
            threads
                .into_iter()
                .map(|thread| thread.join().unwrap())
                .filter(|admitted| *admitted)
                .count(),
            1
        );
        assert!(gate.is_active());
        gate.finish();
        assert!(!gate.is_active());
        assert!(gate.try_begin());
    }
}
