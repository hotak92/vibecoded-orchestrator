//! Engine module for the golden-fixture repo (Rust, regex-parsed).

use std::collections::HashMap;

/// A simple counter struct.
pub struct Counter {
    count: u64,
}

impl Counter {
    pub fn new() -> Self {
        Counter { count: 0 }
    }

    pub fn increment(&mut self) {
        self.count += 1;
    }

    pub fn value(&self) -> u64 {
        self.count
    }
}

/// A trait describing something that can be reset.
pub trait Resettable {
    fn reset(&mut self);
}

impl Resettable for Counter {
    fn reset(&mut self) {
        self.count = 0;
    }
}

pub fn build_registry() -> HashMap<String, u64> {
    let mut map = HashMap::new();
    map.insert("start".to_string(), 0);
    map
}

async fn fetch_remote(id: u64) -> u64 {
    id * 2
}
