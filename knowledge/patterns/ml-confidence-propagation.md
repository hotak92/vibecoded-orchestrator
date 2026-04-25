---
title: ML Confidence Propagation Pattern
type: pattern
tags:
- pattern
- machine-learning
- confidence
- pose-detection
- computer-vision
- AI
- low-level-implementation
- python
created: 2026-01-28 19:00:00+00:00
updated: 2026-04-05T14:34:11Z
status: active
---

# ML Confidence Propagation Pattern

#pattern #machine-learning #confidence #pose-detection #computer-vision

## Problem

**Derived measurements lose connection to source uncertainty**:
- ML model provides keypoint confidences (0.0-1.0)
- Calculations use these keypoints but discard confidence
- Result: High-confidence detection from low-confidence keypoints

### Example Problem

```python
# Model detects keypoints with varying confidence
left_ankle = {'x': 100, 'y': 200, 'confidence': 0.3}   # ❌ Low confidence
right_ankle = {'x': 300, 'y': 205, 'confidence': 0.9}  # ✅ High confidence

# Calculation uses both but ignores confidence
foot_separation = abs(left_ankle['x'] - right_ankle['x'])  # 200 pixels

if foot_separation > threshold:
    return ("legs spread", 1.0)  # ❌ Claiming high confidence from uncertain data!
```

**Issue**: One low-confidence keypoint (0.3) should reduce overall detection confidence, but result claims 1.0 confidence.

## Solution

**Propagate ML model confidence through all derived measurements**

### Core Principle

> The confidence of a derived measurement cannot exceed the confidence of its least certain component

### Propagation Formula

Balance optimistic and conservative estimates:

```python
def _propagate_confidence(*confidences: float) -> float:
    """
    Propagate multiple ML confidence values through a calculation.

    Args:
        *confidences: Individual keypoint confidences (0.0-1.0)

    Returns:
        Propagated confidence (0.0-1.0)
    """
    if not confidences:
        return 0.0

    # Optimistic: Simple average (treats all equally)
    simple_avg = sum(confidences) / len(confidences)

    # Conservative: Product (penalizes low confidences)
    product = 1.0
    for c in confidences:
        product *= c

    # Balanced: Average of both approaches
    return (simple_avg + product) / 2
```

### Why This Formula?

**Simple Average** (optimistic):
- `(0.3 + 0.9) / 2 = 0.6`
- Too forgiving of low confidence values

**Product** (conservative):
- `0.3 × 0.9 = 0.27`
- Too harsh, penalizes even slightly lower confidence

**Balanced** (average of both):
- `(0.6 + 0.27) / 2 = 0.435`
- ✅ Reasonable: Lower than simple average, higher than product
- ✅ Penalizes low confidence without being overly harsh

## Implementation Pattern

### Step 1: Collect Relevant Confidences

```python
def detect_legs_spread(self, kp: dict) -> Optional[tuple[str, float]]:
    """Detect legs spread with confidence propagation."""

    left_ankle = kp.get('left_ankle', {})
    right_ankle = kp.get('right_ankle', {})
    left_hip = kp.get('left_hip', {})
    right_hip = kp.get('right_hip', {})

    # Collect confidences for all keypoints used
    confidences = [
        left_ankle.get('confidence', 0),
        right_ankle.get('confidence', 0),
        left_hip.get('confidence', 0),
        right_hip.get('confidence', 0),
    ]

    # ...
```

### Step 2: Propagate Confidence

```python
    # Propagate confidence through calculation
    propagated_conf = self._propagate_confidence(*confidences)

    # Check confidence threshold FIRST
    if propagated_conf < 0.5:  # ✅ Reject low-confidence detections early
        return None
```

### Step 3: Use Body-Relative Coordinates

```python
    # Transform to body-relative space
    yaw, pitch = self._get_torso_orientation_angles(kp)
    kp_rel = self._transform_to_body_relative(kp, yaw, pitch)

    # Calculate separation in body-relative space
    foot_separation = abs(kp_rel['left_ankle']['x'] - kp_rel['right_ankle']['x'])
    body_scale = self._get_body_scale(kp)

    # Check threshold
    if foot_separation > body_scale * 0.4:
        return ("legs spread", propagated_conf)  # ✅ Return propagated confidence

    return None
```

## Complete Pattern

```python
def detect_pose(self, kp: dict) -> Optional[tuple[str, float]]:
    """Standard pose detection pattern with ML confidence propagation."""

    # 1. Extract keypoints
    keypoint_a = kp.get('keypoint_a', {})
    keypoint_b = kp.get('keypoint_b', {})

    # 2. Collect confidences
    confidences = [
        keypoint_a.get('confidence', 0),
        keypoint_b.get('confidence', 0),
    ]

    # 3. Propagate confidence
    propagated_conf = self._propagate_confidence(*confidences)

    # 4. Early rejection on low confidence
    if propagated_conf < 0.5:
        return None

    # 5. Transform to body-relative coordinates
    yaw, pitch = self._get_torso_orientation_angles(kp)
    if yaw is None:
        return None

    kp_rel = self._transform_to_body_relative(kp, yaw, pitch)

    # 6. Calculate measurement in body-relative space
    measurement = calculate_measurement(kp_rel)
    body_scale = self._get_body_scale(kp)

    # 7. Check threshold
    if measurement > threshold * body_scale:
        return ("pose_name", propagated_conf)  # ✅ Return propagated confidence

    # 8. No arbitrary adjustments - trust ML model
    return None
```

## Benefits

### 1. Principled Confidence Values

```python
# High-confidence keypoints → High-confidence detection
kp_confs = [0.9, 0.85, 0.92]
propagated = _propagate_confidence(*kp_confs)  # 0.88 ✅

# Mixed confidence → Medium confidence
kp_confs = [0.9, 0.5, 0.85]
propagated = _propagate_confidence(*kp_confs)  # 0.62 ✅

# Low-confidence keypoint → Low confidence (rejected)
kp_confs = [0.9, 0.2, 0.85]
propagated = _propagate_confidence(*kp_confs)  # 0.38 ❌ (< 0.5 threshold)
```

### 2. No Magic Numbers

```python
# ❌ Before: Arbitrary adjustments
if arms_crossed:
    conf = 0.8  # Why 0.8? Magic number!
    return ("arms crossed", conf)

# ✅ After: ML-derived confidence
arm_confs = [left_wrist['conf'], right_wrist['conf'], left_shoulder['conf'], right_shoulder['conf']]
propagated = self._propagate_confidence(*arm_confs)
if propagated > 0.5:
    return ("arms crossed", propagated)  # ✅ Principled value from ML model
```

### 3. Automatic Uncertainty Tracking

```python
# Low-confidence keypoints automatically reduce detection confidence
# No manual logic needed - propagation handles it
```

## Common Mistakes

### ❌ Mistake 1: Ignoring Confidence

```python
# Wrong: Use keypoints without checking confidence
foot_sep = abs(left_ankle['x'] - right_ankle['x'])
if foot_sep > threshold:
    return ("legs spread", 1.0)  # ❌ No confidence check!
```

### ❌ Mistake 2: Manual Confidence Adjustment

```python
# Wrong: Arbitrary confidence values
if pose_detected:
    confidence = 0.7  # ❌ Why 0.7? Magic number!
    return ("pose", confidence)
```

### ❌ Mistake 3: Using Only Minimum Confidence

```python
# Wrong: Too conservative
conf = min(confidences)  # ❌ Single low value dominates
# Example: [0.9, 0.9, 0.5] → 0.5 (too harsh)
```

### ❌ Mistake 4: Using Only Average

```python
# Wrong: Too optimistic
conf = sum(confidences) / len(confidences)  # ❌ Forgives low values
# Example: [0.9, 0.9, 0.1] → 0.63 (too forgiving)
```

## Detection Threshold

**Standard threshold: 0.5** (50% confidence)

Rationale:
- Below 0.5: More likely wrong than right
- Above 0.5: More likely right than wrong
- Balanced approach with proper propagation

Previous threshold (0.6) was too conservative when combined with propagation.

## Benefits in Practice

- Principled confidence values (no magic numbers)
- Automatic uncertainty tracking
- Low-confidence keypoints → low-confidence detections (as expected)

## Related Patterns

- [[Body-Relative Coordinate System]]
- [[Uncertainty Quantification]]
