//! Edge cases for the definitions gate (Rust).
use std::collections::HashMap;

pub struct Wrapper<T: Clone> {
    inner: Vec<T>,
}

impl<T: Clone + Into<HashMap<String, u8>>> Wrapper<T> {
    pub fn first(&self) -> Option<&T> {
        self.inner.first()
    }
}

pub enum Mode {
    Fast,
    Slow,
}

pub fn template() -> &'static str {
    "fn main() {}\n"
}

const SNIPPET: &str = r#"
pub fn not_real(x: u8) {
}
"#;

#[cfg(not(test))]
pub fn production_only() -> u8 {
    SNIPPET.len() as u8
}

pub fn outer() -> u8 {
    fn helper(x: u8) -> u8 {
        x + 1
    }
    helper(1)
}

macro_rules! make_fn {
    ($name:ident) => {
        fn $name() {}
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture;

    fn setup() -> Fixture {
        Fixture
    }

    #[tokio::test]
    async fn runs_async() {
        let _ = setup();
    }

    #[test]
    fn runs_sync() {}
}

#[tokio::test]
async fn top_level_async_test() {}
