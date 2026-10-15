"""Python port of the fish-generation algorithm from
https://github.com/LingDong-/fishdraw (MIT licensed, single-file JS).

Faithful (ported from verified verbatim source) where it matters for visual
parity: the xorshift32 RNG + string seeding, the Perlin noise implementation,
geometry/clipping utilities, the body spine math, the `fish_body_c` body
variant, all four fin primitives (`fin_a`/`fin_b`/`finlet`/`fin_adipose`), the
full head construction (`fish_head` and its lip/teeth/jaw/barbel helpers), and
`binomen()`'s exact word-fragment/weight tables.

Ported faithfully from upstream source: the RNG/noise, geometry and clipping
utilities, `squama`/`squama_mask`/`squama_mesh` (scale meshes),
`shade_shape`/`fill_shape`/`patternshade_shape` (hatching), `pattern_dot`,
both eye variants, the fin primitives, the head construction, and `binomen()`.

Known deviations, all cosmetic:
  - `squama_mesh` is called with interclip off (scales may overlap very
    slightly rather than trimming each other via `poly_union`).
  - `vein_shape` / `smalldot_shape` are approximations in the same spirit as
    the originals (fine irregular filaments / stipple), not ports - upstream
    source for them wasn't obtainable.
  - `fish_body_a`/`b`/`d` wire the verbatim mesh/texture helpers together
    from upstream's described behaviour rather than byte-exact source, and
    the per-fin angle/curvature constants in `fish()` are reconstructions
    around its (verbatim) width-profile formulas.
"""

import logging
import math
import re
import time
import random as _pyrandom

logger = logging.getLogger(__name__)

PI = math.pi

# --------------------------------------------------------------------- RNG --
_jsr = 0x5EED
_perlin = None


def _to_i32(n):
    n &= 0xFFFFFFFF
    if n >= 0x80000000:
        n -= 0x100000000
    return n


def _xorshift32(n):
    n = _to_i32(n ^ ((n << 17) & 0xFFFFFFFF))
    n = _to_i32(n ^ (n >> 13))
    n = _to_i32(n ^ ((n << 5) & 0xFFFFFFFF))
    return n


def rand():
    global _jsr
    _jsr = _xorshift32(_jsr)
    return (_jsr & 0xFFFFFFFF) / 4294967295.0


def str_to_seed(s):
    global _jsr
    n = 0
    for i, ch in enumerate(s):
        n = _to_i32(n ^ (((ord(ch) + 1) << (7 + (i % 5))) & 0xFFFFFFFF))
    n = _xorshift32(n)
    _jsr = _to_i32((n & 0xFFFFFFFF) % 4294967295)


# ------------------------------------------------------------------- noise --
_PERLIN_YWRAPB = 4
_PERLIN_ZWRAPB = 8
_PERLIN_SIZE = 4095


def _scaled_cosine(i):
    return 0.5 * (1 - math.cos(i * PI))


def noise(x, y=0.0, z=0.0):
    global _perlin
    if _perlin is None:
        _perlin = [rand() for _ in range(_PERLIN_SIZE + 1)]
    if x < 0:
        x = -x
    if y < 0:
        y = -y
    if z < 0:
        z = -z
    xi = int(x)
    yi = int(y)
    zi = int(z)
    xf = x - xi
    yf = y - yi
    zf = z - zi
    r = 0.0
    ampl = 0.5
    for _ in range(4):
        of = xi + (yi << _PERLIN_YWRAPB) + (zi << _PERLIN_ZWRAPB)
        rxf = _scaled_cosine(xf)
        ryf = _scaled_cosine(yf)
        n1 = _perlin[of & _PERLIN_SIZE]
        n1 += rxf * (_perlin[(of + 1) & _PERLIN_SIZE] - n1)
        n2 = _perlin[(of + (1 << _PERLIN_YWRAPB)) & _PERLIN_SIZE]
        n2 += rxf * (_perlin[(of + (1 << _PERLIN_YWRAPB) + 1) & _PERLIN_SIZE] - n2)
        n1 += ryf * (n2 - n1)
        of += 1 << _PERLIN_ZWRAPB
        n2 = _perlin[of & _PERLIN_SIZE]
        n2 += rxf * (_perlin[(of + 1) & _PERLIN_SIZE] - n2)
        n3 = _perlin[(of + (1 << _PERLIN_YWRAPB)) & _PERLIN_SIZE]
        n3 += rxf * (_perlin[(of + (1 << _PERLIN_YWRAPB) + 1) & _PERLIN_SIZE] - n3)
        n2 += ryf * (n3 - n2)
        n1 += _scaled_cosine(zf) * (n2 - n1)
        r += n1 * ampl
        ampl *= 0.5
        xi <<= 1
        xf *= 2
        if xf >= 1:
            xi += 1
            xf -= 1
        yi <<= 1
        yf *= 2
        if yf >= 1:
            yi += 1
            yf -= 1
        zi <<= 1
        zf *= 2
        if zf >= 1:
            zi += 1
            zf -= 1
    return r


# --------------------------------------------------------------------- math --
def dist(x0, y0, x1, y1):
    return math.hypot(x1 - x0, y1 - y0)


def lerp(a, b, t):
    return a * (1 - t) + b * t


def lerp2d(x0, y0, x1, y1, t):
    return [lerp(x0, x1, t), lerp(y0, y1, t)]


def get_bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    return {"x": xmin, "y": ymin, "w": xmax - xmin, "h": ymax - ymin}


def distsq(x0, y0, x1, y1):
    dx = x0 - x1
    dy = y0 - y1
    return dx * dx + dy * dy


def bean(x):
    return math.sqrt(max(0.0, 0.25 - (x - 0.5) ** 2)) * (2.6 + 2.4 * x ** 1.5) * 0.542


def trsl_poly(poly, x, y):
    return [[p[0] + x, p[1] + y] for p in poly]


# -------------------------------------------------------- geometry/clipping --
def pt_in_pl(x, y, x0, y0, x1, y1):
    dx = x1 - x0
    dy = y1 - y0
    return (x - x0) * dy - (y - y0) * dx


def seg_isect(p0x, p0y, p1x, p1y, q0x, q0y, q1x, q1y, is_ray=False):
    d0x = p1x - p0x
    d0y = p1y - p0y
    d1x = q1x - q0x
    d1y = q1y - q0y
    vc = d0x * d1y - d0y * d1x
    if vc == 0:
        return None
    vcn = vc * vc
    q0x_p0x = q0x - p0x
    q0y_p0y = q0y - p0y
    vc_vcn = vc / vcn
    t = (q0x_p0x * d1y - q0y_p0y * d1x) * vc_vcn
    s = (q0x_p0x * d0y - q0y_p0y * d0x) * vc_vcn
    if 0 <= t and (is_ray or t < 1) and 0 <= s < 1:
        xy = [p1x * t + p0x * (1 - t), p1y * t + p0y * (1 - t)]
        side = 1 if pt_in_pl(p0x, p0y, p1x, p1y, q0x, q0y) < 0 else -1
        return {"t": t, "s": s, "side": side, "xy": xy}
    return None


def seg_isect_poly(x0, y0, x1, y1, poly, is_ray=False):
    n = len(poly)
    isects = []
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        xx = seg_isect(x0, y0, x1, y1, a[0], a[1], b[0], b[1], is_ray)
        if xx:
            isects.append(xx)
    isects.sort(key=lambda r: r["t"])
    return isects


def _build_edges(poly):
    """Edge list with per-edge bboxes, so segment tests can reject most edges
    with four comparisons instead of running the full intersection maths. The
    scale meshes clip thousands of polylines against the body outline, and
    without this the clipping dominates generation time."""
    n = len(poly)
    edges = []
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        edges.append((
            a[0], a[1], b[0], b[1],
            a[0] if a[0] < b[0] else b[0], a[1] if a[1] < b[1] else b[1],
            a[0] if a[0] > b[0] else b[0], a[1] if a[1] > b[1] else b[1],
        ))
    return edges


def _isects_seg(x0, y0, x1, y1, edges):
    sminx = x0 if x0 < x1 else x1
    smaxx = x0 if x0 > x1 else x1
    sminy = y0 if y0 < y1 else y1
    smaxy = y0 if y0 > y1 else y1
    out = []
    for ax, ay, bx, by, eminx, eminy, emaxx, emaxy in edges:
        if eminx > smaxx or emaxx < sminx or eminy > smaxy or emaxy < sminy:
            continue
        xx = seg_isect(x0, y0, x1, y1, ax, ay, bx, by, False)
        if xx:
            out.append(xx)
    out.sort(key=lambda r: r["t"])
    return out


def _point_in_poly(x, y, edges):
    """Even-odd containment via a horizontal crossing count. The original
    casts a ray at an arbitrary angle and counts seg_isect hits; this is
    equivalent but inline arithmetic rather than a call per edge, which
    matters because the scale meshes run this tens of thousands of times."""
    inside = False
    for ax, ay, bx, by, _a, _b, _c, _d in edges:
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def clip(polyline, polygon, edges=None):
    if not polyline:
        return {True: [], False: []}
    if edges is None:
        edges = _build_edges(polygon)
    p0 = polyline[0]
    zero = _point_in_poly(p0[0], p0[1], edges)
    out = {True: [[]], False: [[]]}
    io = zero
    for i in range(len(polyline)):
        a = polyline[i]
        b = polyline[i + 1] if i + 1 < len(polyline) else None
        out[io][-1].append(a)
        if b is None:
            break
        for isx in _isects_seg(a[0], a[1], b[0], b[1], edges):
            out[io][-1].append(isx["xy"])
            io = not io
            out[io].append([isx["xy"]])
    out[True] = [x for x in out[True] if x]
    out[False] = [x for x in out[False] if x]
    return out


def binclip(polyline, func):
    if not polyline:
        return {True: [], False: []}
    n = len(polyline)
    bins = [func(polyline[i][0], polyline[i][1], i / (n - 1) if n > 1 else 0.0) for i in range(n)]
    out = {True: [[]], False: [[]]}
    io = bins[0]
    for i in range(n):
        a = polyline[i]
        b = polyline[i + 1] if i + 1 < n else None
        out[io][-1].append(a)
        if b is None:
            break
        if bins[i] != bins[i + 1]:
            pt = lerp2d(a[0], a[1], b[0], b[1], 0.5)
            out[io][-1].append(pt)
            io = not io
            out[io].append([pt])
    out[True] = [x for x in out[True] if x]
    out[False] = [x for x in out[False] if x]
    return out


def clip_multi(polylines, polygon, clipper_func=clip):
    out = {True: [], False: []}
    # build the polygon's edge list once for the whole batch
    edges = _build_edges(polygon) if clipper_func is clip else None
    for pl in polylines:
        c = clipper_func(pl, polygon, edges) if edges is not None else clipper_func(pl, polygon)
        out[True].extend(c[True])
        out[False].extend(c[False])
    return out


def pt_seg_dist(p, p0, p1):
    x, y = p
    x1, y1 = p0
    x2, y2 = p1
    a = x - x1
    b = y - y1
    c = x2 - x1
    d = y2 - y1
    dot = a * c + b * d
    len_sq = c * c + d * d
    param = dot / len_sq if len_sq != 0 else -1
    if param < 0:
        xx, yy = x1, y1
    elif param > 1:
        xx, yy = x2, y2
    else:
        xx = x1 + param * c
        yy = y1 + param * d
    dx = x - xx
    dy = y - yy
    return math.sqrt(dx * dx + dy * dy)


def approx_poly_dp(polyline, epsilon):
    if len(polyline) <= 2:
        return polyline
    dmax = 0
    argmax = -1
    for i in range(1, len(polyline) - 1):
        d = pt_seg_dist(polyline[i], polyline[0], polyline[-1])
        if d > dmax:
            dmax = d
            argmax = i
    if dmax > epsilon:
        left = approx_poly_dp(polyline[: argmax + 1], epsilon)
        right = approx_poly_dp(polyline[argmax:], epsilon)
        return left[:-1] + right
    return [list(polyline[0]), list(polyline[-1])]


def isect_circ_line(cx, cy, r, x0, y0, x1, y1):
    dx = x1 - x0
    dy = y1 - y0
    fx = x0 - cx
    fy = y0 - cy
    a = dx * dx + dy * dy
    b = 2 * (fx * dx + fy * dy)
    c = (fx * fx + fy * fy) - r * r
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    disc = math.sqrt(disc)
    t0 = (-b - disc) / (2 * a)
    if 0 <= t0 <= 1:
        return t0
    t = (-b + disc) / (2 * a)
    if t > 1 or t < 0:
        return None
    return t


def resample(polyline, step):
    if len(polyline) < 2:
        return [list(p) for p in polyline]
    polyline = [list(p) for p in polyline]
    out = [list(polyline[0])]
    i = 0
    while i < len(polyline) - 1:
        a = polyline[i]
        b = polyline[i + 1]
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        d = math.sqrt(dx * dx + dy * dy)
        if d == 0:
            i += 1
            continue
        n = int(d / step)
        rest = (n * step) / d
        rpx = a[0] * (1 - rest) + b[0] * rest
        rpy = a[1] * (1 - rest) + b[1] * rest
        for j in range(1, n + 1):
            t = j / n
            x = a[0] * (1 - t) + rpx * t
            y = a[1] * (1 - t) + rpy * t
            out.append([x, y])
        nxt = None
        for j in range(i + 2, len(polyline)):
            bb = polyline[j - 1]
            cc = polyline[j]
            if bb[0] == cc[0] and bb[1] == cc[1]:
                continue
            t = isect_circ_line(rpx, rpy, step, bb[0], bb[1], cc[0], cc[1])
            if t is None:
                continue
            q = [bb[0] * (1 - t) + cc[0] * t, bb[1] * (1 - t) + cc[1] * t]
            out.append(q)
            polyline[j - 1] = q
            nxt = j - 1
            break
        if nxt is None:
            break
        i = nxt
    if len(out) > 1:
        lx, ly = out[-1]
        mx, my = polyline[-1]
        d = math.sqrt((mx - lx) ** 2 + (my - ly) ** 2)
        if d < step * 0.5:
            out.pop()
    out.append(list(polyline[-1]))
    return out


# ---------------------------------------------------------------- textures --
def signed_pow(a, b):
    """The original shadows Math.pow with a sign-preserving version; the body
    and head curves rely on it for negative inputs."""
    if a == 0:
        return 0.0
    return math.copysign(abs(a) ** b, a)


def gauss2d(x, y):
    return math.exp(-0.5 * x * x) * math.exp(-0.5 * y * y)


def scl_poly(poly, sx, sy=None):
    if sy is None:
        sy = sx
    return [[p[0] * sx, p[1] * sy] for p in poly]


def shr_poly(poly, sx):
    return [[p[0] + p[1] * sx, p[1]] for p in poly]


def rot_poly(poly, th):
    c, s = math.cos(th), math.sin(th)
    return [[p[0] * c - p[1] * s, p[0] * s + p[1] * c] for p in poly]


def squama_mask(w, h):
    n = 7
    out = []
    for i in range(n):
        a = (i / n) * PI * 2
        out.append([-signed_pow(math.cos(a), 1.3) * w, signed_pow(math.sin(a), 1.3) * h])
    return out


def squama(w, h, m=3):
    n = 8
    p = []
    for i in range(n):
        a = (i / (n - 1)) * PI + PI / 2
        p.append([-signed_pow(math.cos(a), 1.4) * w, signed_pow(math.sin(a), 1.4) * h])
    q = [p]
    if m >= 2:
        for i in range(m):
            t = i / (m - 1)
            q.append([
                [-w * 0.3 + (rand() - 0.5), -h * 0.2 + t * h * 0.4 + (rand() - 0.5)],
                [w * 0.5 + (rand() - 0.5), -h * 0.3 + t * h * 0.6 + (rand() - 0.5)],
            ])
    return q


def squama_mesh(m, n, uw, uh, squama_func, noise_x, noise_y, interclip=False):
    """Lays out the scale grid. interclip (mutual scale clipping via
    poly_union) is not implemented - scales may overlap very slightly instead
    of trimming each other, which is not noticeable at 1-bit/800x480."""
    pts = []
    for i in range(n):
        for j in range(m):
            x = j * uw
            y = (n * uh / 2) - math.cos(i / (n - 1) * PI) * (n * uh / 2)
            a = noise(x * 0.005, y * 0.005) * PI * 2 - PI
            r = noise(x * 0.005, y * 0.005)
            pts.append([x + math.cos(a) * r * noise_x, y + math.sin(a) * r * noise_y])

    whs = []
    for i in range(n):
        for j in range(m):
            if i == 0 or j == 0 or i == n - 1 or j == m - 1:
                whs.append([uw / 2, uh / 2])
                continue
            a = pts[i * m + j]
            b = pts[i * m + j + 1]
            c = pts[i * m + j - 1]
            d = pts[(i - 1) * m + j]
            e = pts[(i + 1) * m + j]
            dw = (dist(a[0], a[1], b[0], b[1]) + dist(a[0], a[1], c[0], c[1])) / 4
            dh = (dist(a[0], a[1], d[0], d[1]) + dist(a[0], a[1], e[0], e[1])) / 4
            whs.append([dw, dh])

    out = []
    for j in range(1, m - 1):
        for i in range(1, n - 1):
            x, y = pts[i * m + j]
            dw, dh = whs[i * m + j]
            out.extend(trsl_poly(a, x, y) for a in squama_func(x, y, dw, dh))
        for i in range(1, n - 1):
            a = pts[i * m + j]
            b = pts[i * m + j + 1]
            c = pts[(i + 1) * m + j]
            d = pts[(i + 1) * m + j + 1]
            dwa, dha = whs[i * m + j]
            dwb, dhb = whs[i * m + j + 1]
            dwc, dhc = whs[(i + 1) * m + j]
            dwd, dhd = whs[(i + 1) * m + j + 1]
            x = (a[0] + b[0] + c[0] + d[0]) / 4
            y = (a[1] + b[1] + c[1] + d[1]) / 4
            dw = (dwa + dwb + dwc + dwd) / 4 * 1.2
            dh = (dha + dhb + dhc + dhd) / 4
            out.extend(trsl_poly(p, x, y) for p in squama_func(x, y, dw, dh))
    return out


def shade_shape(poly, step=5, dx=10, dy=20):
    """Directional hatching inside poly, carved back by an offset copy of
    itself so the shading reads as a lit form rather than flat fill."""
    bbox = get_bbox(poly)
    bx = bbox["x"] - step
    by = bbox["y"] - step
    bw = bbox["w"] + step * 2
    bh = bbox["h"] + step * 2
    lines = []
    i = -bh
    while i < bw:
        lines.append([[bx + i, by], [bx + i + bh, by + bh]])
        i += step
    lines = clip_multi(lines, poly)[True]
    lines = clip_multi(lines, trsl_poly(poly, -dx, -dy))[False]
    for i in range(len(lines)):
        if len(lines[i]) < 2:
            continue
        a, b = lines[i][0], lines[i][-1]
        s = rand() * 0.5
        if dy > 0:
            lines[i][0] = lerp2d(a[0], a[1], b[0], b[1], s)
        else:
            lines[i][-1] = lerp2d(b[0], b[1], a[0], a[1], s)
    return lines


def fill_shape(poly, step=5):
    bbox = get_bbox(poly)
    bx = bbox["x"] - step
    by = bbox["y"] - step
    bw = bbox["w"] + step * 2
    bh = bbox["h"] + step * 2
    lines = []
    i = 0
    while i < bw + bh / 2:
        lines.append([[bx + i, by], [bx + i - bh / 2, by + bh]])
        i += step
    return clip_multi(lines, poly)[True]


def patternshade_shape(poly, step=5, pattern_func=None):
    bbox = get_bbox(poly)
    bx = bbox["x"] - step
    by = bbox["y"] - step
    bw = bbox["w"] + step * 2
    bh = bbox["h"] + step * 2
    lines = []
    i = -bh / 2
    while i < bw:
        lines.append([[bx + i, by], [bx + i + bh / 2, by + bh]])
        i += step
    lines = clip_multi(lines, poly)[True]
    lines = [resample(l, 2) for l in lines]
    if pattern_func is None:
        return lines
    return clip_multi(lines, pattern_func, binclip)[True]


def vein_shape(poly, n=50):
    """Approximation of the original's organic vein texture: short
    noise-steered filaments seeded inside the shape. Not a port - the
    upstream implementation wasn't available - but it fills the same role
    (fine irregular detail on the head/jaw)."""
    bbox = get_bbox(poly)
    out = []
    for _ in range(int(n)):
        x = bbox["x"] + rand() * bbox["w"]
        y = bbox["y"] + rand() * bbox["h"]
        ang = rand() * PI * 2
        pts = [[x, y]]
        for k in range(8):
            ang += (noise(x * 0.1, y * 0.1, k * 0.3) - 0.5) * 1.2
            x += math.cos(ang) * 2.2
            y += math.sin(ang) * 2.2
            pts.append([x, y])
        out.append(pts)
    return clip_multi(out, poly)[True]


def smalldot_shape(poly, scale=1):
    """Approximation of the original's stipple texture: small rings scattered
    inside the shape."""
    bbox = get_bbox(poly)
    out = []
    count = int((bbox["w"] * bbox["h"]) / (900 * max(scale, 0.2)))
    for _ in range(max(0, min(400, count))):
        cx = bbox["x"] + rand() * bbox["w"]
        cy = bbox["y"] + rand() * bbox["h"]
        r = (rand() * 1.2 + 0.6) * scale
        ring = [[cx + math.cos(k / 5 * PI * 2) * r, cy + math.sin(k / 5 * PI * 2) * r]
                for k in range(6)]
        out.append(ring)
    return clip_multi(out, poly)[True]


def poissondisk(w, h, r, samples):
    """Bridson-style blue-noise sampling, appending into `samples` like the
    original's out-parameter signature."""
    cell = r / math.sqrt(2)
    gw = int(w / cell) + 1
    gh = int(h / cell) + 1
    grid = [None] * (gw * gh)
    active = []

    def emit(p):
        samples.append(p)
        active.append(p)
        grid[int(p[1] / cell) * gw + int(p[0] / cell)] = p

    def ok(p):
        if not (0 <= p[0] < w and 0 <= p[1] < h):
            return False
        gx, gy = int(p[0] / cell), int(p[1] / cell)
        for yy in range(max(0, gy - 2), min(gh, gy + 3)):
            for xx in range(max(0, gx - 2), min(gw, gx + 3)):
                q = grid[yy * gw + xx]
                if q is not None and distsq(p[0], p[1], q[0], q[1]) < r * r:
                    return False
        return True

    emit([rand() * w, rand() * h])
    while active:
        idx = int(rand() * len(active))
        base = active[idx]
        placed = False
        for _ in range(20):
            a = rand() * PI * 2
            d = r * (1 + rand())
            p = [base[0] + math.cos(a) * d, base[1] + math.sin(a) * d]
            if ok(p):
                emit(p)
                placed = True
                break
        if not placed:
            active.pop(idx)
    return samples


def pattern_dot(scale=1):
    samples = []
    poissondisk(500, 300, 20 * scale, samples)
    rs = [(rand() * 5 + 10) * scale for _ in samples]

    # bucket the samples so a lookup only tests nearby ones - this is called
    # once per scale cell, and a linear scan over every sample would make
    # generation quadratic
    cell = max(1.0, (max(rs) if rs else 1.0))
    buckets = {}
    for i, s in enumerate(samples):
        key = (int(s[0] / cell), int(s[1] / cell))
        buckets.setdefault(key, []).append(i)

    def f(x, y, t=None):
        gx, gy = int(x / cell), int(y / cell)
        for by in (gy - 1, gy, gy + 1):
            for bx in (gx - 1, gx, gx + 1):
                for i in buckets.get((bx, by), ()):
                    s = samples[i]
                    r = rs[i]
                    if dist(x, y, s[0], s[1]) < r:
                        if gauss2d((x - s[0]) / r * 2, (y - s[1]) / r * 2) * noise(x, y, 999) > 0.2:
                            return True
        return False

    return f


# ------------------------------------------------------------------ bodies --
def _scaled_body(curve0, curve1, scale_scale, pattern_func, fine):
    """Scale-mesh body (fish_body_a / fish_body_b). The mesh is laid out over
    the body bbox then clipped to the outline."""
    outline = curve0 + list(reversed(curve1))
    bbox = get_bbox(outline)
    uw = (6.0 if fine else 10.0) * scale_scale
    uh = (5.0 if fine else 8.0) * scale_scale
    # coarsen if the grid would get so dense that clipping dominates the
    # generation time; ~2600 cells still reads as fully scaled at this size
    while (bbox["w"] / uw) * (bbox["h"] / uh) > 2600:
        uw *= 1.15
        uh *= 1.15
    m = max(4, int(bbox["w"] / uw) + 2)
    n = max(4, int(bbox["h"] / uh) + 2)

    def squama_func(x, y, dw, dh):
        gx = x + bbox["x"]
        gy = y + bbox["y"]
        detailed = pattern_func(gx, gy) if pattern_func else False
        return squama(dw, dh, 3 if detailed else (0 if fine else 2))

    mesh = squama_mesh(m, n, uw, uh, squama_func, 4, 4)
    mesh = [trsl_poly(p, bbox["x"], bbox["y"]) for p in mesh]
    return [curve0, list(reversed(curve1))] + clip_multi(mesh, outline)[True]


def fish_body_a(curve0, curve1, scale_scale, pattern_func=None):
    return _scaled_body(curve0, curve1, scale_scale, pattern_func, fine=False)


def fish_body_b(curve0, curve1, scale_scale, pattern_func=None):
    return _scaled_body(curve0, curve1, scale_scale, pattern_func, fine=True)


def fish_body_c(curve0, curve1, scale_scale):
    """Outline + a hatched/engraved body texture (no scale-mesh texture)."""
    outline = curve0 + list(reversed(curve1))
    bbox = get_bbox(outline)
    diag = math.hypot(bbox["w"], bbox["h"]) + 1
    step = max(4.0, 6.0 * scale_scale)
    lines = []
    cx = bbox["x"] + bbox["w"] / 2
    cy = bbox["y"] + bbox["h"] / 2
    n = int(diag / step) + 2
    for angle in (0.6, -0.6):
        ca, sa = math.cos(angle), math.sin(angle)
        for k in range(-n, n):
            px = cx - sa * k * step
            py = cy + ca * k * step
            a = [px - ca * diag, py - sa * diag]
            b = [px + ca * diag, py + sa * diag]
            seg = resample([a, b], 6)
            for p in seg:
                p[0] += noise(p[0] * 0.05, p[1] * 0.05, angle) * 3 - 1.5
                p[1] += noise(p[0] * 0.05, p[1] * 0.05, angle + 1) * 3 - 1.5
            for s in clip(seg, outline)[True]:
                if len(s) >= 2 and rand() < 0.8:
                    lines.append(s)
    return [curve0, list(reversed(curve1))] + lines


def fish_body_d(curve0, curve1, scale_scale):
    """Outline + connector filaments between the two body curves, plus the
    fine vein texture the original overlays on this variant."""
    outline = curve0 + list(reversed(curve1))
    c0 = resample(curve0, 6)
    c1 = resample(curve1, 6)
    n = min(len(c0), len(c1))
    lines = []
    for i in range(n):
        t = i / max(1, n - 1)
        a = c0[i]
        b = c1[i]
        mid = lerp2d(a[0], a[1], b[0], b[1], 0.5)
        seg = resample([a, [mid[0] + (noise(t * 3, 5) - 0.5) * 20, mid[1]], b], 5)
        for p in seg:
            p[0] += noise(p[0] * 0.05, p[1] * 0.05, 7) * 4 - 2
            p[1] += noise(p[0] * 0.05, p[1] * 0.05, 8) * 4 - 2
        for s in clip_multi([seg], lambda x, y, tt: rand() < 0.65, binclip)[True]:
            lines.extend(clip(s, outline)[True])
    lines += vein_shape(outline, 40 * scale_scale)
    return [curve0, list(reversed(curve1))] + lines


# --------------------------------------------------------------------- fins --
def _curve_normal_angles(curve):
    n = len(curve)
    angs = []
    for i in range(n):
        if i == 0:
            angs.append(math.atan2(curve[i + 1][1] - curve[i][1], curve[i + 1][0] - curve[i][0]) - PI / 2)
        elif i == n - 1:
            angs.append(math.atan2(curve[i][1] - curve[i - 1][1], curve[i][0] - curve[i - 1][0]) - PI / 2)
        else:
            a0 = math.atan2(curve[i - 1][1] - curve[i][1], curve[i - 1][0] - curve[i][0])
            a1 = math.atan2(curve[i + 1][1] - curve[i][1], curve[i + 1][0] - curve[i][0])
            while a1 > a0:
                a1 -= PI * 2
            a1 += PI * 2
            angs.append((a0 + a1) / 2)
    return angs


def fin_a(curve, ang0, ang1, func, clip_root=False, curvature0=0, curvature1=0, softness=10):
    n = len(curve)
    angs = _curve_normal_angles(curve)
    out0, out1, out2, out3 = [], [], [], []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        a = angs[i] + lerp(ang0, ang1, t)
        w = func(t)
        x0, y0 = curve[i]
        x1 = x0 + math.cos(a) * w
        y1 = y0 + math.sin(a) * w
        p = resample([[x0, y0], [x1, y1]], 3)
        m = len(p)
        for j in range(m):
            s = j / (m - 1) if m > 1 else 0.0
            ss = math.sqrt(s)
            x, y = p[j]
            cv = lerp(curvature0, curvature1, t) * math.sin(s * PI)
            p[j][0] += noise(x * 0.1, y * 0.1, 3) * ss * softness + math.cos(a - PI / 2) * cv
            p[j][1] += noise(x * 0.1, y * 0.1, 4) * ss * softness + math.sin(a - PI / 2) * cv
        if i == 0:
            out2 = p
        elif i == n - 1:
            out3 = list(reversed(p))
        else:
            out0.append(p[-1])
            lo = int(rand() * 4) if clip_root else 0
            hi = max(2, int(len(p) * (rand() * 0.5 + 0.5)))
            q = p[lo:hi]
            if q:
                out1.append(q)
    out0 = resample(out0, 3)
    for i in range(len(out0)):
        x, y = out0[i]
        out0[i][0] += (noise(x * 0.1, y * 0.1) * 6 - 3) * (softness / 10)
        out0[i][1] += (noise(x * 0.1, y * 0.1) * 6 - 3) * (softness / 10)
    o = out2 + out0 + out3
    out1.insert(0, o)
    return [o + list(reversed(curve)), out1]


def fin_b(curve, ang0, ang1, func, dark=1):
    n = len(curve)
    angs = _curve_normal_angles(curve)
    out0, out1, out2, out3 = [], [], [], []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        a = angs[i] + lerp(ang0, ang1, t)
        w = func(t)
        x0, y0 = curve[i]
        x1 = x0 + math.cos(a) * w
        y1 = y0 + math.sin(a) * w
        b = [x1 + 0.5 * math.cos(a - PI / 2), y1 + 0.5 * math.sin(a - PI / 2)]
        c = [x1 + 0.5 * math.cos(a + PI / 2), y1 + 0.5 * math.sin(a + PI / 2)]
        p = [curve[i][0] + 1.8 * math.cos(a - PI / 2), curve[i][1] + 1.8 * math.sin(a - PI / 2)]
        q = [curve[i][0] + 1.8 * math.cos(a + PI / 2), curve[i][1] + 1.8 * math.sin(a + PI / 2)]
        out1.append([x1, y1])
        out0.append([p, b, c, q])
    m = 10
    for i in range(n - 1):
        _unused0, _unused1, a0, q0 = out0[i]
        p1, a1, _unused2, _unused3 = out0[i + 1]
        b = lerp2d(a0[0], a0[1], q0[0], q0[1], 0.1)
        c = lerp2d(a1[0], a1[1], p1[0], p1[1], 0.1)
        o = []
        ang = math.atan2(c[1] - b[1], c[0] - b[0])
        for j in range(m):
            t = j / (m - 1)
            d = math.sin(t * PI) * 2
            pa = lerp2d(b[0], b[1], c[0], c[1], t)
            o.append([pa[0] + math.cos(ang + PI / 2) * d, pa[1] + math.sin(ang + PI / 2) * d])
        out2.append(o)
        mm = int(min(dist(a0[0], a0[1], q0[0], q0[1]), dist(a1[0], a1[1], p1[0], p1[1])) / 10 * dark)
        e = lerp2d(curve[i][0], curve[i][1], curve[i + 1][0], curve[i + 1][1], 0.5)
        for k in range(mm):
            s = k / mm * 0.7
            p_ = [lerp2d(o[j][0], o[j][1], e[0], e[1], s) for j in range(1, m - 1)]
            out3.append(p_)
    out4 = []
    if len(out0) > 1:
        out4.append(out0[0])
        for i in range(1, len(out0)):
            out4.extend(clip(out0[i], out0[i - 1])[False])
    out2_flat = [pt for seg in out2 for pt in seg]
    return [out2_flat + list(reversed(curve)), out4 + out2 + out3]


def finlet(curve, h, dir=1):
    n = len(curve)
    angs = _curve_normal_angles(curve)
    out0 = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.0
        a = angs[i]
        w = 0 if (i + 1) % 3 else h
        w *= (1 - t * 0.5) if dir > 0 else (0.5 + t * 0.5)
        x0, y0 = curve[i]
        out0.append([x0 + math.cos(a) * w, y0 + math.sin(a) * w])
    out0 = resample(out0, 2)
    for i in range(len(out0)):
        x, y = out0[i]
        out0[i][0] += noise(x * 0.1, y * 0.1) * 2 - 3
        out0[i][1] += noise(x * 0.1, y * 0.1) * 2 - 3
    out0.append(list(curve[-1]))
    return [out0 + list(reversed(curve)), [out0]]


def fin_adipose(curve, dx, dy, r):
    n = 20
    x0, y0 = curve[len(curve) // 2]
    x, y = x0 + dx, y0 + dy
    x1, y1 = curve[0]
    x2, y2 = curve[-1]
    d1 = max(dist(x, y, x1, y1), 1e-6)
    d2 = max(dist(x, y, x2, y2), 1e-6)
    a1 = math.acos(max(-1.0, min(1.0, r / d1)))
    a2 = math.acos(max(-1.0, min(1.0, r / d2)))
    a01 = math.atan2(y1 - y, x1 - x) + a1
    a02 = math.atan2(y2 - y, x2 - x) - a2
    a02 -= PI * 2
    while a02 < a01:
        a02 += PI * 2
    out0 = [[x1, y1]]
    for i in range(n):
        t = i / (n - 1)
        a = lerp(a01, a02, t)
        out0.append([x + math.cos(a) * r, y + math.sin(a) * r])
    out0.append([x2, y2])
    out0 = resample(out0, 3)
    m = len(out0)
    for i in range(m):
        t = i / (m - 1) if m > 1 else 0.0
        s = math.sin(t * PI)
        x, y = out0[i]
        out0[i][0] += (noise(x * 0.01, y * 0.01) - 0.5) * s * 50
        out0[i][1] += (noise(x * 0.01, y * 0.01) - 0.5) * s * 50
    cc = out0 + list(reversed(curve))
    out1 = clip(trsl_poly(out0, 0, 4), cc)[True]
    out1 = clip_multi(out1, lambda x, y, t: rand() < math.sin(t * PI), binclip)[True]
    return [cc, [out0] + out1]


# --------------------------------------------------------------------- head --
def fish_lip(x0, y0, x1, y1, w):
    x0 += rand() * 0.001 - 0.0005
    y0 += rand() * 0.001 - 0.0005
    x1 += rand() * 0.001 - 0.0005
    y1 += rand() * 0.001 - 0.0005
    h = max(dist(x0, y0, x1, y1), 1e-6)
    a0 = math.atan2(y1 - y0, x1 - x0)
    n = 10
    ang = math.acos(max(-1.0, min(1.0, w / h)))
    dx = math.cos(a0 + PI / 2) * 0.5
    dy = math.sin(a0 + PI / 2) * 0.5
    o = [[x0 - dx, y0 - dy]]
    for i in range(n):
        t = i / (n - 1)
        a = lerp(ang, PI * 2 - ang, t) + a0
        o.append([-math.cos(a) * w + x1, -math.sin(a) * w + y1])
    o.append([x0 + dx, y0 + dy])
    o = resample(o, 2.5)
    for i in range(len(o)):
        x, y = o[i]
        o[i][0] += noise(x * 0.05, y * 0.05, -1) * 2 - 1
        o[i][1] += noise(x * 0.05, y * 0.05, -2) * 2 - 1
    return o


def fish_teeth(x0, y0, x1, y1, h, dir, sep=3.5):
    n = max(2, int(dist(x0, y0, x1, y1) / sep))
    ang = math.atan2(y1 - y0, x1 - x0)
    out = []
    for i in range(n):
        t = i / (n - 1)
        a = lerp2d(x0, y0, x1, y1, t)
        w = h * t
        b = [a[0] + math.cos(ang + dir * PI / 2) * w, a[1] + math.sin(ang + dir * PI / 2) * w]
        c = [a[0] + math.cos(ang), a[1] + math.sin(ang)]
        d = [a[0] + math.cos(ang + PI), a[1] + math.sin(ang + PI)]
        e = lerp2d(c[0], c[1], b[0], b[1], 0.7)
        f = lerp2d(d[0], d[1], b[0], b[1], 0.7)
        g = [a[0] + math.cos(ang + dir * (PI / 2 + 0.15)) * w, a[1] + math.sin(ang + dir * (PI / 2 + 0.15)) * w]
        out.append([c, e, g, f, d])
    return out


def fish_jaw(x0, y0, x1, y1, x2, y2):
    n = 10
    ang = math.atan2(y2 - y0, x2 - x0)
    d = dist(x0, y0, x2, y2)
    o = []
    for i in range(n):
        t = i / (n - 1)
        s = math.sin(t * PI)
        w = s * d / 20
        p = lerp2d(x2, y2, x0, y0, t)
        q = [p[0] + math.cos(ang - PI / 2) * w, p[1] + math.sin(ang - PI / 2) * w]
        qq = [
            q[0] + (noise(q[0] * 0.01, q[1] * 0.01, 1) - 0.5) * 4 * s,
            q[1] + (noise(q[0] * 0.01, q[1] * 0.01, 4) - 0.5) * 4 * s,
        ]
        o.append(qq)
    return [[[x2, y2], [x1, y1], [x0, y0]], [o]]


def barbel(x, y, n, ang, dd=3):
    n = max(2, int(n))
    curve = [[x, y]]
    sd = rand() * PI * 2
    ar = 1.0
    for i in range(n):
        x += math.cos(ang) * dd
        y += math.sin(ang) * dd
        ang += (noise(i * 0.1, sd) - 0.5) * ar
        ar *= 1.02 if i < n / 2 else 0.92
        curve.append([x, y])
    o0, o1 = [], []
    for i in range(n - 1):
        t = i / (n - 1)
        w = 1.5 * (1 - t)
        a = curve[i - 1] if i - 1 >= 0 else None
        b = curve[i]
        c = curve[i + 1]
        a1 = math.atan2(c[1] - b[1], c[0] - b[0])
        if a is not None:
            a0 = math.atan2(a[1] - b[1], a[0] - b[0])
            a1 -= PI * 2
            while a1 < a0:
                a1 += PI * 2
            a2 = (a0 + a1) / 2
        else:
            a2 = a1 - PI / 2
        o0.append([b[0] + math.cos(a2) * w, b[1] + math.sin(a2) * w])
        o1.append([b[0] + math.cos(a2 + PI) * w, b[1] + math.sin(a2 + PI) * w])
    o0.append(curve[-1])
    return o0 + list(reversed(o1))


def fish_eye_a(ex, ey, rad):
    n = 20
    eye0, eye1, eye2 = [], [], []
    for i in range(n):
        t = i / (n - 1)
        a = t * PI * 2 + PI / 4 * 3
        eye0.append([ex + math.cos(a) * rad, ey + math.sin(a) * rad])
        if t > 0.5:
            eye1.append([ex + math.cos(a) * (rad * 0.8), ey + math.sin(a) * (rad * 0.8)])
        eye2.append([ex + math.cos(a) * (rad * 0.4) - 0.75,
                     ey + math.sin(a) * (rad * 0.4) - 0.75])
    ef = shade_shape(eye2, 2.7, 10, 10)
    return [eye0, [eye0, eye1, eye2] + ef]


def fish_eye_b(ex, ey, rad):
    n = 20
    eye0, eye1, eye2 = [], [], []
    for i in range(n):
        t = i / (n - 1)
        a = t * PI * 2 + PI / 4 * 3
        eye0.append([ex + math.cos(a) * rad, ey + math.sin(a) * rad])
        eye1.append([ex + math.cos(a) * (rad * 0.9), ey + math.sin(a) * (rad * 0.9)])
        eye2.append([ex + math.cos(a) * (rad * 0.4), ey + math.sin(a) * (rad * 0.4)])
    ef = fill_shape(eye2, 1.5)
    rings = [eye0, eye1, eye2]
    return [eye0, rings + ef]


def fish_head(x0, y0, x1, y1, x2, y2, arg):
    n = 20
    curve0 = []
    for i in range(n):
        t = i / (n - 1)
        a = PI / 2 * t
        x = x1 - (math.cos(a) ** 1.5) * (x1 - x0)
        y = y0 - (math.sin(a) ** 1.5) * (y0 - y1)
        dx = (noise(x * 0.01, y * 0.01, 9) * 40 - 20) * (1.01 - t)
        dy = (noise(x * 0.01, y * 0.01, 8) * 40 - 20) * (1.01 - t)
        curve0.append([x + dx, y + dy])
    curve1 = []
    for i in range(n):
        t = i / (n - 1)
        a = PI / 2 * t
        x = x2 - (math.cos(a) ** 0.8) * (x2 - x0)
        y = y0 + (math.sin(a) ** 1.5) * (y2 - y0)
        dx = (noise(x * 0.01, y * 0.01, 9) * 40 - 20) * (1.01 - t)
        dy = (noise(x * 0.01, y * 0.01, 8) * 40 - 20) * (1.01 - t)
        curve1.insert(0, [x + dx, y + dy])
    ang = math.atan2(y2 - y1, x2 - x1)
    curve2 = []
    for i in range(1, n - 1):
        t = i / (n - 1)
        p = lerp2d(x1, y1, x2, y2, t)
        s = math.sin(t * PI) ** 0.5
        r = noise(t * 2, 1.2) * s * 20
        curve2.append([p[0] + math.cos(ang - PI / 2) * r, p[1] + math.sin(ang - PI / 2) * r])
    outline = curve0 + curve2 + curve1

    inline = (curve2[len(curve2) // 3 :] + curve1[: len(curve1) // 2])[: len(curve0)]
    m = len(inline)
    for i in range(m):
        t = i / (m - 1) if m > 1 else 0.0
        s = math.sin(t * PI) ** 2 * 0.1 + 0.12
        inline[i] = lerp2d(inline[i][0], inline[i][1], curve0[i][0], curve0[i][1], s)
    if m:
        dix = (x0 - inline[-1][0]) * 0.3
        diy = (y0 - inline[-1][1]) * 0.2
        for i in range(m):
            inline[i][0] += dix
            inline[i][1] += diy

    par = [0.475, 0.375]
    ex = x0 * par[0] + x1 * par[1] + x2 * (1 - par[0] - par[1])
    ey = y0 * par[0] + y1 * par[1] + y2 * (1 - par[0] - par[1])
    d0 = pt_seg_dist([ex, ey], [x0, y0], [x1, y1])
    d1 = pt_seg_dist([ex, ey], [x0, y0], [x2, y2])
    eye_size = arg["eye_size"]
    if d0 < eye_size and d1 < eye_size:
        eye_size = min(d0, d1)
    elif d0 < eye_size:
        a_ = math.atan2(y1 - y0, x1 - x0) + PI / 2
        ex = x0 * 0.5 + x1 * 0.5 + math.cos(a_) * eye_size
        ey = y0 * 0.5 + y1 * 0.5 + math.sin(a_) * eye_size
    eye_size = max(eye_size, 2.0)

    mouth_size = max(1, min(17, int(arg["mouth_size"])))
    jaw_pt0 = curve1[18 - mouth_size]
    jaw_l = dist(jaw_pt0[0], jaw_pt0[1], curve1[18][0], curve1[18][1]) * arg["jaw_size"]
    jaw_ang0 = math.atan2(curve1[18][1] - jaw_pt0[1], curve1[18][0] - jaw_pt0[0])
    jaw_ang = jaw_ang0 - (arg["has_teeth"] * 0.5 + 0.5) * arg["jaw_open"] * PI / 4
    jaw_pt1 = [jaw_pt0[0] + math.cos(jaw_ang) * jaw_l, jaw_pt0[1] + math.sin(jaw_ang) * jaw_l]

    eye0, ef = (fish_eye_b if arg["eye_type"] else fish_eye_a)(ex, ey, eye_size)
    ef = clip_multi(ef, outline)[True]
    inlines = clip(inline, eye0)[False] if inline else []

    lip0 = fish_lip(jaw_pt0[0], jaw_pt0[1], curve1[18][0], curve1[18][1], 3)
    lip1 = fish_lip(jaw_pt0[0], jaw_pt0[1], jaw_pt1[0], jaw_pt1[1], 3)

    jaw_src_idx = max(0, 15 - mouth_size)
    _jc, jaw = fish_jaw(curve1[jaw_src_idx][0], curve1[jaw_src_idx][1], jaw_pt0[0], jaw_pt0[1], jaw_pt1[0], jaw_pt1[1])
    jaw = clip_multi(jaw, lip1)[False]
    jaw = clip_multi(jaw, outline)[False]

    teeth0s, teeth1s = [], []
    if arg["has_teeth"]:
        teeth0 = fish_teeth(jaw_pt0[0], jaw_pt0[1], curve1[18][0], curve1[18][1], arg["teeth_length"], -1, arg["teeth_space"])
        teeth1 = fish_teeth(jaw_pt0[0], jaw_pt0[1], jaw_pt1[0], jaw_pt1[1], arg["teeth_length"], 1, arg["teeth_space"])
        teeth0s = clip_multi(teeth0, lip0)[False]
        teeth1s = clip_multi(teeth1, lip1)[False]

    olines = clip(outline, lip0)[False]
    lip0s = clip(lip0, lip1)[False]

    # head shading + fine texture, both trimmed around the lip and eye
    sh = shade_shape(outline, 6, -6, -6)
    sh = clip_multi(sh, lip0)[False]
    sh = clip_multi(sh, eye0)[False]
    sh2 = vein_shape(outline, arg["head_texture_amount"])
    sh2 = clip_multi(sh2, lip0)[False]
    sh2 = clip_multi(sh2, eye0)[False]

    bbs = []
    lip1s = [lip1]
    if arg["has_moustache"]:
        bb0 = barbel(jaw_pt0[0], jaw_pt0[1], arg["moustache_length"], PI * 3 / 4, 1.5)
        lip1s = clip(lip1, bb0)[False]
        jaw = clip_multi(jaw, bb0)[False]
        bbs.append(bb0)

    if arg["has_beard"]:
        if jaw and jaw[0]:
            jaw_pt = jaw[0][len(jaw[0]) // 2]
        else:
            jaw_pt = curve1[8]
        bb1 = trsl_poly(barbel(jaw_pt[0], jaw_pt[1], arg["beard_length"], PI * 0.6 + rand() * 0.4 - 0.2), rand() - 0.5, rand() - 0.5)
        bb2 = trsl_poly(barbel(jaw_pt[0], jaw_pt[1], arg["beard_length"], PI * 0.6 + rand() * 0.4 - 0.2), rand() - 0.5, rand() - 0.5)
        bb3 = trsl_poly(barbel(jaw_pt[0], jaw_pt[1], arg["beard_length"], PI * 0.6 + rand() * 0.4 - 0.2), rand() - 0.5, rand() - 0.5)
        bb3c = clip_multi([bb3], bb2)[False]
        bb3c = clip_multi(bb3c, bb1)[False]
        bb2c = clip_multi([bb2], bb1)[False]
        bbs.extend([bb1] + bb2c + bb3c)

    outlinel = [[0, 0], [curve0[-1][0], 0], curve0[-1]] + curve2 + [curve1[0], [curve1[0][0], 300], [0, 300]]

    return outlinel, (olines + inlines + lip0s + lip1s + ef + sh + sh2
                      + bbs + teeth0s + teeth1s + jaw)


# ---------------------------------------------------------------- orchestrator --
_TAIL_FUNCS = [
    lambda t, arg: (75 - (10 + noise(t * 3) * 10) * math.sin(3 * t * PI - PI)) / 75 * arg["tail_length"],
    lambda t, arg: arg["tail_length"] * (math.sin(t * PI) * 0.5 + 0.5),
    lambda t, arg: (abs(math.cos(PI * t)) * 0.8 + 0.2) * arg["tail_length"],
    lambda t, arg: (1 - math.sin(t * PI) * 0.3) * arg["tail_length"],
    lambda t, arg: (1 - math.sin(t * PI) * 0.6) * (1 - t * 0.45) * arg["tail_length"],
    lambda t, arg: (1 - math.sin(t * PI) ** 0.4 * 0.55) * arg["tail_length"],
]


def _clamp_idx(i, lo, hi):
    return max(lo, min(hi, int(i)))


def fish(arg):
    n = 32
    s = arg["body_curve_amount"]
    curve0, curve1 = [], []
    for i in range(n):
        t = i / (n - 1)
        x = 225 + (t - 0.5) * arg["body_length"]
        if arg["body_curve_type"] == 0:
            y = 150 - (math.sin(t * PI) * lerp(0.5, 1, noise(t * 2, 1)) * s + (1 - s)) * arg["body_height"]
        else:
            y = 150 - lerp(1 - s, 1, lerp(0, 1, noise(t * 1.2, 1)) * bean(1 - t)) * arg["body_height"]
        curve0.append([x, y])
    for i in range(n):
        t = i / (n - 1)
        x = 225 + (t - 0.5) * arg["body_length"]
        if arg["body_curve_type"] == 0:
            y = 150 + (math.sin(t * PI) * lerp(0.5, 1, noise(t * 2, 2)) * s + (1 - s)) * arg["body_height"] * 0.6
        else:
            y = 150 + lerp(1 - s, 1, lerp(0, 1, noise(t * 1.2, 2)) * bean(1 - t)) * arg["body_height"] * 0.6
        curve1.append([x, y])

    outline = curve0 + list(reversed(curve1))

    # body texture: scale meshes (a/b), engraved hatching (c), filaments (d)
    pattern_func = pattern_dot(arg["pattern_scale"]) if arg["pattern_type"] in (1, 2) else None
    try:
        if arg["scale_type"] == 0:
            bd = fish_body_a(curve0, curve1, arg["scale_scale"], pattern_func)
        elif arg["scale_type"] == 1:
            bd = fish_body_b(curve0, curve1, arg["scale_scale"], pattern_func)
        elif arg["scale_type"] == 2:
            bd = fish_body_c(curve0, curve1, arg["scale_scale"])
        else:
            bd = fish_body_d(curve0, curve1, arg["scale_scale"])
    except Exception:
        logger.exception("body texture %s failed, falling back to hatching", arg["scale_type"])
        bd = fish_body_c(curve0, curve1, arg["scale_scale"])
    f0, f1, f2, f3, f4, f5, fh = [], [], [], [], [], [], []

    try:
        d0 = _clamp_idx(arg["dorsal_start"], 0, n - 2)
        d1 = _clamp_idx(arg["dorsal_end"], d0 + 1, n - 1)
        if arg["dorsal_type"] == 0:
            f0_func = lambda t: (0.3 + noise(t * 3) * 0.7) * arg["dorsal_length"] * math.sin(t * PI) ** 0.5
        else:
            f0_func = lambda t: arg["dorsal_length"] * ((t - 1) ** 2 * 0.5 + (1 - t) * 0.5)
        src = curve0[d0:d1]
        if arg["dorsal_texture_type"] == 0:
            c0, f0 = fin_a(resample(src, 5), -0.3, 0.3, f0_func, False, 20, 0)
        else:
            c0, f0 = fin_b(resample(src, 15), -0.3, 0.3, f0_func)
        f0 = clip_multi(f0, trsl_poly(outline, 0, 0.001))[False]
    except Exception:
        logger.exception("dorsal fin generation failed, skipping")
        f0 = []

    try:
        w0 = _clamp_idx(arg["wing_start"], 0, n - 1)
        w1 = _clamp_idx(arg["wing_end"], 0, n - 1)
        f1_pt = lerp2d(curve0[w0][0], curve0[w0][1], curve1[w1][0], curve1[w1][1], arg["wing_y"])
        src = [[f1_pt[0], f1_pt[1] - arg["wing_width"] / 2], [f1_pt[0], f1_pt[1] + arg["wing_width"] / 2]]
        if arg["wing_type"] == 0:
            f1_func = lambda t: (40 + (20 + noise(t * 3) * 70) * math.sin(t * PI) ** 0.5) / 130 * arg["wing_length"]
        else:
            f1_func = lambda t: arg["wing_length"] * (1 - t * 0.95)
        if arg["wing_texture_type"] == 0:
            c1, f1 = fin_a(resample(src, 1.5), PI / 2 - 0.3, PI / 2 + 0.3, f1_func, True, 20, 0, 15)
        else:
            c1, f1 = fin_b(resample(src, 4), PI / 2 - 0.3, PI / 2 + 0.3, f1_func, 0.3)
        bd = clip_multi(bd, c1)[False]
    except Exception:
        logger.exception("pectoral fin generation failed, skipping")
        f1 = []

    try:
        p0 = _clamp_idx(arg["pelvic_start"], 0, n - 1)
        p1 = _clamp_idx(arg["pelvic_end"], 0, n - 1)
        f2_pt = lerp2d(curve0[p0][0], curve0[p0][1], curve1[p1][0], curve1[p1][1], 0.9)
        src = [f2_pt, [f2_pt[0] + 10, f2_pt[1]]]
        if arg["pelvic_type"] == 0:
            f2_func = lambda t: (10 + (15 + noise(t * 3) * 60) * math.sin(t * PI) ** 0.5) / 85 * arg["pelvic_length"]
        else:
            f2_func = lambda t: (t * 0.5 + 0.5) * arg["pelvic_length"]
        if arg["pelvic_texture_type"] == 0:
            c2, f2 = fin_a(resample(src, 1.5), PI / 2 - 0.2, PI / 2 + 0.2, f2_func, True, 10, 0, 10)
        else:
            c2, f2 = fin_b(resample(src, 4), PI / 2 - 0.2, PI / 2 + 0.2, f2_func, 0.3)
        bd = clip_multi(bd, c2)[False]
    except Exception:
        logger.exception("pelvic fin generation failed, skipping")
        f2 = []

    try:
        a0 = _clamp_idx(arg["anal_start"], 0, n - 1)
        a1 = _clamp_idx(arg["anal_end"], 0, n - 1)
        f3_pt = lerp2d(curve0[a0][0], curve0[a0][1], curve1[a1][0], curve1[a1][1], 0.95)
        src = [f3_pt, [f3_pt[0] + 8, f3_pt[1]]]
        if arg["anal_type"] == 0:
            f3_func = lambda t: (10 + (10 + noise(t * 3) * 30) * math.sin(t * PI) ** 0.5) / 50 * arg["anal_length"]
        else:
            f3_func = lambda t: arg["anal_length"] * (t * t * 0.8 + 0.2)
        if arg["anal_texture_type"] == 0:
            c3, f3 = fin_a(resample(src, 1.5), PI / 2 - 0.2, PI / 2 + 0.2, f3_func, True, 10, 0, 10)
        else:
            c3, f3 = fin_b(resample(src, 4), PI / 2 - 0.2, PI / 2 + 0.2, f3_func, 0.3)
        bd = clip_multi(bd, c3)[False]
    except Exception:
        logger.exception("anal fin generation failed, skipping")
        f3 = []

    try:
        f4_func = lambda t: _TAIL_FUNCS[arg["tail_type"]](t, arg)
        src = resample([curve0[-1], curve1[-1]], 4)
        c4, f4 = fin_a(src, -0.6, 0.6, f4_func, True)
        bd = clip_multi(bd, trsl_poly(c4, 1, 0))[False]
    except Exception:
        logger.exception("tail fin generation failed, skipping")
        f4 = []

    try:
        d1 = _clamp_idx(arg["dorsal_end"], 0, n - 2)
        fl_curve_src = curve0[d1:]
        if len(fl_curve_src) >= 2:
            if arg["finlet_type"] == 2:
                c5, f5 = fin_adipose(resample(fl_curve_src, 5), 20, -5, 15)
                bd = clip_multi(bd, c5)[False]
            elif arg["finlet_type"] in (1, 3):
                c5, f5 = finlet(resample(fl_curve_src, 4), 8, 1)
                bd = clip_multi(bd, c5)[False]
    except Exception:
        logger.exception("finlet generation failed, skipping")
        f5 = []

    try:
        neck_a, neck_b = (6, 5) if arg["neck_type"] == 0 else (5, 6)
        neck_a = _clamp_idx(neck_a, 0, n - 1)
        neck_b = _clamp_idx(neck_b, 0, n - 1)
        fh_outline, fh = fish_head(
            50 - arg["head_length"], 150 + arg["nose_height"],
            curve0[neck_a][0], curve0[neck_a][1],
            curve1[neck_b][0], curve1[neck_b][1],
            arg,
        )
        bd = clip_multi(bd, fh_outline)[False]
        f0 = clip_multi(f0, fh_outline)[False]
    except Exception:
        logger.exception("head generation failed, skipping")
        fh = []

    return bd + f0 + f1 + f2 + f3 + f4 + f5 + fh


# -------------------------------------------------------------- params/reframe --
def choice(opts, percs=None):
    if percs is None:
        percs = [1] * len(opts)
    total = sum(percs)
    r = rand() * total
    c = 0
    for i, p in enumerate(percs):
        c += p
        if r <= c:
            return opts[i]
    return opts[-1]


def rndtri(a, b, c):
    s0 = (b - a) / 2
    s1 = (c - b) / 2
    s = s0 + s1
    r = rand() * s
    if r < s0:
        d = math.sqrt(2 * r * (b - a))
        return a + d
    d = math.sqrt(2 * (s - r) * (c - b))
    return c - d


def default_params():
    return dict(
        body_curve_type=0, body_curve_amount=0.85, body_length=350, body_height=90,
        scale_type=1, scale_scale=1, pattern_type=3, pattern_scale=1,
        dorsal_texture_type=1, dorsal_type=0, dorsal_length=100, dorsal_start=8, dorsal_end=27,
        wing_texture_type=0, wing_type=0, wing_start=6, wing_end=6, wing_y=0.7, wing_length=130, wing_width=10,
        pelvic_start=9, pelvic_end=14, pelvic_length=85, pelvic_type=0, pelvic_texture_type=0,
        anal_start=19, anal_end=29, anal_length=50, anal_type=0, anal_texture_type=0,
        tail_type=0, tail_length=75, finlet_type=0,
        neck_type=0, nose_height=0, mouth_size=8, head_length=30, head_texture_amount=60,
        has_moustache=1, moustache_length=10, has_beard=0, beard_length=30,
        has_teeth=1, teeth_length=8, teeth_space=3.5,
        eye_type=1, eye_size=10, jaw_size=1, jaw_open=1,
    )


def generate_params():
    arg = default_params()
    arg["body_curve_type"] = choice([0, 1])
    arg["body_curve_amount"] = rndtri(0.5, 0.85, 0.98)
    arg["body_length"] = rndtri(200, 350, 420)
    arg["body_height"] = rndtri(45, 90, 150)
    arg["scale_type"] = choice([0, 1, 2, 3])
    arg["scale_scale"] = rndtri(0.8, 1, 1.5)
    arg["pattern_type"] = choice([0, 1, 2, 3, 4])
    arg["pattern_scale"] = rndtri(0.5, 1, 2)
    arg["dorsal_texture_type"] = choice([0, 1])
    arg["dorsal_type"] = choice([0, 1])
    arg["dorsal_length"] = rndtri(30, 90, 180)
    if arg["dorsal_type"] == 0:
        arg["dorsal_start"] = int(rndtri(7, 8, 15))
        arg["dorsal_end"] = int(rndtri(20, 27, 28))
    else:
        arg["dorsal_start"] = int(rndtri(11, 12, 16))
        arg["dorsal_end"] = int(rndtri(19, 21, 24))
    arg["wing_texture_type"] = choice([0, 1])
    arg["wing_type"] = choice([0, 1])
    if arg["wing_type"] == 0:
        arg["wing_length"] = rndtri(40, 130, 200)
    else:
        arg["wing_length"] = rndtri(40, 150, 350)
    if arg["wing_texture_type"] == 0:
        arg["wing_width"] = rndtri(7, 10, 20)
        arg["wing_y"] = rndtri(0.45, 0.7, 0.85)
    else:
        arg["wing_width"] = rndtri(20, 30, 50)
        arg["wing_y"] = rndtri(0.45, 0.65, 0.75)
    arg["wing_start"] = int(rndtri(5, 6, 8))
    arg["wing_end"] = int(rndtri(5, 6, 8))
    arg["pelvic_texture_type"] = choice([0, 1]) if arg["dorsal_texture_type"] else 0
    arg["pelvic_type"] = choice([0, 1])
    arg["pelvic_length"] = rndtri(30, 85, 140)
    if arg["pelvic_type"] == 0:
        arg["pelvic_start"] = int(rndtri(7, 9, 11))
        arg["pelvic_end"] = int(rndtri(13, 14, 15))
    else:
        arg["pelvic_start"] = int(rndtri(7, 9, 12))
        arg["pelvic_end"] = arg["pelvic_start"] + 2
    arg["anal_texture_type"] = choice([0, 1]) if arg["dorsal_texture_type"] else 0
    arg["anal_type"] = choice([0, 1])
    arg["anal_length"] = rndtri(20, 50, 80)
    arg["anal_start"] = int(rndtri(16, 19, 23))
    arg["anal_end"] = int(rndtri(25, 29, 31))
    arg["tail_type"] = choice([0, 1, 2, 3, 4, 5])
    arg["tail_length"] = rndtri(50, 75, 180)
    arg["finlet_type"] = choice([0, 1, 2, 3])
    arg["neck_type"] = choice([0, 1])
    arg["nose_height"] = rndtri(-50, 0, 35)
    arg["head_length"] = rndtri(20, 30, 35)
    arg["mouth_size"] = int(rndtri(6, 8, 11))
    arg["head_texture_amount"] = int(rndtri(30, 60, 160))
    arg["has_moustache"] = choice([0, 0, 0, 1])
    arg["has_beard"] = choice([0, 0, 0, 0, 0, 1])
    arg["moustache_length"] = int(rndtri(10, 20, 40))
    arg["beard_length"] = int(rndtri(20, 30, 50))
    arg["eye_type"] = choice([0, 1])
    arg["eye_size"] = rndtri(8, 10, 28)
    arg["jaw_size"] = rndtri(0.7, 1, 1.4)
    arg["has_teeth"] = choice([0, 1, 1])
    arg["teeth_length"] = rndtri(5, 8, 15)
    arg["teeth_space"] = rndtri(3, 3.5, 6)
    return arg


def reframe(polylines, target_w=800, target_h=440, pad=20):
    all_pts = [p for pl in polylines for p in pl]
    if not all_pts:
        return []
    bbox = get_bbox(all_pts)
    w_avail = target_w - pad * 2
    h_avail = target_h - pad * 2
    sw = w_avail / bbox["w"] if bbox["w"] else 1.0
    sh = h_avail / bbox["h"] if bbox["h"] else 1.0
    s = min(sw, sh)
    px = (w_avail - bbox["w"] * s) / 2
    py = (h_avail - bbox["h"] * s) / 2
    out = []
    for pl in polylines:
        out.append([[(x - bbox["x"]) * s + px + pad, (y - bbox["y"]) * s + py + pad] for x, y in pl])
    return out


def cleanup(polylines):
    out = []
    for pl in polylines:
        simp = approx_poly_dp(pl, 0.1)
        simp = [[round(x, 2), round(y, 2)] for x, y in simp]
        if len(simp) < 2:
            continue
        if len(simp) == 2 and dist(simp[0][0], simp[0][1], simp[1][0], simp[1][1]) < 0.9:
            continue
        out.append(simp)
    return out


# ------------------------------------------------------------------ binomen --
_BINOMEN_DATA = [
    ["A", "AB", "AL", "AN", "AP", "AR", "AU", "BA", "BE", "BO", "BRA", "CA", "CAR", "CENT", "CHAE", "CHAN", "CHI",
     "CHRO", "CHRY", "CO", "CTE", "CY", "CYP", "DE", "E", "EU", "GA", "GAS", "GNA", "GO", "HE", "HIP", "HO", "HY",
     "LA", "LAB", "LE", "LI", "LO", "LU", "MAC", "ME", "MIC", "MO", "MU", "MY", "NA", "NAN", "NE", "NO", "O", "ON",
     "OP", "OS", "PA", "PER", "PHO", "PI", "PLA", "PLEU", "PO", "PSEU", "PTE", "RA", "RHI", "RHOM", "RU", "SAL",
     "SAR", "SCA", "SCOM", "SE", "SI", "STE", "TAU", "TEL", "THO", "TRI", "XE", "XI"],
    ["BE", "BI", "BO", "BU", "CA", "CAM", "CAN", "CE", "CENT", "CHA", "CHEI", "CHI", "CHO", "CHY", "CI", "CIRR",
     "CO", "DI", "DO", "DON", "DOP", "GA", "GAS", "GO", "HI", "HYN", "LA", "LAB", "LE", "LEOT", "LI", "LICH", "LIS",
     "LO", "LOS", "LU", "LY", "MA", "ME", "MI", "MICH", "MO", "MU", "NA", "NE", "NEC", "NI", "NO", "NOCH", "NOP",
     "NOS", "PA", "PE", "PEN", "PHA", "PHI", "PHO", "PHY", "PHYO", "PI", "PIP", "PIS", "PO", "POG", "POPH", "RA",
     "RAE", "RAM", "REOCH", "RI", "RICH", "RIP", "RIS", "RO", "ROI", "ROP", "ROS", "RY", "RYN", "SE", "SO", "TA",
     "TE", "TEL", "THAL", "THE", "THO", "THOP", "THU", "TI", "TICH", "TO", "TOG", "TOP", "TOS", "VA", "XI", "XO"],
    ["BIUS", "BUS", "CA", "CHUS", "CION", "CON", "CUS", "DA", "DES", "DEUS", "DON", "DUS", "GER", "GON", "GUS",
     "HUS", "LA", "LEA", "LIS", "LIUS", "LUS", "MA", "MIS", "MUS", "NA", "NIA", "NIO", "NIUS", "NOPS", "NUS",
     "PHEUS", "PHIS", "PIS", "PUS", "RA", "RAS", "RAX", "RIA", "RION", "RIS", "RUS", "RYS", "SA", "SER", "SIA",
     "SIS", "SUS", "TER", "TES", "TEUS", "THUS", "THYS", "TIA", "TIS", "TUS", "TYS"],
    ["A", "AE", "AL", "AN", "AR", "AT", "AU", "AUST", "AY", "BA", "BAR", "BE", "BI", "BO", "CA", "CAL", "CAM",
     "CAN", "CAR", "CAU", "CE", "CHI", "CHRY", "COR", "CRY", "CU", "CYA", "DA", "DE", "DEN", "DI", "DIA", "DO",
     "DOR", "DU", "E", "FA", "FAS", "FES", "FI", "FLO", "FOR", "FRE", "FUR", "GLA", "GO", "HA", "HE", "HIP", "HO",
     "HYP", "I", "IM", "IN", "JA", "LA", "LAB", "LE", "LEU", "LI", "LO", "LU", "MA", "MAC", "MAR", "ME", "MO",
     "MOO", "MOR", "NA", "NE", "NI", "NIG", "NO", "O", "OR", "PA", "PAL", "PE", "PEC", "PHO", "PLA", "PLU", "PO",
     "PRO", "PU", "PUL", "RA", "RE", "RHOM", "RI", "RO", "ROST", "RU", "SA", "SAL", "SE", "SO", "SPI", "SPLEN",
     "STRIA", "TAU", "THO", "TRI", "TY", "U", "UN", "VA", "VI", "VIT", "VUL", "WAL", "XAN"],
    ["BA", "BAR", "BER", "BI", "BO", "BOI", "BU", "CA", "CAN", "CAU", "CE", "CEL", "CHA", "CHEL", "CHOP", "CI",
     "CIA", "CIL", "CIO", "CO", "COS", "CU", "DA", "DE", "DEL", "DI", "DIA", "DO", "FAS", "FEL", "FI", "FOR", "GA",
     "GE", "GI", "HA", "HYN", "KE", "LA", "LAN", "LE", "LEA", "LEU", "LI", "LIA", "LO", "LON", "LOP", "MA", "ME",
     "MEN", "MI", "MIE", "MO", "NA", "NE", "NEA", "NEL", "NEN", "NI", "NIF", "NO", "NOI", "NOP", "NU", "PA", "PE",
     "PER", "PHA", "PHE", "PI", "PIN", "PO", "QUI", "RA", "RAC", "RE", "REN", "RES", "RI", "RIA", "RIEN", "RIF",
     "RO", "ROR", "ROS", "ROST", "RU", "RYTH", "SA", "SE", "SI", "SO", "SU", "TA", "TAE", "TE", "TER", "THAL",
     "THO", "THU", "TI", "TIG", "TO", "TU", "VA", "VE", "VES", "VI", "VIT", "XEL", "XI", "ZO"],
    ["BEUS", "CA", "CENS", "CEPS", "CEUS", "CHA", "CHUS", "CI", "CUS", "DA", "DAX", "DENS", "DES", "DI", "DIS",
     "DUS", "FER", "GA", "GI", "GUS", "KEI", "KI", "LA", "LAS", "LI", "LIS", "LIUS", "LOR", "LUM", "LUS", "MA",
     "MIS", "MUS", "NA", "NEUS", "NI", "NII", "NIS", "NIUS", "NUS", "PIS", "PUS", "RA", "RE", "RI", "RIAE", "RIE",
     "RII", "RIO", "RIS", "RIX", "RONS", "RU", "RUM", "RUS", "SA", "SEUS", "SI", "SIS", "SUS", "TA", "TEUS",
     "THUS", "TI", "TIS", "TOR", "TUM", "TUS", "TZI", "ZI"],
]
_BINOMEN_FREQ = [
    [27, 2, 4, 4, 2, 2, 2, 5, 2, 2, 3, 4, 2, 5, 3, 2, 2, 2, 3, 8, 3, 3, 2, 2, 7, 2, 3, 2, 2, 2, 6, 3, 2, 4, 5, 2, 5,
     2, 3, 2, 2, 5, 5, 4, 2, 3, 2, 3, 2, 2, 9, 2, 2, 2, 7, 2, 2, 2, 2, 2, 5, 6, 2, 2, 2, 2, 2, 2, 2, 2, 2, 4, 2, 2,
     2, 2, 3, 3, 2, 3],
    [2, 2, 3, 3, 5, 2, 11, 6, 4, 2, 7, 2, 4, 4, 3, 3, 5, 4, 9, 2, 2, 4, 5, 13, 3, 3, 12, 3, 3, 2, 8, 3, 4, 15, 6, 2,
     3, 10, 3, 3, 2, 2, 2, 8, 7, 3, 4, 20, 2, 2, 3, 4, 3, 2, 10, 2, 6, 2, 2, 5, 2, 2, 13, 2, 2, 14, 3, 2, 2, 9, 4,
     2, 5, 42, 2, 4, 2, 6, 3, 3, 11, 2, 19, 2, 3, 2, 5, 3, 2, 4, 2, 27, 2, 2, 2, 2, 2, 2],
    [3, 3, 7, 7, 3, 2, 3, 2, 5, 2, 13, 7, 2, 3, 4, 2, 13, 2, 2, 2, 24, 18, 13, 17, 12, 4, 2, 5, 3, 19, 3, 2, 2, 3,
     7, 3, 2, 5, 2, 6, 29, 3, 2, 2, 2, 3, 4, 4, 16, 2, 6, 12, 5, 5, 6, 2],
    [23, 3, 11, 6, 6, 3, 8, 2, 2, 2, 3, 3, 9, 3, 8, 2, 2, 3, 2, 2, 2, 2, 6, 2, 2, 3, 4, 3, 4, 2, 2, 2, 2, 2, 2, 15,
     2, 4, 2, 2, 2, 2, 2, 2, 2, 2, 2, 5, 3, 2, 2, 3, 2, 2, 2, 7, 2, 2, 3, 3, 4, 4, 13, 7, 3, 10, 2, 2, 2, 5, 2, 3,
     6, 4, 14, 2, 3, 2, 5, 2, 2, 3, 2, 3, 2, 2, 2, 3, 5, 2, 2, 3, 2, 3, 5, 2, 5, 2, 3, 3, 3, 3, 3, 7, 2, 3, 2, 4, 3,
     2, 2, 2, 2],
    [5, 2, 2, 4, 4, 2, 2, 10, 6, 2, 3, 5, 3, 2, 2, 6, 12, 2, 3, 6, 2, 22, 4, 4, 2, 7, 5, 6, 10, 2, 2, 2, 9, 7, 4, 2,
     2, 2, 39, 3, 10, 3, 2, 20, 2, 10, 2, 2, 12, 9, 3, 8, 2, 4, 19, 5, 5, 3, 3, 12, 2, 9, 3, 2, 7, 3, 4, 3, 6, 2, 8,
     5, 2, 4, 25, 2, 4, 3, 2, 26, 2, 2, 2, 21, 2, 2, 4, 6, 5, 3, 6, 4, 6, 2, 14, 2, 19, 2, 2, 2, 2, 21, 3, 14, 2, 3,
     5, 2, 5, 2, 2, 2, 3],
    [2, 7, 4, 3, 5, 2, 5, 2, 13, 6, 2, 2, 6, 8, 2, 4, 3, 4, 2, 5, 2, 5, 11, 3, 7, 19, 2, 2, 2, 11, 10, 4, 6, 12, 3,
     15, 4, 6, 2, 18, 3, 3, 11, 4, 14, 2, 2, 3, 2, 13, 2, 3, 2, 4, 21, 7, 2, 10, 8, 13, 31, 2, 5, 5, 2, 2, 10, 68,
     2, 3],
]


def binomen():
    name = choice(_BINOMEN_DATA[0], _BINOMEN_FREQ[0])
    for _ in range(int(rand() * 3)):
        name += choice(_BINOMEN_DATA[1], _BINOMEN_FREQ[1])
    name += choice(_BINOMEN_DATA[2], _BINOMEN_FREQ[2])
    name += " "
    name += choice(_BINOMEN_DATA[3], _BINOMEN_FREQ[3])
    for _ in range(int(rand() * 3)):
        name += choice(_BINOMEN_DATA[4], _BINOMEN_FREQ[4])
    name += choice(_BINOMEN_DATA[5], _BINOMEN_FREQ[5])
    name = re.sub(r"([A-Z])\1\1+", r"\1\1", name)
    return name[0] + name[1:].lower()


# ------------------------------------------------------------------ public --
def generate(target_w=800, target_h=440, pad=20):
    """Generate one fish. Returns (polylines, name, params) with the polylines
    in 0..target_w/target_h space. The params are the generated traits, handy
    for writing descriptive text about the specimen."""
    seed = "%d-%d" % (time.time_ns(), _pyrandom.getrandbits(32))
    str_to_seed(seed)
    arg = generate_params()
    polylines = fish(arg)
    name = binomen()
    polylines = reframe(polylines, target_w, target_h, pad)
    polylines = cleanup(polylines)
    return polylines, name, arg
