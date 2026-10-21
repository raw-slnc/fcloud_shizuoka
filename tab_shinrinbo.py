# -*- coding: utf-8 -*-
import math
import sip
import struct

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView, QAbstractItemView,
    QHeaderView, QFrame, QPushButton,
)
from qgis.PyQt.QtCore import Qt, QAbstractTableModel, QModelIndex
from qgis.core import (
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsProject, QgsPointXY, QgsFeatureRequest,
)

from .constants import (
    _API_BASE, _TOGGLE_BTN_QSS_ONOFF,
    _PRIMARY_FIELDS, _HISTORY_FIELDS, _MORINBO_OLD_CD_TO_CITY, _SHINRINBO_COL_WIDTHS,
)

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


def _text_width(fm, text):
    """文字列の幅（ピクセル）。horizontalAdvance は Qt 5.11 から。それより古い Qt では width を使う。"""
    return fm.horizontalAdvance(text) if hasattr(fm, 'horizontalAdvance') else fm.width(text)


class _ShinrinboApiModel(QAbstractTableModel):
    """森林クラウドから取った森林簿の表（1行＝1小班の dict）。
    値は表が描くときに文字にする（セルごとの部品を作らないので、行が多くても軽い）。"""

    def __init__(self, col_map, rows, parent=None):
        super().__init__(parent)
        self._cols = list(col_map)
        self._rows = list(rows)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._cols)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self._cols):
            return self._cols[section][1]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        d = self._rows[index.row()] or {}
        src = self._cols[index.column()][0]
        raw = d.get(src)
        text = '' if raw is None or str(raw) in _NULL_DISPLAY else str(raw)
        if '年度' in src and text == '0':
            text = ''
        return text

    def row_data(self, row):
        return (self._rows[row] or {}) if 0 <= row < len(self._rows) else None


class _ShinrinboLayerModel(QAbstractTableModel):
    """計画図レイヤーの森林簿の列の表。最初に持つのは選択中の小班の ID（fid）の一覧だけで、
    属性は表が描く行の分だけ、100 行ずつまとめて読む（読んだ行は覚えておく）。
    行が多くても表はすぐ出て、スクロールしたところだけ読む。値は属性の値をそのまま文字にする（NULL は空欄）。"""

    _BLOCK = 100  # 一度に読む行数

    def __init__(self, layer, fids, col_map, parent=None):
        super().__init__(parent)
        self._layer = layer
        self._fids = list(fids)
        self._labels = [label for _, label in col_map]
        self._idx = [layer.fields().indexOf(src) for src, _ in col_map]
        self._values = {}     # fid → 列の値
        self._loaded = set()  # 読み終えたまとまりの番号
        # レイヤーが編集などで変わったら、読んだ値を捨てて描き直す（描くときに読み直す）
        layer.dataChanged.connect(self._on_layer_changed)

    def _on_layer_changed(self):
        self.beginResetModel()
        self._values.clear()
        self._loaded.clear()
        self.endResetModel()

    def _load_block(self, block):
        ids = self._fids[block * self._BLOCK:(block + 1) * self._BLOCK]
        req = (QgsFeatureRequest().setFilterFids(ids)
               .setSubsetOfAttributes(self._idx).setFlags(QgsFeatureRequest.NoGeometry))
        try:
            for f in self._layer.getFeatures(req):
                attrs = f.attributes()
                self._values[f.id()] = [attrs[i] for i in self._idx]
        except RuntimeError:
            pass  # レイヤーが削除済み（表はすぐ差し替えられる）
        self._loaded.add(block)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._fids)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._labels)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self._labels):
            return self._labels[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        row = index.row()
        block = row // self._BLOCK
        if block not in self._loaded:
            self._load_block(block)
        values = self._values.get(self._fids[row])
        v = values[index.column()] if values else None
        if v is None or (hasattr(v, 'isNull') and v.isNull()) or str(v) == 'NULL':
            return ''
        return str(v)

    def fid(self, row):
        return self._fids[row]


class ShinrinboMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_shinrinbo(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_kozu_shinrinbo = QPushButton('公図連携OFF')
        self.btn_kozu_shinrinbo.setCheckable(True)
        self.btn_kozu_shinrinbo.setStyleSheet(_TOGGLE_BTN_QSS_ONOFF)
        # 幅は広い方（公図連携OFF）で固定して ON/OFF 切替で伸縮させない
        self.btn_kozu_shinrinbo.setFixedWidth(self.btn_kozu_shinrinbo.sizeHint().width())
        self.btn_kozu_shinrinbo.setToolTip('ONにすると小班選択のたびに自動で Kozu XML Integrator へ送信')
        row.addWidget(self.btn_kozu_shinrinbo)
        self._update_kozu_btn(self.btn_kozu_shinrinbo)
        v.addLayout(row)

        # 表はモデルとビューを分ける作り（行が多くても、描くセルの分だけ値を読む）。
        # モデルは型で違う：計画図の属性（gpkg 型）は _ShinrinboLayerModel、森林クラウドの森林簿は _ShinrinboApiModel
        self.tbl_shinrinbo = QTableView()
        self.tbl_shinrinbo.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_shinrinbo.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_shinrinbo.setAlternatingRowColors(True)
        self.tbl_shinrinbo.setFrameShape(QFrame.NoFrame)
        self.tbl_shinrinbo.setStyleSheet(
            'QTableView { border: 1px solid palette(dark); }')
        hdr = self.tbl_shinrinbo.horizontalHeader()
        # 列幅は列ごとに決めた幅（_SHINRINBO_COL_WIDTHS）。データや選択では変えない（利用者は手で変えられる）
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        self.tbl_shinrinbo.verticalHeader().setVisible(False)
        # 入りきらない値は折り返さず「…」で省略する（行の高さは1行のまま）
        self.tbl_shinrinbo.setWordWrap(False)
        # ピクセルスクロール／Shift+ホイール水平／Ctrl+C・右クリックでコピー
        self._enable_table_shortcuts(self.tbl_shinrinbo)

        v.addWidget(self.tbl_shinrinbo, 1)

        self.btn_kozu_shinrinbo.toggled.connect(
            lambda checked: self._on_kozu_toggled(checked, self.btn_kozu_shinrinbo))
        self._shinrinbo_model_keep = ()
        self._clear_shinrinbo_table()
        return w

    # ------------------------------------------------------------------
    # 表のモデルの差し替え
    # ------------------------------------------------------------------

    def _set_shinrinbo_model(self, model, keep=()):
        """表のモデルを差し替える。keep：モデルと一緒に持っておくもの（読み込みのキャッシュなど）。
        前のモデルなどは、差し替えたあとに片付ける。"""
        table = self.tbl_shinrinbo
        old_sel = table.selectionModel()
        old = getattr(self, '_shinrinbo_model_keep', ())
        table.setModel(model)
        self._shinrinbo_model_keep = (model,) + tuple(keep)
        table.selectionModel().selectionChanged.connect(self._on_shinrinbo_row_selected)
        for obj in (old_sel,) + tuple(old):
            if obj is not None and not sip.isdeleted(obj):
                obj.deleteLater()
        self._apply_shinrinbo_col_widths()

    def _apply_shinrinbo_col_widths(self):
        """列幅を、列ごとに決めた幅（_SHINRINBO_COL_WIDTHS の文字列が入る幅）にする。
        見出しの方が長い列は見出しに合わせる。決めていない列だけ、中身に合わせる。"""
        table = self.tbl_shinrinbo
        model = table.model()
        cell_fm = table.fontMetrics()
        head_fm = table.horizontalHeader().fontMetrics()
        pad = 16  # セルの左右の余白
        for col in range(model.columnCount(QModelIndex())):
            label = str(model.headerData(col, Qt.Horizontal, Qt.DisplayRole) or '')
            sample = _SHINRINBO_COL_WIDTHS.get(label) or _SHINRINBO_COL_WIDTHS.get(label.rstrip('0123456789'))
            if sample is None:
                table.resizeColumnToContents(col)
                continue
            table.setColumnWidth(col, max(_text_width(cell_fm, sample),
                                          _text_width(head_fm, label)) + pad)

    def _clear_shinrinbo_table(self):
        """表を空にする（見出しだけ出す）。"""
        self._set_shinrinbo_model(_ShinrinboApiModel(
            getattr(self, '_shinrinbo_col_map', []), [], self.tbl_shinrinbo))

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
                except RuntimeError:
                    pass  # チェック後にレイヤーが削除された場合のみ発生
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

        elif self._layer_type in ('cd_gpkg', 'shp'):
            self._shinrinbo_col_map = list(_API_TABLE_COLS)

        else:
            self._shinrinbo_col_map = []

        self._clear_shinrinbo_table()

    # ------------------------------------------------------------------
    # 更新エントリポイント（features = list[QgsFeature]、選択変更時）
    # ------------------------------------------------------------------

    def _refresh_shinrinbo_tab(self, features):
        """features：選択中の小班。gpkg 型は表を選択中の小班すべてから作るので、空かどうかだけを見る
        （呼び出し元は先頭の1件だけ読めばよい）。cd_gpkg 型は、この地物から森林クラウドの森林簿を取る。"""
        self._update_kozu_btn(self.btn_kozu_shinrinbo)
        self._current_shinrinbo_key = ''
        self._shinrinbo_api_ids = []
        self._shinrinbo_row_features = list(features or []) if self._layer_type != 'gpkg' else []
        self._shinrinbo_generation = getattr(self, '_shinrinbo_generation', 0) + 1
        gen = self._shinrinbo_generation

        if not features:
            self._clear_shinrinbo_table()
            self._clear_selection_highlights()
            self._clear_cloud_record_info()
            self._update_cache_btn_states()
            self._progress_end()
            return

        # 地図上の選択はテーブルの行選択と連動させない。
        # ラバーバンド/ズームはテーブルで明示的に行を選んだ時だけ行う。
        self._clear_selection_highlights()
        self._clear_cloud_record_info()

        if self._layer_type == 'gpkg':
            self._build_shinrinbo_table_layer()
            self._sync_shinrinbo_cache_ts('ローカルデータ')
            self._update_cache_btn_states()
            self._progress_end()
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
        self._progress_end()  # 画面の処理の中で行う分（小班の位置の計算）はここまで。取得の進み具合は下部の取得日時の欄に出る

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
            self._progress_step(i, n, 50, 50)
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
                b = buf[pos]
                pos += 1
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
            pos = 0
            tags = []
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                field, wtype = tag >> 3, tag & 7
                if field == 2 and wtype == 2:
                    l, pos = read_varint(buf, pos)
                    chunk = buf[pos:pos + l]
                    pos += l
                    p2 = 0
                    while p2 < len(chunk):
                        v, p2 = read_varint(chunk, p2)
                        tags.append(v)
                elif wtype == 2:
                    l, pos = read_varint(buf, pos)
                    pos += l
                elif wtype == 0:
                    _, pos = read_varint(buf, pos)
                elif wtype in (1, 5):
                    pos += (8 if wtype == 1 else 4)
                else:
                    break
            return tags

        def parse_layer(buf):
            pos = 0
            keys = []
            vals = []
            feat_tags_list = []
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                field, wtype = tag >> 3, tag & 7
                if wtype == 2:
                    l, pos = read_varint(buf, pos)
                    chunk = buf[pos:pos + l]
                    pos += l
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
                if 'KEY1' not in keys or 'THE_FID' not in keys:
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
                l, pos = read_varint(buf, pos)
                pos += l
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

    def _build_shinrinbo_table_layer(self):
        """計画図レイヤー（gpkg 型）で選択中の小班の森林簿の表を作る。表のモデルが持つのは小班の ID の一覧だけで、
        属性は見えている行の分だけ読むので、選んだ小班が多くても表はすぐ出る。
        形状は読まない（行を選んだときにその小班だけ読む）。行の並びは地物の番号順。"""
        layer = self._connected_layer
        if layer is None or sip.isdeleted(layer):
            self._clear_shinrinbo_table()
            return
        table = self.tbl_shinrinbo
        model = _ShinrinboLayerModel(
            layer, sorted(layer.selectedFeatureIds()), getattr(self, '_shinrinbo_col_map', []), table)
        self._set_shinrinbo_model(model)
        table.clearSelection()

    def _build_shinrinbo_table_api(self, results):
        """森林クラウドから取った森林簿の表を作る（値は表が描くときに文字にする）。"""
        self._set_shinrinbo_model(_ShinrinboApiModel(
            getattr(self, '_shinrinbo_col_map', []), results, self.tbl_shinrinbo))
        self.tbl_shinrinbo.clearSelection()

    # ------------------------------------------------------------------
    # 行選択 → kozu連携
    # ------------------------------------------------------------------

    def _on_shinrinbo_row_selected(self, *_args):
        self._clear_selection_highlights()
        # 選ばれた行の一覧（selectedRows）は作らない。表で Ctrl+A を押すと全行分になり、止まるため。
        # 使うのは選ばれた範囲の先頭の行だけ
        sel = self.tbl_shinrinbo.selectionModel()
        if sel is None or not sel.hasSelection():
            self._clear_cloud_record_info()
            return
        row = min(r.top() for r in sel.selection())
        model = self.tbl_shinrinbo.model()
        layer = self._connected_layer
        feature = None
        if isinstance(model, _ShinrinboLayerModel):
            # gpkg 型：その行の小班を、ハイライト・公図連携のために1件だけ読む
            if layer is None or sip.isdeleted(layer):
                return
            feature = next(layer.getFeatures(QgsFeatureRequest().setFilterFid(model.fid(row))), None)
            row_data = feature
        else:
            row_data = model.row_data(row) if isinstance(model, _ShinrinboApiModel) else None
        if row_data is None:
            self._clear_cloud_record_info()
            return
        self._show_cloud_table_row_info('森林簿', self.tbl_shinrinbo, row)

        if feature is None:
            row_features = getattr(self, '_shinrinbo_row_features', [])
            if 0 <= row < len(row_features):
                feature = row_features[row]
        if feature is not None:
            try:
                geom = feature.geometry()
            except Exception:
                geom = None
            layer = self._connected_layer
            if geom and not geom.isEmpty() and layer is not None and not sip.isdeleted(layer):
                self._add_selection_highlight(geom, layer.crs())
                self._zoom_to_selection_highlights()

        if not self.btn_kozu_shinrinbo.isChecked():
            return

        if self._layer_type == 'gpkg':
            feat = row_data
            fnames = feat.fields().names()
            city  = str(feat['市町村名称'] or '').strip() if '市町村名称' in fnames else ''
            # 旧市町村区分（旧井川村・旧静岡市… の飾り付き表記）は市町村CDで
            # 現行市名へ変換してから Kozu へ渡す。表に無いCDは市町村名称のまま。
            if '市町村CD' in fnames:
                try:
                    _cur = _MORINBO_OLD_CD_TO_CITY.get(int(str(feat['市町村CD']).strip()))
                except (TypeError, ValueError):
                    _cur = None
                if _cur:
                    city = _cur
            daiji = str(feat['大字名称']  or '').strip() if '大字名称'  in fnames else ''
            oban  = str(feat['地番_親番'] or '').strip() if '地番_親番' in fnames else ''
            eda   = str(feat['地番_枝番'] or '').strip() if '地番_枝番' in fnames else ''
            # 親番・枝番は別フィールドのため、kozu側のchiban形式（親番-枝番）に合わせて結合
            chiban = f'{oban}-{eda}' if eda and eda != '0' else oban
        else:
            d = row_data or {}
            city  = str(d.get('市町村', '') or '').strip()
            daiji = str(d.get('大字', '') or '').strip()
            # 表示用_地番は既に「親番-枝番」形式で提供されるためそのまま使用
            chiban = str(d.get('表示用_地番', '') or '').strip()

        if chiban:
            self._send_to_kozu(city, daiji, chiban)

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    def _sync_shinrinbo_cache_ts(self, text):
        if self.cloud_tab.currentIndex() == self._shinrinbo_tab_index:
            self.lbl_cache_ts.setText(text)
