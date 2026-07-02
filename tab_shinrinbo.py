# -*- coding: utf-8 -*-
import math
import sip
import struct

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame,
)
from qgis.PyQt.QtCore import Qt, QObject, QEvent
from qgis.core import (
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsProject, QgsPointXY,
)

from .constants import (
    _API_BASE,
    _PRIMARY_FIELDS, _HISTORY_FIELDS,
)

class _ShiftScrollFilter(QObject):
    """Shift+ホイールで水平スクロールに変換するイベントフィルタ。"""
    def __init__(self, table):
        super().__init__(table)
        self._table = table

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            if event.modifiers() & Qt.ShiftModifier:
                sb = self._table.horizontalScrollBar()
                sb.setValue(sb.value() - event.angleDelta().y())
                return True
        return False


_MVT_BASE = 'https://fcloud.pref.shizuoka.jp'
_MVT_PATH = '/MAP/MVT/MAGIS.SHOHAN_SHINRINBO.5JOU'

# API応答の表示列（順序固定・全列常時表示）
_API_TABLE_COLS = [
    ('計画区',               '計画区'),
    ('市町村',               '市町村'),
    ('林班',                 '林班'),
    ('準林班',               '準林班'),
    ('小班ラベル',           '小班'),
    ('小班_枝番',            '枝番'),
    ('大字',                 '大字'),
    ('表示用_地番',          '地番'),
    ('面積',                 '面積(ha)'),
    ('林種',                 '林種'),
    ('樹種',                 '樹種'),
    ('林齢',                 '林齢'),
    ('齢級',                 '齢級'),
    ('林地の生産力',         '林地生産力'),
    ('ゾーニング',           'ゾーニング'),
    ('施業種',               '施業種'),
    ('新特定施業森林',       '新特定施業森林'),
    ('表示用_材積',          '材積(m³)'),
    ('表示用_成長量',        '成長量(m³)'),
    ('道からの距離',         '道からの距離(m)'),
    ('土壌１名称',           '土壌'),
    ('施業履歴_施業年度１',  '施業年度1'),
    ('施業履歴_施業方法１',  '施業方法1'),
    ('施業履歴_事業種別１',  '事業種別1'),
    ('施業履歴_施業年度２',  '施業年度2'),
    ('施業履歴_施業方法２',  '施業方法2'),
    ('施業履歴_事業種別２',  '事業種別2'),
    ('施業履歴_施業年度３',  '施業年度3'),
    ('施業履歴_施業方法３',  '施業方法3'),
    ('施業履歴_事業種別３',  '事業種別3'),
    ('施業履歴_施業年度４',  '施業年度4'),
    ('施業履歴_施業方法４',  '施業方法4'),
    ('施業履歴_事業種別４',  '事業種別4'),
    ('施業履歴_施業年度５',  '施業年度5'),
    ('施業履歴_施業方法５',  '施業方法5'),
    ('施業履歴_事業種別５',  '事業種別5'),
]

_NULL_DISPLAY = {'', 'NULL', 'None', None}


class ShinrinboMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_shinrinbo(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(0)

        self.tbl_shinrinbo = QTableWidget(0, 0)
        self.tbl_shinrinbo.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_shinrinbo.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_shinrinbo.setAlternatingRowColors(True)
        self.tbl_shinrinbo.setFrameShape(QFrame.NoFrame)
        self.tbl_shinrinbo.setStyleSheet(
            'QTableWidget { border: 1px solid palette(mid); }')
        self.tbl_shinrinbo.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        self.tbl_shinrinbo.setVerticalScrollMode(QTableWidget.ScrollPerPixel)
        hdr = self.tbl_shinrinbo.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setStretchLastSection(False)
        self.tbl_shinrinbo.verticalHeader().setVisible(False)
        self.tbl_shinrinbo.viewport().installEventFilter(
            _ShiftScrollFilter(self.tbl_shinrinbo)
        )

        v.addWidget(self.tbl_shinrinbo, 1)
        return w

    # ------------------------------------------------------------------
    # ヘッダー確定（レイヤー選択時に一度だけ呼ぶ）
    # ------------------------------------------------------------------

    def _init_shinrinbo_headers(self):
        """_layer_type に基づきヘッダー列を確定する。フィーチャー選択では呼ばない。"""
        if self._layer_type == 'gpkg':
            layer = self._connected_layer
            existing = set()
            if layer is not None and not sip.isdeleted(layer):
                try:
                    existing = {f.name() for f in layer.fields()}
                except Exception:
                    pass
            col_map = [
                (src, label) for src, label in _PRIMARY_FIELDS
                if src in existing
            ]
            for i, (y_f, m_f, e_f) in enumerate(_HISTORY_FIELDS):
                if y_f in existing:
                    n = str(i + 1)
                    col_map += [
                        (y_f, f'施業年度{n}'),
                        (m_f, f'施業方法{n}'),
                        (e_f, f'事業種別{n}'),
                    ]
            self._shinrinbo_col_map = col_map

        elif self._layer_type == 'cd_gpkg':
            self._shinrinbo_col_map = list(_API_TABLE_COLS)

        else:
            self._shinrinbo_col_map = []

        headers = [label for _, label in self._shinrinbo_col_map]
        self.tbl_shinrinbo.setColumnCount(len(headers))
        if headers:
            self.tbl_shinrinbo.setHorizontalHeaderLabels(headers)
        self.tbl_shinrinbo.setRowCount(0)

    # ------------------------------------------------------------------
    # 更新エントリポイント（features = list[QgsFeature]、選択変更時）
    # ------------------------------------------------------------------

    def _refresh_shinrinbo_tab(self, features):
        self._current_shinrinbo_key = ''
        self._shinrinbo_api_ids = []
        self._shinrinbo_generation = getattr(self, '_shinrinbo_generation', 0) + 1
        gen = self._shinrinbo_generation

        if not features:
            self.tbl_shinrinbo.setRowCount(0)
            self._update_cache_btn_states()
            return

        if self._layer_type == 'gpkg':
            self._build_shinrinbo_table_gpkg(features)
            self._sync_shinrinbo_cache_ts('ローカルデータ')
            self._update_cache_btn_states()
            return

        # cd_gpkg: 2段階
        # Stage 1: KEY1 → THE_FID (MVT タイル)
        self._sync_shinrinbo_cache_ts('小班ID解決中...')

        def on_fids_resolved(fid_list):
            if self._shinrinbo_generation != gen:
                return
            api_ids = [str(f) if f is not None else None for f in fid_list]
            self._shinrinbo_api_ids = [a for a in api_ids if a]
            if self._shinrinbo_api_ids:
                self._current_shinrinbo_key = f'森林簿/{self._shinrinbo_api_ids[-1]}'
            # Stage 2: 森林簿データ取得
            self._fetch_shinrinbo_batch(api_ids, gen, force=False)

        self._resolve_fids_via_mvt(features, on_fids_resolved)

    # ------------------------------------------------------------------
    # MVT タイルで KEY1 → THE_FID を一括解決
    # ------------------------------------------------------------------

    def _resolve_fids_via_mvt(self, features, on_done):
        """KEY1 → THE_FID を MVT タイルで解決。完了後 on_done(fid_list) を呼ぶ。
        世代チェックは呼び出し元の on_done 内で行うこと。"""
        n = len(features)
        fid_list = [None] * n

        tile_groups = {}
        for i, feat in enumerate(features):
            fnames = feat.fields().names()
            if 'KEY1' not in fnames:
                continue
            key1 = str(feat['KEY1'])
            if not key1 or key1 in ('NULL', 'None', ''):
                continue
            centroid = self._feature_centroid_wgs84(feat)
            if centroid is None:
                continue
            lon, lat = centroid
            tx, ty = self._tile_xyz(lon, lat, 13)
            tile_key = (13, tx, ty)
            tile_groups.setdefault(tile_key, []).append((i, key1))

        if not tile_groups:
            on_done(fid_list)
            return

        pending = [len(tile_groups)]
        mvt_cache = getattr(self, '_mvt_tile_cache', {})

        def process_tile(tile_key, mapping):
            for idx, key1 in tile_groups[tile_key]:
                if key1 in mapping:
                    fid_list[idx] = mapping[key1]
            pending[0] -= 1
            if pending[0] == 0:
                on_done(fid_list)

        for tile_key in tile_groups:
            if tile_key in mvt_cache:
                process_tile(tile_key, mvt_cache[tile_key])
            else:
                z, tx, ty = tile_key
                url = f'{_MVT_BASE}{_MVT_PATH}/{z}/{tx}/{ty}.pbf'

                def make_cb(tk):
                    def cb(data):
                        mapping = self._parse_mvt_key1_to_fid(data) if data else {}
                        self._mvt_tile_cache[tk] = mapping
                        process_tile(tk, mapping)
                    return cb

                self._get_binary(url, make_cb(tile_key))

    def _feature_centroid_wgs84(self, feat):
        try:
            layer = self._connected_layer
            if layer is None or sip.isdeleted(layer):
                return None
            geom = feat.geometry()
            cp = geom.centroid().asPoint()
            src_crs = layer.crs()
            dst_crs = QgsCoordinateReferenceSystem('EPSG:4326')
            xf = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
            pt = xf.transform(QgsPointXY(cp.x(), cp.y()))
            return pt.x(), pt.y()
        except Exception:
            return None

    @staticmethod
    def _tile_xyz(lon, lat, zoom=13):
        n = 2 ** zoom
        x = int((lon + 180) / 360 * n)
        lat_r = math.radians(lat)
        y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
        return x, y

    @staticmethod
    def _parse_mvt_key1_to_fid(raw_bytes):
        """MVT PBF を解析して {key1: the_fid} マッピングを返す"""
        def read_varint(buf, pos):
            result = shift = 0
            while True:
                b = buf[pos]; pos += 1
                result |= (b & 0x7F) << shift
                if not (b & 0x80):
                    return result, pos
                shift += 7

        def parse_value(buf):
            pos = 0
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                wtype = tag & 7
                if wtype == 2:
                    l, pos = read_varint(buf, pos)
                    return buf[pos:pos + l].decode('utf-8', errors='replace')
                elif wtype == 0:
                    v, pos = read_varint(buf, pos)
                    return v
                elif wtype == 5:
                    return struct.unpack_from('<f', buf, pos)[0]
                elif wtype == 1:
                    return struct.unpack_from('<d', buf, pos)[0]
            return None

        def parse_feature_tags(buf):
            pos = 0; tags = []
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                field, wtype = tag >> 3, tag & 7
                if field == 2 and wtype == 2:
                    l, pos = read_varint(buf, pos)
                    chunk = buf[pos:pos + l]; pos += l
                    p2 = 0
                    while p2 < len(chunk):
                        v, p2 = read_varint(chunk, p2)
                        tags.append(v)
                elif wtype == 2:
                    l, pos = read_varint(buf, pos); pos += l
                elif wtype == 0:
                    _, pos = read_varint(buf, pos)
                elif wtype in (1, 5):
                    pos += (8 if wtype == 1 else 4)
                else:
                    break
            return tags

        def parse_layer(buf):
            pos = 0; keys = []; vals = []; feat_tags_list = []
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                field, wtype = tag >> 3, tag & 7
                if wtype == 2:
                    l, pos = read_varint(buf, pos)
                    chunk = buf[pos:pos + l]; pos += l
                    if field == 3:
                        keys.append(chunk.decode('utf-8', errors='replace'))
                    elif field == 4:
                        vals.append(parse_value(chunk))
                    elif field == 2:
                        feat_tags_list.append(parse_feature_tags(chunk))
                elif wtype == 0:
                    _, pos = read_varint(buf, pos)
                elif wtype in (1, 5):
                    pos += (8 if wtype == 1 else 4)
                else:
                    break
            return keys, vals, feat_tags_list

        buf = raw_bytes
        pos = 0
        while pos < len(buf):
            tag, pos = read_varint(buf, pos)
            field, wtype = tag >> 3, tag & 7
            if field == 3 and wtype == 2:
                l, pos = read_varint(buf, pos)
                keys, vals, feat_tags_list = parse_layer(buf[pos:pos + l])
                pos += l
                try:
                    ki_key1 = keys.index('KEY1')
                    ki_fid = keys.index('THE_FID')
                except ValueError:
                    continue
                mapping = {}
                for tags in feat_tags_list:
                    prop = {keys[tags[i]]: vals[tags[i + 1]]
                            for i in range(0, len(tags) - 1, 2)}
                    k1 = prop.get('KEY1')
                    fid = prop.get('THE_FID')
                    if k1 and fid is not None and k1 not in mapping:
                        mapping[k1] = int(fid)
                return mapping
            elif wtype == 2:
                l, pos = read_varint(buf, pos); pos += l
            elif wtype == 0:
                _, pos = read_varint(buf, pos)
            elif wtype in (1, 5):
                pos += (8 if wtype == 1 else 4)
            else:
                break
        return {}

    # ------------------------------------------------------------------
    # バッチ取得（Stage 2）
    # ------------------------------------------------------------------

    def _fetch_shinrinbo_batch(self, api_ids, gen, force=False):
        n = len(api_ids)
        results = [None] * n
        pending = [0]

        def on_done():
            if self._shinrinbo_generation != gen:
                return
            self._build_shinrinbo_table_api(results)
            last_id = next((aid for aid in reversed(api_ids) if aid), None)
            ts_text = '取得日時: —'
            if last_id:
                db = self._get_db('森林簿')
                if db:
                    ts = db.get_fetched_at(f'森林簿/{last_id}')
                    if ts:
                        ts_text = f'取得日時: {ts}'
            self._sync_shinrinbo_cache_ts(ts_text)
            self._update_cache_btn_states()

        for i, api_id in enumerate(api_ids):
            if api_id is None:
                results[i] = {}
                continue
            cache_key = f'森林簿/{api_id}'
            if not force:
                db = self._get_db('森林簿')
                if db is not None:
                    cached, _ = db.get(cache_key)
                    if cached is not None:
                        results[i] = cached
                        continue
            pending[0] += 1

            def make_cb(idx, key, gen_cap):
                def cb(data):
                    if self._shinrinbo_generation != gen_cap:
                        return
                    results[idx] = data or {}
                    if data:
                        db2 = self._get_db('森林簿')
                        if db2:
                            db2.put(key, data)
                    pending[0] -= 1
                    if pending[0] == 0:
                        on_done()
                return cb

            self._get_api(
                f'{_API_BASE}/advanced-search/森林簿/{api_id}',
                make_cb(i, cache_key, gen),
            )

        if pending[0] == 0:
            on_done()
        else:
            self._sync_shinrinbo_cache_ts('取得中...')

    # ------------------------------------------------------------------
    # 更新ボタン
    # ------------------------------------------------------------------

    def _update_shinrinbo_cache(self):
        if not self._shinrinbo_api_ids:
            return
        self._shinrinbo_generation = getattr(self, '_shinrinbo_generation', 0) + 1
        self.lbl_cache_ts.setText('取得日時: 更新中...')
        self._fetch_shinrinbo_batch(
            self._shinrinbo_api_ids,
            self._shinrinbo_generation,
            force=True,
        )

    # ------------------------------------------------------------------
    # テーブル行更新（ヘッダーは変えない）
    # ------------------------------------------------------------------

    def _build_shinrinbo_table_gpkg(self, features):
        col_map = getattr(self, '_shinrinbo_col_map', [])
        self.tbl_shinrinbo.setRowCount(len(features))
        for row, feat in enumerate(features):
            fnames = feat.fields().names()
            for col, (src, _) in enumerate(col_map):
                val = ''
                if src in fnames:
                    v = feat[src]
                    if v is not None and str(v) not in ('NULL',):
                        val = str(v)
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.tbl_shinrinbo.setItem(row, col, item)

    def _build_shinrinbo_table_api(self, results):
        col_map = getattr(self, '_shinrinbo_col_map', [])
        self.tbl_shinrinbo.setRowCount(len(results))
        for row, data in enumerate(results):
            d = data or {}
            for col, (src, _) in enumerate(col_map):
                raw = d.get(src)
                text = '' if raw is None or str(raw) in _NULL_DISPLAY else str(raw)
                if '年度' in src and text == '0':
                    text = ''
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.tbl_shinrinbo.setItem(row, col, item)

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    def _sync_shinrinbo_cache_ts(self, text):
        if self.cloud_tab.currentIndex() == self._shinrinbo_tab_index:
            self.lbl_cache_ts.setText(text)
