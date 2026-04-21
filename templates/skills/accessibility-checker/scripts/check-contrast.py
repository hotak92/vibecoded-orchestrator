#!/usr/bin/env python3
"""
Check color contrast ratio (WCAG 2.1 compliance)

Usage:
    python check-contrast.py "#333333" "#FFFFFF"
    python check-contrast.py "rgb(51, 51, 51)" "rgb(255, 255, 255)"
"""

import sys
import re

def hex_to_rgb(hex_color):
    """Convert hex color to RGB tuple"""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def rgb_string_to_tuple(rgb_string):
    """Convert 'rgb(r, g, b)' to (r, g, b) tuple"""
    match = re.match(r'rgb\((\d+),\s*(\d+),\s*(\d+)\)', rgb_string)
    if match:
        return tuple(int(x) for x in match.groups())
    return None

def relative_luminance(rgb):
    """Calculate relative luminance (WCAG formula)"""
    def adjust(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = [adjust(c) for c in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def contrast_ratio(color1, color2):
    """Calculate contrast ratio between two colors"""
    l1 = relative_luminance(color1)
    l2 = relative_luminance(color2)

    lighter = max(l1, l2)
    darker = min(l1, l2)

    return (lighter + 0.05) / (darker + 0.05)

def parse_color(color_string):
    """Parse color string (hex or rgb) to RGB tuple"""
    if color_string.startswith('#'):
        return hex_to_rgb(color_string)
    elif color_string.startswith('rgb'):
        return rgb_string_to_tuple(color_string)
    else:
        print(f"Error: Invalid color format '{color_string}'")
        print("Supported formats: #RRGGBB or rgb(r, g, b)")
        sys.exit(1)

def check_wcag_compliance(ratio):
    """Check WCAG compliance levels"""
    results = {
        'normal_text_aa': ratio >= 4.5,
        'normal_text_aaa': ratio >= 7.0,
        'large_text_aa': ratio >= 3.0,
        'large_text_aaa': ratio >= 4.5,
    }
    return results

def main():
    if len(sys.argv) != 3:
        print("Usage: python check-contrast.py <color1> <color2>")
        print("Examples:")
        print('  python check-contrast.py "#333333" "#FFFFFF"')
        print("  python check-contrast.py 'rgb(51, 51, 51)' 'rgb(255, 255, 255)'")
        sys.exit(1)

    color1_str = sys.argv[1]
    color2_str = sys.argv[2]

    color1 = parse_color(color1_str)
    color2 = parse_color(color2_str)

    ratio = contrast_ratio(color1, color2)
    compliance = check_wcag_compliance(ratio)

    print(f"\n{'='*50}")
    print("COLOR CONTRAST ANALYSIS")
    print(f"{'='*50}")
    print(f"Color 1: {color1_str} → RGB{color1}")
    print(f"Color 2: {color2_str} → RGB{color2}")
    print(f"\nContrast Ratio: {ratio:.2f}:1")
    print(f"{'='*50}")

    print("\nWCAG 2.1 Compliance:")
    print("-" * 50)

    aa_normal = "✅ PASS" if compliance['normal_text_aa'] else "❌ FAIL"
    aaa_normal = "✅ PASS" if compliance['normal_text_aaa'] else "❌ FAIL"
    aa_large = "✅ PASS" if compliance['large_text_aa'] else "❌ FAIL"
    aaa_large = "✅ PASS" if compliance['large_text_aaa'] else "❌ FAIL"

    print(f"Normal Text (< 18pt):")
    print(f"  AA  (4.5:1 required): {aa_normal}")
    print(f"  AAA (7.0:1 required): {aaa_normal}")
    print(f"\nLarge Text (≥ 18pt or ≥ 14pt bold):")
    print(f"  AA  (3.0:1 required): {aa_large}")
    print(f"  AAA (4.5:1 required): {aaa_large}")
    print(f"{'='*50}\n")

    # Overall recommendation
    if compliance['normal_text_aaa']:
        print("✅ Excellent! Passes all WCAG levels (AAA)")
    elif compliance['normal_text_aa']:
        print("✅ Good! Passes WCAG AA (recommended minimum)")
    elif compliance['large_text_aa']:
        print("⚠️  Only suitable for large text (AA large)")
    else:
        print("❌ FAIL: Does not meet WCAG requirements")
        print("   Recommendation: Adjust colors to achieve 4.5:1 ratio")

if __name__ == "__main__":
    main()
