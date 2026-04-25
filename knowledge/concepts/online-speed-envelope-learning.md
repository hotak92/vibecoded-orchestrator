---
title: Online Speed Envelope Learning
type: concept
tags: [AI, sim-racing, adaptive-control, real-time-learning, slip-angle, performance-envelope]
created: 2026-02-20T00:00:00Z
updated: 2026-04-05T14:33:42Z
valid_from: 2026-02-20T00:00:00Z
valid_until: null
status: active
---

# Online Speed Envelope Learning

Adaptive per-section speed limit learning for racing AI, using real-time slip angle telemetry. Used when the vehicle's physics DataLibrary is inaccessible and `AccurateCorneringSpeed()` cannot be called.

## Problem

Racing AI needs a speed limit per track section based on grip and curvature:
```
v_max = sqrt(g × μ × r)    where r = 1/κ (radius from curvature)
```
But μ (grip coefficient) is unknown at runtime without the physics DataLibrary.

## Solution: Observe and Adapt

Maintain a per-waypoint speed table. Initialize from a conservative estimate, then adapt each lap based on observed slip.

### Initial Estimate (Conservative)
```lua
-- κ = curvature at waypoint (pre-computed from raceline.xml)
-- g = 9.81, mu_init = 0.8 (conservative friction estimate)
v_max[i] = math.sqrt(9.81 * mu_init / math.max(kappa[i], 1e-4))
v_max[i] = math.min(v_max[i], MAX_STRAIGHT_SPEED)
```

### Adaptation Rule (per lap)
```lua
-- After completing each waypoint section:
local slip = telemetry.avgSlipAngleRelaxed  -- degrees, from UDP

if slip > SLIP_HIGH_THRESHOLD then
  -- Too fast: reduce speed limit
  v_max[i] = v_max[i] * (1 - REDUCE_FACTOR)   -- e.g. REDUCE_FACTOR = 0.05

elseif slip < SLIP_LOW_THRESHOLD and lap_completed_clean then
  -- Comfortable margin: cautiously increase
  v_max[i] = v_max[i] * (1 + INCREASE_FACTOR)  -- e.g. INCREASE_FACTOR = 0.02
end

-- Clamp to safety bounds
v_max[i] = math.max(MIN_CORNER_SPEED, math.min(v_max[i], MAX_STRAIGHT_SPEED))
```

### Typical Parameters
| Parameter | Value | Notes |
|---|---|---|
| `mu_init` | 0.8 | Conservative initial grip estimate |
| `SLIP_HIGH_THRESHOLD` | 5–8° | Above → too fast, reduce |
| `SLIP_LOW_THRESHOLD` | 2° | Below + clean lap → can push |
| `REDUCE_FACTOR` | 0.05 | 5% reduction when overdriving |
| `INCREASE_FACTOR` | 0.02 | 2% increase when comfortable |
| Convergence | 3–5 laps | Stabilizes to near-optimal |

## Data Source (PMR UDP Telemetry)

```
UDPVehicleTelemetryGeneral.avgSlipAngleRelaxed  -- Average relaxed slip angle
UDPVehicleTelemetryChassis.sideslip             -- Chassis sideslip angle
UDPVehicleTelemetryWheel[n].slipAngleRelaxed    -- Per-wheel relaxed slip
```

Relaxed slip angles (filtered) are preferred over instantaneous — more stable signal.

## Why Per-Section, Not Global

Track grip varies: wet patches, kerbs, banking, surface type. A single global μ underperforms. Per-waypoint indexing lets the AI learn:
- Kerb sections (may be grippier or slippier)
- Elevation changes (affect effective g)
- Long-lap vs short-lap variance

## Benefits

- ✅ No DataLibrary access needed
- ✅ Automatically adapts to tire wear, fuel load, damage
- ✅ Works for wet/dry if run with appropriate initial μ
- ✅ Generalizes across tracks (learns track-specific grip per session)
- ✅ Converges in 3–5 laps from a safe starting estimate

## Caveats

- First 3–5 laps are slower than optimal (conservative start)
- If slip threshold too high, risks exceeding real grip → oversteer/understeer
- Adaptation rate is a tradeoff: fast convergence vs. stability

## Links

- [[implements::SimRacing AI Mod — PMR]]
- [[relatedTo::PID Controller Pattern]]
