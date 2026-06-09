---
title: Tauri Windows CREATE_NO_WINDOW subprocess pattern
type: concept
tags: [tauri, windows, rust, subprocess, GUI, console-flash, fork-bomb, windows_subsystem]
created: 2026-05-26T15:00:00Z
updated: 2026-05-26T15:00:00Z
valid_from: 2026-05-26T00:00:00Z
valid_until: null
status: active
---

# Tauri Windows CREATE_NO_WINDOW subprocess pattern

## Problema

Qualsiasi app **Tauri (o Win32 GUI in generale)** compilata con `windows_subsystem = "windows"` su Windows che spawna subprocess via `std::process::Command::new` o `tokio::process::Command::new` **alloca una nuova console window per il child** se non si specifica `CREATE_NO_WINDOW` (0x08000000).

Effetto visibile: ogni `Command::new("git").output()`, `Command::new("docker").output()`, ecc. fa lampeggiare una `conhost.exe` window sullo schermo per la durata del subprocess (tipicamente 50-500ms). Con multiple subprocess concorrenti al boot → cascata di flash che mascherano la GUI principale.

Sintomi tipici riportati dall'utente:
- "milioni di finestre" che lampeggiano
- App che sembra non aprirsi (sovrapposizione visiva nasconde la GUI Tauri)
- Sensazione di crash / fork bomb

## Root cause

`CreateProcessW` di Win32 di default eredita la console allocation dal parent. Quando il parent è subsystem `GUI` (no console attaccata), il child chiede una NUOVA console al kernel, che gli alloca un conhost.exe visibile. Solo `CREATE_NO_WINDOW` (0x08000000) suprime questo comportamento.

Non è documentato chiaramente nella documentazione Tauri standard. È un footgun comune che colpisce tutti i Tauri/Rust GUI apps su Windows che spawnano subprocess.

## Fix corretto: pattern centralizzato

Definire un trait estensione `CommandExt` con metodo `.silent()` chainable:

```rust
pub trait CommandExt: Sized {
    fn silent(self) -> Self;
}

impl CommandExt for std::process::Command {
    fn silent(mut self) -> Self {
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt as _;
            self.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        self
    }
}

impl CommandExt for tokio::process::Command {
    fn silent(mut self) -> Self {
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt as _;
            self.creation_flags(0x0800_0000);
        }
        self
    }
}
```

Pattern d'uso ai call sites:
```rust
use vct_launcher_core::process::CommandExt as _;
Command::new("git").silent().arg("status").output()?;
```

**Importante**: trait deve consumare `self` (owned) e ritornare `Self`, NON `&mut self`. Altrimenti `Command::new(x).silent()` causa lifetime error (temporary drop).

## Fix alternativo inline (verboso, sconsigliato per >20 sites)

Pattern inline alternativo:

```rust
let mut cmd = Command::new("git");
cmd.args(["status"]);
#[cfg(windows)]
{
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW on Windows
}
cmd.output()?;
```

4 righe per call site invece di 1. OK per pochi siti, ingestibile a scala.

## Sweep automatizzato per >200 call sites

Script Python regex-based per rolling out su codebase grande:

- Match `Command::new(...)` e `TokioCommand::new(...)` not in comments
- Skip se già contiene `.silent()`, `creation_flags(0x08000000)`, `silent_command(`
- Skip se dentro `#[cfg(test)] mod tests {}`
- Append `.silent()` dopo la closing paren
- Aggiungi `use <crate>::process::CommandExt as _;` import a top file (brace-depth aware per multi-line `use crate::foo::{ a, b };` blocks)

Pattern: uno script di sweep applicato in un singolo run, idempotente per future audit. Esempio reale di order-of-magnitude: codebase Tauri di taglia media-grande, ~200 call sites bonificati in un singolo run.

## Verifica visiva del fix

EnumWindows snapshot via PowerShell `Add-Type` di Win32 API:

```powershell
Add-Type @"
using System.Runtime.InteropServices;
public class WinEnum {
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    // ...
}
"@
```

Filtra per `CASCADIA_HOSTING_WINDOW_CLASS` (Windows Terminal hosting class), `PseudoConsoleWindow`, `XamlExplorerHostIslandWindow`. **Pre-fix**: dozzine. **Post-fix**: 0.

Granularity importante: snapshot a >200ms perde flash sub-frame. Per audit completo serve <100ms o registrazione video OBS dell'utente come ground truth.

## Cosa NON è risolto da questo pattern

- Tauri main window e dialog plugin native (`messagebox`, tray menu) — sono Win32 API direct, NON `Command::new`. Non sono affected.
- Subprocess che vogliono attach-console deliberatamente (es. debug interactive shell) — pattern opposto, non da .silent()
- Console-subsystem children che fanno output streaming (es. `ffmpeg` con progress bar) — possono nascondere stdout, ma `.silent()` non lo fa: sopprime SOLO l'allocazione console, lo stdio capture continua a funzionare

## Storia / scoperte

Scoperto in [[VibeCoded-Orchestrator-Launcher]] durante session debug del fork bomb Windows post-install. L'audit ha trovato che la vasta maggioranza dei `Command::new` call sites del launcher era priva di `CREATE_NO_WINDOW`. Bastavano una decina di subprocess concorrenti al boot per causare cascata visiva di console flash sufficiente a mascherare la GUI principale.

Il fix usa la versione centralizzata con trait `silent()`.

## Riferimenti

- Microsoft docs: https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags (CREATE_NO_WINDOW = 0x08000000)
- std::os::windows::process::CommandExt: https://doc.rust-lang.org/std/os/windows/process/trait.CommandExt.html
- VibeCoded Orchestrator launcher: `vct-launcher-core/src/process.rs` (trait).
