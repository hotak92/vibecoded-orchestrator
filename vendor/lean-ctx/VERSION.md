# vendor/lean-ctx — provenance

Bundled `lean-ctx` is the **upstream-released binary**, downloaded
verbatim from the official lean-ctx GitHub Releases page. It is
NOT a self-built artifact — that distinction matters because a
self-built copy of a third-party tool embeds the build host's
username and on-disk layout in panic-message metadata (see audit
2026-05-06 §5).

## Currently vendored

| Platform | Path | Upstream version | Source URL |
|---|---|---|---|
| Windows x86_64 (MSVC) | `windows-x64/lean-ctx.exe` | v3.5.0 | https://github.com/yvgude/lean-ctx/releases/download/v3.5.0/lean-ctx-x86_64-pc-windows-msvc.zip |

Linux and macOS are NOT vendored — those platforms install
`lean-ctx` via `cargo install lean-ctx` or the upstream prebuilt
tarballs. Windows-x64 is vendored because Windows users typically
don't have a Rust toolchain handy; bundling the upstream-signed
zip-extracted exe gives them a working `lean-ctx` without a
multi-GB dependency install.

## Provenance verification

The `windows-x64/lean-ctx.exe` was extracted from the upstream zip
asset. Verify by:

```bash
# 1. Download upstream zip + checksums
gh release download v3.5.0 \
  --repo yvgude/lean-ctx \
  --pattern lean-ctx-x86_64-pc-windows-msvc.zip \
  --pattern SHA256SUMS

# 2. Verify the zip checksum (expected: 04a480a9a4d2e88fccabebe605569ad0f3c273a6cc191406a1c9aed68f4f12ee)
sha256sum lean-ctx-x86_64-pc-windows-msvc.zip
grep windows-msvc SHA256SUMS

# 3. Extract and compare against vendored copy
unzip lean-ctx-x86_64-pc-windows-msvc.zip
cmp lean-ctx.exe path/to/repo/vendor/lean-ctx/windows-x64/lean-ctx.exe
# (Should be byte-identical.)
```

## SHA256

```
04a480a9a4d2e88fccabebe605569ad0f3c273a6cc191406a1c9aed68f4f12ee  upstream zip (lean-ctx-x86_64-pc-windows-msvc.zip)
```

The vendored `lean-ctx.exe` is the sole file inside that zip, and
its SHA256 matches what `unzip` produces. We do NOT pin the
extracted-exe SHA separately because zip extraction is
deterministic (zip stores file content uncompressed-or-deflated,
not as a stream that could vary across decompressors).

## Why we re-vendored on 2026-05-06 (PR-4)

The previous `vendor/lean-ctx/windows-x64/lean-ctx.exe` was a
**self-built copy of lean-ctx 3.4.3** compiled on a developer
machine. `strings vendor/lean-ctx/windows-x64/lean-ctx.exe` showed
~1500 references to `C:\Users\marti\.cargo\registry\src\...` —
the build host's username embedded in panic-message rodata. Even
though `lean-ctx` itself is harmless to redistribute, shipping a
locally-rebuilt copy of any third-party binary is two strikes:
- **privacy:** leaks the build host's username
- **supply-chain trust:** end users can't verify the binary came
  from upstream because the bytes don't match upstream's release

PR-4 replaced the rebuild with the verbatim upstream zip artifact.
Future updates: download the new release zip from
https://github.com/yvgude/lean-ctx/releases, verify its SHA256
against the upstream `SHA256SUMS`, extract `lean-ctx.exe`,
overwrite `windows-x64/lean-ctx.exe`, and update the version table
above.

## Update procedure

```bash
NEW_VERSION=v3.5.1   # whatever upstream tag
cd /tmp
gh release download "$NEW_VERSION" \
  --repo yvgude/lean-ctx \
  --pattern lean-ctx-x86_64-pc-windows-msvc.zip \
  --pattern SHA256SUMS

# Verify checksum BEFORE trusting
expected=$(grep windows-msvc SHA256SUMS | awk '{print $1}')
actual=$(sha256sum lean-ctx-x86_64-pc-windows-msvc.zip | awk '{print $1}')
[ "$expected" = "$actual" ] || { echo "checksum mismatch — abort"; exit 1; }

# Extract and copy
unzip -o lean-ctx-x86_64-pc-windows-msvc.zip
cp lean-ctx.exe <repo>/vendor/lean-ctx/windows-x64/lean-ctx.exe

# Update VERSION.md (this file) — bump the version + SHA256 row.
```
