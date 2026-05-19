---
title: Batch Image Pipeline Recipes
type: pattern
tags:
- patterns
- design
- image-processing
- asset-pipeline
- low-level-implementation
- ImageMagick
- Pillow
- ffmpeg
- libvips
- exiftool
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Batch Image Pipeline Recipes

Concrete, runnable patterns for asset pipelines: one source comp becomes 40 deliverables. App icons across iOS / Android / Web densities, social headers in many aspect ratios, AVIF + WebP + JPEG triplets, watermarks, EXIF strips, video transcodes. These recipes assume the operator runs them from a script directory; constants are at the top so paths can be edited without reading the body.

## Tool selection matrix

Each tool has a sweet spot — pick the right one, don't default. All tools below ship cross-platform (Linux / macOS / Windows) via Homebrew, Chocolatey, apt, or pip.

| Task | Best tool | Why |
|---|---|---|
| Resize, format-convert, simple ops on PNG/JPEG | **Pillow** (Python) | Cross-platform, scriptable, library-friendly |
| ICC profile-aware ops, complex compositing, batch | **ImageMagick** (`magick` CLI v7+) | Color-managed, every format under the sun |
| AVIF / WebP encode at scale, large batches | **libvips** (`vips` CLI / pyvips) | Faster + lower memory than Pillow for large batches |
| Video frames / transcode / GIF / motion | **ffmpeg** | Universal video swiss-army knife |
| HDR, wide gamut, EXR / DPX / scientific | **OpenImageIO** (`oiiotool`) | Color-managed beyond what ImageMagick handles |
| PDF / vector | **Ghostscript**, **pdftk**, **Inkscape CLI** | Vector-aware |
| EXIF strip / preserve | **exiftool** | Standard for metadata |

Important: on modern systems use `magick` (ImageMagick v7+), not the legacy `convert` binary — `convert.exe` conflicts with Windows' built-in command of the same name.

## Recipe 1: App icon set from one square PNG

Generates the full iOS / Android / Web icon set from a single high-resolution source.

```python
# requirements: pip install Pillow
from pathlib import Path
from PIL import Image

SOURCE = Path("source/icon-1024.png")
OUT = Path("dist/icons")
OUT.mkdir(parents=True, exist_ok=True)

# (size_px, output_name)
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
```

**Resampler choice**: Lanczos for downscaling photos and detailed marks. NEAREST for pixel-art / hard-edge marks.

## Recipe 2: Social headers in N aspect ratios

```python
from pathlib import Path
from PIL import Image, ImageOps

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
    out = ImageOps.fit(src, (tw, th), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    out.save(OUT / f"{name}.jpg", "JPEG", quality=88, optimize=True, progressive=True)
```

**Critical**: for brand-critical assets, do NOT auto-crop. Render variations and let the designer pick. Automated centering destroys composition for off-center focal points.

## Recipe 3: AVIF + WebP + JPEG triplet for `<picture>` srcset

Modern web pages serve three formats; the browser picks the smallest it supports.

```bash
# ImageMagick v7
for f in source/*.jpg; do
  base="${f%.*}"
  name=$(basename "$base")
  magick "$f" -quality 60 "dist/${name}.avif"     # AVIF: best compression
  magick "$f" -quality 80 "dist/${name}.webp"     # WebP: broad support
  magick "$f" -quality 85 -strip -interlace Plane "dist/${name}.jpg"   # JPEG: fallback
done
```

`-strip` removes EXIF (smaller files + privacy). `-interlace Plane` = progressive JPEG, renders progressively as it loads.

## Recipe 4: Watermark a folder

```bash
# Semi-transparent watermark in the lower-right
for f in input/*.{jpg,png}; do
  magick composite -dissolve 35% \
    -gravity southeast -geometry +20+20 \
    watermark.png "$f" "preview/$(basename "$f")"
done
```

Visible-but-removable watermarks: 30–40% dissolve. "Do not redistribute" stamps: tiled diagonal pattern across the full image.

## Recipe 5: ICC color-space conversion with profile tagging

The most-misunderstood operation in asset pipelines. Tagging without converting changes the color; converting without tagging produces an untagged file that browsers will interpret as sRGB.

```bash
# Convert AND tag — sRGB source to Display P3
magick input.jpg \
  -profile /path/to/sRGB.icc \
  -profile /path/to/DisplayP3.icc \
  output-p3.jpg
```

The first `-profile` declares the source color space (tagging); the second performs conversion to the destination and tags the output. Always do both. Free ICC profiles available from https://www.color.org/. See [[relatedTo::Color Management for Designers]] for the full mental model.

## Recipe 6: Strip ALL metadata for client delivery (privacy)

```bash
exiftool -all= -overwrite_original_in_place ./dist/*.jpg
```

Strips GPS, camera serial number, original timestamps, software identifiers — important for photographers shipping to clients, designers shipping mockups (don't leak the Mac you made it on). Confirm the designer is okay with this before stripping; sometimes EXIF is needed downstream.

## Recipe 7: Video frame extraction

```bash
# Every frame as numbered PNG (fps=24 ≈ standard frame rate)
ffmpeg -i input.mp4 -vf "fps=24" frame_%04d.png

# Every 30th frame
ffmpeg -i input.mp4 -vf "select='not(mod(n,30))'" -vsync vfr frame_%04d.png

# Single frame at 1:23
ffmpeg -ss 00:01:23 -i input.mp4 -frames:v 1 frame.png
```

## Recipe 8: Video to optimized web formats

```bash
# H.264 MP4 (max compatibility)
ffmpeg -i input.mov -c:v libx264 -preset slow -crf 22 \
  -c:a aac -b:a 128k -movflags +faststart output.mp4

# WebM VP9 (smaller, modern browsers)
ffmpeg -i input.mov -c:v libvpx-vp9 -crf 32 -b:v 0 \
  -c:a libopus -b:a 96k output.webm

# Looping silent GIF (designer-friendly preview)
ffmpeg -i input.mov -vf "fps=15,scale=480:-1:flags=lanczos" \
  -loop 0 output.gif
```

`+faststart` moves metadata to the head of the file so playback starts before the file finishes loading.

## Recipe 9: PDF/X-1a for offset print

```bash
gs -dPDFX -dBATCH -dNOPAUSE -dNOOUTERSAVE \
   -sDEVICE=pdfwrite \
   -sColorConversionStrategy=CMYK \
   -dProcessColorModel=/DeviceCMYK \
   -sOutputFile=output-printready.pdf \
   PDFX_def.ps input.pdf
```

For commercial print, ALWAYS get the print shop's preferred profile and PDF/X version (X-1a, X-3, X-4). Don't guess at flatness, color profile, or compression.

## Recipe 10: Lossless bulk optimization

```bash
# JPEG — mozjpeg recompression, often 10-25% smaller, visually identical
for f in *.jpg; do mozjpeg -progressive -optimize -copy none "$f" > "opt/$f"; done

# PNG — pngquant
for f in *.png; do pngquant --quality=70-95 --output "opt/$f" "$f"; done

# SVG — svgo
svgo -f input-folder -o output-folder
```

## Output discipline for pipeline scripts

When delivering a script as a designer-runnable artifact:

1. **One file, runnable as-is** — no `# TODO: fill this in`.
2. **Top-of-file dependency line** — `pip install ...` or `brew install ...`.
3. **Configurable paths in CONSTANTS at the top** — easy to edit without reading the body.
4. **Progress output** — `print()` or `tqdm` for batches > 50 items.
5. **Dry-run option** — print what would happen before doing it.
6. **Idempotent** — running twice doesn't double-process or skip the second run silently.
7. **Cross-platform paths** — `pathlib.Path` in Python; quoted paths in bash for spaces.

## Common gotchas

- **EXIF rotation** — JPEGs from phones may have rotation in EXIF, not pixel data. Use Pillow's `ImageOps.exif_transpose()` before resizing.
- **PNG with alpha → JPEG** — JPEG has no alpha. Flatten on a background color explicitly, document the choice.
- **Untagged color profile assumed = sRGB** — but originals are often Display P3 or AdobeRGB. Tag first, convert second, or you'll get saturation surprises.
- **Resampling differs for downscale vs upscale** — Lanczos for downscale, Mitchell or Bicubic for slight upscale, NEAREST for pixel-art preservation.
- **ffmpeg pixel format** — many sources are `yuv420p10le` (10-bit). Some players reject; `-pix_fmt yuv420p` forces 8-bit.
- **Trailing comma in design-token JSON** — sometimes fails parsing. Use `jsonc` or `json5` if you need comments or trailing commas.

## When to recommend GUI over scripting

Below ~20 outputs OR less-than-monthly recurrence, scripting overhead exceeds the win. Recommend the designer use Photoshop / Affinity directly. Script when: 20+ outputs, monthly+ recurrence, regulated output (icon set with required sizes, print specs), or shipping to clients who'll re-request.

## Cross-platform notes

- Pillow, ImageMagick, ffmpeg, libvips, exiftool all work on Linux / macOS / Windows.
- Use `magick` (v7+), not legacy `convert` — `convert.exe` conflicts with Windows.
- Quote path-spaces in shell scripts. Forward slashes work in most CLIs on Windows too.

## Relations

[[uses::ImageMagick]]
[[uses::Pillow]]
[[uses::ffmpeg]]
[[uses::libvips]]
[[uses::exiftool]]
[[relatedTo::Color Management for Designers]]
[[relatedTo::AI Image Generation Workflows 2026]]
[[implements::Asset Pipeline Production Practice]]

## References

- ImageMagick documentation: https://imagemagick.org/script/command-line-options.php
- Pillow documentation: https://pillow.readthedocs.io/
- ffmpeg documentation: https://ffmpeg.org/documentation.html
- libvips: https://www.libvips.org/
- exiftool: https://exiftool.org/
- ICC profile repository: https://www.color.org/
