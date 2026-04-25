// Per-project visual accent. Client-side only (no DB migration) — the
// color is derived deterministically from the project ID, so the same
// project always gets the same color across sessions and machines.
//
// Used by:
//   - layout chrome (4px header strip / sidebar accent)
//   - ProjectSelector (colored dot next to project name)
//
// Palette is small and chosen to be distinguishable; colors come from the
// existing app aurora vocabulary (teal, purple, pink) plus a few extras
// that keep contrast against the dark background.

// Six-hue palette tuned for deuteranopia/protanopia/tritanopia
// distinguishability — verified pairwise with the Coblis simulator. The
// previous 8-hue palette folded purple/pink/violet into the same
// perceptual cluster under red-green color-blindness, defeating the
// purpose of the accent. We pick 6 hues spread across hue+luminance:
//   blue / orange — Wong (2011) recommendation, max contrast
//   teal / amber — secondary contrast pair
//   pink / green — adds two distinct luminance bands
const PALETTE = [
  '#3aa3ff', // blue
  '#ff9b3d', // orange
  '#00bfa6', // teal
  '#ffd24a', // amber
  '#ff4fa0', // pink
  '#5fd97b', // green
] as const;

/** Stable djb2-ish hash → palette index. */
function hashIndex(id: string): number {
  let h = 5381;
  for (let i = 0; i < id.length; i++) {
    h = ((h << 5) + h + id.charCodeAt(i)) | 0;
  }
  return Math.abs(h) % PALETTE.length;
}

/** Hex color for the given project. Returns transparent for null. */
export function projectColor(projectId: string | null | undefined): string {
  if (!projectId) return 'transparent';
  return PALETTE[hashIndex(projectId)];
}
