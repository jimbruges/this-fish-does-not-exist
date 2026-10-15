"""Page layouts for This Fish Does Not Exist.

Builds an ordered list of draw ops so the renderer can reveal the page a
little at a time. Op forms:

    ("line", [(x, y), ...])
    ("text", x, y, string, font_key, oblique)

Order matters: whatever should be legible while the drawing is still in
progress (captions, headings, the specimen name) goes first, the fish
strokes next, and body prose last so it types out word by word.

Only DejaVu Serif/Mono are installed on the board - there is no italic serif -
so binomial names are set in synthetic oblique (a sheared render), which is
what the `oblique` flag on a text op means.
"""

import logging

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
SERIF_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"

FONT_SPECS = {
    "name": (SERIF_BOLD, 30),
    "heading": (SERIF_BOLD, 24),
    "meta": (SERIF, 16),
    "body": (SERIF, 16),
    "caption": (SERIF, 14),
    "record": (SERIF_BOLD, 15),
    "runhead": (SERIF, 12),
    "folio": (SERIF, 14),
    "taxon": (SERIF, 15),
    "taxon_key": (SERIF_BOLD, 13),
}

OBLIQUE_SHEAR = 0.22

# logical canvas per mode: (width, height, portrait)
CANVAS = {
    "plate": (800, 480, False),
    "figure": (800, 480, False),
    "page": (480, 800, True),
}

MODES = ("plate", "figure", "page")

_fonts = None


def fonts():
    global _fonts
    if _fonts is None:
        _fonts = {}
        for key, (path, size) in FONT_SPECS.items():
            try:
                _fonts[key] = ImageFont.truetype(path, size)
            except Exception:
                logger.warning("font %s missing; using default for %r", path, key)
                try:
                    _fonts[key] = ImageFont.load_default(size=size)
                except TypeError:
                    _fonts[key] = ImageFont.load_default()
    return _fonts


def draw_text(img, draw, x, y, s, font, oblique=False):
    """Draw text at (x, y). Oblique renders to a scratch bitmap, shears it and
    pastes it through as a mask - PIL has no synthetic-italic support and the
    board has no italic serif face."""
    if not oblique:
        draw.text((x, y), s, font=font, fill=0)
        return
    box = draw.textbbox((0, 0), s, font=font)
    tw = max(1, box[2] - box[0] + 4)
    th = max(1, box[3] - box[1] + 4)
    pad = int(th * OBLIQUE_SHEAR) + 2
    tmp = Image.new("1", (tw + pad, th), 0)
    ImageDraw.Draw(tmp).text((2 - box[0], 2 - box[1]), s, font=font, fill=1)
    # sample input at x + shear*y - shear*h, i.e. slant the top to the right
    tmp = tmp.transform(
        tmp.size, Image.AFFINE,
        (1, OBLIQUE_SHEAR, -OBLIQUE_SHEAR * th, 0, 1, 0),
        resample=Image.NEAREST, fillcolor=0,
    )
    img.paste(0, (int(x + box[0] - 2), int(y + box[1] - 2)), tmp)


def text_width(draw, s, font):
    return draw.textlength(s, font=font)


def _measure_draw():
    """Scratch draw context for measuring text while building ops."""
    return ImageDraw.Draw(Image.new("1", (8, 8), 1))


def letterspace(s, spaces=1):
    sep = " " * spaces
    return sep.join(s)


def paragraph_ops(text, font_key, x, y, max_w, line_h, justify=True, max_y=None):
    """Lay out prose as one op per word, so it can be revealed word by word.
    Lines that would run past max_y are dropped rather than printed over the
    folio. Returns (ops, y_after)."""
    d = _measure_draw()
    font = fonts()[font_key]
    space = text_width(d, " ", font)
    ops = []
    for para in [p for p in text.split("\n") if p.strip()]:
        words = para.split()
        lines, cur = [], []
        for w in words:
            trial = " ".join(cur + [w])
            if cur and text_width(d, trial, font) > max_w:
                lines.append(cur)
                cur = [w]
            else:
                cur.append(w)
        if cur:
            lines.append(cur)
        for i, line in enumerate(lines):
            if max_y is not None and y + line_h > max_y:
                return ops, y          # out of page; drop the remainder
            widths = [text_width(d, w, font) for w in line]
            gap = space
            if justify and i < len(lines) - 1 and len(line) > 1:
                slack = max_w - sum(widths) - space * (len(line) - 1)
                if slack > 0:
                    gap = space + slack / (len(line) - 1)
            cx = x
            for w, wd in zip(line, widths):
                ops.append(("text", round(cx), y, w, font_key, False))
                cx += wd + gap
            y += line_h
        y += 6  # paragraph spacing
    return ops, y


def _rule(x0, y, x1):
    return ("line", [(x0, y), (x1, y)])


def taxonomy(name):
    """Invent a plausible higher classification from the binomial, so the
    taxonomy block reads like a real entry."""
    genus = name.split()[0]
    stem = genus[:-2] if len(genus) > 4 else genus
    return {
        "order": stem[:5].capitalize() + "iformes",
        "family": stem.capitalize() + "idae",
        "genus": genus,
        "species": name.split()[-1],
    }


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def plate(name, when, fish_region, fish_ops):
    """Landscape specimen plate, with the caption drawn first so it is
    readable while the fish is still being drawn.

    Laid out bottom-up from measured ink heights: stacking downwards from the
    fish area overflowed the panel once the name was set at 30pt.
    """
    w, h = CANVAS["plate"][0], CANVAS["plate"][1]
    d = _measure_draw()
    f = fonts()

    nb = d.textbbox((0, 0), name, font=f["name"])
    meta = "First recorded: " + when
    mb = d.textbbox((0, 0), meta, font=f["record"])
    name_h = nb[3] - nb[1]
    meta_h = mb[3] - mb[1]

    meta_top = h - 14 - meta_h          # 14px clear of the panel edge
    name_top = meta_top - 10 - name_h
    rule_y = name_top - 12

    ops = [_rule(40, rule_y, w - 40)]
    # textbbox offsets are subtracted so the ink lands where intended rather
    # than the glyph box's top-left
    ops.append(("text", (w - (nb[2] - nb[0])) // 2 - nb[0], name_top - nb[1],
                name, "name", True))
    ops.append(("text", (w - (mb[2] - mb[0])) // 2 - mb[0], meta_top - mb[1],
                meta, "record", False))
    return ops + fish_ops


def figure(name, when, fish_region, fish_ops, number=1):
    """Landscape textbook figure: plate on the left, taxonomy block on the
    right, running head and folio. No prose, so it needs no LLM."""
    w, h = CANVAS["figure"][0], CANVAS["figure"][1]
    d = _measure_draw()
    f = fonts()
    tx = fish_region[0] + fish_region[2] + 28     # taxonomy column x

    ops = []
    head = letterspace("SYSTEMATIC ICHTHYOLOGY")
    hb = d.textbbox((0, 0), head, font=f["runhead"])
    ops.append(("text", (w - (hb[2] - hb[0])) // 2 - hb[0], 18, head, "runhead", False))
    ops.append(_rule(40, 40, w - 40))

    # caption is drawn in three pieces so the binomial can be set oblique
    cap_y = fish_region[1] + fish_region[3] + 10
    lead = "Fig. %d.— " % number
    x = fish_region[0]
    ops.append(("text", x, cap_y, lead.rstrip(), "caption", False))
    x += d.textlength(lead, font=f["caption"])
    ops.append(("text", round(x), cap_y, name, "caption", True))
    x += d.textlength(name + " ", font=f["caption"])
    ops.append(("text", round(x), cap_y, ", lateral view.", "caption", False))

    t = taxonomy(name)
    ops.append(("text", tx, 64, letterspace("CLASSIFICATION"), "taxon_key", False))
    ops.append(_rule(tx, 84, w - 40))
    y = 96
    for key, val, oblique in (
        ("Order", t["order"], False),
        ("Family", t["family"], False),
        ("Genus", t["genus"], True),
        ("Species", t["species"], True),
    ):
        ops.append(("text", tx, y, key, "taxon_key", False))
        ops.append(("text", tx, y + 17, val, "taxon", oblique))
        y += 44
    ops.append(_rule(tx, y + 2, w - 40))
    ops.append(("text", tx, y + 12, "First recorded", "taxon_key", False))
    ops.append(("text", tx, y + 29, when, "record", False))

    folio = "— %d —" % number       # running specimen count
    fb = d.textbbox((0, 0), folio, font=f["folio"])
    ops.append(("text", (w - (fb[2] - fb[0])) // 2 - fb[0], h - 30, folio, "folio", False))
    return ops + fish_ops


def page(name, when, fish_region, fish_ops, body_text, number=1):
    """Portrait textbook page: running head, figure with caption, species
    heading, then justified prose that types out word by word.

    Returns (ops, body_start_index) so the renderer can give the prose its own
    share of the reveal time instead of letting the fish strokes crowd it out.
    """
    w, h = CANVAS["page"][0], CANVAS["page"][1]
    margin = 44
    d = _measure_draw()
    f = fonts()
    ops = []

    head = letterspace("SYSTEMATIC ICHTHYOLOGY")
    hb = d.textbbox((0, 0), head, font=f["runhead"])
    ops.append(("text", (w - (hb[2] - hb[0])) // 2 - hb[0], 22, head, "runhead", False))
    ops.append(_rule(margin, 44, w - margin))

    cap_y = fish_region[1] + fish_region[3] + 10
    ops.append(("text", margin, cap_y, "Fig. %d.—" % number, "caption", False))
    ops.append(("text", margin + d.textlength("Fig. %d.— " % number, font=f["caption"]),
                cap_y, name, "caption", True))

    y = cap_y + 30
    ops.append(_rule(margin, y, w - margin))
    y += 12
    ops.append(("text", margin, y, name, "heading", True))
    y += 34

    t = taxonomy(name)
    ops.append(("text", margin, y, "%s: %s" % (t["order"], t["family"]), "meta", False))
    y += 24
    ops.append(("text", margin, y, "First recorded: %s" % when, "record", False))
    y += 24
    ops.append(_rule(margin, y, w - margin))
    y += 12

    # folio belongs to the page furniture, so draw it up front with the rest.
    # It is the running count of specimens drawn, so the page number doubles
    # as a tally of how many fish this board has produced.
    folio = "— %d —" % number
    fb = d.textbbox((0, 0), folio, font=f["folio"])
    ops.append(("text", (w - (fb[2] - fb[0])) // 2 - fb[0], h - 36, folio, "folio", False))

    body_start = len(ops) + len(fish_ops)
    body, _y_end = paragraph_ops(
        body_text, "body", margin, y, w - margin * 2, 21, max_y=h - 58,
    )
    return ops + fish_ops + body, body_start


def fish_area(mode):
    """Region the fish illustration occupies, per mode: (x, y, w, h)."""
    if mode == "page":
        return (44, 60, 392, 290)
    if mode == "figure":
        return (40, 56, 500, 360)
    # plate: full width, stopping clear of the rule above the caption block
    # (name at 30pt + record line + margins needs ~92px)
    return (0, 0, 800, 384)
