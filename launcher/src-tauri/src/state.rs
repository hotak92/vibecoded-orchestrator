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
