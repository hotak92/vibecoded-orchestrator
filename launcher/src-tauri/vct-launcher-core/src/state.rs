// State containers retained from the v1 Tauri command surface. Live commands
// no longer read them, but `lib.rs::run` still constructs and `manage`s them
// so the Tauri app's State<T> registry remains stable for any future commands
// that resurrect the app-process or v1-projects flows. See the archived files
// in the orchestrator's private launch-assets/launcher-archived-rust/ for
// the consumer code.
#![allow(dead_code)]
use std::collections::HashMap;
use std::process::Child;
use std::sync::Mutex;

use crate::types::{Project, ServiceEntry};

pub struct AppManager(pub Mutex<HashMap<String, AppProcess>>);

pub struct AppProcess {
    pub child: Child,
    pub entry: ServiceEntry,
}

pub struct ProjectStore(pub Mutex<ProjectState>);

pub struct ProjectState {
    pub projects: HashMap<String, Project>,
    pub active_project: Option<String>,
}
