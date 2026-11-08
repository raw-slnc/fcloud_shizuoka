# -*- coding: utf-8 -*-
"""
整備事業の検索軽量化リスト（検索条件と番号の対応表）。

森林クラウドの整備事業検索APIは、条件に合う先頭500件しか返さない（総行数は返る）。
このリストは、番号（検索APIの fid ＝ ベクトルタイルの THE_FID）ごとに
  - 検索条件の組み合わせ（年度・事業内容・種類・樹種）
  - その地物が入っているベクトルタイル（ズーム12）
  - 行の内容の照合用の符号（年度・事業内容・種類・樹種・面積・林齢から作る16ビットの値）
を持つ。面積・林齢・形などの値そのものは持たない。表示する値は検索API・タイル・
1件取り出しから取り、呼び出し側でリストと照合したうえで再利用できる。

Qt / QGIS に依存しない（3.x / 4.x のどちらでもそのまま使える）。通信は呼び出し側が行う。
"""
import json
import math
import os
import zlib

FORMAT_NAME = 'fcloud_seibi_index'
FORMAT_VERSION = 1
TILE_ZOOM = 12
TILE_LAYER = 'SHINRIN_SEIBI_JIGYOU'

API_SEARCH_PATH = '/advanced-search/整備事業検索'
API_RECORD_PATH = '/advanced-search/整備事業/{fid}'
API_YEAR_OPTIONS_PATH = '/search-option-value/整備事業年度'
TILE_URL = 'https://fcloud.pref.shizuoka.jp/MAP/MVT/SHINRIN_SEIBI_JIGYOU/{z}/{x}/{y}.pbf'

API_ROW_LIMIT = 500

# 本家サイトの選択肢（固定の一覧。本家サイトのプログラムに書かれている順）
JIGYOU_OPTIONS = (
    '一般造林', '居住地森林環境整備', '無立木地等森林緊急造成', '流域循環資源林整備',
    '流域公益保全林整備', '森林環境保全直接支援', '絆の森整備', '流域育成林整備',
    '流域森総・広域水源', '流域森総・通常分', '被害地等森林整備', '野性鳥獣共存の森整備',
    '森林居住環境整備',
)
SHURUI_OPTIONS = (
    '枝打', '特定間伐', '作業路', '樹下植栽', '改良', '間伐', '除間伐', '誘導伐', '更新伐',
    '付帯施設整備（忌避剤）', '下刈（天然）', '人工造林', '除伐', '下刈', '森林作業道',
    '除間伐（天然）', '付帯施設整備（防護柵）', '下刈（複層林）',
)
JUSHU_OPTIONS = (
    'コナラ', 'クス', 'スギコンテナ苗', 'サクラ', 'カラマツ', 'ミズナラ', 'カエデ', 'ケヤキ',
    '抵抗性ｸﾛﾏﾂ(普通苗)', 'スギ', 'モミ', 'アカマツ', 'ヤマモミジ', 'ヤマボウシ', 'サワラ',
    'クロマツ', 'ヒノキ', 'トウヒ', 'ヤマザクラ', 'フジザクラ', 'クヌギ',
)


class SeibiIndexError(Exception):
    pass


# ── 行の値 ────────────────────────────────────────────────────────────────

def row_from_tile_feature(feat):
    """タイルの地物（mvt_loader.parse_tile の1件）を共通の行の形にする。"""
    return {
        'fid': int(feat['THE_FID']),
        '年度': str(int(feat['YEAR'])),
        '事業内容': str(feat.get('JIGYOU') or ''),
        '種類': str(feat.get('TYPE') or ''),
        '樹種': str(feat.get('JUSHU') or ''),
        '面積': float(feat.get('AREA') or 0.0),
        '林齢': int(feat.get('RINREI') or 0),
    }


def row_from_api_record(rec):
    """検索API・1件取り出しの1件を共通の行の形にする（位置も残す）。"""
    row = {
        'fid': int(rec['fid']),
        '年度': str(int(rec['年度'])),
        '事業内容': str(rec.get('事業内容') or ''),
        '種類': str(rec.get('種類') or ''),
        '樹種': str(rec.get('樹種') or ''),
        '面積': float(rec.get('面積') or 0.0),
        '林齢': int(rec.get('林齢') or 0),
    }
    x, y = rec.get('hilight_point_x'), rec.get('hilight_point_y')
    if x is not None and y is not None:
        row['lon'] = float(x)
        row['lat'] = float(y)
    return row


def combo_of(row):
    return (row['年度'], row['事業内容'], row['種類'], row['樹種'])


def row_hash(row):
    """行の内容の照合用の符号（16ビット）。値を復元できないよう短くしている。"""
    s = '|'.join((row['年度'], row['事業内容'], row['種類'], row['樹種'],
                  f"{float(row['面積']):.2f}", str(int(row['林齢']))))
    return zlib.crc32(s.encode('utf-8')) & 0xFFFF


def sort_key(row):
    """本家サイト（検索API）と同じ並び順：年度の新しい順→事業内容→種類→樹種→面積の大きい順→番号。"""
    return (-int(row['年度']), row['事業内容'], row['種類'], row['樹種'],
            -round(float(row['面積']), 2), int(row['fid']))


def wareki_label(year):
    """本家サイトの年度の表示（表示用_年度）と同じ：2018→H30、2019→H31、2020→R02。"""
    y = int(year)
    if y <= 2019:
        return f'H{y - 1988:02d}'
    return f'R{y - 2018:02d}'


def area_label(area):
    return f'{float(area):.2f}'


# ── タイル ────────────────────────────────────────────────────────────────

def tile_of(lon, lat, zoom=TILE_ZOOM):
    n = 2 ** zoom
    lr = math.radians(lat)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi) / 2.0 * n)
    return x, y


def tile_bounds(x, y, zoom=TILE_ZOOM):
    """タイルの範囲 (西, 南, 東, 北)（経度・緯度）。"""
    n = 2 ** zoom

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return (x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y))


def _boxes_overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


# ── リスト本体 ────────────────────────────────────────────────────────────

class SeibiIndex:
    """番号 → (組み合わせ, タイル, 照合用の符号)。番号は 1〜total。"""

    def __init__(self, total, built, combos, combo_of_fid, tiles, tiles_of_fid, hash_of_fid,
                 tile_less=None, source='', hint=None):
        self.total = int(total)
        self.built = built
        self.combos = [tuple(c) for c in combos]
        self.combo_of_fid = combo_of_fid      # list, index fid-1, -1 = 番号なし
        self.tiles = [tuple(t) for t in tiles]
        self.tiles_of_fid = tiles_of_fid      # list of list[int], index fid-1
        self.hash_of_fid = hash_of_fid        # list, index fid-1, -1 = 番号なし
        self.tile_less = set(tile_less or ())  # ズーム12のタイルに無い番号
        # タイルに無い番号の、位置（森林クラウドの位置）が入るタイルの目印 {番号: tiles の添字}。
        # 計画図レイヤーの範囲に入りうるかの見当に使う（位置そのものは持たない）
        self.hint = dict(hint or {})
        self.source = source
        self._fids_by_combo = None

    # 読み書き
    def to_json(self):
        tiles_col = []
        for ts in self.tiles_of_fid:
            tiles_col.append(ts[0] if len(ts) == 1 else (list(ts) if ts else -1))
        doc = {
            'format': FORMAT_NAME,
            'version': FORMAT_VERSION,
            'built': self.built,
            'total': self.total,
            'zoom': TILE_ZOOM,
            'combos': [list(c) for c in self.combos],
            'tiles': [list(t) for t in self.tiles],
            'c': self.combo_of_fid,
            't': tiles_col,
            'h': self.hash_of_fid,
            'tile_less': sorted(self.tile_less),
            'hint': [[f, ti] for f, ti in sorted(self.hint.items())],
        }
        return json.dumps(doc, ensure_ascii=False, separators=(',', ':'))

    @classmethod
    def from_json(cls, text, source=''):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise SeibiIndexError(f'リストを読めません: {e}')
        if doc.get('format') != FORMAT_NAME or int(doc.get('version', 0)) != FORMAT_VERSION:
            raise SeibiIndexError('リストの形式が違います')
        total = int(doc['total'])
        c, t, h = doc['c'], doc['t'], doc['h']
        if not (len(c) == len(t) == len(h) == total):
            raise SeibiIndexError('リストの件数が合いません')
        tiles_of_fid = []
        for v in t:
            if isinstance(v, list):
                tiles_of_fid.append(v)
            elif v is None or v < 0:
                tiles_of_fid.append([])
            else:
                tiles_of_fid.append([v])
        hint = {int(f): int(ti) for f, ti in doc.get('hint', ())}
        return cls(total, doc.get('built', ''), doc['combos'], c, doc['tiles'], tiles_of_fid, h,
                   doc.get('tile_less', ()), source, hint)

    @classmethod
    def load(cls, path):
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_json(f.read(), source=path)

    def save(self, path):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(self.to_json())
        os.replace(tmp, path)

    # 問い合わせ
    def has(self, fid):
        return 1 <= fid <= self.total and self.combo_of_fid[fid - 1] >= 0

    def missing_fids(self):
        """1〜total のうちリストに無い番号（県の側で消えた等）。"""
        return [i + 1 for i, c in enumerate(self.combo_of_fid) if c < 0]

    def _build_combo_map(self):
        m = {}
        for i, ci in enumerate(self.combo_of_fid):
            if ci >= 0:
                m.setdefault(ci, []).append(i + 1)
        self._fids_by_combo = m

    def fids_for(self, year='', jigyou='', shurui='', jushu=''):
        """条件（空欄は指定なし）に合う番号の一覧。"""
        if self._fids_by_combo is None:
            self._build_combo_map()
        want = (str(year or ''), jigyou or '', shurui or '', jushu or '')
        out = []
        for ci, combo in enumerate(self.combos):
            if all(w == '' or w == v for w, v in zip(want, combo)):
                out.extend(self._fids_by_combo.get(ci, ()))
        out.sort()
        return out

    def tiles_for(self, fids):
        """番号の一覧が入っているタイル（x, y）の集合。"""
        s = set()
        for f in fids:
            for ti in self.tiles_of_fid[f - 1]:
                s.add(self.tiles[ti])
        return s

    def candidates_in_box(self, box):
        """範囲 box (西, 南, 東, 北) に入りうるもの。
        戻り値：(重なるタイル [(x, y)], タイルに無い番号のうち目印のタイルが重なるもの [番号])"""
        tiles = [t for t in self.tiles if _boxes_overlap(tile_bounds(*t), box)]
        tset = set(tiles)
        hinted = [f for f, ti in self.hint.items() if self.tiles[ti] in tset]
        # 目印の無いタイルに無い番号は、位置が分からないので候補に入れて確かめる
        unknown = [f for f in self.tile_less if f not in self.hint and self.has(f)]
        return tiles, sorted(hinted + unknown)

    def check_row(self, row):
        """行がリストと同じか。戻り値：'ok' | 'unknown'（番号がリストに無い） | 'changed'。"""
        fid = row['fid']
        if not self.has(fid):
            return 'unknown'
        ci = self.combo_of_fid[fid - 1]
        if self.combos[ci] != combo_of(row) or self.hash_of_fid[fid - 1] != row_hash(row):
            return 'changed'
        return 'ok'

    # 書き足し（番号が無いものを足す）
    def with_added_rows(self, rows, total, built):
        """total までの範囲に rows（1件取り出しの結果）を書き足した新しいリストを返す。"""
        total = int(total)
        if total < self.total:
            raise SeibiIndexError('総件数が減っています')
        combos = list(self.combos)
        combo_idx = {c: i for i, c in enumerate(combos)}
        tiles = list(self.tiles)
        tile_idx = {t: i for i, t in enumerate(tiles)}
        c = list(self.combo_of_fid) + [-1] * (total - self.total)
        t = [list(v) for v in self.tiles_of_fid] + [[] for _ in range(total - self.total)]
        h = list(self.hash_of_fid) + [-1] * (total - self.total)
        tile_less = set(self.tile_less)
        for row in rows:
            fid = row['fid']
            if not 1 <= fid <= total:
                continue
            cb = combo_of(row)
            if cb not in combo_idx:
                combo_idx[cb] = len(combos)
                combos.append(cb)
            c[fid - 1] = combo_idx[cb]
            h[fid - 1] = row_hash(row)
            if 'lon' in row:
                tt = tile_of(row['lon'], row['lat'])
                if tt not in tile_idx:
                    tile_idx[tt] = len(tiles)
                    tiles.append(tt)
                t[fid - 1] = [tile_idx[tt]]
                # 位置のタイルに本当に入っているかは、表示のときにタイルで確かめる
        return SeibiIndex(total, built, combos, c, tiles, t, h, tile_less, self.source, self.hint)


class SeibiIndexBuilder:
    """タイルと1件取り出しから、リストを一から作る。"""

    def __init__(self):
        self.rows = {}           # fid -> row
        self.tiles_of = {}       # fid -> set((x, y))
        self.conflicts = []      # 同じ番号で値が違ったもの

    def add_tile_features(self, tile_x, tile_y, features):
        for feat in features:
            if 'THE_FID' not in feat:
                continue
            row = row_from_tile_feature(feat)
            fid = row['fid']
            old = self.rows.get(fid)
            if old is not None and (combo_of(old) != combo_of(row) or row_hash(old) != row_hash(row)):
                self.conflicts.append(fid)
            self.rows[fid] = row
            self.tiles_of.setdefault(fid, set()).add((tile_x, tile_y))

    def missing_fids(self, total):
        return [f for f in range(1, int(total) + 1) if f not in self.rows]

    def add_api_row(self, row):
        """タイルに無かった番号を1件取り出しで足す（タイルは持たせない）。"""
        self.rows[row['fid']] = row

    def build(self, total, built):
        total = int(total)
        extra = [f for f in self.rows if not 1 <= f <= total]
        if extra:
            raise SeibiIndexError(f'総件数（{total}）を超える番号があります: {extra[:5]}')
        combos, combo_idx = [], {}
        tiles, tile_idx = [], {}
        c, t, h = [-1] * total, [[] for _ in range(total)], [-1] * total
        tile_less = set()
        hint = {}
        for fid in sorted(self.rows):
            row = self.rows[fid]
            cb = combo_of(row)
            if cb not in combo_idx:
                combo_idx[cb] = len(combos)
                combos.append(cb)
            c[fid - 1] = combo_idx[cb]
            h[fid - 1] = row_hash(row)
            ts = self.tiles_of.get(fid)
            if ts:
                for tt in sorted(ts):
                    if tt not in tile_idx:
                        tile_idx[tt] = len(tiles)
                        tiles.append(tt)
                t[fid - 1] = [tile_idx[tt] for tt in sorted(ts)]
            else:
                tile_less.add(fid)
                if 'lon' in row:
                    tt = tile_of(row['lon'], row['lat'])
                    if tt not in tile_idx:
                        tile_idx[tt] = len(tiles)
                        tiles.append(tt)
                    hint[fid] = tile_idx[tt]
        return SeibiIndex(total, built, combos, c, tiles, t, h, tile_less, hint=hint)
