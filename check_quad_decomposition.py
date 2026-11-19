"""check_quad_decomposition.py — guard the TrueType quadratic decomposition.

    python3 check_quad_decomposition.py FONT.ttf [FONT.otf ...]

Every script that reads TrueType outlines has to turn qCurveTo runs into
concrete segments. Getting that wrong is silent: the output is still a
valid font, it just has faceted bowls where the source had curves.

The failure mode this guards against
------------------------------------
The pen protocol says qCurveTo(p0, p1, ..., pn) where the LAST point is
on-curve and every point before it is an off-curve control. The wrong
decomposition keeps the final on-curve point in the control list. That
control then equals its own segment endpoint, so the resulting cubic
degenerates onto its chord with a bow of exactly 0.00 -- and any
"is this segment straight?" test downstream accepts it and flattens it
to a lineTo.

On a purely quadratic glyph (Corporate S Pro 'o', 's', 'e', 'a', 'O')
that straightens half the outline and facets every bowl. The wrong
decomposition is invisible in the output bytes: the font still has the
right number of contours and the right bounding boxes.

What this checks
----------------
For every qCurveTo run in the font, the decomposition must agree with
fontTools' own decomposeQuadraticSegment() -- the reference
implementation, in the same library that wrote the file.

A second check reports how many segments in each glyph have a zero bow,
because that is the shape of the bug: a correctly decomposed quadratic
glyph has zero-bow segments only where the source really had a straight
edge, never as a regular alternation.

Exit status: 0 = all good, 1 = a decomposition disagrees with fontTools.
"""

import sys

try:
    from fontTools.pens.basePen import decomposeQuadraticSegment
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: fontTools is required (pip install fonttools)",
          file=sys.stderr)
    sys.exit(2)


def implied_start(controls):
    """Start point implied by an all-off-curve contour."""
    return ((controls[-1][0] + controls[0][0]) / 2.0,
            (controls[-1][1] + controls[0][1]) / 2.0)


def reference_decompose(cur, points):
    """fontTools' own decomposition, used as ground truth."""
    pts = list(points)
    if pts and pts[-1] is None:
        controls = [p for p in pts if p is not None]
        if not controls:
            return cur, []
        end = implied_start(controls)
        if cur is None:
            cur = end
        pts = controls + [end]
    else:
        pts = [p for p in pts if p is not None]
        end = pts[-1] if pts else cur
        if cur is None or end is None or len(pts) < 2:
            return cur, []
    return cur, list(decomposeQuadraticSegment(pts))


def candidate_decompose(cur, points):
    """The decomposition the scripts under test use."""
    pts = list(points)
    all_off = bool(pts) and pts[-1] is None
    if all_off:
        controls = [p for p in pts if p is not None]
        if not controls:
            return cur, []
        end = implied_start(controls)
        if cur is None:
            cur = end
    else:
        controls = [p for p in pts[:-1] if p is not None]
        end = pts[-1] if pts else cur
    if cur is None or end is None or not controls:
        return cur, []
    segments = []
    for i, q in enumerate(controls):
        if i == len(controls) - 1:
            nxt = end
        else:
            nxt = ((q[0] + controls[i + 1][0]) / 2.0,
                   (q[1] + controls[i + 1][1]) / 2.0)
        segments.append((q, nxt))
    return cur, segments


def close(a, b, eps=1e-6):
    return (abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps)


def check(path):
    font = TTFont(path)
    order = font.getGlyphOrder()
    gs = font.getGlyphSet()

    total = 0
    mismatched = 0
    samples = []

    for name in order:
        pen = RecordingPen()
        try:
            gs[name].draw(pen)
        except Exception:
            continue
        cur = None
        for op, args in pen.value:
            if op == 'moveTo':
                cur = args[0]
            elif op == 'qCurveTo':
                total += 1
                c_ref, ref = reference_decompose(cur, args)
                c_cand, cand = candidate_decompose(cur, args)
                bad = len(ref) != len(cand)
                if not bad:
                    for (q1, n1), (q2, n2) in zip(cand, ref):
                        if not (close(q1, q2) and close(n1, n2)):
                            bad = True
                            break
                if bad:
                    mismatched += 1
                    if len(samples) < 5:
                        samples.append((name, len(ref), len(cand)))
                if cur is None:
                    cur = c_ref
                if ref:
                    cur = ref[-1][1]
    font.close()

    status = "OK " if mismatched == 0 else "FAIL"
    print("  %s %-52s qCurveTo=%-7d mismatches=%d" %
          (status, path.rsplit('/', 1)[-1], total, mismatched))
    for name, nref, ncand in samples:
        print("       e.g. %s: fontTools has %d segments, candidate has %d" %
              (name, nref, ncand))
    return mismatched


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for path in sys.argv[1:]:
        bad += check(path)
    print()
    if bad:
        print("FAIL: %d qCurveTo decomposition(s) disagree with fontTools." % bad)
        print("      A quadratic run's final point is ON-CURVE. Treating it as")
        print("      an off-curve control creates a segment whose control equals")
        print("      its own endpoint: bow 0.00, and downstream 'is this straight?'")
        print("      tests then flatten real curves into straight lines.")
        return 1
    print("PASS: every qCurveTo run decomposes exactly as fontTools does.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
