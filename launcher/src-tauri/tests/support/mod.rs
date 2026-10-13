// SPDX-License-Identifier: AGPL-3.0-or-later
// Part of VibeCoded Orchestrator.
//! Shared helpers for the source-scanning integration tests in `tests/`.
//!
//! ONE home for "which lines of this Rust file are production code?" on the
//! Rust side (v0.2.102 W6-Z2). The Python lints answer the same question with
//! `tests/common/rust_source.py` (`cfg_test_spans` / `production_source`);
//! these two are the only implementations, and both blank test-gated ITEMS
//! instead of cutting the file at the first `#[cfg(test)]`. Cutting at the
//! first marker is the defect this module exists to retire: a gated `use`, a
//! gated helper, a test module followed by more production code, or a comment
//! that quotes the marker all end the scan early, and the gate then fails
//! toward GREEN.
//!
//! Users: `command_silent_gate.rs`, `spawn_evidence_lint.rs` (each declares
//! `mod support;`). Files under a `tests/` SUBdirectory are not compiled as
//! test crates of their own, so this module is only ever built as a child of
//! those.

/// Strip `//` line comments and `/* */` block comments and the contents
/// of string/char literals from a single source file, producing a parallel
/// vector of "code-only" lines. We keep line count identical so reported
/// line numbers match the original file. Anything removed is replaced by a
/// space so column-insensitive substring checks (`Command::new`,
/// `.silent()`) only ever match real code.
///
/// This is a small hand-rolled lexer — good enough for Rust source where
/// the only constructs that could hide a spurious `Command::new` are
/// comments and string literals. It is deliberately conservative: raw
/// strings (`r#"..."#`) are handled; nested block comments are handled.
pub fn strip_comments_and_strings(src: &str) -> Vec<String> {
    let chars: Vec<char> = src.chars().collect();
    let mut out_line = String::new();
    let mut lines: Vec<String> = Vec::new();

    let mut i = 0usize;
    let n = chars.len();

    // Lexer state.
    let mut block_depth: u32 = 0; // /* */ nesting
    let mut in_line_comment = false;
    let mut in_string = false;
    let mut in_char = false;
    // Raw string: Some(hashes) while inside r#"..."# with `hashes` #'s.
    let mut raw_string_hashes: Option<usize> = None;

    let push_char = |out_line: &mut String, lines: &mut Vec<String>, c: char| {
        if c == '\n' {
            lines.push(std::mem::take(out_line));
        } else {
            out_line.push(c);
        }
    };
    // When inside a removed region we still must emit the newlines so line
    // numbers stay aligned; non-newline chars become a single space.
    let push_blank = |out_line: &mut String, lines: &mut Vec<String>, c: char| {
        if c == '\n' {
            lines.push(std::mem::take(out_line));
        } else {
            out_line.push(' ');
        }
    };

    while i < n {
        let c = chars[i];

        if in_line_comment {
            if c == '\n' {
                in_line_comment = false;
                push_char(&mut out_line, &mut lines, c);
            } else {
                push_blank(&mut out_line, &mut lines, c);
            }
            i += 1;
            continue;
        }

        if block_depth > 0 {
            if c == '/' && i + 1 < n && chars[i + 1] == '*' {
                block_depth += 1;
                push_blank(&mut out_line, &mut lines, ' ');
                push_blank(&mut out_line, &mut lines, ' ');
                i += 2;
                continue;
            }
            if c == '*' && i + 1 < n && chars[i + 1] == '/' {
                block_depth -= 1;
                push_blank(&mut out_line, &mut lines, ' ');
                push_blank(&mut out_line, &mut lines, ' ');
                i += 2;
                continue;
            }
            push_blank(&mut out_line, &mut lines, c);
            i += 1;
            continue;
        }

        if let Some(hashes) = raw_string_hashes {
            // Look for the terminating "###... sequence of the same length.
            if c == '"' {
                let mut matched = true;
                for k in 0..hashes {
                    if i + 1 + k >= n || chars[i + 1 + k] != '#' {
                        matched = false;
                        break;
                    }
                }
                if matched {
                    raw_string_hashes = None;
                    push_blank(&mut out_line, &mut lines, ' '); // the closing quote
                    for _ in 0..hashes {
                        push_blank(&mut out_line, &mut lines, ' ');
                    }
                    i += 1 + hashes;
                    continue;
                }
            }
            push_blank(&mut out_line, &mut lines, c);
            i += 1;
            continue;
        }

        if in_string {
            if c == '\\' && i + 1 < n {
                // Escaped char: blank both, keep newline handling correct.
                push_blank(&mut out_line, &mut lines, ' ');
                push_blank(&mut out_line, &mut lines, chars[i + 1]);
                i += 2;
                continue;
            }
            if c == '"' {
                in_string = false;
            }
            push_blank(&mut out_line, &mut lines, if c == '\n' { '\n' } else { c });
            // Note: a normal (non-raw) string literal cannot span a raw
            // newline without a `\` continuation in Rust, but tolerating
            // it here only over-blanks — never under-blanks — so it stays
            // conservative.
            i += 1;
            continue;
        }

        if in_char {
            if c == '\\' && i + 1 < n {
                push_blank(&mut out_line, &mut lines, ' ');
                push_blank(&mut out_line, &mut lines, chars[i + 1]);
                i += 2;
                continue;
            }
            if c == '\'' {
                in_char = false;
            }
            push_blank(&mut out_line, &mut lines, c);
            i += 1;
            continue;
        }

        // Not in any special region: detect region starts.
        if c == '/' && i + 1 < n && chars[i + 1] == '/' {
            in_line_comment = true;
            push_blank(&mut out_line, &mut lines, ' ');
            push_blank(&mut out_line, &mut lines, ' ');
            i += 2;
            continue;
        }
        if c == '/' && i + 1 < n && chars[i + 1] == '*' {
            block_depth = 1;
            push_blank(&mut out_line, &mut lines, ' ');
            push_blank(&mut out_line, &mut lines, ' ');
            i += 2;
            continue;
        }
        // Raw string start: r"..." or r#"..."# (and br"...", etc.).
        if c == 'r' || c == 'b' {
            // Detect r, br, r#, br# … followed by the opening quote.
            let mut j = i;
            if chars[j] == 'b' {
                j += 1;
            }
            if j < n && chars[j] == 'r' {
                let mut k = j + 1;
                let mut hashes = 0usize;
                while k < n && chars[k] == '#' {
                    hashes += 1;
                    k += 1;
                }
                if k < n && chars[k] == '"' {
                    // Confirmed raw string opener.
                    raw_string_hashes = Some(hashes);
                    for _ in i..=k {
                        push_blank(&mut out_line, &mut lines, ' ');
                    }
                    i = k + 1;
                    continue;
                }
            }
        }
        if c == '"' {
            in_string = true;
            push_blank(&mut out_line, &mut lines, ' ');
            i += 1;
            continue;
        }
        if c == '\'' {
            // Could be a char literal or a lifetime (`'a`). Lifetimes are
            // identifiers, not quoted — only enter char mode if this looks
            // like a quoted literal (next-next char is a closing quote, or
            // an escape). Over-conservatism here would blank a lifetime,
            // which is harmless for our `Command::new` detection.
            let is_charlit = (i + 2 < n && chars[i + 2] == '\'')
                || (i + 1 < n && chars[i + 1] == '\\');
            if is_charlit {
                in_char = true;
                push_blank(&mut out_line, &mut lines, ' ');
                i += 1;
                continue;
            }
        }

        push_char(&mut out_line, &mut lines, c);
        i += 1;
    }
    // Flush the final line (files may not end in newline).
    lines.push(out_line);
    lines
}

/// Whether an attribute line gates its item to test builds: `#[cfg(test)]`,
/// `#[cfg(any(test, debug_assertions))]`, or `#[cfg(all(..., test, ...))]`
/// where `test` is one of `all`'s top-level arguments (so the item exists
/// only when compiling tests). `#[cfg(any(test, feature = "x"))]` and
/// `not(test)` are NOT test-only and do not match.
pub fn is_test_cfg_attr(s: &str) -> bool {
    let t = s.replace(char::is_whitespace, "");
    if t.contains("#[cfg(test)]") {
        return true;
    }
    if t.contains("#[cfg(") && t.contains("test") && t.contains("debug_assertions") {
        return true;
    }
    if let Some(start) = t.find("#[cfg(all(") {
        let inner = &t[start + "#[cfg(all(".len()..];
        let mut depth = 0i32;
        let mut arg = String::new();
        for c in inner.chars() {
            match c {
                '(' => {
                    depth += 1;
                    arg.push(c);
                }
                ')' if depth == 0 => return arg == "test",
                ')' => {
                    depth -= 1;
                    arg.push(c);
                }
                ',' if depth == 0 => {
                    if arg == "test" {
                        return true;
                    }
                    arg.clear();
                }
                _ => arg.push(c),
            }
        }
    }
    false
}

/// Compute, for each line index, whether it sits inside a test-gated item
/// (an attribute [`is_test_cfg_attr`] accepts).
///
/// The item is delimited the way the compiler delimits it: from the
/// attribute, the FIRST of
///   * a `;` before any `{` — a one-line item (`#[cfg(test)] use x;`,
///     `#[cfg(test)] mod tests;`, `#[cfg(test)] const X: u8 = 1;`), or
///   * the `}` that balances the item's first `{`.
///
/// Operates on [`strip_comments_and_strings`] output, so a brace or a `;`
/// inside a string or comment is not counted, and a comment that merely
/// QUOTES `#[cfg(test)]` is not an attribute.
///
/// v0.2.102 (W6-Z2): the previous version counted braces only. A gated
/// `use …;` therefore ran on to the NEXT production item's braces and marked
/// that whole item as test code — about 210 production lines across the
/// three crates (e.g. `installer.rs`'s gated `use` hid the item after it),
/// so a `Command::new` there would never have been reported. An item that
/// never terminates before EOF now gates NOTHING (fail closed: the gate can
/// only get noisier, never blind), where it used to gate the rest of the
/// file.
pub fn test_gated_lines(stripped: &[String]) -> Vec<bool> {
    let n = stripped.len();
    let mut gated = vec![false; n];

    let mut i = 0usize;
    while i < n {
        if !is_test_cfg_attr(&stripped[i]) {
            i += 1;
            continue;
        }
        match gated_item_end(stripped, i) {
            Some(end) => {
                for g in gated.iter_mut().take(end + 1).skip(i) {
                    *g = true;
                }
                i = end + 1;
            }
            None => i += 1,
        }
    }
    gated
}

/// 0-based last line of the item whose test-gate attribute is on
/// `stripped[attr]`, or `None` when it does not terminate before EOF.
/// The attribute text itself carries no `{`, `}` or `;`, so scanning from the
/// attribute's own line also handles an item written on the same line.
fn gated_item_end(stripped: &[String], attr: usize) -> Option<usize> {
    let mut depth: i64 = 0;
    let mut started = false;
    for (k, line) in stripped.iter().enumerate().skip(attr) {
        for c in line.chars() {
            match c {
                ';' if !started => return Some(k),
                '{' => {
                    depth += 1;
                    started = true;
                }
                '}' if started => {
                    depth -= 1;
                    if depth <= 0 {
                        return Some(k);
                    }
                }
                _ => {}
            }
        }
    }
    None
}
