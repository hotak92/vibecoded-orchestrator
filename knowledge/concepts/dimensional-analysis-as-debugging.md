---
title: Dimensional Analysis and Equation Verification
type: concept
tags: [physics, applied-math, debugging, scientific-computing, equation-verification, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Dimensional Analysis and Equation Verification

## Overview

Before trusting a derivation, four cheap and independent sanity checks should pass: **(1) dimensional homogeneity**, **(2) limiting / boundary behaviour**, **(3) conservation and symmetry**, and **(4) order-of-magnitude with canonical numbers**. Each catches a different class of error; together they form a near-complete acceptance test before a manuscript is submitted or a numerical result is trusted. The four checks are necessary but not individually sufficient — sign errors and missing dimensionless prefactors slip past the dimensional check alone, which is why all four should run.

This node covers all four checks. Dimensional analysis is the most powerful but the others are equally cheap; build the habit of running them as a fixed acceptance test.

## Check 1 — Dimensional Homogeneity

### Base dimensions

In SI, seven base dimensions: $\mathsf{L}$ (length), $\mathsf{M}$ (mass), $\mathsf{T}$ (time), $\mathsf{\Theta}$ (temperature), $\mathsf{I}$ (current), $\mathsf{N}$ (amount), $\mathsf{J}$ (luminous intensity). Every other quantity is a product of integer powers of these. Force: $[F] = \mathsf{M L T^{-2}}$. Energy: $[E] = \mathsf{M L^2 T^{-2}}$. Power: $[P] = \mathsf{M L^2 T^{-3}}$. Pressure: $[p] = \mathsf{M L^{-1} T^{-2}}$.

### The homogeneity rule

For an equation $A + B = C$, all three of $[A]$, $[B]$, $[C]$ must be equal. For $f(x)$ with $f \in \{\exp, \log, \sin, \cos, \tan, \mathrm{erf}, \dots\}$, $x$ must be dimensionless. Example: $\exp(-E / k_B T)$ — exponent must be dimensionless; check $[E] = \mathsf{M L^2 T^{-2}}$, $[k_B] = \mathsf{M L^2 T^{-2} \Theta^{-1}}$, $[T] = \mathsf{\Theta}$, so $[E/(k_B T)] = 1$. ✓

### Buckingham π theorem

If a physical law involves $n$ variables with $k$ independent base dimensions, it can be re-expressed as a relation between $n - k$ dimensionless groups $\pi_i$:

$$f(\pi_1, \pi_2, \dots, \pi_{n-k}) = 0.$$

This collapses an unknown $n$-variable problem to an $(n-k)$-variable one *before* doing any experiment. Two of the most useful $\pi$ groups in engineering science:

- **Reynolds number** $\mathrm{Re} = \rho U L / \mu$ (inertia vs viscous forces; controls onset of turbulence ~ 2000-4000 in pipe flow).
- **Péclet number** $\mathrm{Pe} = U L / D$ (advection vs diffusion; sets whether transport is mixing-limited or boundary-layer-limited).

### Worked checks

**Schrödinger equation (time-independent)** — $-\frac{\hbar^2}{2m} \nabla^2 \psi + V \psi = E \psi$:

- $[\hbar] = \mathsf{M L^2 T^{-1}}$, so $[\hbar^2] = \mathsf{M^2 L^4 T^{-2}}$.
- $[\nabla^2] = \mathsf{L^{-2}}$.
- $[\hbar^2 \nabla^2 / m] = \mathsf{M^2 L^4 T^{-2}} \cdot \mathsf{L^{-2}} / \mathsf{M} = \mathsf{M L^2 T^{-2}} = $ energy. ✓
- $[V\psi] = [V][\psi]$ and $[V] = \mathsf{M L^2 T^{-2}}$ = energy. ✓

A missing $\hbar^2$ or wrong power of $m$ shows up instantly.

**Diffusion equation** — $\partial c/\partial t = D \nabla^2 c$:

$[c/t]$ vs $[D / L^2][c]$: $[D] = \mathsf{L^2 T^{-1}}$, both sides $\mathsf{[c] T^{-1}}$. ✓ This is also how you remember that the *diffusion length* scales as $\sqrt{Dt}$ — there is no other combination of $D$ and $t$ with units of length.

**Navier-Stokes scaling → Reynolds number** — The full equation $\rho(\partial_t u + u \cdot \nabla u) = -\nabla p + \mu \nabla^2 u$ has inertial term $\sim \rho U^2 / L$ and viscous term $\sim \mu U / L^2$. Their ratio:

$$\frac{\rho U^2 / L}{\mu U / L^2} = \frac{\rho U L}{\mu} = \mathrm{Re}.$$

That's the only dimensionless group from these variables — Buckingham hands you the controlling parameter without solving anything.

### What dimensions don't catch

Sign errors, dimensionless numerical-prefactor errors ($\tfrac{1}{2}$, $2\pi$, $4\pi$), or errors that preserve dimensions while breaking physics. Hence the next three checks.

## Check 2 — Limiting and Boundary Behaviour

Probe the equation in characteristic limits. A correct expression must recover the known behaviour in each.

| Limit | Expected behaviour |
|---|---|
| $v \to 0$ | Relativistic → Newtonian; thermal velocity terms vanish |
| $T \to 0$ | Approach ground state; entropic terms freeze out |
| $T \to \infty$ | Equipartition for classical systems; quantum partition function → classical |
| $r \to 0$ | Singularity expected (point charge / mass) or cutoff defined |
| $r \to \infty$ | Asymptotic free / no interaction |
| $\hbar \to 0$ | Quantum → classical |
| $c \to \infty$ | Relativistic → Newtonian |
| $N \to \infty$ | Thermodynamic limit; fluctuations $\sim 1/\sqrt{N}$ |
| Symmetric input | Symmetric output (parity, rotation, exchange) |

Example: Newtonian → relativistic energy $E = \gamma m c^2$ with $\gamma = 1/\sqrt{1 - v^2/c^2}$. At $v=0$, $\gamma=1$, $E = mc^2$ (rest energy). At $v \ll c$, expand: $E \approx mc^2 + \tfrac{1}{2}mv^2 + \dots$ — recovers classical KE plus the rest term. If your expression doesn't, sign or factor error.

If your equation fails a limit it shouldn't, that is the bug. Limits catch sign errors that dimensional analysis misses.

## Check 3 — Conservation Laws and Symmetries

Check whether the expression respects conservation of:

- **Energy** in closed systems: $\mathrm{d}E/\mathrm{d}t = 0$.
- **Momentum** for translation-invariant systems.
- **Angular momentum** for rotation-invariant systems.
- **Charge** always.
- **Probability** for wavefunctions: $\int |\psi|^2 \mathrm{d}^3 x = 1$ at all times; quantum operators must be Hermitian for observables.
- **Mass** in non-relativistic systems; weighted volumes in transport.
- **Detailed balance** for equilibrium systems: forward rate × $p_\text{forward}$ = reverse rate × $p_\text{reverse}$.

Symmetry-breaking the user did not flag is a red flag — e.g. an asymmetric expression purporting to describe an isotropic system, or a time-asymmetric expression for an equilibrium.

## Check 4 — Order-of-Magnitude with Canonical Numbers

Substitute representative values and verify the answer falls in the expected order. Common-magnitude offsets immediately diagnose the error:

| Offset | Likely cause |
|---|---|
| $10^{23}$ | Avogadro: mol vs molecule |
| $10^7$ | Joules vs erg, Pa vs bar |
| $10^9$ | GHz vs Hz, nm vs m |
| $2\pi$ | $\omega$ (rad/s) vs $f$ (Hz) |
| $4\pi$ | Gaussian vs SI units in electromagnetism |
| $\sqrt{2}$ | RMS vs peak amplitude |
| $10^{34}$ (atomic / molecular) | Missing $\hbar$ |
| Wrong sign | Convention mismatch (FFT $\exp(\pm i\omega t)$, etc.) |

Useful combinations to memorise (catch errors fast):

- $k_B T$ at room temperature ($T = 300\,$K) ≈ $4.14 \times 10^{-21}$ J ≈ $25.85$ meV ≈ $0.596$ kcal/mol ≈ $2.494$ kJ/mol.
- $\hbar c$ ≈ $197.327$ MeV·fm ≈ $1.973 \times 10^{-7}$ eV·m.
- $1/(4\pi\varepsilon_0)$ ≈ $8.988 \times 10^9$ N·m²/C².
- $N_A k_B = R$ ≈ $8.314$ J/(mol·K) — bridges molecular and molar quantities.

Full constant table: [[relatedTo::Physical Constants Quick Reference]].

## Common-Error Catalogue (Pattern-Match Against)

| Pattern | Likely error |
|---|---|
| `exp(-E/T)` where $T$ is in Kelvin | Missing $k_B$; should be `exp(-E/(k_B T))` |
| Off by $2\pi$ in oscillator / spectroscopy / FFT | $\omega = 2\pi f$ confusion |
| Off by $4\pi$ in EM | Gaussian vs SI Coulomb's law |
| Off by $\tfrac{1}{2}$ in KE / PE / field energy | The $\tfrac{1}{2}$ in $\tfrac{1}{2}mv^2$, $\tfrac{1}{2}kx^2$, $\tfrac{1}{2}\varepsilon_0 E^2$ |
| Off by $\sqrt{2}$ in RMS vs peak | $V_\text{RMS} = V_\text{peak}/\sqrt{2}$ for sinusoids |
| Off by $10^{23}$ | Mol vs molecule (Avogadro) |
| Sign in $\exp(\pm i \omega t)$ | Physics vs engineering FFT convention |
| Wrong power of $r$ in field vs potential | Potential $\propto 1/r$; field $\propto 1/r^2$ (point source) |
| `log` ambiguity | `math.log` is natural in Python; `math.log10` is base-10; in textbooks `log` is sometimes base-10. Always confirm base. |
| Degrees vs radians in trig | numpy `sin` takes radians; `np.deg2rad` to convert |
| Dimensionally consistent but sign-wrong | Pure dimensional check passes; need a limit check |
| Missing $\hbar$ in quantum | Energy off by $\sim 10^{-34}$; momentum off by similar factor |
| Missing $c$ in relativistic | Often off by $c^2$ from energy-mass conversion |
| Density vs total | Per-volume vs total quantity in extensive properties |
| Wrong scaling with system size in MD or MC | Energy reported per particle vs total; or per mole vs per molecule (Avogadro). |
| Mismatched units across functions in a pipeline | Reading wavelengths in nm into a function expecting m; degrees C into K. |
| Result that depends on choice of base unit | The expression is dimensionally inhomogeneous — a sign that a non-dimensionalisation step was botched. |

## Algorithmic Recipe

1. **Parse** the equation into a list of additive terms and multiplicative factors. For non-trivial chains, use SymPy (`sympy.parsing.latex.parse_latex`) or symbolic substitution.
2. **Tag every symbol** with its physical dimension. State all inferences explicitly so they can be corrected.
3. **Compute dimensions of each additive term** and confirm consistency.
4. **Confirm dimensionless arguments** to transcendental functions.
5. **Run 2-3 limit checks** appropriate to the expression.
6. **Run a conservation / symmetry check** appropriate to the system.
7. **Plug in canonical numbers** and verify order of magnitude.
8. **Pattern-match against the common-error catalogue.**
9. **Report**: state whether each check passes, flag any failures with the specific suspected cause and proposed correction.

## When All Four Checks Pass But the Equation Is Still Wrong

The four checks are necessary, not sufficient. They cannot catch:

- A sign error that preserves dimensions and a chosen limit.
- A wrong dimensionless prefactor (the $\tfrac{1}{2}$ family) that survives both dimensions and the limits you happened to check.
- A missing or extra cross term that happens to have the same dimensions.
- A convention mismatch (physics vs engineering FFT, Gaussian vs SI).

Next step: **numerical comparison to a known case** (e.g. evaluating your expression at a special point where the answer is known analytically — hydrogen 1s state, simple harmonic oscillator ground state, ideal gas limit).

## Use in ML for Science

Physics-informed neural networks (PINNs) and equation discovery (SINDy, AI Feynman) are increasingly used, and a recurring failure mode is producing a regression that fits training data but is dimensionally inconsistent. Practical recommendations:

- **Non-dimensionalise inputs and outputs** before training: replace raw $x, t$ with $\tilde{x} = x / L$, $\tilde{t} = t / \tau$ for problem-natural scales $L, \tau$. Training is more stable and the learned model generalises across scales.
- **Encode dimensional constraints as loss terms** or as hard architectural constraints (e.g. dimensionally-aware basis functions in symbolic regression).
- **Validate discovered equations** by running the dimensional + limit + symmetry + order-of-magnitude checks above before reporting them.

## Tools

- **Python: `pint`** — quantity arithmetic with units; raises on incompatible operations. Pairs with `pandas` (`pint-pandas`) and `xarray` (`pint-xarray`).
- **Python: `astropy.units`** — astronomy-flavoured, integrates with `astropy.constants`.
- **Python: `sympy.physics.units`** — dimensional verification for symbolic derivations.
- **R: `units` and `errors`** packages — quantity + uncertainty propagation.
- **Julia: `Unitful.jl`** — first-class units; compile-time checked.

## References

- Barenblatt (1996): *Scaling, Self-similarity, and Intermediate Asymptotics*. The deepest treatment of dimensional analysis and self-similar solutions.
- Bridgman (1922): *Dimensional Analysis*. The historical primer; still readable.
- Buckingham (1914): "On physically similar systems". *Physical Review*.
- Mahajan (2014): *The Art of Insight in Science and Engineering*. Order-of-magnitude reasoning and limit checks as a habit.
- Karniadakis et al. (2021): "Physics-informed machine learning". *Nature Reviews Physics*. Non-dimensionalisation in PINNs.
- CODATA 2022 recommended values: https://physics.nist.gov/cuu/Constants/

[[relatedTo::Physical Constants Quick Reference]]
[[relatedTo::Scientific Python Stack 2026]]
[[relatedTo::Hypothesis Testing Decision Tree]]
