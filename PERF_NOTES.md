# Performance notes for `otf_optimize-ff-v2.0.py`

Measured on the same machine, FontForge 20251009, Python 3.14,
12 worker processes (`--jobs` default = `os.cpu_count() = 12`).

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
