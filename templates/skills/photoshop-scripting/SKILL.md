---
name: photoshop-scripting
description: Writes Adobe Photoshop automation scripts in UXP (JavaScript) or legacy ExtendScript (JSX), and GIMP scripts in Python (Script-Fu fallback). Use when a repetitive Photoshop task needs to run on dozens of files, when a custom panel/plugin is wanted, or when an Action recorder won't capture the logic needed.
short_desc: Photoshop UXP/ExtendScript + GIMP automation
keywords: ["Photoshop automation", UXP, ExtendScript, "JSX panel", GIMP, "Script-Fu", "batch process images", "automate Photoshop", "write a Photoshop script"]
model: opus
effort: high
---

# Photoshop & GIMP Scripting

You write automation scripts on demand. The designer describes what they need; you produce a runnable script with installation instructions. Both Adobe Photoshop and GIMP support scripting — choose the right runtime for the task.

## When to invoke

- "Process this folder of 200 PSDs the same way"
- "Generate this layout but with different text / images per row of a CSV"
- "Export every artboard / every visible group as its own PNG"
- "Replace a smart object across multiple files"
- "Build a custom Photoshop panel for our team's workflow"
- "Automate a task that the Actions recorder can't capture conditionals on"

## Runtime selection

| Need | Use |
|---|---|
| **Modern Photoshop automation** (2022+) | UXP — JavaScript, modern async/await |
| **Legacy Photoshop scripts** (pre-2022) or **simple file-loop scripts** | ExtendScript (JSX) — older JS dialect, still supported |
| **Photoshop custom panel / plugin distributed to a team** | UXP plugin (`.ccx` package) |
| **GIMP automation** | Python-Fu (Python) — preferred. Script-Fu (Scheme) only for legacy. |
| **Cross-app, no Adobe Photoshop available** | Pillow / ImageMagick — see `batch-image-pipeline` skill |
| **Heavy raw editing across hundreds of files** | Lightroom Classic + scripting (via `lr-plugin-sdk`) or Capture One |

**UXP vs ExtendScript** (Adobe migration is in progress as of 2026):
- New panels MUST be UXP — ExtendScript-based panels (CEP) are being phased out.
- Standalone scripts (run via File → Scripts → Browse) — ExtendScript still works and is simpler to deliver as a one-off `.jsx` file.
- Async I/O, modern syntax, Node-style modules — only UXP.

Adobe's reference: https://developer.adobe.com/photoshop/uxp/

## ExtendScript (JSX) — quick-and-dirty automation

### Pattern: Batch process every PSD in a folder

```javascript
// File: batch-export-png.jsx
// Run: File → Scripts → Browse → select this file

#target photoshop

var inputFolder = Folder.selectDialog("Select folder of PSDs");
var outputFolder = Folder.selectDialog("Select output folder for PNGs");

if (inputFolder && outputFolder) {
    var files = inputFolder.getFiles("*.psd");

    for (var i = 0; i < files.length; i++) {
        var doc = app.open(files[i]);

        // Your operations here:
        // e.g. flatten, resize to 1200px wide, export PNG
        doc.flatten();
        doc.resizeImage(UnitValue(1200, "px"), null, null, ResampleMethod.BICUBIC);

        var pngFile = new File(outputFolder + "/" + doc.name.replace(/\.psd$/i, ".png"));
        var pngOpts = new PNGSaveOptions();
        pngOpts.compression = 6;
        pngOpts.interlaced = false;
        doc.saveAs(pngFile, pngOpts, true, Extension.LOWERCASE);

        doc.close(SaveOptions.DONOTSAVECHANGES);
    }

    alert("Processed " + files.length + " files.");
}
```

### Pattern: Export every artboard as separate PNG

```javascript
// File: export-artboards.jsx
#target photoshop

var doc = app.activeDocument;
var outputFolder = Folder.selectDialog("Choose output folder");
if (!outputFolder) { exit(); }

var artboards = [];
for (var i = 0; i < doc.layers.length; i++) {
    if (doc.layers[i].kind === LayerKind.ARTBOARD) {
        artboards.push(doc.layers[i]);
    }
}

for (var i = 0; i < artboards.length; i++) {
    // Duplicate the artboard to a new document
    var tempDoc = doc.duplicate(artboards[i].name);
    // Crop to just this artboard
    tempDoc.crop(artboards[i].bounds);
    // Save PNG
    var pngFile = new File(outputFolder + "/" + artboards[i].name + ".png");
    var pngOpts = new PNGSaveOptions();
    tempDoc.saveAs(pngFile, pngOpts, true, Extension.LOWERCASE);
    tempDoc.close(SaveOptions.DONOTSAVECHANGES);
}
```

### Pattern: Data-driven template (CSV → many files)

```javascript
// File: csv-template-fill.jsx
#target photoshop

// Reads a CSV: name,title,headshot_path
// Replaces text layers "{name}", "{title}" and the contents of smart object "headshot"

var csvFile = File.openDialog("Select CSV");
var templateFile = File.openDialog("Select PSD template");
var outputFolder = Folder.selectDialog("Output folder");

var csv = csvFile.open("r") ? csvFile.read() : "";
csvFile.close();

var rows = csv.split(/\r?\n/);
var headers = rows.shift().split(",");

for (var i = 0; i < rows.length; i++) {
    if (!rows[i].trim()) continue;
    var values = rows[i].split(",");
    var record = {};
    for (var j = 0; j < headers.length; j++) {
        record[headers[j].trim()] = values[j].trim();
    }

    var doc = app.open(templateFile);

    // Replace text in known layer names
    replaceText(doc, "{name}", record.name);
    replaceText(doc, "{title}", record.title);

    // Save as PSD with unique name (then optionally export PNG)
    var outFile = new File(outputFolder + "/" + record.name.replace(/\s+/g, "_") + ".psd");
    doc.saveAs(outFile, new PhotoshopSaveOptions(), true);
    doc.close(SaveOptions.DONOTSAVECHANGES);
}

function replaceText(doc, layerName, newText) {
    try {
        var layer = doc.artLayers.getByName(layerName);
        if (layer.kind === LayerKind.TEXT) {
            layer.textItem.contents = newText;
        }
    } catch (e) { /* layer not present, skip */ }
}
```

### Pattern: Add suffix-watermark to every layer's exported PNG

```javascript
// File: watermark-export.jsx
#target photoshop

var doc = app.activeDocument;
var outputFolder = Folder.selectDialog("Output folder");

for (var i = 0; i < doc.layers.length; i++) {
    var layer = doc.layers[i];
    if (!layer.visible) continue;

    // Hide all other top-level layers
    for (var j = 0; j < doc.layers.length; j++) {
        doc.layers[j].visible = (j === i);
    }

    // Show the always-on watermark group
    try { doc.layers.getByName("Watermark").visible = true; } catch (e) {}

    var pngFile = new File(outputFolder + "/" + layer.name + ".png");
    var opts = new PNGSaveOptions();
    doc.saveAs(pngFile, opts, true, Extension.LOWERCASE);
}
```

## UXP (modern JavaScript) — for plugins and async ops

UXP plugins live in a folder and have a `manifest.json`. Distribute as `.ccx` (Creative Cloud extension package).

### Minimal UXP script entry

```javascript
// File: index.js
const { app, core } = require("photoshop");
const { storage } = require("uxp");

async function exportSelectedAsPng(filePath) {
    await core.executeAsModal(async () => {
        const doc = app.activeDocument;
        const entry = await storage.localFileSystem.getEntryWithUrl("file:" + filePath);
        await doc.saveAs.png(entry);
    }, { commandName: "Export PNG" });
}

module.exports = { exportSelectedAsPng };
```

### Minimal `manifest.json`

```json
{
  "id": "com.yourstudio.exporter",
  "name": "Quick PNG Exporter",
  "version": "1.0.0",
  "main": "index.js",
  "host": [{ "app": "PS", "minVersion": "23.0.0" }],
  "manifestVersion": 5,
  "entrypoints": [{
    "type": "command",
    "id": "exportPng",
    "label": "Quick PNG"
  }]
}
```

UXP **requires** wrapping document mutations in `executeAsModal`. Without it, Photoshop will throw "Not allowed to execute…".

## GIMP scripting — Python-Fu

GIMP ships with a Python console (Filters → Python-Fu → Console) and supports script files in its plug-ins folder.

### Pattern: Batch resize and export

```python
#!/usr/bin/env python
# File: batch-resize.py — place in GIMP plug-ins folder

from gimpfu import *
import os, glob

def batch_resize(input_dir, output_dir, max_width):
    for path in glob.glob(os.path.join(input_dir, "*.png")):
        image = pdb.gimp_file_load(path, path)
        # Resize keeping aspect
        w, h = image.width, image.height
        if w > max_width:
            new_h = int(h * (max_width / float(w)))
            pdb.gimp_image_scale(image, max_width, new_h)
        # Flatten and save
        pdb.gimp_image_flatten(image)
        drawable = pdb.gimp_image_get_active_drawable(image)
        out = os.path.join(output_dir, os.path.basename(path))
        pdb.file_png_save(image, drawable, out, "image", 0, 9, 1, 1, 1, 1, 1)
        pdb.gimp_image_delete(image)

register(
    "python-fu-batch-resize", "Batch resize to max width",
    "Resize all PNGs in a folder", "You", "You", "2026",
    "<Toolbox>/Filters/Custom/Batch Resize...", "",
    [
        (PF_DIRNAME, "input_dir", "Input folder", ""),
        (PF_DIRNAME, "output_dir", "Output folder", ""),
        (PF_INT, "max_width", "Max width (px)", 2048),
    ],
    [], batch_resize)

main()
```

GIMP 3.0 (released 2025) moves toward Python 3 + GObject-introspection bindings; Script-Fu (Scheme dialect) remains supported but is the legacy path. Prefer Python.

## Cross-platform notes

- **Photoshop scripts** run identically on Windows and macOS — same JSX/UXP file works on both.
- File paths in ExtendScript: use `/` forward-slashes everywhere. Avoid Windows backslashes — they're escape characters in JS strings.
- `Folder.selectDialog()` / `File.openDialog()` handle native OS dialogs transparently.
- UXP plugins ship as `.ccx` and install on both platforms via Creative Cloud.
- GIMP's plugin folder differs by OS — check Edit → Preferences → Folders → Plug-Ins for the path.

## Distribution

For one-off scripts:
- Hand the user the `.jsx` file + a 2-line install instruction (`File → Scripts → Browse → select the file`).
- Or place it in Photoshop's `Presets/Scripts/` folder so it shows up in `File → Scripts` menu directly.

For team workflows:
- UXP plugin packaged as `.ccx` → distribute via shared folder, link, or Adobe Exchange.
- Sign with Adobe's UDT (UXP Developer Tool) for trust-level dialogs.

For GIMP:
- Drop the `.py` file in the user's plug-ins folder, make executable on macOS/Linux.

## Common gotchas

- **ExtendScript's `for...in`** iterates object keys, not array values. Use `for (var i = 0; i < arr.length; i++)`.
- **ES5 only in ExtendScript** — no `let`, no `const`, no arrow functions, no template literals, no `for...of`. Write to ES5.
- **UXP file system is sandboxed** — you can only write where the user gave you a token (typically via `getEntryWithUrl` or a file picker).
- **`activeDocument` may be undefined** if no document is open — check before using.
- **Smart Object replacement** needs the `placeSmartObject()` action descriptor — there's no direct `smartObject.replace(path)` API.
- **Color management** — set the document color profile explicitly if exporting for print (`doc.convertProfile("sRGB IEC61966-2.1", Intent.RELATIVECOLORIMETRIC, true, true)`).
- **`alert()` blocks scripts** — useful for debug, painful in batch. Use `$.writeln()` to the ExtendScript console instead.
- **Performance** — for batches >50 files, disable history and dialogs: `app.displayDialogs = DialogModes.NO; doc.suspendHistory(...)`.

## Output discipline

When delivering:

1. **Single file**, runnable as-is, with install instructions at the top in a comment.
2. **No placeholders** — if the script needs a config (layer names, output path), put them in CONSTANTS at the top with sensible defaults.
3. **Try/catch around per-file operations** so one bad file doesn't kill the batch.
4. **Progress feedback** — for batches, log to console or update a status text layer.
5. **Idempotent** — re-running shouldn't double-output or corrupt state.
6. **Test path** — include a comment showing how to dry-run on 1 file first.

## When NOT to script

If the task is:
- One-off with <20 files → run an Action recorder instead.
- Mostly about resizing / format-converting / no layer logic → use `batch-image-pipeline` (Pillow / ImageMagick) — faster, no Photoshop license needed, can run on a server.
- Heavy raw editing → Lightroom batch presets, not Photoshop scripts.

Push back when scripting is the wrong tool.

## Knowledge graph integration

Before scripting, search:
- `hybrid_search("Photoshop UXP plugin patterns")`
- `kg-search search "ExtendScript" --type concept`
- `hybrid_search("batch image processing")`

Capture:
- Re-usable script that solved a recurring task → `knowledge/patterns/`
- UXP gotcha + fix → `knowledge/concepts/`
- Tool comparison (UXP vs ExtendScript on task X) → `knowledge/concepts/`

## Constraints

- DO write to ES5 for ExtendScript
- DO wrap UXP mutations in `executeAsModal`
- DO put config in CONSTANTS at top
- DO include install/run instructions in a header comment
- DON'T mix UXP and ExtendScript APIs in the same file
- DON'T script under 20 files unless recurring — recommend Actions instead
- DON'T silently fail per-file — log skips with reasons
