use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, RunEvent, State, WindowEvent};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[derive(Clone, Serialize, Deserialize, Debug)]
struct BackendInfo {
    port: u16,
    token: String,
}

#[derive(Deserialize)]
struct ReadyLine {
    port: u16,
    token: String,
}

struct BackendState {
    info: Mutex<Option<BackendInfo>>,
    child: Mutex<Option<Child>>,
}

impl BackendState {
    fn kill_child(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn find_sidecar_candidates(app: &AppHandle) -> Vec<(PathBuf, Vec<String>)> {
    let mut candidates: Vec<(PathBuf, Vec<String>)> = Vec::new();
    let server_args = vec![
        "server".to_string(),
        "--host".to_string(),
        "127.0.0.1".to_string(),
        "--port".to_string(),
        "0".to_string(),
    ];

    // 1. Paketlenmiş bağımsız sidecar ikili dosyası (Windows/macOS/Linux installer)
    let bin_name = if cfg!(windows) {
        "seslinot-server.exe"
    } else {
        "seslinot-server"
    };
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let sibling = dir.join(bin_name);
            if sibling.is_file() {
                candidates.push((sibling, vec!["--host".into(), "127.0.0.1".into(), "--port".into(), "0".into()]));
            }
        }
    }
    if let Ok(res_dir) = app.path().resource_dir() {
        let bundled = res_dir.join("binaries").join(bin_name);
        if bundled.is_file() {
            candidates.push((bundled, vec!["--host".into(), "127.0.0.1".into(), "--port".into(), "0".into()]));
        }
    }

    // 2. Geliştirme repo .venv içi Python
    if let Ok(cwd) = std::env::current_dir() {
        for base in [cwd.clone(), cwd.join("../.."), cwd.join("../../..")] {
            let venv_py = if cfg!(windows) {
                base.join(".venv").join("Scripts").join("python.exe")
            } else {
                base.join(".venv").join("bin").join("python")
            };
            if venv_py.is_file() {
                let mut args = vec!["-m".to_string(), "seslinot.cli".to_string()];
                args.extend(server_args.clone());
                candidates.push((venv_py, args));
                break;
            }
        }
    }

    // 3. Sistem PATH üzerinde kurulu `seslinot` veya `python3`/`python`
    candidates.push((PathBuf::from(if cfg!(windows) { "seslinot.exe" } else { "seslinot" }), server_args.clone()));
    let mut py_args = vec!["-m".to_string(), "seslinot.cli".to_string()];
    py_args.extend(server_args);
    candidates.push((PathBuf::from(if cfg!(windows) { "python" } else { "python3" }), py_args));

    candidates
}

fn spawn_backend(app: &AppHandle) -> Result<(BackendInfo, Child), String> {
    let mut last_err = String::from("SesliNot backend komutu bulunamadı");

    for (program, args) in find_sidecar_candidates(app) {
        let mut cmd = Command::new(&program);
        cmd.args(&args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());

        #[cfg(windows)]
        {
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        match cmd.spawn() {
            Ok(mut child) => {
                if let Some(stdout) = child.stdout.take() {
                    let mut reader = BufReader::new(stdout);
                    let mut line = String::new();
                    for _ in 0..8 {
                        line.clear();
                        match reader.read_line(&mut line) {
                            Ok(0) => break,
                            Ok(_) => {
                                let trimmed = line.trim();
                                if let Ok(ready) = serde_json::from_str::<ReadyLine>(trimmed) {
                                    // stdout akışını bloklamamak için arka planda tüket
                                    std::thread::spawn(move || {
                                        let mut sink = String::new();
                                        while let Ok(n) = reader.read_line(&mut sink) {
                                            if n == 0 {
                                                break;
                                            }
                                            sink.clear();
                                        }
                                    });
                                    return Ok((
                                        BackendInfo {
                                            port: ready.port,
                                            token: ready.token,
                                        },
                                        child,
                                    ));
                                }
                            }
                            Err(_) => {
                                break;
                            }
                        }
                    }
                }
                let _ = child.kill();
                let _ = child.wait();
                last_err = format!("{} başlatıldı ancak hazır sinyali vermedi", program.display());
            }
            Err(e) => {
                last_err = format!("{} başlatılamadı: {e}", program.display());
            }
        }
    }

    Err(last_err)
}

#[tauri::command]
fn get_backend_info(state: State<'_, BackendState>) -> Result<BackendInfo, String> {
    state
        .info
        .lock()
        .map_err(|_| "Backend kilidi okunamadı".to_string())?
        .clone()
        .ok_or_else(|| "SesliNot yerel motoru henüz hazır değil".to_string())
}

#[tauri::command]
fn set_hud_visible(app: AppHandle, visible: bool) -> Result<(), String> {
    if let Some(hud) = app.get_webview_window("hud") {
        if visible {
            if let Ok(Some(monitor)) = hud.current_monitor() {
                let size = monitor.size();
                let pos = monitor.position();
                let hud_w = 310i32;
                let x = pos.x + ((size.width as i32 - hud_w) / 2);
                let y = pos.y + 42;
                let _ = hud.set_position(PhysicalPosition::new(x, y));
            }
            let _ = hud.show();
        } else {
            let _ = hud.hide();
        }
    }
    Ok(())
}

#[tauri::command]
fn set_tray_recording(app: AppHandle, recording: bool) -> Result<(), String> {
    if let Some(tray) = app.tray_by_id("main-tray") {
        let tip = if recording {
            "SesliNot — Kayıt yapılıyor..."
        } else {
            "SesliNot — Hazır"
        };
        let _ = tray.set_tooltip(Some(tip));
    }
    Ok(())
}

fn show_main_window(app: &AppHandle, tab: Option<&str>) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
    if let Some(t) = tab {
        let _ = app.emit("seslinot://open-tab", t);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        let _ = app.emit("seslinot://toggle-record", ());
                    }
                })
                .build(),
        )
        .manage(BackendState {
            info: Mutex::new(None),
            child: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_info,
            set_hud_visible,
            set_tray_recording
        ])
        .setup(|app| {
            let handle = app.handle().clone();

            // 1. Yerel Python HTTP+SSE motorunu başlat
            match spawn_backend(&handle) {
                Ok((info, child)) => {
                    let state = app.state::<BackendState>();
                    if let Ok(mut info_guard) = state.info.lock() {
                        *info_guard = Some(info);
                    };
                    if let Ok(mut child_guard) = state.child.lock() {
                        *child_guard = Some(child);
                    };
                }
                Err(err) => {
                    eprintln!("[SesliNot] Yerel motor başlatma uyarısı: {err}");
                }
            }

            // 2. Native Sistem Tepsisi (Windows / macOS / Linux)
            let record_item = MenuItem::with_id(app, "record", "Sesli Not Kaydet / Durdur", true, None::<&str>)?;
            let approvals_item = MenuItem::with_id(app, "approvals", "Onay Bekleyen Raporlar", true, None::<&str>)?;
            let open_item = MenuItem::with_id(app, "open", "SesliNot Penceresini Aç", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Çıkış", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&record_item, &approvals_item, &open_item, &quit_item])?;

            let mut tray_builder = TrayIconBuilder::with_id("main-tray")
                .menu(&menu)
                .tooltip("SesliNot — Hazır")
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "record" => {
                        let _ = app.emit("seslinot://toggle-record", ());
                    }
                    "approvals" => show_main_window(app, Some("approvals")),
                    "open" => show_main_window(app, None),
                    "quit" => {
                        app.state::<BackendState>().kill_child();
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle(), None);
                    }
                });

            if let Some(icon) = app.default_window_icon().cloned() {
                tray_builder = tray_builder.icon(icon);
            }
            let _ = tray_builder.build(app);

            // 3. Global Kısayol (Windows/Linux: Ctrl+Alt+N, macOS: Cmd+Shift+N)
            #[cfg(target_os = "macos")]
            let shortcut = Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::KeyN);
            #[cfg(not(target_os = "macos"))]
            let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyN);

            let _ = app.global_shortcut().register(shortcut);

            Ok(())
        })
        .on_window_event(|window, event| {
            // Ana pencere kapatılınca arka planda araştırmalar kesilmesin diye tepsiye gizle
            if window.label() == "main" {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    let _ = window.hide();
                    api.prevent_close();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("SesliNot Tauri uygulaması başlatılamadı");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            app_handle.state::<BackendState>().kill_child();
        }
    });
}
