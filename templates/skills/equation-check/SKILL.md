---
name: equation-check
description: Audits a mathematical equation or derivation for dimensional consistency, limiting behaviour, sign sanity, and common-error patterns. Use when the user pastes an equation, asks "is this derivation right", "check the units", "what should this reduce to in the limit", "I'm getting an off-by-factor-of-2 error", or shares a snippet of LaTeX they want validated before publication.
short_desc: "validate equations: dimensions, limits, signs"
keywords: [equation, derivation, dimensional analysis, LaTeX, unit check, check the units, "check derivation", "verify equation", "audit derivation", "LaTeX equation", "math check", "limiting behavior"]
model: opus
effort: xhigh
allowed-tools: Read, Write, WebSearch, Bash
---

# Equation Check (Opus)

**Purpose**: Validate a physical or mathematical equation against the four standard sanity checks — dimensional homogeneity, limiting / boundary behaviour, symmetry / conservation, and order-of-magnitude — and flag the canonical errors (missing constants, $2\pi$ confusion, factor-of-2, sign mistakes).

**Model**: Opus — this is one of the few routine tasks that genuinely needs deep reasoning. Equation auditing fails catastrophically if you hallucinate maths, and Sonnet has shown drift on multi-step dimensional checks for nontrivial derivations. Opus's lower hallucination rate justifies the cost.

**When to invoke autonomously**:
- The user pastes a derivation or a chain of equations.
- The user reports "I think there's a factor wrong somewhere".
- The user shares LaTeX from a manuscript and asks for a review.
- The user's numerical result is off by a suspicious power of $2\pi$, $\mathrm{e}$, $c$, $\hbar$, or order of magnitude $10^k$.

**Do NOT invoke** for:
- Pure code bugs unrelated to maths (use `@debug-expert`).
- Statistical formulae (use `/stats-consult`).
- Conceptual physics questions ("what does entropy mean") — answer in conversation.

## Usage

```
/equation-check $E = \tfrac{1}{2} m v^2 + V(r)$ — is this the total energy in a central potential?
/equation-check check this derivation of the diffusion length [paste LaTeX]
/equation-check my MD simulation gives kT too small by ~6.022e23 — what did I do?
/equation-check is $\sigma = \epsilon_0 E$ dimensionally consistent in SI?
```

## What This Skill Does

### 1. Dimensional Homogeneity Audit

For every additive group of terms, compute base-dimension factorisations $[\mathsf{M}^a \mathsf{L}^b \mathsf{T}^c \mathsf{\Theta}^d \mathsf{I}^e \mathsf{N}^f]$. All terms in a sum must share the exponents. Arguments to `exp`, `log`, `sin`, `cos`, `tan`, `arctan`, `erf` must be dimensionless.

Use SI base by default. If the user is working in Gaussian / natural / Planck units, ask and re-derive — most off-by-$4\pi$ errors trace to silent Gaussian/SI conversion.

**Worked check (always include in output if dims are nontrivial):**

```
Term 1:  ½ m v²
  [m] = M, [v²] = L²T⁻²  →  [Term 1] = M L² T⁻² ✓ (energy)

Term 2:  V(r) (assumed potential energy)
  [V] = M L² T⁻²         →  [Term 2] = M L² T⁻² ✓

Sum dimensionally consistent. Both sides have units of energy.
```

### 2. Limiting and Boundary Behaviour

Probe the equation at characteristic limits:

| Limit | What should happen |
|---|---|
| $v \to 0$ | Relativistic → Newtonian; thermal velocity terms vanish |
| $T \to 0$ | Approach ground state; entropic terms freeze out |
| $T \to \infty$ | Equipartition for classical systems; quantum partition function → classical |
| $r \to 0$ | Singularity expected (point charge, point mass) or cutoff? |
| $r \to \infty$ | Asymptotic free / no interaction |
| $\hbar \to 0$ | Quantum → classical |
| $c \to \infty$ | Relativistic → Newtonian |
| Symmetric input | Symmetric output (parity, rotation, exchange) |

If the user's equation fails a limit it shouldn't, that's the bug.

### 3. Conservation Laws

Check whether the equation respects conservation of:
- **Energy** — in closed systems, $\mathrm{d}E/\mathrm{d}t = 0$.
- **Momentum** — for translation-invariant systems.
- **Angular momentum** — for rotation-invariant systems.
- **Charge** — always.
- **Probability** — wavefunctions: $\int |\psi|^2 \mathrm{d}^3 x = 1$ at all times; quantum operators must be Hermitian for observables.
- **Mass** — non-relativistic; weighted volumes in transport.

Symmetry-breaking that the user didn't flag is a red flag (e.g. an asymmetric expression purporting to describe an isotropic system).

### 4. Order-of-Magnitude Sanity

Substitute canonical numbers. If the answer is wrong by $10^{23}$, you've dropped Avogadro. By $10^7$, possibly Joules vs erg, or Pa vs bar. By $10^9$, GHz vs Hz or nm vs m. By $2\pi$ or $1/(2\pi)$, angular frequency vs frequency.

Constants reference table the skill applies (CODATA 2022 values, to 5-6 sig figs sufficient for sanity):

| Constant | Symbol | Value (SI) |
|---|---|---|
| Speed of light | $c$ | $2.99792 \times 10^8$ m/s |
| Planck constant | $h$ | $6.62607 \times 10^{-34}$ J·s |
| Reduced Planck | $\hbar = h/(2\pi)$ | $1.05457 \times 10^{-34}$ J·s |
| Boltzmann constant | $k_B$ | $1.38065 \times 10^{-23}$ J/K |
| Avogadro | $N_A$ | $6.02214 \times 10^{23}$ /mol |
| Electron charge | $e$ | $1.60218 \times 10^{-19}$ C |
| Electron mass | $m_e$ | $9.10938 \times 10^{-31}$ kg |
| Proton mass | $m_p$ | $1.67262 \times 10^{-27}$ kg |
| Permittivity | $\varepsilon_0$ | $8.85419 \times 10^{-12}$ F/m |
| Permeability | $\mu_0$ | $1.25664 \times 10^{-6}$ H/m |
| Gravitational | $G$ | $6.67430 \times 10^{-11}$ m³/(kg·s²) |
| Stefan-Boltzmann | $\sigma$ | $5.67037 \times 10^{-8}$ W/(m²·K⁴) |
| Gas constant | $R$ | $8.31446$ J/(mol·K) |

Convenient combinations (memorise these — they catch errors fast):
- $k_B T$ at room temp ($T = 300$ K) ≈ $4.14 \times 10^{-21}$ J ≈ $25.85$ meV ≈ $0.593$ kcal/mol ≈ $2.48$ kJ/mol.
- $\hbar c$ ≈ $197.327$ MeV·fm ≈ $1.973 \times 10^{-7}$ eV·m.
- $1/(4\pi \varepsilon_0)$ ≈ $8.988 \times 10^9$ N·m²/C².
- Avogadro's number — order $10^{23}$ — catches mol vs molecule mistakes instantly.

### 5. Common-Error Catalogue (Pattern-Match Against)

| Pattern | Likely error |
|---|---|
| `exp(-E/T)` where $T$ is in Kelvin | Missing $k_B$; should be `exp(-E/(k_B T))` |
| Off by $2\pi$ in oscillator / spectroscopy / FFT | Angular frequency $\omega$ vs ordinary frequency $f$; $\omega = 2\pi f$ |
| Off by $4\pi$ in EM | Gaussian vs SI: Coulomb's law is $q_1 q_2 / r^2$ (Gaussian) vs $q_1 q_2 / (4\pi\varepsilon_0 r^2)$ (SI) |
| Off by ½ in kinetic / potential energy | The $\tfrac{1}{2}$ in $\tfrac{1}{2}mv^2$, $\tfrac{1}{2}kx^2$, $\tfrac{1}{2}\varepsilon_0 E^2$ |
| Off by $\sqrt{2}$ in RMS vs peak | $V_{\mathrm{RMS}} = V_{\mathrm{peak}}/\sqrt{2}$ for sinusoids |
| Off by $10^{23}$ | Mol vs molecule (Avogadro) |
| Sign in $\exp(\pm i \omega t)$ | Physics vs engineering FFT convention |
| Wrong power of $r$ in field vs potential | $\propto 1/r$ for potential, $\propto 1/r^2$ for field (point charge / mass) |
| Logarithm with no base specified | `ln` (natural) vs `log` (base 10 in some texts, natural in scientific Python — `math.log` is natural, `math.log10` is base-10) |
| Degrees vs radians in trig | Numpy `sin` takes radians; `np.deg2rad` to convert |
| Dimensionally consistent but sign-wrong | Pure dimensional check passes; need a limit check |

### 6. Algorithmic Recipe

1. **Parse**. Convert LaTeX → SymPy expression where possible (`sympy.parsing.latex.parse_latex`) for ground-truth dimensional checking. Else parse by hand into a list of additive terms and a list of multiplicative factors.
2. **Tag every symbol** with its physical dimension. If the user didn't specify, infer (E = energy, m = mass, etc.) and state the inference.
3. **Compute dimensions of each additive term** and confirm consistency.
4. **Confirm dimensionless arguments** to transcendental functions.
5. **Run 2-3 limit checks** appropriate to the expression.
6. **Run a conservation/symmetry check** appropriate to the system.
7. **Plug in canonical numbers** and verify order of magnitude.
8. **Pattern-match against the common-error catalogue.**
9. **Report**: state whether the equation passes each check, flag any failures with the specific suspected cause.

## Output Format

```markdown
## Equation Audit

**Equation (as given)**: [restate exactly]

**Inferred meaning**: [1 sentence — what this represents]

**Symbol dimensions** (state inference, ask if uncertain):
| Symbol | Dimension | Notes |
|---|---|---|

### Check 1 — Dimensional homogeneity
[walk through every additive group]
**Verdict**: ✓ PASS / ✗ FAIL [specific term that doesn't match]

### Check 2 — Limits
- $X \to 0$: [expected] vs [actual] — [verdict]
- $X \to \infty$: [expected] vs [actual] — [verdict]

### Check 3 — Conservation / symmetry
[what should be conserved; whether it is]

### Check 4 — Order of magnitude
With [canonical values], LHS ≈ [number], RHS ≈ [number] — [verdict]

### Check 5 — Common-error scan
[any patterns matched from the catalogue]

## Overall

[PASS — ready to publish | FAIL — specific issue and proposed fix | UNCERTAIN — need clarification on X]

**Suggested correction** (if FAIL): [specific change with justification]

**Suggested next checks** (if PASS but worth more confidence):
- Compute [related quantity] and verify it agrees with [known formula/value].
- Run a numerical test for a case with known answer (e.g. unit-radius hydrogen 1s state).
```

## Hard Rules

1. **Never confirm an equation without doing all four checks** (dimensions, limits, conservation, order-of-magnitude). Quick yes/no answers on equations are how reviewer comments embarrass people.
2. **State every dimensional inference explicitly.** "I assumed $E$ has units of energy" — the user should be able to correct silently-wrong inferences.
3. **If you're not sure, say so.** "This passes dimensional homogeneity, but I cannot verify the numerical prefactor without seeing the derivation step that produced it" is acceptable. Hallucinating a confirmation is not.
4. **Use SymPy or `pint` for any non-trivial dimensional bookkeeping.** Do not rely on mental arithmetic for chains of 6+ symbols.
5. **When the equation is from a paper or textbook**, cite the canonical form for comparison.
6. **Do not "fix" the user's equation silently.** Point out the issue, propose the correction, explain the reasoning, let them decide.

## Integration with Knowledge Graph

Leans on:
- [[Dimensional Analysis as a Debugging Tool]] for the underlying methodology and worked examples.
- [[Scientific Python Stack 2026]] for tooling (`sympy`, `pint`, `astropy.units`).

After a non-trivial audit, if you uncovered a pattern worth keeping (a textbook had a typo; a non-obvious limit check exposed an error), write a brief KG node under `knowledge/concepts/equation-<topic>.md`.

## Examples of What This Skill Catches

- **Missing $\hbar$**: User wrote $E = p^2/(2m)$ for a quantum problem expecting $E$ in joules but $p$ in some natural unit — order-of-magnitude check fails by $\sim 10^{34}$.
- **Factor of $4\pi$**: User mixed Gaussian and SI in the same expression; field looks too large by exactly $4\pi$.
- **Sign in propagator**: User's $\exp(-i\omega t)$ should have been $\exp(+i\omega t)$ for the convention their software uses; phase comes out conjugated. Dimensional check would not catch this — limit check (`t \to -t` reverses propagation) does.
- **Statistical mechanics drift**: $\langle E \rangle$ formula divided by $N$ when it should have been per mole — off by Avogadro, fix is straightforward once detected.

## Success Criteria

- Every additive term shown dimensionally.
- At least two non-dimensional checks (limit, symmetry, or order-of-magnitude).
- Common-error catalogue scanned.
- If failure: specific term/factor identified, specific correction proposed.
- If pass: confidence level stated, follow-up checks suggested.
