//! Opt-in local dictation shortcuts in the classic Windows RDP client.
//! The hook only consumes configured chords in a verified mstsc session window.
//! It never records keyboard input; backend work runs outside the hook callback.

use std::{
    cell::RefCell,
    ptr::null_mut,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
};
use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut};
use windows_sys::Win32::{
    Foundation::{CloseHandle, HWND, LPARAM, LRESULT, WPARAM},
    System::{
        LibraryLoader::GetModuleHandleW,
        Threading::{
            GetCurrentThreadId, OpenProcess, QueryFullProcessImageNameW,
            PROCESS_QUERY_LIMITED_INFORMATION,
        },
    },
    UI::{Input::KeyboardAndMouse::*, WindowsAndMessaging::*},
};

const DISPATCH: u32 = WM_APP + 47;

#[derive(Clone, Copy)]
struct Binding {
    id: u32,
    vk: u32,
    modifiers: Modifiers,
}

impl Binding {
    fn parse(value: &str) -> Result<Self, String> {
        let shortcut: Shortcut = value
            .parse()
            .map_err(|_| "Invalid remote desktop shortcut")?;
        // Deliberately limit the opt-in feature to unambiguous keys supported by
        // both RegisterHotKey and mstsc. Other shortcuts remain usable locally.
        let name = shortcut.key.to_string();
        let vk = match shortcut.key {
            Code::Space => VK_SPACE,
            Code::Enter => VK_RETURN,
            Code::Tab => VK_TAB,
            Code::Escape => VK_ESCAPE,
            _ if name.len() == 4 && name.starts_with("Key") => name.as_bytes()[3] as u16,
            _ if name.len() == 6 && name.starts_with("Digit") => name.as_bytes()[5] as u16,
            _ => name.strip_prefix('F').and_then(|s| s.parse::<u16>().ok())
                .filter(|n| (1..=24).contains(n)).map(|n| VK_F1 + n - 1)
                .ok_or("Remote desktop shortcuts require a letter, digit, function key, Space, Enter, Tab or Escape")?,
        };
        if shortcut.mods.is_empty() {
            return Err("Remote desktop shortcuts require a modifier key".into());
        }
        Ok(Self {
            id: shortcut.id(),
            vk: vk as u32,
            modifiers: shortcut.mods,
        })
    }
}

#[derive(Default)]
struct Chords {
    bindings: Vec<Binding>,
    held: Vec<Binding>,
    physical_down: Vec<u32>,
}

impl Chords {
    /// Reconcile releases missed while another input desktop owned the keys.
    /// The caller only samples outside mstsc, whose hook can mask async state.
    fn reconcile(&mut self, mut is_down: impl FnMut(u32) -> bool) -> Vec<u32> {
        self.physical_down.retain(|key| is_down(*key));
        let mut released = Vec::new();
        self.held.retain(|binding| {
            if self.physical_down.contains(&binding.vk) {
                true
            } else {
                released.push(binding.id);
                false
            }
        });
        released
    }

    /// Return (consume, event). A swallowed down always owns its up, even after
    /// focus/modifiers change. Injected paste input never changes this state.
    fn event(
        &mut self,
        vk: u32,
        down: bool,
        injected: bool,
        target: bool,
        modifiers: Modifiers,
    ) -> (bool, Option<(u32, bool)>) {
        if injected {
            return (false, None);
        }
        let repeated = self.physical_down.contains(&vk);
        if down {
            if !repeated && self.bindings.iter().any(|b| b.vk == vk) {
                self.physical_down.push(vk);
            }
        } else {
            self.physical_down.retain(|key| *key != vk);
        }
        if let Some(index) = self.held.iter().position(|b| b.vk == vk) {
            if down {
                return (true, None);
            }
            let binding = self.held.remove(index);
            return (true, Some((binding.id, false)));
        }
        if down && !repeated && target {
            if let Some(binding) = self
                .bindings
                .iter()
                .find(|b| b.vk == vk && b.modifiers == modifiers)
                .copied()
            {
                self.held.push(binding);
                return (true, Some((binding.id, true)));
            }
        }
        (false, None)
    }
}

struct HookState {
    chords: Chords,
    target: HWND,
    modifiers: [bool; 8],
}

const MODIFIER_KEYS: [u16; 8] = [
    VK_LCONTROL,
    VK_RCONTROL,
    VK_LSHIFT,
    VK_RSHIFT,
    VK_LMENU,
    VK_RMENU,
    VK_LWIN,
    VK_RWIN,
];

fn modifier_flags(keys: &[bool; 8]) -> Modifiers {
    let mut result = Modifiers::empty();
    for (index, flag) in [
        Modifiers::CONTROL,
        Modifiers::SHIFT,
        Modifiers::ALT,
        Modifiers::SUPER,
    ]
    .into_iter()
    .enumerate()
    {
        if keys[index * 2] || keys[index * 2 + 1] {
            result.insert(flag);
        }
    }
    result
}

thread_local! {
    static STATE: RefCell<Option<HookState>> = const { RefCell::new(None) };
}

unsafe extern "system" fn keyboard_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code != HC_ACTION as i32 {
        return CallNextHookEx(null_mut(), code, wparam, lparam);
    }
    let down = matches!(wparam as u32, WM_KEYDOWN | WM_SYSKEYDOWN);
    if !down && !matches!(wparam as u32, WM_KEYUP | WM_SYSKEYUP) {
        return CallNextHookEx(null_mut(), code, wparam, lparam);
    }
    let key = &*(lparam as *const KBDLLHOOKSTRUCT);
    let consume = STATE.with(|cell| {
        let mut state = cell.borrow_mut();
        let Some(state) = state.as_mut() else {
            return false;
        };
        if key.flags & LLKHF_INJECTED == 0 {
            if let Some(index) = MODIFIER_KEYS.iter().position(|vk| *vk as u32 == key.vkCode) {
                state.modifiers[index] = down;
            }
        }
        let target = !state.target.is_null() && state.target == GetForegroundWindow();
        let (consume, event) = state.chords.event(
            key.vkCode,
            down,
            key.flags & LLKHF_INJECTED != 0,
            target,
            modifier_flags(&state.modifiers),
        );
        if let Some((id, pressed)) = event {
            // Post only a shortcut identity, never a raw keystroke or text.
            if PostThreadMessageW(
                GetCurrentThreadId(),
                DISPATCH,
                id as usize,
                pressed as isize,
            ) == 0
            {
                state.chords.held.retain(|b| b.id != id);
                return false;
            }
        }
        consume
    });
    if consume {
        1
    } else {
        CallNextHookEx(null_mut(), code, wparam, lparam)
    }
}

unsafe fn mstsc_foreground() -> HWND {
    let window = GetForegroundWindow();
    let mut class = [0u16; 128];
    let count = GetClassNameW(window, class.as_mut_ptr(), class.len() as i32);
    if count <= 0 || String::from_utf16_lossy(&class[..count as usize]) != "TscShellContainerClass"
    {
        return null_mut();
    }
    let mut pid = 0;
    GetWindowThreadProcessId(window, &mut pid);
    let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
    if process.is_null() {
        return null_mut();
    }
    let mut path = [0u16; 32768];
    let mut count = path.len() as u32;
    let ok = QueryFullProcessImageNameW(process, 0, path.as_mut_ptr(), &mut count);
    CloseHandle(process);
    if ok != 0
        && String::from_utf16_lossy(&path[..count as usize])
            .rsplit('\\')
            .next()
            .is_some_and(|name| name.eq_ignore_ascii_case("mstsc.exe"))
    {
        window
    } else {
        null_mut()
    }
}

pub struct Monitor {
    thread_id: u32,
    thread: Option<thread::JoinHandle<()>>,
    stopping: Arc<AtomicBool>,
}

impl Monitor {
    pub fn start(
        shortcuts: &[String],
        dispatch: impl Fn(u32, bool) + Send + 'static,
    ) -> Result<Self, String> {
        let bindings = shortcuts
            .iter()
            .map(|s| Binding::parse(s))
            .collect::<Result<Vec<_>, _>>()?;
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let stopping = Arc::new(AtomicBool::new(false));
        let thread_stopping = Arc::clone(&stopping);
        let thread = thread::Builder::new()
            .name("scriber-rdp-hotkeys".into())
            .spawn(move || unsafe {
                let mut message: MSG = std::mem::zeroed();
                PeekMessageW(&mut message, null_mut(), 0, 0, PM_NOREMOVE);
                STATE.with(|cell| {
                    *cell.borrow_mut() = Some(HookState {
                        chords: Chords {
                            bindings,
                            held: Vec::new(),
                            physical_down: Vec::new(),
                        },
                        target: mstsc_foreground(),
                        modifiers: MODIFIER_KEYS.map(|key| GetAsyncKeyState(key as i32) < 0),
                    })
                });
                let mut hook = SetWindowsHookExW(
                    WH_KEYBOARD_LL,
                    Some(keyboard_hook),
                    GetModuleHandleW(std::ptr::null()),
                    0,
                );
                let timer = SetTimer(null_mut(), 0, 250, None);
                let mut delivered = Vec::new();
                if hook.is_null() || timer == 0 {
                    let _ =
                        ready_tx.send(Err("Could not enable remote desktop hotkeys".to_string()));
                } else {
                    let _ = ready_tx.send(Ok(GetCurrentThreadId()));
                    let mut previous: (usize, i32, i32, i32, i32) = (0, 0, 0, 0, 0);
                    while GetMessageW(&mut message, null_mut(), 0, 0) > 0 {
                        if thread_stopping.load(Ordering::Acquire) {
                            break;
                        }
                        if message.message == DISPATCH {
                            let id = message.wParam as u32;
                            let pressed = message.lParam != 0;
                            if pressed {
                                delivered.push(id);
                            } else {
                                delivered.retain(|active| *active != id);
                            }
                            dispatch(id, pressed);
                        } else if message.message == WM_TIMER {
                            let target = mstsc_foreground();
                            let mut rect = std::mem::zeroed();
                            GetWindowRect(target, &mut rect);
                            let identity = (
                                target as usize,
                                rect.left,
                                rect.top,
                                rect.right,
                                rect.bottom,
                            );
                            let released = STATE.with(|cell| {
                                if let Some(state) = cell.borrow_mut().as_mut() {
                                    state.target = target;
                                    // Reconcile missed releases (for example across a
                                    // secure-desktop switch) only outside mstsc: its
                                    // hook may suppress the OS async modifier state.
                                    if target.is_null() {
                                        state.modifiers = MODIFIER_KEYS
                                            .map(|key| GetAsyncKeyState(key as i32) < 0);
                                        return state
                                            .chords
                                            .reconcile(|key| GetAsyncKeyState(key as i32) < 0);
                                    }
                                }
                                Vec::new()
                            });
                            for id in released {
                                if delivered.contains(&id) {
                                    delivered.retain(|active| *active != id);
                                    dispatch(id, false);
                                }
                            }
                            // mstsc installs its own hook on activation/fullscreen.
                            // Reinsert ours after that transition, not on every key.
                            if !target.is_null() && identity != previous {
                                let replacement = SetWindowsHookExW(
                                    WH_KEYBOARD_LL,
                                    Some(keyboard_hook),
                                    GetModuleHandleW(std::ptr::null()),
                                    0,
                                );
                                if !replacement.is_null() {
                                    UnhookWindowsHookEx(hook);
                                    hook = replacement;
                                }
                            }
                            previous = identity;
                        } else {
                            TranslateMessage(&message);
                            DispatchMessageW(&message);
                        }
                    }
                }
                if timer != 0 {
                    KillTimer(null_mut(), timer);
                }
                if !hook.is_null() {
                    UnhookWindowsHookEx(hook);
                }
                STATE.with(|cell| {
                    cell.borrow_mut().take();
                });
                // Release only presses actually delivered to the dispatcher,
                // including a release still pending in the Windows queue.
                for id in delivered {
                    dispatch(id, false);
                }
            })
            .map_err(|err| err.to_string())?;
        match ready_rx.recv() {
            Ok(Ok(thread_id)) => Ok(Self {
                thread_id,
                thread: Some(thread),
                stopping,
            }),
            _ => {
                let _ = thread.join();
                Err("Could not enable remote desktop hotkeys".into())
            }
        }
    }
}

impl Drop for Monitor {
    fn drop(&mut self) {
        // The timer also observes this flag if posting the wake-up fails.
        self.stopping.store(true, Ordering::Release);
        unsafe {
            PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0);
        }
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chords() -> Chords {
        Chords {
            bindings: vec![Binding::parse("ctrl+space").unwrap()],
            held: Vec::new(),
            physical_down: Vec::new(),
        }
    }

    #[test]
    fn only_exact_physical_chord_in_remote_session_is_consumed() {
        let mut c = chords();
        for (vk, injected, target, mods) in [
            (VK_SPACE, false, false, Modifiers::CONTROL),
            (VK_SPACE, true, true, Modifiers::CONTROL),
            (VK_SPACE, false, true, Modifiers::CONTROL | Modifiers::SHIFT),
            (VK_V, false, true, Modifiers::CONTROL),
        ] {
            assert_eq!(
                c.event(vk as u32, true, injected, target, mods),
                (false, None)
            );
            c.event(vk as u32, false, injected, target, mods);
        }
        assert_eq!(
            c.event(VK_SPACE as u32, true, false, true, Modifiers::CONTROL),
            (true, Some((c.bindings[0].id, true)))
        );
    }

    #[test]
    fn repeats_are_swallowed_and_release_survives_focus_and_modifier_changes() {
        let mut c = chords();
        let id = c.bindings[0].id;
        c.event(VK_SPACE as u32, true, false, true, Modifiers::CONTROL);
        assert_eq!(
            c.event(VK_SPACE as u32, true, false, false, Modifiers::empty()),
            (true, None)
        );
        assert_eq!(
            c.event(VK_SPACE as u32, false, true, true, Modifiers::CONTROL),
            (false, None)
        );
        assert_eq!(
            c.event(VK_SPACE as u32, false, false, false, Modifiers::empty()),
            (true, Some((id, false)))
        );
        assert_eq!(
            c.event(VK_SPACE as u32, false, false, true, Modifiers::CONTROL),
            (false, None)
        );
    }

    #[test]
    fn parsing_preserves_shortcut_identity_and_rejects_unsupported_keys() {
        for (key, vk) in [
            ("ctrl+space", VK_SPACE),
            ("ctrl+shift+d", VK_D),
            ("alt+1", VK_1),
            ("ctrl+F24", VK_F24),
        ] {
            let binding = Binding::parse(key).unwrap();
            assert_eq!(binding.vk, vk as u32);
            assert_eq!(binding.id, key.parse::<Shortcut>().unwrap().id());
        }
        assert!(Binding::parse("Space").is_err());
        assert!(Binding::parse("ctrl+MediaPlayPause").is_err());
    }

    #[test]
    fn left_and_right_modifiers_have_independent_release_state() {
        let mut keys = [false; 8];
        keys[0] = true;
        keys[1] = true;
        keys[3] = true;
        assert_eq!(modifier_flags(&keys), Modifiers::CONTROL | Modifiers::SHIFT);
        keys[0] = false;
        assert_eq!(modifier_flags(&keys), Modifiers::CONTROL | Modifiers::SHIFT);
        keys[1] = false;
        assert_eq!(modifier_flags(&keys), Modifiers::SHIFT);
    }

    #[test]
    fn focus_change_does_not_turn_an_existing_key_hold_into_a_new_shortcut() {
        let mut c = chords();
        assert_eq!(
            c.event(VK_SPACE as u32, true, false, false, Modifiers::CONTROL),
            (false, None)
        );
        assert_eq!(
            c.event(VK_SPACE as u32, true, false, true, Modifiers::CONTROL),
            (false, None)
        );
        c.event(VK_SPACE as u32, false, false, true, Modifiers::CONTROL);
        assert!(
            c.event(VK_SPACE as u32, true, false, true, Modifiers::CONTROL)
                .0
        );
    }

    #[test]
    fn native_hook_thread_starts_and_shuts_down_repeatedly() {
        // Exercise the real Win32 hook/message-loop lifecycle without sending
        // keystrokes, changing foreground focus or requiring an RDP session.
        for _ in 0..3 {
            let monitor = Monitor::start(&["ctrl+shift+F24".into()], |_, _| {}).unwrap();
            drop(monitor);
        }
    }

    #[test]
    fn missing_release_is_dispatched_once_and_next_press_is_accepted() {
        let mut c = chords();
        let id = c.bindings[0].id;
        c.event(VK_SPACE as u32, true, false, true, Modifiers::CONTROL);
        assert!(c.reconcile(|_| true).is_empty());
        assert_eq!(c.reconcile(|_| false), vec![id]);
        assert!(c.reconcile(|_| false).is_empty());
        assert_eq!(
            c.event(VK_SPACE as u32, true, false, true, Modifiers::CONTROL),
            (true, Some((id, true)))
        );
    }
}
