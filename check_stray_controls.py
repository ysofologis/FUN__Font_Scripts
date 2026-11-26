"""check_stray_controls.py — guard against control points on collapsed segments.

    python3 check_stray_controls.py FONT.ttf FONT.otf [FONT.otf ...]

FontForge 20251009's changeWeight() and simplify() can leave a control
point hundreds of units outside the glyph, while the two on-curve points
either side of it sit a unit apart. The control then drags the curve out
of the shape: a combining circumflex comes out 664 units tall instead of
157.

How it is detected
------------------
By the two conditions together:

  * the control is far from its neighbours (more than 100 units)
  * the segment it controls has collapsed (under 5 units)

Neither alone is enough. The absolute distance alone matches perfectly
ordinary controls on a big glyph -- Corporate S Pro's 'Ccommaaccent' has
controls 2075 units from their neighbours. The ratio alone is also not
enough: legitimate ratios reach 74.9 in PFBeauSansPro's
'ozonefriendly'.

Measured over 1,647,279 control points in five families (Amazon Ember,
Sofia Sans, Corporate S Pro, PFBeauSansPro, Absolut Pro), the two
conditions together match nothing at all. The strays FontForge leaves
measure 313-352 units against a segment of 1.4 units.

Exit status: 0 = no stray controls, 1 = at least one glyph has one.
"""

import math
import sys

try:
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: fontTools is required (pip install fonttools)",
          file=sys.stderr)
    sys.exit(2)

MIN_DISTANCE = 100.0
MAX_SEGMENT = 5.0


def sequence(ops):
    """Rebuild a contour's (point, on_curve) sequence from a pen recording."""
    seq = []
    for op, a in ops:
        if op == 'moveTo':
            seq.append((a[0], True))
        elif op == 'lineTo':
            seq.append((a[0], True))
        elif op == 'curveTo':
            seq.append((a[0], False))
            seq.append((a[1], False))
            seq.append((a[2], True))
        elif op == 'qCurveTo':
            for q in a:
                if q is not None:
                    seq.append((q, False))
    return seq


def strays(ops, min_distance=MIN_DISTANCE, max_segment=MAX_SEGMENT):
    """(index, distance, segment) for each control on a collapsed segment."""
    seq = sequence(ops)
    n = len(seq)
    out = []
    for i, (pt, on) in enumerate(seq):
        if on:
            continue
        prev = nxt = None
        for j in range(i - 1, -1, -1):
            if seq[j][1]:
                prev = seq[j][0]
                break
        for j in range(i + 1, n):
            if seq[j][1]:
                nxt = seq[j][0]
                break
        if prev is None or nxt is None:
            continue
        d = max(math.hypot(prev[0] - pt[0], prev[1] - pt[1]),
                math.hypot(nxt[0] - pt[0], nxt[1] - pt[1]))
        seg = math.hypot(prev[0] - nxt[0], prev[1] - nxt[1])
        if d > min_distance and seg < max_segment:
            out.append((i, d, seg))
    return out


def check(path, min_distance=MIN_DISTANCE, max_segment=MAX_SEGMENT):
    font = TTFont(path)
    glyph_set = font.getGlyphSet()
    hits = []
    for name in font.getGlyphOrder():
        pen = RecordingPen()
        try:
            glyph_set[name].draw(pen)
        except Exception:
            continue
        s = strays(pen.value, min_distance, max_segment)
        if s:
            hits.append((name, s))
    font.close()

    status = 'OK  ' if not hits else 'FAIL'
    print('  %s %-48s glyphs=%-6d stray=%d' %
          (status, path.rsplit('/', 1)[-1], len(font.getGlyphOrder()),
           len(hits)))
    for name, s in hits[:8]:
        print('       %-22s %s' % (name, [(i, round(d), round(sg))
                                        for i, d, sg in s[:4]]))
    return len(hits)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for path in sys.argv[1:]:
        bad += check(path)
    print()
    if bad:
        print('FAIL: %d glyph(s) carry a control point on a collapsed '
              'segment.' % bad)
        print('      Such a control drags its curve out of the glyph. The')
        print("      glyph's contour count, point count and advance width")
        print('      are unchanged, so only the rendered shape shows it.')
        print("      FontForge 20251009's changeWeight() and simplify() are")
        print('      the cause.')
        return 1
    print('PASS: no control point sits on a collapsed segment.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
