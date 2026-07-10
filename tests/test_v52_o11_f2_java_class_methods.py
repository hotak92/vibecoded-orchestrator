# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.F.2-JAVA — Java class.methods correct attribution.

Pre-V52-O.11.F.2 (audit a79152, 2026-06-09): the Java analyzer at
``analyze_code_graph.py:3675`` did:

    methods = [m.group(1) for m in method_pattern.finditer(content_clean)]

This iterated ``method_pattern`` over the WHOLE file's ``content_clean``
inside the per-class loop, attributing EVERY method in the file to EVERY
class. Same antipattern as the Rust V52-O.11.F bug — the audit flagged
it in Rust, Go, JS/TS, Java, and C# parsers. V52-O.11.F shipped the fix
for Rust; this task (V52-O.11.F.2-JAVA) ships the Java fix.

Audit a79152 also noted a SECOND bug in the Java method regex at
line 3645: the ``(?:public|private|...|\\s)+`` modifier section has a
``+`` quantifier, REQUIRING at least one modifier keyword. Package-
private methods (no visibility modifier, common in Java for
package-scope helpers) are silently dropped. The new
``_java_methods_for_class`` helper's inner pattern uses ``*`` instead,
capturing package-private methods.

Both bugs are fixed in a single helper:
``_java_methods_for_class(content_clean, class_name, source_lines)``.
"""

from __future__ import annotations

from pathlib import Path

# P2f stage 2 (v0.2.76): `_java_methods_for_class` (+ its
# `_strip_nested_java_classes` / `_find_matching_brace` helpers) moved
# verbatim to vco_lib/codegraph_lang/java.py, which is import-safe (no
# weaviate-client / sys.path side effects) — the old isolated-importlib
# loader for the full analyzer script is no longer needed. Alias the module
# as ``acg`` so every assertion below stays byte-identical.
from vco_lib.codegraph_lang import java as acg


# ---------------------------------------------------------------------------
# Test 1 — public method (basic happy path)
# ---------------------------------------------------------------------------


def test_public_method_captured() -> None:
    """public void m() — visibility-modifier method captured."""
    content = """
public class Foo {
    public void publicMethod() {}
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    assert methods == ["publicMethod"]


# ---------------------------------------------------------------------------
# Test 2 — private + protected methods captured
# ---------------------------------------------------------------------------


def test_private_and_protected_methods_captured() -> None:
    """private + protected visibility methods captured."""
    content = """
public class Foo {
    private int privateMethod() { return 0; }
    protected String protectedMethod() { return null; }
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    assert "privateMethod" in methods
    assert "protectedMethod" in methods


# ---------------------------------------------------------------------------
# Test 3 — package-private method (no modifier) is captured
# ---------------------------------------------------------------------------


def test_package_private_method_captured() -> None:
    """Package-private methods (no visibility modifier) are captured.

    This is the second bug fixed by V52-O.11.F.2-JAVA — audit a79152
    flagged that the pre-fix method regex at line 3645 used
    ``(?:public|private|...|\\s)+`` (with ``+`` quantifier), which
    REQUIRED at least one modifier and silently dropped no-modifier
    methods. Common in Java for package-scope helpers.
    """
    content = """
public class Foo {
    void packagePrivateMethod() {}
    int anotherPackagePrivate() { return 0; }
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    assert "packagePrivateMethod" in methods, (
        "Package-private method (no modifier) must be captured. "
        "Bug #2 in audit a79152: pre-fix regex required a leading "
        "modifier keyword."
    )
    assert "anotherPackagePrivate" in methods


# ---------------------------------------------------------------------------
# Test 4 — static + abstract + final + synchronized modifiers
# ---------------------------------------------------------------------------


def test_static_abstract_final_synchronized_captured() -> None:
    """Non-visibility modifiers also work."""
    content = """
public abstract class Foo {
    static void staticMethod() {}
    final void finalMethod() {}
    synchronized void synchronizedMethod() {}
    public static final synchronized void allOfThem() {}
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    expected = {
        "staticMethod", "finalMethod", "synchronizedMethod", "allOfThem",
    }
    assert expected.issubset(set(methods)), (
        f"Expected subset {expected}; got {set(methods)}."
    )


def test_abstract_method_with_body_captured() -> None:
    """abstract methods that have a body (uncommon but legal in some
    constructs — though typically ``abstract`` means no body) are
    captured. We test the modifier accepted in the regex, not whether
    the class is actually abstract."""
    # The pre-fix regex required `{` at the end (the helper does too).
    # A truly-abstract method ends in `;`, not `{`. So we test only
    # abstract-modifier methods WITH bodies (rare in real Java but
    # syntactically legal — keep the regex consistent with pre-fix).
    content = """
public class Foo {
    abstract void abstractWithBody() {}
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    assert "abstractWithBody" in methods


# ---------------------------------------------------------------------------
# Test 5 — generics on the METHOD
# ---------------------------------------------------------------------------


def test_method_with_generics_captured() -> None:
    """``public <T> T process()`` — method-level generics."""
    content = """
public class Foo {
    public <T> T process(T input) { return input; }
    public <K, V> java.util.Map<K, V> empty() { return null; }
    public <T extends Comparable<T>> T max(T a, T b) { return null; }
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    expected = {"process", "empty", "max"}
    assert expected.issubset(set(methods)), (
        f"Expected method-generic methods captured; got {set(methods)}."
    )


# ---------------------------------------------------------------------------
# Test 6 — `throws` clauses
# ---------------------------------------------------------------------------


def test_method_with_throws_clause_captured() -> None:
    """``void m() throws IOException, SQLException { ... }`` captured."""
    content = """
public class Foo {
    public void readFile() throws java.io.IOException {}
    public void multiThrow() throws java.io.IOException, java.sql.SQLException {}
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    assert "readFile" in methods
    assert "multiThrow" in methods


# ---------------------------------------------------------------------------
# Test 7 — nested classes: methods of inner class DO NOT leak into outer
# ---------------------------------------------------------------------------


def test_nested_class_methods_do_not_leak_to_outer() -> None:
    """Java allows ``class Outer { class Inner { method() {} } }``.
    Inner's methods must NOT appear in Outer's method list — only Outer's
    own methods do."""
    content = """
public class Outer {
    public void outerMethod() {}

    public static class Inner {
        public void innerMethod() {}
        public void anotherInnerMethod() {}
    }

    public void anotherOuterMethod() {}
}
"""
    methods_outer = acg._java_methods_for_class(
        content, "Outer", content.split("\n")
    )
    methods_inner = acg._java_methods_for_class(
        content, "Inner", content.split("\n")
    )
    # Outer methods MUST NOT include inner methods.
    assert "outerMethod" in methods_outer
    assert "anotherOuterMethod" in methods_outer
    assert "innerMethod" not in methods_outer, (
        "Inner class method leaked into outer class — nested-class "
        "stripping failed."
    )
    assert "anotherInnerMethod" not in methods_outer
    # Inner methods are captured when querying the inner class.
    assert "innerMethod" in methods_inner
    assert "anotherInnerMethod" in methods_inner
    # And outer methods don't bleed into Inner.
    assert "outerMethod" not in methods_inner


# ---------------------------------------------------------------------------
# Test 8 — extends + implements clauses
# ---------------------------------------------------------------------------


def test_class_with_extends_and_implements() -> None:
    """``class X extends Y implements Z1, Z2<T>`` — both clauses handled
    by the class-decl regex; methods captured."""
    content = """
public class MyClass extends Parent<String>
        implements Iface1, java.util.Comparator<Integer> {
    public void m1() {}
    public int m2() { return 0; }
    @Override
    public int compare(Integer a, Integer b) { return 0; }
}
"""
    methods = acg._java_methods_for_class(
        content, "MyClass", content.split("\n")
    )
    expected = {"m1", "m2", "compare"}
    assert expected.issubset(set(methods)), (
        f"Expected {expected}; got {set(methods)}. "
        f"extends/implements parsing may have broken the class-decl regex."
    )


# ---------------------------------------------------------------------------
# Test 9 — generics on the CLASS
# ---------------------------------------------------------------------------


def test_class_with_generics() -> None:
    """``class Service<T extends Comparable>`` captured."""
    content = """
public class Service<T extends Comparable<T>> {
    public void process(T input) {}
    public T get() { return null; }
}
"""
    methods = acg._java_methods_for_class(
        content, "Service", content.split("\n")
    )
    assert "process" in methods
    assert "get" in methods


# ---------------------------------------------------------------------------
# Test 10 — methods on DIFFERENT classes are NOT mixed (regression)
# ---------------------------------------------------------------------------


def test_different_classes_have_distinct_methods() -> None:
    """The audit reproduction case: file with 3 classes + free
    functions/comments, each class gets only its own methods."""
    content = """
public class A {
    public void aMethod1() {}
    public void aMethod2() {}
}

public class B {
    public void bMethod1() {}
}

public class C {
    public void cMethod1() {}
    public void cMethod2() {}
    public void cMethod3() {}
}
"""
    methods_a = acg._java_methods_for_class(content, "A", content.split("\n"))
    methods_b = acg._java_methods_for_class(content, "B", content.split("\n"))
    methods_c = acg._java_methods_for_class(content, "C", content.split("\n"))

    assert sorted(methods_a) == ["aMethod1", "aMethod2"]
    assert sorted(methods_b) == ["bMethod1"]
    assert sorted(methods_c) == ["cMethod1", "cMethod2", "cMethod3"]

    # Cross-class contamination check (the V52-O.11.F.2-JAVA regression).
    for m in ["aMethod1", "aMethod2"]:
        assert m not in methods_b, (
            f"V52-O.11.F.2-JAVA regression: {m!r} (class A) leaked into "
            f"class B's methods. Pre-fix behavior was every method "
            f"attributed to every class."
        )
        assert m not in methods_c
    for m in ["bMethod1"]:
        assert m not in methods_a
        assert m not in methods_c
    for m in ["cMethod1", "cMethod2", "cMethod3"]:
        assert m not in methods_a
        assert m not in methods_b


# ---------------------------------------------------------------------------
# Test 11 — class with no methods returns empty list
# ---------------------------------------------------------------------------


def test_data_class_returns_empty_methods() -> None:
    """A class with only fields (no methods) → methods = []."""
    content = """
public class DataOnly {
    public int field1;
    public String field2;
    private final long field3 = 42L;
}
"""
    methods = acg._java_methods_for_class(
        content, "DataOnly", content.split("\n")
    )
    assert methods == [], (
        f"Pure data class should have no methods; got {methods}."
    )


# ---------------------------------------------------------------------------
# Test 12 — non-existent class returns empty list
# ---------------------------------------------------------------------------


def test_nonexistent_class_returns_empty() -> None:
    """Querying a class that doesn't exist returns []."""
    content = """
public class Foo {
    public void m1() {}
}
"""
    methods = acg._java_methods_for_class(
        content, "DoesNotExist", content.split("\n")
    )
    assert methods == []


# ---------------------------------------------------------------------------
# Test 13 — interface methods with bodies (default methods) captured
# ---------------------------------------------------------------------------


def test_interface_default_methods_captured() -> None:
    """``interface X { default void m() {} }`` — default methods (Java
    8+) have bodies and should be captured. Abstract methods without
    bodies (``void m();``) are NOT captured by design (the pattern
    requires ``{`` at the end)."""
    content = """
public interface MyService {
    default void logHelper() {}
    default String formatHelper() { return ""; }
    void mustImplement();
    static void utilityMethod() {}
}
"""
    methods = acg._java_methods_for_class(
        content, "MyService", content.split("\n")
    )
    assert "logHelper" in methods
    assert "formatHelper" in methods
    assert "utilityMethod" in methods
    # Abstract (no body) is intentionally excluded.
    assert "mustImplement" not in methods


# ---------------------------------------------------------------------------
# Test 14 — enum with methods
# ---------------------------------------------------------------------------


def test_enum_methods_captured() -> None:
    """Java enums are classes — their methods should be captured."""
    content = """
public enum Status {
    ACTIVE, INACTIVE, PENDING;

    public boolean isActive() {
        return this == ACTIVE;
    }

    String describe() { return name(); }
}
"""
    methods = acg._java_methods_for_class(
        content, "Status", content.split("\n")
    )
    assert "isActive" in methods
    assert "describe" in methods


# ---------------------------------------------------------------------------
# Test 15 — control-flow keywords are NOT captured as method names
# ---------------------------------------------------------------------------


def test_control_flow_keywords_excluded() -> None:
    """``if (x) {`` and similar should not be picked up as methods.

    The pre-fix code at line 3694 (functions loop) explicitly filters
    these. The helper duplicates that filter so the class.methods list
    is clean."""
    content = """
public class Foo {
    public void realMethod() {
        if (x > 0) {
            // body
        }
        while (y < 10) {
            y++;
        }
        for (int i = 0; i < 10; i++) {
            // body
        }
        try {
            switch (z) {
                case 1:
                    break;
            }
        } catch (Exception e) {
            // body
        }
    }
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    # `realMethod` is the only method.
    assert methods == ["realMethod"], (
        f"Expected only realMethod; got {methods}. Control-flow keyword "
        f"leaked into method list."
    )


# ---------------------------------------------------------------------------
# Test 16 — V52-O.11.F.2-JAVA regression: helper used in Java loop
# ---------------------------------------------------------------------------


def test_no_unconditional_method_finditer_in_java_class_loop() -> None:
    """Regression test: prevents a future PR from re-introducing the
    pre-V52-O.11.F.2 line that iterated ``method_pattern`` over the
    WHOLE file inside the Java per-class loop.

    Same shape as the V52-O.11.F Rust regression test, scoped to the
    Java analyzer entry.

    P2f stage 2 (v0.2.76): the Java extractor moved verbatim to
    vco_lib/codegraph_lang/java.py — the source scan follows it there
    (function renamed `_analyze_java_file` -> `analyze_java_file` in the
    move; assertions unchanged).

    P2f stage 3 (v0.2.77 Part 6): the per-class loop moved from the
    imperative ``analyze_java_file`` (now a thin shim) into the pure producer
    ``extract_java_file``. The scan follows it there; the
    ``_java_methods_for_class`` guard is unchanged.
    """
    src = Path(acg.__file__).read_text()

    # Locate the Java-specific signature line (unique to Java —
    # `signature = f"class {cname}"`).
    java_signature_anchor = 'signature = f"class {cname}"'
    anchor_pos = src.find(java_signature_anchor)
    assert anchor_pos >= 0, (
        f"Could not locate Java signature anchor in analyze_code_graph.py — "
        f"has it changed? Looked for: {java_signature_anchor!r}"
    )

    # The Java parser's class loop also uses `signature = f"class {cname}"`,
    # but Kotlin (line ~2540 in the JS/TS area) and Swift (~3340) may use
    # similar strings. We need to be MORE specific. The Java loop is the
    # one that lives inside the pure producer ``extract_java_file`` (P2f-3).
    # Search for the wider context that includes the function header.
    java_func_anchor_pos = src.find("def extract_java_file(")
    assert java_func_anchor_pos >= 0, (
        "Could not find `extract_java_file` function — file shape changed."
    )
    # Find the next top-level `def ` after `extract_java_file` (the end of
    # this function's body; it is a module-level function since the P2f move).
    java_func_end_pos = src.find("\ndef ", java_func_anchor_pos + 1)
    if java_func_end_pos < 0:
        java_func_end_pos = len(src)
    java_func_body = src[java_func_anchor_pos:java_func_end_pos]

    # Sanity: the body must contain the per-class loop header.
    assert "for cname, start_line in class_info.items():" in java_func_body, (
        "Java per-class loop header not found in `_analyze_java_file` body — "
        "file shape has changed unexpectedly."
    )

    # The pre-fix broken line was:
    #     methods = [m.group(1) for m in method_pattern.finditer(content_clean)]
    # The new code uses `_java_methods_for_class(content_clean, cname, source_lines)`.
    assert "method_pattern.finditer(content_clean)]" not in java_func_body, (
        "V52-O.11.F.2-JAVA regression: the Java class loop body contains "
        "``method_pattern.finditer(content_clean)`` which would attribute "
        "EVERY method in the file to every class. Use "
        "``_java_methods_for_class(content_clean, cname, source_lines)`` "
        "instead."
    )
    assert "_java_methods_for_class" in java_func_body, (
        "V52-O.11.F.2-JAVA regression: the Java class loop must call "
        "``_java_methods_for_class`` for per-class method scoping."
    )


# ---------------------------------------------------------------------------
# Test 17 — annotations on methods don't break capture
# ---------------------------------------------------------------------------


def test_annotated_methods_captured() -> None:
    """``@Override`` / ``@Deprecated`` / ``@SuppressWarnings(...)`` on
    methods don't break the regex — annotations are on their own lines
    in typical Java style, so the method-shape regex starts fresh.
    """
    content = """
public class Foo {
    @Override
    public boolean equals(Object o) { return false; }

    @Deprecated
    @SuppressWarnings("unchecked")
    public void deprecated() {}
}
"""
    methods = acg._java_methods_for_class(
        content, "Foo", content.split("\n")
    )
    assert "equals" in methods
    assert "deprecated" in methods
