---
title: Physical Constants Quick Reference
type: concept
tags: [physics, constants, reference, units, scientific-computing, low-level-implementation]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Physical Constants Quick Reference

## Overview

Canonical physical constants and useful combinations, to 5-6 significant figures (sufficient for sanity checks and order-of-magnitude work; for publication-grade precision, fetch the current CODATA values from `scipy.constants` or `astropy.constants`). Values reflect CODATA 2022 recommendations. Primary use case is the order-of-magnitude check in [[Equation Verification Methodology]].

## SI Constants

| Constant | Symbol | Value (SI) |
|---|---|---|
| Speed of light in vacuum | $c$ | $2.99792 \times 10^8$ m/s (exact since 1983 SI redefinition) |
| Planck constant | $h$ | $6.62607 \times 10^{-34}$ J·s (exact since 2019 SI redefinition) |
| Reduced Planck | $\hbar = h/(2\pi)$ | $1.05457 \times 10^{-34}$ J·s |
| Boltzmann constant | $k_B$ | $1.38065 \times 10^{-23}$ J/K (exact since 2019 SI) |
| Avogadro number | $N_A$ | $6.02214 \times 10^{23}$ /mol (exact since 2019 SI) |
| Elementary charge | $e$ | $1.60218 \times 10^{-19}$ C (exact since 2019 SI) |
| Electron mass | $m_e$ | $9.10938 \times 10^{-31}$ kg |
| Proton mass | $m_p$ | $1.67262 \times 10^{-27}$ kg |
| Neutron mass | $m_n$ | $1.67493 \times 10^{-27}$ kg |
| Atomic mass unit | $u$ | $1.66054 \times 10^{-27}$ kg |
| Vacuum permittivity | $\varepsilon_0$ | $8.85419 \times 10^{-12}$ F/m |
| Vacuum permeability | $\mu_0$ | $1.25664 \times 10^{-6}$ H/m |
| Gravitational constant | $G$ | $6.67430 \times 10^{-11}$ m³/(kg·s²) |
| Stefan-Boltzmann constant | $\sigma_\text{SB}$ | $5.67037 \times 10^{-8}$ W/(m²·K⁴) |
| Universal gas constant | $R = N_A k_B$ | $8.31446$ J/(mol·K) |
| Bohr radius | $a_0$ | $5.29177 \times 10^{-11}$ m |
| Rydberg constant | $R_\infty$ | $1.09737 \times 10^7$ /m |
| Fine-structure constant | $\alpha$ | $7.29735 \times 10^{-3} \approx 1/137.036$ |
| Bohr magneton | $\mu_B$ | $9.27401 \times 10^{-24}$ J/T |
| Faraday constant | $F$ | $96485.3$ C/mol |
| Standard gravity | $g_0$ | $9.80665$ m/s² (exact, conventional) |
| Standard atmosphere | $1\,\text{atm}$ | $1.01325 \times 10^5$ Pa (exact, conventional) |

## Memorise These Combinations

These catch a large fraction of order-of-magnitude errors instantly:

| Combination | Value | Use case |
|---|---|---|
| $k_B T$ at $T=300\,$K | $4.14 \times 10^{-21}$ J $\approx 25.85$ meV $\approx 0.596$ kcal/mol $\approx 2.494$ kJ/mol | Thermal energy at room temperature; biological / chemical scale |
| $k_B T_\odot$ at $T=5778\,$K (Sun surface) | $\approx 0.498$ eV | Photospheric thermal scale |
| $\hbar c$ | $197.327$ MeV·fm $\approx 1.973 \times 10^{-7}$ eV·m | Quantum-relativistic scale (nuclear, particle) |
| $1/(4\pi\varepsilon_0)$ | $8.988 \times 10^9$ N·m²/C² | Coulomb constant (SI) |
| $e^2/(4\pi\varepsilon_0)$ | $1.44$ eV·nm $= 14.4$ eV·Å | Atomic-scale Coulomb energy |
| $m_e c^2$ | $0.511$ MeV | Electron rest energy |
| $m_p c^2$ | $938.272$ MeV | Proton rest energy |
| $h c$ | $1240$ eV·nm | Photon energy ↔ wavelength: $E[\text{eV}] = 1240 / \lambda[\text{nm}]$ |
| $\hbar / m_e$ | $1.16 \times 10^{-4}$ m²/s | Quantum-mechanical scale (Bohr radius × velocity-like) |
| $2.4$ GHz | $\hbar \omega \approx 10\,\mu$eV | Microwave / WiFi photon energy |
| Visible light | $1.6$-$3.3$ eV | $380$-$750\,$nm wavelength range |

## Quick-Convert Cheat Sheet

| From | To | Multiplier |
|---|---|---|
| eV | J | $1.60218 \times 10^{-19}$ |
| kcal/mol | kJ/mol | $4.184$ |
| kcal/mol | eV | $0.04336$ |
| Hartree | eV | $27.2114$ |
| cm⁻¹ (wavenumber) | eV | $1.23984 \times 10^{-4}$ |
| Hz | eV (via $hf$) | $4.13567 \times 10^{-15}$ |
| K | eV (via $k_B T$) | $8.617 \times 10^{-5}$ |
| amu | MeV/c² | $931.494$ |
| Bar | Pa | $10^5$ |
| Torr | Pa | $133.322$ |
| Calorie (thermochemical) | Joule | $4.184$ (exact) |
| Calorie (international table) | Joule | $4.1868$ (exact) |
| Angstrom | nm | $0.1$ |
| Angstrom | m | $10^{-10}$ |

## Astronomical Constants

| Constant | Value (SI) |
|---|---|
| Astronomical unit (AU) | $1.49598 \times 10^{11}$ m |
| Parsec | $3.08568 \times 10^{16}$ m $\approx 3.262$ light-years |
| Light-year | $9.46073 \times 10^{15}$ m |
| Solar mass $M_\odot$ | $1.98892 \times 10^{30}$ kg |
| Solar radius $R_\odot$ | $6.95700 \times 10^8$ m |
| Solar luminosity $L_\odot$ | $3.828 \times 10^{26}$ W |
| Solar effective temperature | $5772$ K |
| Earth mass $M_\oplus$ | $5.9722 \times 10^{24}$ kg |
| Earth equatorial radius $R_\oplus$ | $6.3781 \times 10^6$ m |
| Hubble constant $H_0$ (Planck 2018) | $\approx 67.4$ km/s/Mpc (= $2.18 \times 10^{-18}$ /s) |

## Python Access

Always prefer library values over hardcoded ones in production:

```python
from scipy import constants

constants.c               # speed of light
constants.h               # Planck constant
constants.hbar            # reduced Planck
constants.k               # Boltzmann constant
constants.N_A             # Avogadro
constants.e               # elementary charge
constants.epsilon_0       # vacuum permittivity
constants.G               # gravitational constant
constants.Stefan_Boltzmann
constants.R               # gas constant
constants.m_e, constants.m_p, constants.m_n
constants.alpha           # fine-structure constant
constants.value('Bohr radius')  # any CODATA constant by name
```

For astronomy:

```python
from astropy import constants as const, units as u

const.c               # speed of light with units
const.G               # gravitational constant
const.M_sun           # solar mass
const.L_sun           # solar luminosity
const.R_earth, const.M_earth

# units flow through arithmetic, errors raise at runtime
E = (const.m_e * const.c**2).to(u.MeV)   # 0.511 MeV
```

## References

- CODATA 2022 recommended values: https://physics.nist.gov/cuu/Constants/
- `scipy.constants` documentation: https://docs.scipy.org/doc/scipy/reference/constants.html
- `astropy.constants` documentation: https://docs.astropy.org/en/stable/constants/

[[relatedTo::Equation Verification Methodology]]
[[relatedTo::Dimensional Analysis as a Debugging Tool]]
[[relatedTo::Scientific Python Stack 2026]]
