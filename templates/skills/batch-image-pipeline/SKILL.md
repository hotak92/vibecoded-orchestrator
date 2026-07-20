---
name: batch-image-pipeline
description: Writes batch image and video processing scripts (Pillow, ImageMagick, ffmpeg) for asset pipelines — generate size variants, format conversions, colorspace transforms, watermarks, optimization. Use when one design output needs to become 40 deliverables, or when an asset library needs cleanup.
short_desc: Pillow/ImageMagick/ffmpeg batch processing
keywords: [Pillow, ImageMagick, ffmpeg, batch image processing, asset pipeline, image resize, "convert images", "resize images", "image batch", "process images", "video processing", "bulk image", "watermark images"]
model: opus
effort: high
---

# Batch Image Pipeline

You write asset-pipeline scripts on demand. One source comp becomes 40 deliverables: app icons across iOS / Android / Web densities, social headers in 6 aspect ratios, optimized JPEG + AVIF + WebP, watermarked previews, PDF/X print exports. This skill outputs scripts; the designer runs them.

## When to invoke

- "I need this logo in 20 sizes/formats"
- "Generate App Store / Play Store / favicon set from this PNG"
- "Optimize these 400 product photos"
- "Convert this folder from sRGB to Display P3 and tag the profile"
- "Add a watermark to all images in /preview"
- "Make AVIF + WebP + JPEG variants for every image"
- "Extract every frame from this video as a numbered sequence"
- "Strip EXIF / GPS metadata before client delivery"

## Tool selection

Each tool has a sweet spot — recommend the right one, don't default to one.

| Task | Best tool | Why |
|---|---|---|
| Resize, format-convert, simple ops on PNG/JPEG | **Pillow** (Python) | Cross-platform, scriptable, library-friendly |
| ICC profile-aware ops, complex compositing, batch | **ImageMagick** (`magick` CLI v7+) | Color-managed, every format under the sun |
| AVIF / WebP encode at quality | **libvips** (`vips` CLI / pyvips) | Faster + lower memory than Pillow for large batches |
| Video frames / transcode / GIF / motion graphics | **ffmpeg** | The universal video swiss-army knife |
| HDR, large gamut, EXR / DPX / scientific | **OpenImageIO** (`oiiotool`) | Color-managed beyond what IM handles |
| PDF / vector | **Ghostscript**, **pdftk**, **Inkscape CLI** | Vector-aware |
| EXIF strip / preserve | **exiftool** | Standard for metadata |

All of these are cross-platform (Linux, macOS, Windows) — they ship in Homebrew, Chocolatey, apt, and most have pip packages.

## Script patterns

### Pattern 1: App icon set from one square PNG

```python
# requirements: pip install Pillow
from pathlib import Path
from PIL import Image

SOURCE = Path("source/icon-1024.png")
OUT = Path("dist/icons")
OUT.mkdir(parents=True, exist_ok=True)

# iOS app icon sizes (px, name)
IOS = [
    (1024, "ios-marketing-1024.png"),
    (180,  "ios-iphone-60@3x.png"),
    (120,  "ios-iphone-60@2x.png"),
    (167,  "ios-ipad-pro-83.5@2x.png"),
    (152,  "ios-ipad-76@2x.png"),
    (76,   "ios-ipad-76.png"),
]
ANDROID = [
    (512, "android-playstore.png"),
    (192, "android-xxxhdpi.png"),
    (144, "android-xxhdpi.png"),
    (96,  "android-xhdpi.png"),
    (72,  "android-hdpi.png"),
    (48,  "android-mdpi.png"),
]
WEB = [(512, "web-512.png"), (192, "web-192.png"), (32, "favicon-32.png"), (16, "favicon-16.png")]

src = Image.open(SOURCE).convert("RGBA")
for size, name in IOS + ANDROID + WEB:
    img = src.resize((size, size), Image.Resampling.LANCZOS)
    img.save(OUT / name, "PNG", optimize=True)

print(f"Wrote {len(IOS) + len(ANDROID) + len(WEB)} icons to {OUT}")
```

Lanczos is the right resampler for downscaling photos and detailed marks. For pixel-art / hard-edge marks, use `Image.Resampling.NEAREST`.

### Pattern 2: Social headers in 6 aspect ratios

```python
from pathlib import Path
from PIL import Image

ASPECTS = {
    "instagram-square":      (1080, 1080),
    "instagram-portrait":    (1080, 1350),
    "instagram-story":       (1080, 1920),
    "linkedin-banner":       (1584, 396),
    "x-header":              (1500, 500),
    "facebook-cover":        (1640, 924),
    "youtube-thumbnail":     (1280, 720),
    "youtube-banner":        (2560, 1440),
}

SOURCE = Path("source/hero.png")
OUT = Path("dist/social")
OUT.mkdir(parents=True, exist_ok=True)

src = Image.open(SOURCE)

for name, (tw, th) in ASPECTS.items():
    # Smart crop centered on the focal point — Pillow's ImageOps.fit handles this
    from PIL import ImageOps
    out = ImageOps.fit(src, (tw, th), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    out.save(OUT / f"{name}.jpg", "JPEG", quality=88, optimize=True, progressive=True)
```

For brand-critical assets, do NOT auto-crop. Render variations and let the designer pick.

### Pattern 3: AVIF + WebP + JPEG triplet (modern web)

```bash
# Using ImageMagick v7 — outputs three formats for <picture> srcset
for f in source/*.jpg; do
  base="${f%.*}"
  name=$(basename "$base")
  # AVIF: best compression, modern browsers
  magick "$f" -quality 60 "dist/${name}.avif"
  # WebP: broad support, smaller than JPEG
  magick "$f" -quality 80 "dist/${name}.webp"
  # JPEG: fallback
  magick "$f" -quality 85 -strip -interlace Plane "dist/${name}.jpg"
done
```

`-strip` removes EXIF (smaller files + privacy). `-interlace Plane` = progressive JPEG.

### Pattern 4: Watermark all images in a folder

```bash
# ImageMagick — overlay a semi-transparent watermark, lower-right
for f in input/*.{jpg,png}; do
  magick composite -dissolve 35% \
    -gravity southeast -geometry +20+20 \
    watermark.png "$f" "preview/$(basename "$f")"
done
```

For visible-but-removable watermarks, 30-40% dissolve. For "do not redistribute" stamps, use a tiled diagonal pattern across the full image.

### Pattern 5: Color management — sRGB to Display P3 with profile tagging

```bash
# ImageMagick — convert color space AND tag the output with ICC profile
magick input.jpg \
  -profile /System/Library/ColorSync/Profiles/sRGB\ Profile.icc \
  -profile /System/Library/ColorSync/Profiles/Display\ P3.icc \
  output-p3.jpg
```

**Critical**: tagging without converting just lies about the data. Converting without tagging produces an untagged file that browsers will interpret as sRGB. Do both. See `knowledge/concepts/color-management-for-designers.md`.

Free ICC profiles: ICC repository at https://www.color.org/.

### Pattern 6: Strip ALL metadata for client delivery (privacy)

```bash
# exiftool — strip every EXIF / IPTC / XMP tag, in place, with backup
exiftool -all= -overwrite_original_in_place ./dist/*.jpg
```

GPS, camera serial number, original timestamps, software used — all gone. Important for: photographers shipping to clients, designers shipping mockups to web (don't leak the Mac you made it on).

### Pattern 7: Video frame extraction

```bash
# Every frame as numbered PNG
ffmpeg -i input.mp4 -vf "fps=24" frame_%04d.png

# Every 30th frame
ffmpeg -i input.mp4 -vf "select='not(mod(n,30))'" -vsync vfr frame_%04d.png

# Single frame at 00:01:23
ffmpeg -ss 00:01:23 -i input.mp4 -frames:v 1 frame.png
```

### Pattern 8: Convert video to optimized web formats

```bash
# H.264 MP4 (max compatibility)
ffmpeg -i input.mov -c:v libx264 -preset slow -crf 22 \
  -c:a aac -b:a 128k -movflags +faststart output.mp4

# WebM (smaller, modern browsers)
ffmpeg -i input.mov -c:v libvpx-vp9 -crf 32 -b:v 0 \
  -c:a libopus -b:a 96k output.webm

# Looping silent GIF (designer-friendly preview)
ffmpeg -i input.mov -vf "fps=15,scale=480:-1:flags=lanczos" \
  -loop 0 output.gif
```

`+faststart` puts metadata at the head of the file so it plays before fully loading.

### Pattern 9: PDF/X-1a for offset print

```bash
# Ghostscript — flatten transparency, embed all fonts, CMYK
gs -dPDFX -dBATCH -dNOPAUSE -dNOOUTERSAVE \
   -sDEVICE=pdfwrite \
   -sColorConversionStrategy=CMYK \
   -dProcessColorModel=/DeviceCMYK \
   -sOutputFile=output-printready.pdf \
   PDFX_def.ps input.pdf
```

For commercial print, ALWAYS get the print shop's preferred profile and PDF/X version (X-1a, X-3, X-4). Don't guess.

### Pattern 10: Bulk optimize without quality loss (lossless)

```bash
# JPEG — mozjpeg recompression, often 10-25% smaller, visually identical
for f in *.jpg; do mozjpeg -progressive -optimize -copy none "$f" > "opt/$f"; done

# PNG — zopflipng or pngquant
for f in *.png; do pngquant --quality=70-95 --output "opt/$f" "$f"; done

# SVG — svgo
svgo -f input-folder -o output-folder
```

## Output discipline

When delivering a script:

1. **One file, runnable as-is** — no `# TODO: fill this in`.
2. **Top-of-file dependencies** — `pip install` or `brew install` line.
3. **Configurable paths in CONSTANTS at the top** — easy to edit.
4. **Progress output** — `print()` or `tqdm` for batches > 50 items.
5. **Dry-run option** — print what would happen before doing it.
6. **Idempotent** — running twice doesn't double-process or skip the second run silently.
7. **Cross-platform paths** — `pathlib.Path` in Python; quoted paths in bash for spaces.

## Common gotchas

- **EXIF rotation** — JPEGs from phones may have rotation in EXIF, not pixel data. Pillow's `ImageOps.exif_transpose()` fixes this before resizing.
- **PNG with alpha → JPEG** — JPEG has no alpha. Flatten on a background color, document the choice. `Image.new("RGB", size, (255, 255, 255))` then paste.
- **Color profile assumed = sRGB** — untagged images are often Display P3 or AdobeRGB. Tag first, convert second, or you'll get saturation surprises.
- **Resampling for downscale ≠ upscale** — Lanczos for downscale, Mitchell or Bicubic for slight upscale, NEAREST for pixel-art preserving.
- **ffmpeg pixel format** — many sources are `yuv420p10le` (10-bit). Some players reject; `-pix_fmt yuv420p` forces 8-bit.
- **Trailing comma in JSON** — design-token JSON sometimes fails parsing because of this. Use `jsonc` or `json5` if you need comments / trailing commas.

## Cross-platform notes

- Pillow, ImageMagick, ffmpeg, libvips, exiftool: all work on Win/Mac/Linux.
- ImageMagick on macOS: `brew install imagemagick`. On Windows: chocolatey `choco install imagemagick`.
- Don't shell out to `convert` — that's the v6 binary and conflicts with Windows `convert.exe`. Use `magick` (v7+).
- Path-spaces — quote everything. Forward slashes work on Windows in most CLIs.

## When to recommend GUI over scripting

If the task is "make 5 of these once" — recommend the designer just do it in Photoshop / Affinity. Scripting overhead doesn't pay off below ~20 outputs or repeat-frequency of monthly.

Script when: 20+ outputs, monthly+ recurring, regulated output (icon set / print specs), or shipping to clients who'll re-request.

## Knowledge graph integration

Before scripting, search:
- `hybrid_search("image pipeline patterns")`
- `hybrid_search("color management ICC")`
- `kg-search search "ffmpeg" --type tool`

Capture reusable scripts:
- New asset pipeline that generalizes → `knowledge/patterns/` (with the script)
- New tool wrapper → `knowledge/tools/`
- A gotcha-and-fix → `knowledge/concepts/`

## Constraints

- DO produce runnable scripts with constants at the top
- DO note dependencies and cross-platform install commands
- DO include progress output for batch > 50 items
- DON'T hand-roll color conversion math (use ICC profiles via ImageMagick / Pillow ImageCms)
- DON'T silently strip EXIF when the designer might need it (ask first)
- DON'T recommend scripting under 20 outputs unless it's recurring
