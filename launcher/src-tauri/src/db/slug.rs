//! Slug generation for URL-addressable project routes.
//!
//! Rules:
//!   * lowercase ASCII letters, digits, and dashes only
//!   * dash-separated, no leading/trailing/consecutive dashes
//!   * unicode characters are dropped (we don't transliterate; users
//!     working in non-Latin scripts can rename the project to set a
//!     desired ASCII slug, or accept the auto-generated `project-<id6>`
//!     fallback)
//!   * collisions are resolved by appending `-2`, `-3`, … using the
//!     supplied `is_taken` predicate
//!
//! Routes consume slugs via `/p/<slug>` (see launcher/src/routes/p/[slug]).

/// Convert a free-form project name into a base slug. Returns
/// `"project"` if the name produces an empty slug after sanitization
/// (e.g. all unicode); the caller is expected to dedupe via `unique_slug`.
pub fn slugify(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    let mut last_dash = true; // suppresses leading dashes
    for ch in name.chars() {
        let c = ch.to_ascii_lowercase();
        if c.is_ascii_alphanumeric() {
            out.push(c);
            last_dash = false;
        } else if !last_dash {
            out.push('-');
            last_dash = true;
        }
    }
    while out.ends_with('-') {
        out.pop();
    }
    if out.is_empty() {
        "project".to_string()
    } else {
        // Cap at 50 chars so the URL stays readable.
        out.chars().take(50).collect::<String>().trim_end_matches('-').to_string()
    }
}

/// Append a numeric suffix until the result passes `is_taken(&candidate) == false`.
/// `is_taken` is also called for the bare base; if the bare base is free, it
/// is returned as-is.
pub fn unique_slug<F: FnMut(&str) -> bool>(base: &str, mut is_taken: F) -> String {
    let cleaned = if base.is_empty() { "project".to_string() } else { base.to_string() };
    if !is_taken(&cleaned) {
        return cleaned;
    }
    let mut n: u32 = 2;
    loop {
        let candidate = format!("{}-{}", cleaned, n);
        if !is_taken(&candidate) {
            return candidate;
        }
        n += 1;
        // Defensive cap. With reasonable use this is never reached.
        if n > 9999 {
            return format!("{}-{}", cleaned, &uuid::Uuid::new_v4().to_string()[..6]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugify_basic() {
        assert_eq!(slugify("Acme Corp"), "acme-corp");
        assert_eq!(slugify("My_Project!"), "my-project");
        assert_eq!(slugify("  weird   spaces  "), "weird-spaces");
        assert_eq!(slugify("---"), "project");
        assert_eq!(slugify(""), "project");
    }

    #[test]
    fn slugify_unicode_dropped() {
        assert_eq!(slugify("Café"), "caf");
        assert_eq!(slugify("文字"), "project");
    }

    #[test]
    fn unique_slug_resolves_collision() {
        let taken = ["acme".to_string(), "acme-2".to_string()];
        let got = unique_slug("acme", |s| taken.iter().any(|t| t == s));
        assert_eq!(got, "acme-3");
    }

    #[test]
    fn unique_slug_passes_through_when_free() {
        let got = unique_slug("acme", |_| false);
        assert_eq!(got, "acme");
    }
}
