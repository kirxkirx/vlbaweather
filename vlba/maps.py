"""The per-antenna map: the parent's ``weather.radar.SiteMap`` (basemap, MRMS radar, alert
areas, marker, scale bars, dBZ bar, caption) with three additions:

* a radar layer that is already in the frame's pixel space (``radar["image"]`` an RGBA image
  of ``frame.size``: the NWS composite ``vlba.nwsradar`` decodes for the sites outside the
  MRMS grid) is composited as it is instead of being reprojected from the MRMS grid;
* the two rings of the verdict are drawn over the alerts: the radar ring (``cfg.radar_km``,
  solid) and the lightning ring (``cfg.glm_km``, dashed), thin, in the theme's ink over its
  stroke colour so they stay visible over echoes of any hue;
* the GLM flashes of the fetch window (``overlay["flashes"]``, (lat, lon) pairs) are drawn
  as small lightning-yellow diamonds with a dark casing, and a one-line key names them.

The scale bars are 10 mi and 10 km (``SCALE_BARS``) instead of the parent's 25 mi and
10 mi: the rings and the distances on the page are in km. Everything else, including the caption (which names the product from ``radar["label"]``),
is the parent's. The overlay is drawn in ``_decorate`` before the parent's decorations, so
the marker, scale bars and caption stay on top; a failure in the overlay costs the overlay,
never the map (logged, and noted in the render result's ``error``).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw
except Exception:                       # pragma: no cover - optional dependency
    Image = ImageDraw = None

from weather import geo, radar

log = logging.getLogger("vlba.maps")

FLASH_RGB = (255, 221, 0)               # lightning yellow
FLASH_CASING = (40, 30, 0)
RING_DASH = (7.0, 5.0)                  # on/off along the GLM ring, in px


def _circle_points(frame, lat: float, lon: float, km: float, n: int = 180) -> List[Tuple[float, float]]:
    """The ring of ``km`` around (lat, lon) as frame pixels, from geodesic destination
    points (so the ring is right on the Mercator canvas at any latitude)."""
    pts = []
    for k in range(n + 1):
        plat, plon = geo.dest_point(lat, lon, km, 360.0 * k / n)
        pts.append(frame.lonlat_to_px(plon, plat))
    return pts


def _dashes(pts: Sequence[Tuple[float, float]], on: float, off: float) -> List[List[Tuple[float, float]]]:
    """Split a polyline into dashes of ``on`` px separated by ``off`` px."""
    out: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    drawing = True
    left = on
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        pos = 0.0
        if drawing and not cur:
            cur.append((x0, y0))
        while seg - pos > left:
            pos += left
            t = pos / seg
            p = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            if drawing:
                cur.append(p)
                out.append(cur)
                cur = []
            else:
                cur = [p]
            drawing = not drawing
            left = on if drawing else off
        left -= seg - pos
        if drawing:
            cur.append((x1, y1))
    if drawing and len(cur) > 1:
        out.append(cur)
    return out


class VlbaSiteMap(radar.SiteMap):
    """``SiteMap`` plus the rings and flashes. ``overlay`` = ``{"radar_km": float,
    "glm_km": float|None, "flashes": [(lat, lon)], "glm_label": str}`` (any key may be
    missing; None draws nothing extra)."""

    SCALE_BARS = (("10 mi", 10 * geo.MI_TO_KM), ("10 km", 10.0))

    def __init__(self, cfg, cache, site, theme: str, tile_url: str,
                 overlay: Optional[Dict[str, Any]] = None):
        super().__init__(cfg, cache, site, theme, tile_url)
        self.overlay = dict(overlay) if isinstance(overlay, dict) else {}
        self.overlay_note: Optional[str] = None
        self.flashes_drawn = 0

    # -- the radar layer -----------------------------------------------------------------
    def _radar_layer(self, img):
        """An image already in the frame's pixel space (RGBA, ``frame.size``) is the layer
        itself; anything else is the MRMS composite and goes through the parent."""
        if Image is not None and getattr(img, "mode", None) == "RGBA" \
                and tuple(img.size) == tuple(self.frame.size):
            return img
        return super()._radar_layer(img)

    # -- the overlay ---------------------------------------------------------------------
    def _draw_overlay(self, im) -> None:
        fr, col = self.frame, self.colors
        ink, stroke = col["ink"] + (255,), col["stroke"] + (255,)
        w, h = fr.size
        layer = Image.new("RGBA", fr.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        lw = max(1, w // 300)
        radar_km = self.overlay.get("radar_km")
        if isinstance(radar_km, (int, float)) and radar_km > 0:
            pts = _circle_points(fr, self.lat, self.lon, float(radar_km))
            d.line(pts, fill=stroke, width=lw + 2)
            d.line(pts, fill=ink, width=lw)
        glm_km = self.overlay.get("glm_km")
        if isinstance(glm_km, (int, float)) and glm_km > 0:
            pts = _circle_points(fr, self.lat, self.lon, float(glm_km), n=360)
            for dash in _dashes(pts, *RING_DASH):
                d.line(dash, fill=stroke, width=lw + 2)
            for dash in _dashes(pts, *RING_DASH):
                d.line(dash, fill=ink, width=lw)
        flashes = self.overlay.get("flashes") or []
        r = max(3, w // 150)
        drawn = 0
        for f in flashes:
            try:
                flat, flon = float(f[0]), float(f[1])
            except (TypeError, ValueError, IndexError):
                continue
            x, y = fr.lonlat_to_px(flon, flat)
            if not (-r <= x <= w + r and -r <= y <= h + r):
                continue
            diamond = [(x, y - r - 1), (x + r + 1, y), (x, y + r + 1), (x - r - 1, y)]
            d.polygon(diamond, fill=FLASH_CASING + (255,))
            inner = [(x, y - r), (x + r, y), (x, y + r), (x - r, y)]
            d.polygon(inner, fill=FLASH_RGB + (255,))
            drawn += 1
        self.flashes_drawn = drawn
        im.alpha_composite(layer)

    def _caption_bottom(self, im, radar_res: dict, notes: Sequence[str]) -> float:
        """Where the parent's caption ends: it runs top-left, as wide as the map, one line for
        the frame (two when it is stale) and one per note, at the parent's fonts and
        spacing (``SiteMap._decorate``)."""
        w = self.frame.size[0]
        base = max(9, min(14, w // 45))
        d = ImageDraw.Draw(im)
        res = radar_res if isinstance(radar_res, dict) else {}
        n = 1 + (1 if res.get("ok") and isinstance(res.get("ts"), datetime) and res.get("stale") else 0)
        n += sum(1 for x in notes if x)
        y = 6.0
        for i in range(n):
            y += radar._text_size(d, "Mg", radar._font(base if i == 0 else max(8, base - 2)))[1] + 5
        return y

    def _draw_key(self, im, y0: float) -> None:
        """The key for the rings and the flashes, right-aligned from ``y0`` down."""
        fr, col = self.frame, self.colors
        ink, stroke = col["ink"] + (255,), col["stroke"] + (255,)
        w, h = fr.size
        radar_km = self.overlay.get("radar_km")
        glm_km = self.overlay.get("glm_km")
        flashes = self.overlay.get("flashes") or []
        drawn = self.flashes_drawn
        labels = []
        if isinstance(radar_km, (int, float)) and radar_km > 0:
            labels.append("ring %g km: radar check" % radar_km)
        if isinstance(glm_km, (int, float)) and glm_km > 0:
            labels.append("dashed %g km: lightning check" % glm_km)
        if flashes or self.overlay.get("glm_label"):
            what = self.overlay.get("glm_label") or "GLM flashes on the map"
            labels.append("%s %s" % ("◆" if drawn else "no", what))
        if labels:
            # the key, right-aligned, one line each, below the parent's caption (which may
            # run the whole width: a long "radar unavailable" reason); the scale bars and
            # the dBZ bar are bottom-left and bottom-right
            dd = ImageDraw.Draw(im)
            base = max(8, min(12, w // 55))
            font = radar._font(base)
            y = y0
            for text in labels:
                swatch = text.startswith("\u25c6")
                body = text[1:].strip() if swatch else text
                body = radar._fit_text(dd, body, font, w // 2)
                tw, th = radar._text_size(dd, body, font)
                sw = base * 0.45
                x = w - 8 - tw
                if swatch:
                    # a yellow diamond swatch instead of the glyph (fonts lack it at times)
                    cx, cy = x - sw - 4, y + th / 2.0
                    dd.polygon([(cx, cy - sw), (cx + sw, cy), (cx, cy + sw), (cx - sw, cy)],
                               fill=FLASH_CASING + (255,))
                    dd.polygon([(cx, cy - sw + 1), (cx + sw - 1, cy), (cx, cy + sw - 1), (cx - sw + 1, cy)],
                               fill=FLASH_RGB + (255,))
                radar._draw_text(dd, (x, y), body, font, fill=ink, stroke_width=2, stroke_fill=stroke)
                y += th + 4

    def _decorate(self, im, radar_res: dict, notes: Sequence[str], tz: Optional[str],
                  road_kinds: Optional[Dict[str, bool]] = None) -> None:
        if self.overlay:
            try:
                self._draw_overlay(im)
            except Exception as e:      # noqa: BLE001 - the overlay is an extra
                log.warning("map overlay failed for %s/%s: %s", self.slug, self.theme, e)
                self.overlay_note = "rings and flashes not drawn: %s" % e
                notes = list(notes) + [self.overlay_note]
        super()._decorate(im, radar_res, notes, tz, road_kinds=road_kinds)
        if self.overlay and not self.overlay_note:
            try:
                self._draw_key(im, self._caption_bottom(im, radar_res, notes) + 2)
            except Exception as e:      # noqa: BLE001 - the key is an extra too
                log.warning("map key failed for %s/%s: %s", self.slug, self.theme, e)
