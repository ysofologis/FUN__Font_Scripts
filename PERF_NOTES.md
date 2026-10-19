# Performance notes for `otf_optimize-ff-v2.0.py` and `otf_optimize-ff-v2.1.py`

Measured on the same machine, FontForge 20251009, Python 3.14,
12 worker processes (`--jobs` default = `os.cpu_count() = 12`).

## v2.0 vs v2.1 on a single heavy face (no-op controlled)

Single face, `--width 5 --height 2 --thickness 5 --hint none`,
`SF-Compact-Text-Regular.otf` (35,135 glyphs), no parallelism
(`-j 1` to time per-face work in isolation):

| script | wall time | output bytes          | stderr lines |
| ------ | --------- | --------------------- | ------------ |
| v2.0   | 316 s     | 25,828,680            | 55           |
| v2.1   | 314 s     | 25,828,680 (identical) | 47           |

v2.1 is byte-identical to v2.0 on this workload (same 25,828,680 B) and
emits 15% fewer stderr lines. The 2-second wall-time difference is well
within run-to-run noise; **v2.1 is not faster on the heavy path**. It
is a hygiene fix, not a perf fix.

The v2.0 wall time is dominated by `glyph.changeWeight()` in libfontforge,
a third-party C-level stroke-and-union. There is no Python-side change
that can make that faster.

## Measured workloads (SF-Compact, 38 faces)

| flags                                 | wall time | output           | throughput            |
| ------------------------------------- | --------- | ---------------- | --------------------- |
| no-op copy (no flags)                 | ~50 s     | 38/38            | ~0.76 s/face effective |
| `--thickness 5 --hint auto`           | >12 min   | 12/38 + timeouts | ~6 s/face effective    |
| `--thickness 5 --hint none` (live)    | TBD       | TBD              | heavy `changeWeight`   |

Per-face timeout is `OTF_PER_FONT_TIMEOUT` (default 600 s). Faces whose
worker crashes inside libfontforge trip that timeout and are reported
as failures; the rest of the run continues.

## Why v2.0 is already at the design ceiling for FontForge

- **Parallelism** — `multiprocessing.Pool` with `apply_async`, 12 workers.
- **No-op fast path** — when all of `--width/--height/--thickness/--spacing` are 0 and `--quantize-curve` is 0, the script copies the input through. 50 s for 38 faces is the floor.
- **Per-face timeout** — robust to libfontforge crashes; the pool is not blocked.
- **Cross-font GPOS scan** — runs once in the parent before the pool spins up. The per-face re-scan inside the worker only exists to log the GPOS warning and is cheap.

## Where the heavy-path time actually goes

`--thickness N` is the only flag whose cost is not bounded by the script.
The work is `glyph.changeWeight(delta)` per glyph, called inside
FontForge's own libfontforge. On a 35k-glyph face this is real work
performed by a third-party library; there is no Python-side
optimization that would change it.

Skip-on-empty-glyph saves 0.1% of calls on SF-Compact
(20/35135 — verified); not worth the code path.

## When to choose the Skia path instead

For the `--scale` + `--thickness` workload specifically, the
`ttf2otf_ft_v6.py` converter in this repo uses Skia for the
geometry transform and a different code path for the embolden;
on large CJK / multilingual faces it is substantially faster than
`changeWeight`. The output geometry is not identical to
`otf_optimize-ff-v2.0.py` (different rounding, different
miter-limit behaviour), so they are not drop-in alternatives;
verify the visual output before substituting.
