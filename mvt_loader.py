# -*- coding: utf-8 -*-
"""
MVT (Mapbox Vector Tiles) loader for MAGIS.MORI_NO_CHIKARA.

Fetches protobuf tiles, decodes geometry, returns features as dicts with
'geometry' (WKT, EPSG:4326) and attribute key/value pairs.
"""
import gzip
import math
import struct


# ── protobuf helpers ──────────────────────────────────────────────────────────

def _read_varint(buf, pos):
    result = shift = 0
    while True:
        b = buf[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _zigzag(n):
    return (n >> 1) ^ (-(n & 1))


def _proto_fields(buf, start=0, end=None):
    """Yield (field_number, wire_type, raw_value) from a protobuf message."""
    if end is None:
        end = len(buf)
    pos = start
    while pos < end:
        try:
            tag, pos = _read_varint(buf, pos)
        except IndexError:
            break
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, pos = _read_varint(buf, pos)
            yield field, 0, val
        elif wire == 1:            # 64-bit fixed
            yield field, 1, buf[pos:pos + 8]; pos += 8
        elif wire == 2:            # length-delimited
            length, pos = _read_varint(buf, pos)
            yield field, 2, buf[pos:pos + length]; pos += length
        elif wire == 5:            # 32-bit fixed
            yield field, 5, buf[pos:pos + 4]; pos += 4
        else:
            break


# ── MVT geometry decoder ──────────────────────────────────────────────────────

def _decode_commands(cmds):
    """MVT command sequence → list of rings, each a list of (px, py) ints."""
    rings, ring = [], []
    cx = cy = 0
    i = 0
    while i < len(cmds):
        hdr = cmds[i]; i += 1
        cmd, count = hdr & 7, hdr >> 3
        if cmd == 1:               # MoveTo
            for _ in range(count):
                cx += _zigzag(cmds[i]); i += 1
                cy += _zigzag(cmds[i]); i += 1
                if ring:
                    rings.append(ring)
                ring = [(cx, cy)]
        elif cmd == 2:             # LineTo
            for _ in range(count):
                cx += _zigzag(cmds[i]); i += 1
                cy += _zigzag(cmds[i]); i += 1
                ring.append((cx, cy))
        elif cmd == 7:             # ClosePath
            if ring:
                ring.append(ring[0])
                rings.append(ring)
                ring = []
    if ring:
        rings.append(ring)
    return rings


def _tile_coord_to_lonlat(px, py, tile_x, tile_y, zoom, extent):
    n = 2 ** zoom
    xf = (tile_x + px / extent) / n
    yf = (tile_y + py / extent) / n
    lon = xf * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * yf))))
    return lon, lat


def _ring_signed_area(ring):
    area2 = 0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        area2 += x1 * y2 - x2 * y1
    return area2 / 2.0


def _rings_to_wkt(rings, tile_x, tile_y, zoom, extent):
    if not rings:
        return None

    polygons = []
    current = None
    for ring in rings:
        if len(ring) < 4:
            continue
        coords = []
        for px, py in ring:
            lon, lat = _tile_coord_to_lonlat(px, py, tile_x, tile_y, zoom, extent)
            coords.append(f'{lon:.7f} {lat:.7f}')
        if len(coords) < 4:
            continue

        ring_wkt = '(' + ','.join(coords) + ')'
        is_outer = _ring_signed_area(ring) > 0
        if is_outer or current is None:
            current = [ring_wkt]
            polygons.append(current)
        else:
            current.append(ring_wkt)

    if not polygons:
        return None
    if len(polygons) == 1:
        return 'POLYGON(' + ','.join(polygons[0]) + ')'
    return 'MULTIPOLYGON(' + ','.join(
        '(' + ','.join(poly) + ')' for poly in polygons
    ) + ')'


# ── protobuf value decoder ────────────────────────────────────────────────────

def _decode_value(buf):
    """Decode an MVT Value message → Python scalar."""
    for f, w, v in _proto_fields(buf):
        if f == 1 and w == 2:   return v.decode('utf-8', errors='replace')
        if f == 2 and w == 5:   return struct.unpack('<f', v)[0]
        if f == 3 and w == 1:   return struct.unpack('<d', v)[0]
        if f == 4 and w == 0:   return v                   # int64  (no zigzag)
        if f == 5 and w == 0:   return v                   # uint64
        if f == 6 and w == 0:   return _zigzag(v)          # sint64 (zigzag)
        if f == 7 and w == 0:   return bool(v)
    return None


# ── packed uint32 reader ──────────────────────────────────────────────────────

def _read_packed_uint32(buf):
    result = []
    pos = 0
    while pos < len(buf):
        v, pos = _read_varint(buf, pos)
        result.append(v)
    return result


# ── tile parser ───────────────────────────────────────────────────────────────

def parse_tile(tile_bytes, tile_x, tile_y, zoom, target_layer=None):
    """
    Parse MVT binary (possibly gzip-compressed) for one tile.

    Returns a list of dicts: {'geometry': wkt_str, ...attributes...}
    Only polygon features (geom_type==3) are returned.
    """
    raw = bytes(tile_bytes)
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)

    features_out = []

    for fn, wt, val in _proto_fields(raw):
        if fn != 3 or wt != 2:         # only Layer messages (field 3)
            continue
        layer_buf = val

        name = None
        keys = []
        values = []
        extent = 4096
        raw_features = []

        for lf, lwt, lval in _proto_fields(layer_buf):
            if   lf == 1  and lwt == 2: name = lval.decode('utf-8', errors='replace')
            elif lf == 5  and lwt == 0: extent = lval
            elif lf == 3  and lwt == 2: keys.append(lval.decode('utf-8', errors='replace'))
            elif lf == 4  and lwt == 2: values.append(_decode_value(lval))
            elif lf == 2  and lwt == 2: raw_features.append(lval)

        if target_layer and name != target_layer:
            continue

        for feat_buf in raw_features:
            geom_type = 0
            tags_buf = b''
            geom_buf = b''

            for ff, fwt, fval in _proto_fields(feat_buf):
                if   ff == 3 and fwt == 0: geom_type = fval
                elif ff == 2 and fwt == 2: tags_buf = fval
                elif ff == 4 and fwt == 2: geom_buf = fval

            if geom_type != 3:              # polygons only
                continue

            tags  = _read_packed_uint32(tags_buf)
            cmds  = _read_packed_uint32(geom_buf)
            rings = _decode_commands(cmds)
            wkt   = _rings_to_wkt(rings, tile_x, tile_y, zoom, extent)
            if not wkt:
                continue

            attrs = {}
            for i in range(0, len(tags) - 1, 2):
                ki, vi = tags[i], tags[i + 1]
                if ki < len(keys) and vi < len(values):
                    attrs[keys[ki]] = values[vi]

            d = {'geometry': wkt}
            d.update(attrs)
            features_out.append(d)

    return features_out


# ── tile coordinate helpers ───────────────────────────────────────────────────

def _lon_to_tile_x(lon, z):
    return int((lon + 180.0) / 360.0 * (2 ** z))


def _lat_to_tile_y(lat, z):
    lr = math.radians(lat)
    return int((1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi) / 2.0 * (2 ** z))


def shizuoka_tiles(zoom=10):
    """
    Return list of (tile_x, tile_y) pairs covering Shizuoka prefecture
    at the given zoom level.
    """
    x0 = _lon_to_tile_x(137.25, zoom)
    x1 = _lon_to_tile_x(139.25, zoom)
    y0 = _lat_to_tile_y(35.80,  zoom)   # north → smaller y
    y1 = _lat_to_tile_y(34.40,  zoom)   # south → larger y
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
