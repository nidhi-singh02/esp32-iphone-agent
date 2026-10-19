"""Small image checks on screen frames: did the screen change, and where is the pointer."""

import io

from PIL import Image, ImageChops


def to_square(jpeg: bytes, side: int = 1000, quality: int = 85) -> bytes:
    """The frame stretched to side x side, so a pixel position in it equals a 0-1000 coordinate on both axes.

    Models often answer in pixels whatever the prompt says. A square of exactly the coordinate range makes the
    pixel answer and the grid answer the same number. The stretch costs a little shape, not accuracy.
    """
    out = io.BytesIO()
    Image.open(io.BytesIO(jpeg)).convert("RGB").resize((side, side), Image.BILINEAR).save(out, "JPEG", quality=quality)
    return out.getvalue()


def _gray(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg)).convert("L")


def changed(before: bytes | None, after: bytes | None, ignore: tuple = (), threshold: float = 1.0) -> bool:
    """True when the screen content changed, including small things such as a checkmark.

    `ignore` lists screen points (fractions) where the pointer was or went: those spots are masked, so pointer
    moves never count. The status bar (clock) is ignored too.
    """
    if not before or not after:
        return True
    a, b = _gray(before), _gray(after)
    if a.size != b.size:
        return True
    small = ImageChops.difference(a.resize((32, 64), Image.BILINEAR), b.resize((32, 64), Image.BILINEAR)).tobytes()
    if sum(small) / len(small) > threshold:  # a big change: new screen, scrolled list, keyboard
        return True
    w, h = a.size[0] // 2, a.size[1] // 2  # fine pass for small changes, at half resolution
    diff = ImageChops.difference(a.resize((w, h), Image.BILINEAR), b.resize((w, h), Image.BILINEAR)).tobytes()
    masks = [(px * w, py * h) for px, py in ignore]
    hits = 0
    for i, v in enumerate(diff):
        if v > 25:
            x, y = i % w, i // w
            if y > 0.06 * h and all((x - mx) ** 2 + (y - my) ** 2 > (0.05 * w) ** 2 for mx, my in masks):
                hits += 1
    return hits >= 6


def pointer(before: bytes, after: bytes, near: tuple[float, float], shift: float,
            reach: tuple[float, float] = (0.12, 0.08)) -> tuple[float, float] | None:
    """Where the pointer is in `before`, given it moved right by `shift` (fraction of width) in `after`.

    Only the pointer moved between the two frames, so the changed pixels within `reach` of `near` are the
    pointer at both spots; their centroid sits shift/2 to the right of the first one. None if unclear:
    nothing moved, or the change is too big or too spread out to be one small pointer.
    """
    a, b = _gray(before), _gray(after)
    if a.size != b.size:
        return None
    w, h = a.size
    rx, ry = reach
    left, top = max(0, int((near[0] - rx) * w)), max(int(0.05 * h), int((near[1] - ry) * h))  # skip the status bar
    box = (left, top, min(w, int((near[0] + rx) * w)), min(h, int((near[1] + ry) * h)))
    diff = ImageChops.difference(a.crop(box), b.crop(box)).tobytes()
    bw = box[2] - box[0]
    hits = [(i % bw, i // bw) for i, v in enumerate(diff) if v > 30]
    if not 15 <= len(hits) <= len(diff) // 4:  # nothing moved, or much more than a pointer
        return None
    xs, ys = [x for x, _ in hits], [y for _, y in hits]
    if max(xs) - min(xs) > 0.15 * w or max(ys) - min(ys) > 0.07 * h:  # not one compact spot
        return None
    cx = sum(x for x, _ in hits) / len(hits) + left
    cy = sum(y for _, y in hits) / len(hits) + top
    return cx / w - shift / 2, cy / h


def _orange(jpeg: bytes) -> Image.Image:
    """Mask of pixels coloured like the iOS pointer ring (Pointer Control colour Orange): no blue, red twice green.

    Almost nothing else on a screen has zero blue with that red/green ratio; bright app-icon oranges are
    redder and brighter, so they fall outside.
    """
    r, g, b = Image.open(io.BytesIO(jpeg)).convert("RGB").split()
    r_ok = r.point(lambda v: 255 if 100 <= v <= 235 else 0)
    b_ok = b.point(lambda v: 255 if v <= 45 else 0)
    ratio = ImageChops.subtract(r.point(lambda v: v // 2), g)  # r/2 - g, offset below
    ratio_ok = ImageChops.add(ratio, ImageChops.subtract(g, r.point(lambda v: v // 2))).point(
        lambda v: 255 if v <= 28 else 0)  # |g - r/2| <= 28
    return ImageChops.multiply(ImageChops.multiply(r_ok, b_ok), ratio_ok)


def pointer_by_color(jpeg: bytes | None, ring: float = 0.26, least: int = 150) -> tuple[float, float] | None:
    """Where the pointer is in one frame, from its orange ring; None when no ring-sized cluster is there.

    ring: the ring's diameter as a fraction of the frame width. The densest window of that size is taken, so a
    stray orange pixel elsewhere does not move the answer. The result is the centre of the ring (the click point).
    """
    if not jpeg:
        return None
    try:
        mask = _orange(jpeg)
    except OSError:  # not an image
        return None
    w, h = mask.size
    cell = 6
    gw, gh = w // cell, h // cell
    grid = list(mask.resize((gw, gh), Image.BOX).tobytes())  # 0-255: share of orange in each cell
    side = max(2, round(ring * w / cell))
    best, at = 0, None
    # a sliding window over the cell grid, with row-wise running sums
    for top in range(0, max(1, gh - side + 1)):
        cols = [sum(grid[(top + dy) * gw + x] for dy in range(side)) for x in range(gw)]
        run = sum(cols[:side])
        for left in range(0, max(1, gw - side + 1)):
            if left:
                run += cols[left + side - 1] - cols[left - 1]
            if run > best:
                best, at = run, (left, top)
    if at is None or best * cell * cell / 255 < least:  # orange pixels in the window
        return None
    x0, y0 = at[0] * cell, at[1] * cell
    box = (x0, y0, min(w, x0 + side * cell), min(h, y0 + side * cell))
    data = mask.crop(box).tobytes()
    bw = box[2] - box[0]
    pts = [(i % bw, i // bw) for i, v in enumerate(data) if v]
    if len(pts) < least:
        return None
    cx = sum(x for x, _ in pts) / len(pts) + box[0]
    cy = sum(y for _, y in pts) / len(pts) + box[1]
    return cx / w, cy / h
