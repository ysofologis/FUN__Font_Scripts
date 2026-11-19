"""check_counter_winding.py — guard the counter winding against the source.

    python3 check_counter_winding.py SOURCE.ttf OUTPUT.otf [OUTPUT.otf ...]

A converter must not change a glyph's counter winding RELATIVE to the
source font. When it does, the counter fills in: the hole disappears and
the glyph renders solid.

What is and is not invariant
-----------------------------
The absolute winding sign is NOT an invariant. FontForge flips every
contour of every glyph on the way out of TrueType into CFF, so a glyph
whose source contours are all negative comes out all positive -- and
that is fine, because the holes are still holes.

What IS invariant is the RELATIONSHIP between a contour and the outer
contour that ENCLOSES it:

    source 'e'   outer area -573557   counter area +86551   -> opposite
    output 'e'   outer area +686405   counter area +92366   -> SAME  (broken)

When a counter winds the same way as the shape enclosing it, it is not a
hole: under the nonzero fill rule the two cancel and the counter fills
in. That is what a filled-in 'e' is.

So the check is: for every contour that is enclosed by another contour,
does it still wind opposite to its container, as it did in the source?

How this went undetected
------------------------
FontForge's own contour accounting stays correct: the areas and the
bounding boxes are unchanged, only the relationship flips. Contour
count, bounding box, advance width and counter ratio are all identical
for a correct output and a broken one. The only reliable signals are
the containment-based winding relationship and the rendered pixels.

Note: glyphs whose set of enclosed contours changed are reported
separately as 'recount' -- the converter may legitimately merge or split
contours (that is what removeOverlap does), and a change there is not a
winding fault, but it also means the per-contour comparison could not be
made for that glyph.

Exit status: 0 = no winding faults, 1 = at least one glyph's counter
winding was flipped relative to the source.
"""

import sys

try:
    from fontTools.pens.areaPen import AreaPen
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: fontTools is required (pip install fonttools)",
          file=sys.stderr)
    sys.exit(2)


def contours_of(glyph_set, name):
    """Per-contour (signed area, bbox) for one glyph."""
    pen = RecordingPen()
    try:
        glyph_set[name].draw(pen)
    except Exception:
        return None
    out = []
    cur = None
    for op, args in pen.value:
        if op == 'moveTo':
            cur = [(op, args)]
            out.append(cur)
        elif cur is not None:
            cur.append((op, args))
            if op in ('closePath', 'endPath'):
                cur = None
    result = []
    for c in out:
        ap = AreaPen(None)
        xs = []
        ys = []
        for op, args in c:
            try:
                getattr(ap, op)(*args)
            except Exception:
                pass
            if op == 'moveTo':
                xs.append(args[0][0]); ys.append(args[0][1])
            elif op == 'lineTo':
                xs.append(args[0][0]); ys.append(args[0][1])
            elif op == 'curveTo':
                for p in args:
                    xs.append(p[0]); ys.append(p[1])
            elif op == 'qCurveTo':
                for p in args:
                    if p is not None:
                        xs.append(p[0]); ys.append(p[1])
        bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        result.append((ap.value, bbox))
    return result


def contains(outer, inner):
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def relationships(contours):
    """{contour index: (area, bbox, is_opposite_to_its_container)}."""
    real = [i for i, (a, b) in enumerate(contours) if abs(a) > 1.0]
    out = {}
    for i in real:
        area, box = contours[i]
        if box is None:
            continue
        best = None
        for j in real:
            if j == i:
                continue
            jbox = contours[j][1]
            if jbox is None or not contains(jbox, box):
                continue
            if best is None or abs(contours[j][0]) > abs(contours[best][0]):
                best = j
        if best is None:
            continue          # an outer shape: not a counter
        out[i] = (area, box, (area > 0) == (contours[best][0] > 0))
    return out


def check(src_path, out_path, verbose=True):
    src = TTFont(src_path)
    out = TTFont(out_path)
    src_gs = src.getGlyphSet()
    out_gs = out.getGlyphSet()

    compared = 0
    flipped = []
    recount = []

    for g in src.getGlyphOrder():
        if g not in out.getGlyphOrder():
            continue
        sc = contours_of(src_gs, g)
        oc = contours_of(out_gs, g)
        if not sc or not oc:
            continue
        s_rel = relationships(sc)
        o_rel = relationships(oc)
        if len(s_rel) != len(o_rel):
            recount.append((g, s_rel, o_rel))
            continue
        compared += 1
        # Match the source's counters to the output's by GEOMETRY (the
        # winding is what is being checked, so it cannot be the key).
        bad = []
        for i, (area, box, rel) in o_rel.items():
            best = None
            bestd = None
            for k, (sarea, sbox, srel) in s_rel.items():
                d = ((sbox[0] - box[0]) ** 2 + (sbox[1] - box[1]) ** 2
                     + (sbox[2] - box[2]) ** 2 + (sbox[3] - box[3]) ** 2)
                if bestd is None or d < bestd:
                    bestd, best = d, k
            if best is None:
                continue
            if s_rel[best][2] != rel:
                bad.append((g, box, rel, s_rel[best][2]))
        if bad:
            flipped.append(bad[0])

    src.close()
    out.close()

    status = 'OK  ' if not flipped else 'FAIL'
    print('  %s %-46s counters=%4d flipped=%3d  recount=%3d' %
          (status, out_path.rsplit('/', 1)[-1], compared, len(flipped),
           len(recount)))
    if verbose:
        for g, box, rel, srel in flipped[:10]:
            print('       %-18s counter bbox=%s  output_rel=%s '
                  'source_rel=%s' % (g, tuple(round(v) for v in box),
                                     'opposite' if not rel else 'SAME',
                                     'opposite' if not srel else 'SAME'))
    return len(flipped)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src_path = sys.argv[1]
    bad = 0
    for out_path in sys.argv[2:]:
        bad += check(src_path, out_path)
    print()
    if bad:
        print('FAIL: %d glyph(s) have a counter that no longer winds '
              'opposite to the shape enclosing it.' % bad)
        print('      Such a counter is not a hole: under the nonzero fill')
        print('      rule it cancels the shape around it and fills in.')
        print('      FontForge 20251009\'s glyph.correctDirection() can')
        print('      cause this once changeWeight() has thickened the')
        print('      strokes.')
        return 1
    print('PASS: every counter still winds opposite to its container.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
