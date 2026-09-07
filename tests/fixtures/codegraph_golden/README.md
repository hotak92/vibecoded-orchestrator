<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Code-graph golden fixture corpus

A small, committed, multi-language fixture repo used by
`tests/test_codegraph_golden.py` to PIN the analyzer's current behaviour
(walk order, dedup keys, property stamping, chunk boundaries, `content_hash`)
before and across the P2f `CodeEntity` IR refactor.

## Layout

`repo/` is the analyzed tree. All names are neutral (no real project names).

| File | Exercises |
|---|---|
| `repo/src/widgets.py` | Python: module docstring, imports, classes + inheritance, nested function, decorators, type annotations, internal calls (n_callers source) |
| `repo/tests/test_widgets.py` | Python: the `is_test` path-heuristic axis (lives under a `tests/` part) |
| `repo/src/big_module.py` | Python: one function whose body exceeds the codesage per-entity budget (derived from `chunking.MODEL_TOKEN_LIMITS`; 1024 tokens ≈ 3584 chars since the v0.2.92 W1 served-window correction) → CHUNKING (multiple chunk rows) |
| `repo/src/engine.rs` | Rust (regex): struct/enum/trait, `impl` blocks, `async fn` |
| `repo/src/client.js` | JavaScript (regex): class, function, arrow fn, imports |
| `repo/src/models.ts` | TypeScript (regex): interface, class, function, arrow fn |
| `repo/src/service.go` | Go (regex): struct, methods, functions |
| `repo/src/Account.java` | Java (regex): class + methods; and (v0.2.92 WP-5b) an `interface` with a BODILESS method, an `abstract` method, and a `new X();` statement that only the modifier/return-type run guard distinguishes from a declaration |
| `repo/src/deploy.ps1` | PowerShell: function + filter, plus a nested function at 8-space indent (the v0.2.75 deep-indent regression case) |
| `repo/src/routes.js` | Fastify-style routes → CodeAPI rows |
| `repo/src/geometry.cpp` | C++ (regex): namespace, class + struct + template, out-of-line `Class::method` defs; and (v0.2.92 WP-5c) FREE FUNCTIONS at file and namespace scope, a `template<…>` free function, a header-only class whose members are DEFINED in its body, plus a lambda / brace-init / `if` / range-`for` that must mint no row |
| `repo/src/Inventory.cs` | C# (regex): namespace, interface + class + POSITIONAL record (`record Item(int Id, string Name);` — no body, so no `{` for the class pattern to anchor on), generic method, property, `[Route]`+`[Http*]` → CodeAPI |
| `repo/src/ledger.rb` | Ruby (regex): module + class + class-reopening + subclass, `def self.` methods; and (v0.2.92 WP-5b) a statement-MODIFIER `if`/`unless` (no `end`), a Ruby-3.0 endless method, a `do…end` block, an INDENTED class inside a module, and a top-level `def` after every class has closed |
| `repo/src/vector.lua` | Lua (regex): table-OOP class (`Name = {}` + `__index`), colon/dot/assigned methods, standalone fn with nested `end`s |
| `repo/src/backup.sh` | Shell (regex): BOTH `name()` and `function name` syntaxes |
| `repo/src/catalog.proto` | Proto (regex): messages → CodeClass, service rpcs → CodeAPI |
| `repo/src/Counter.svelte` | Svelte (regex): default + module-context `<script>`, function/export/arrow-export/reactive decls |
| `repo/src/vendor.js` | Minified content (single huge line) → MUST BE SKIPPED (CG-5) |
| `repo/node_modules/ignored.js` | Lives in an ignored dir → MUST NOT appear in any snapshot |

## Snapshots

`expected/*.json` is one normalized snapshot per code collection
(`CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction`).
Each is the analyzer's stored output, normalized (sorted by
`(path, full_name)`, volatile fields stripped, `content_hash` KEPT). They are
the CONTRACT: the P2f IR refactor must leave them byte-identical.

## Regenerating (requires human review of the diff)

Snapshots are regenerated only deliberately:

```
CODEGRAPH_GOLDEN_REGEN=1 python -m pytest tests/test_codegraph_golden.py -q
```

A snapshot diff after a refactor is a SEMANTIC REGRESSION unless a human has
reviewed and accepted it. Do NOT regen to make a red test pass.

## What a snapshot pins — and what it does not

Read this before treating a committed value as correct. **A golden snapshot
records what the analyzer DOES, which is not always what it SHOULD do**, and
this corpus has twice been found ratifying a real defect as expected
behaviour:

* v0.2.92 (Defect B): `engine.rs` declares `fn reset` twice and the snapshot
  stored ONE row — the corpus encoded a lost entity, and the dedup test's
  assertion was written so that FIXING the loss would have failed it;
* v0.2.92 (WP-5): every C# row carried a skewed `start_line`; a class stored
  `end_line: 45` for a 44-line file; `IRepository`'s stored body was the whole
  namespace; Java's `deposit` and `getBalance` each started on the previous
  method's closing `}` with a body running to end-of-file; C++'s `Circle` took
  the entire `namespace shapes` block; both PowerShell functions started on a
  blank line; the Rust trait's bodiless `fn reset(&mut self);` borrowed the
  `impl`'s braces; and two rows were minted from a `record` declaration and a
  `return new` statement while the file's one generic method had no row at all.
  None of it was caught by a snapshot comparison, because the snapshot WAS the
  defect.

So when a diff appears, the question is not "does this match the committed
file" but **"which of the two is right"**. Answer it against the fixture
source: open `repo/src/<file>` and check that `source_lines[start_line - 1]`
really is the entity's declaration, that `end_line` is inside the file, and
that the stored body is the entity and nothing else.
You do not have to do that by hand:
`tests/test_v0292_golden_corpus_audit.py` runs exactly that check over EVERY
stored row against `repo/src/**`, and is the tool that found the nine defects
listed above (a scratch script at the time, shipped at v0.2.92 WP-5c so the
next regeneration cannot quietly turn a new loss into expected). Its three
known-deliberate divergences are an explicit allow-list with a reason each —
never a lowered threshold, because a bare count would let a new loss take a
retired one's place. `tests/test_v0292_wp5_entity_line_fidelity.py` asserts
the same property per language over INLINE sources, so it holds even if this
corpus is deleted.

**The corpus cannot cover a defect its fixtures do not exhibit.** Measured at
v0.2.92: NO fixture file contains a `/* … */` or `=begin` block comment, so
the multi-line-comment line desync that affected eight extractors was
invisible here and is pinned by
`tests/test_v0292_wp5_block_comment_scrub.py` instead. Before concluding "the
golden covers it", check that a fixture actually has the shape.

v0.2.92 WP-5b acted on that rule rather than restating it: the Ruby
modifier-form `if`, the Ruby endless method, the indented class, the Java
interface / abstract method and the `new X();` statement were all ADDED to the
fixtures in the same change as the code that handles them, because a capability
whose shape no fixture carries is a capability nothing tests. The C# positional
record was already present (`Inventory.cs:15`) and had simply never produced a
row.

## Known uncaptured shapes (documented gaps, not silent losses)

Read this before adding a row that "should" be here and concluding the
extractor regressed. Each of these is a stated limitation of the regex
extractors, exercised by a fixture that deliberately contains the shape:

* **C++ out-of-line CONSTRUCTORS.** `geometry.cpp:35` is
  `shapes::Circle::Circle(double radius) : radius_(radius) {}` — it produces
  no `CodeFunction` row, because `method_pattern`'s `\)\s*(?:const)?…\{` tail
  is broken by the member-initialiser list. It is an out-of-line MEMBER
  rather than a free function, so v0.2.92 WP-5c's free-function capture
  deliberately does not reach it either: that pattern refuses every
  `::`-qualified name, which is the same guard that stops it re-capturing
  `Class::method` and double-counting every member in the corpus. Pinned by
  `test_v0292_wp5c_cpp_negative_space.py::test_an_out_of_line_constructor_with_a_member_init_list_has_no_row`,
  which is the ONE assertion to invert if that decision is ever reversed —
  the `count_` / `items_` absences asserted beside it stay correct either way.
* **C++ shapes that fail SAFE.** Each of the following produces NO row rather
  than a WRONG one, which is what makes them acceptable gaps rather than
  defects: the failure mode WP-5b declined the free-function capture over,
  and the one R25 made the acceptance bar, is a SPURIOUS row. A junk row is
  worse than the honest gap it replaces; a missing row is merely the gap
  continuing. Measured against the adversarial probe at v0.2.92 WP-5c:
  - a **default argument containing a call** (`int f(int x, int y = g()) {`) —
    the argument group is `[^)]*` and cannot cross the inner `)`. That bound
    is load-bearing: it is what stops a match sweeping across statements.
  - a **function-pointer parameter** (`void install(void (*cb)(int)) {`), for
    the same reason.
  - a **function-try-block** (`int f(int x) try { … } catch (…) { … }`) — the
    `try` sits where the pattern requires `{`.
  - a **one-line `extern "C" int f(int x) { … }`** — the `"C"` string is not a
    type-run token. The BLOCK form (`extern "C" {` on its own line with
    definitions inside) IS captured; `geometry.cpp` exercises neither, but
    `test_v0292_wp5c_cpp_free_functions.py` covers the block form.
  - **operator overloads** (`Point operator+(const Point&, const Point&) {`,
    `int operator()(int) {`) — no `\w+` sits immediately before the argument
    list, so no name can be captured. Capturing them would require deciding
    what to call them, which is the trap that stored C# conversion operators
    under the target type's name.
  - **destructors** (`~Widget() {}`) — `~` is not an identifier character.
  - a **template specialization's type** (`template <> class Box<int> {`) and
    a **nested-typedef return type** (`std::vector<int>::iterator f() {`).
* **Lua class bodies are SYNTHESISED**, not sliced from source: `vector.Vector`
  stores `Vector = {}` plus one generated `function Vector.<m>(...) end` line
  per method. `start_line == end_line` for it by construction, so a
  body-equals-its-slice audit will flag it and should not.
* **Rust methods live in `impl` blocks**, outside the `struct` body, so
  `engine.Counter`'s `methods` list names four functions that are legitimately
  not inside its stored `class_body`.
* **A reopened Ruby class / a C# partial class** carries the UNION of the
  method sets of all its declarations on EVERY one of its rows, mirroring
  `_csharp_methods_for_class`'s documented partial-class behaviour. So
  `ledger.Account`'s first row (14-26) lists `withdraw?`, which is declared in
  the reopening at 29-33.
