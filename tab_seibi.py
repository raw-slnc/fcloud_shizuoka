# -*- coding: utf-8 -*-
"""
整備事業タブ。

森林クラウドの整備事業検索APIは、条件に合う先頭500件しか返さない（総行数は返る）。
このタブは、検索軽量化リスト（seibi_index.py：検索条件と番号の対応表）を使って、
501件目以降を「その番号が入っているベクトルタイル」と「1件取り出し」で補い、
総行数どおりの件数を本家サイトと同じ並び順で表示する。
"""
import datetime
import json
import os

from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QCheckBox,
    QTableView, QHeaderView, QFrame, QAbstractItemView, QMessageBox,
)
from qgis.PyQt.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QThread, QTimer, QSettings, pyqtSignal, QObject, QEvent,
    QMetaType,
)
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsFeatureRequest,
    QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject, QgsSpatialIndex, QgsVectorLayer,
    QgsVectorLayerFeatureSource,
)

from .constants import (
    _API_BASE, _API_CITY_MAP, _CD_CITY, _CITY_CD, _MORINBO_OLD_CD_TO_CITY,
    _TOGGLE_BTN_QSS_LAYER,
)
from .layer_cleanup import remove_project_layer
from . import seibi_index as si

_TAB_INDEX = 2
_WGS84 = QgsCoordinateReferenceSystem('EPSG:4326')
_BUNDLED_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'seibi_index.json')

# 表の列（本家サイトの一覧の8列に、年度と事業内容の間へ市町を足す）
_HEADERS = ['行番号', '総行数', '年度', '市町', '事業内容', '種類', '面積', '樹種', '林齢']
_COL_SAMPLES = ['00000', '00000', 'R00', '伊豆の国市', '無立木地等森林緊急造成', '付帯施設整備（防護柵）',
                '000.00', '抵抗性ｸﾛﾏﾂ(普通苗)', '000']
_CENTER_COLS = (0, 1, 2)    # 行番号・総行数・年度（本家サイトと同じ中央寄せ）
_RIGHT_COLS = (6, 8)        # 面積・林齢
_CITY_COL = 3
# 列の幅の比率（本家サイトの一覧の列幅から。市町は足した列）。表の幅をこの比率で全部の列に分ける
_COL_WEIGHTS = (125, 125, 113, 180, 323, 345, 100, 236, 98)

_NO_PLAN_LAYER = '計画図レイヤーがありません'
_CD_CITY_ORDER = sorted(_CITY_CD.items(), key=lambda t: t[1])  # 市町の選択肢の並び（県の市町CDの順）
_OUT_OF_PLAN = '計画図の範囲外'
_SEIBI_LAYER_NAME = 'fcloud_整備事業_統合'

_ALL = '（すべて）'
_FETCH_PARALLEL = 4          # 検索のときに同時に取るタイルの数
_RECORD_INTERVAL_MS = 500    # 1件取り出しを続けるときの間隔
_REBUILD_INTERVAL_MS = 500   # リストを作り直すときのタイルの間隔
_DEEP_ZOOMS = (13, 14, 15, 16)  # ズーム12のタイルに形が無いとき、探すズーム

# 実行中の QThread が Python の参照切れで破棄されると QGIS ごと落ちるため、終わるまで参照を持つ
_SEIBI_WORKERS = set()


def _user_dir():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), 'fcloud_shizuoka', 'seibi')


def _user_index_path():
    return os.path.join(_user_dir(), 'seibi_index.json')


def _tile_cache_path(z, x, y):
    return os.path.join(_user_dir(), 'tiles', f'{z}_{x}_{y}.pbf')


def _point_cache_path():
    # タイルに無い番号の位置（1件取り出しで得たもの）の控え。タイルの控えと同じく、リストの更新で消す
    return os.path.join(_user_dir(), 'tiles', 'record_points.json')


def _record_cache_path():
    # タイルに無い番号の完全な行。条件を変えるたび同じ1件を取り直さないための控え
    return os.path.join(_user_dir(), 'tiles', 'record_rows.json')


# ── スレッド ──────────────────────────────────────────────────────────────

class _TileParseWorker(QThread):
    """タイルを読み、行（属性）と代表点を返す。
    target_fid は1番号の形の一覧、target_fids は番号ごとに統合した形を返す。
    """
    progress = pyqtSignal(int, int, int)       # (世代, 読んだ枚数, 全体)
    finished_ok = pyqtSignal(int, object)      # (世代, 結果)
    failed = pyqtSignal(int, str)

    def __init__(self, gen, tiles, target_fid=None, target_fids=None):
        super().__init__()
        self.gen = gen
        self.tiles = tiles          # [(z, x, y, bytes), ...]
        self.target_fid = target_fid
        self.target_fids = set(target_fids) if target_fids is not None else None
        self.cancelled = False

    def run(self):
        from .mvt_loader import parse_tile
        try:
            if self.target_fids is not None:
                by_fid = {}
                n = len(self.tiles)
                for i, (z, x, y, raw) in enumerate(self.tiles):
                    if self.cancelled:
                        return
                    for f in parse_tile(raw, x, y, z, si.TILE_LAYER):
                        fid = int(f.get('THE_FID', -1))
                        if fid not in self.target_fids:
                            continue
                        g = QgsGeometry.fromWkt(f['geometry'])
                        if g is not None and not g.isEmpty():
                            by_fid.setdefault(fid, []).append(g)
                    self.progress.emit(self.gen, i + 1, n)
                merged = {}
                for fid, geoms in by_fid.items():
                    if self.cancelled:
                        return
                    try:
                        geom = QgsGeometry.unaryUnion(geoms) if len(geoms) > 1 else geoms[0]
                    except Exception:  # noqa: BLE001 - 壊れた一片があっても残りを出す
                        geom = geoms[0]
                        for extra in geoms[1:]:
                            try:
                                geom = geom.combine(extra)
                            except Exception as e:  # noqa: BLE001
                                print(f'[fcloud] 整備事業 {fid} のポリゴンの一部を統合できません: {e}')
                    if geom is not None and not geom.isEmpty():
                        merged[fid] = geom.asWkt()
                self.finished_ok.emit(self.gen, merged)
                return
            if self.target_fid is not None:
                wkts = []
                for z, x, y, raw in self.tiles:
                    if self.cancelled:
                        return
                    for f in parse_tile(raw, x, y, z, si.TILE_LAYER):
                        if int(f.get('THE_FID', -1)) == self.target_fid:
                            wkts.append(f['geometry'])
                self.finished_ok.emit(self.gen, wkts)
                return
            result = {}   # (x, y) -> (rows {fid: row}, points {fid: (lon, lat)})
            n = len(self.tiles)
            for i, (z, x, y, raw) in enumerate(self.tiles):
                if self.cancelled:
                    return
                rows, points = {}, {}
                for f in parse_tile(raw, x, y, z, si.TILE_LAYER):
                    if 'THE_FID' not in f:
                        continue
                    row = si.row_from_tile_feature(f)
                    rows[row['fid']] = row
                    g = QgsGeometry.fromWkt(f['geometry'])
                    if g is not None and not g.isEmpty():
                        p = g.pointOnSurface()
                        if p is not None and not p.isEmpty():
                            pt = p.asPoint()
                            points[row['fid']] = (pt.x(), pt.y())
                result[(x, y)] = (rows, points)
                self.progress.emit(self.gen, i + 1, n)
            self.finished_ok.emit(self.gen, result)
        except Exception as e:  # noqa: BLE001 - 壊れたタイル等。画面に知らせる
            self.failed.emit(self.gen, str(e))


def _plan_city_name(cd, name):
    """計画図の小班の市町。市町村CDがあれば旧市町村区分を現行の市名に直し（_MORINBO_OLD_CD_TO_CITY）、
    無ければ市町村名称をそのまま使う。市町村名称も無ければ CD から名前を引く（郡は外す）。"""
    try:
        cd_int = int(str(cd).strip()) if cd is not None and str(cd) not in ('', 'NULL') else None
    except (TypeError, ValueError):
        cd_int = None
    if cd_int is not None and cd_int in _MORINBO_OLD_CD_TO_CITY:
        return _MORINBO_OLD_CD_TO_CITY[cd_int]
    if name is not None and str(name) not in ('', 'NULL'):
        return str(name)
    if cd_int is not None:
        full = _CD_CITY.get(cd_int, '')
        return _API_CITY_MAP.get(full, full)
    return ''


class _RangeWorker(QThread):
    """計画図レイヤーの小班に、形の一部でも重なる整備事業の番号と、重なる小班の市町を求める。
    タイルの地物は形で重ねる（代表点で判定すると、形の大部分が小班に重なっていても点が小班の外に
    落ちて抜けることがあるため）。タイルに無い番号は、森林クラウドの位置（点）で判定する。
    レイヤーは複製した読み取り元（QgsVectorLayerFeatureSource）から読む。"""
    progress = pyqtSignal(int, int, int)    # (世代, 済んだ枚数, 全体)
    finished_ok = pyqtSignal(int, object)   # (世代, (範囲内の番号の集合, {番号: 市町の並び}))
    failed = pyqtSignal(int, str)

    def __init__(self, gen, source, transform, tiles, points, cd_idx=-1, name_idx=-1):
        super().__init__()
        self.gen = gen
        self.source = source        # QgsVectorLayerFeatureSource（別スレッドで読める）
        self.transform = transform  # WGS84 → レイヤーの座標系
        self.tiles = tiles          # [(z, x, y, bytes)]
        self.points = points        # タイルに無い番号 {fid: (lon, lat)}
        self.cd_idx = cd_idx        # 計画図の 市町村CD 列（無ければ -1）
        self.name_idx = name_idx    # 計画図の 市町村名称 列（無ければ -1）
        self.cancelled = False

    def run(self):
        from .mvt_loader import parse_tile
        try:
            attrs = [i for i in (self.cd_idx, self.name_idx) if i >= 0]
            req = QgsFeatureRequest().setSubsetOfAttributes(attrs) if attrs else QgsFeatureRequest().setNoAttributes()
            index = QgsSpatialIndex(QgsSpatialIndex.Flag.FlagStoreFeatureGeometries)
            city_of = {}
            for feat in self.source.getFeatures(req):
                if self.cancelled:
                    return
                index.addFeature(feat)
                if attrs:
                    a = feat.attributes()
                    city_of[feat.id()] = _plan_city_name(
                        a[self.cd_idx] if self.cd_idx >= 0 else None,
                        a[self.name_idx] if self.name_idx >= 0 else None)

            def cities_hit(geom):
                found = set()
                for cand in index.intersects(geom.boundingBox()):
                    g = index.geometry(cand)
                    if g is not None and g.intersects(geom):
                        found.add(city_of.get(cand, ''))
                return found

            inside, cities = set(), {}
            n = len(self.tiles)
            for i, (z, x, y, raw) in enumerate(self.tiles):
                for f in parse_tile(raw, x, y, z, si.TILE_LAYER):
                    if self.cancelled:
                        return
                    fid = int(f.get('THE_FID', -1))
                    if fid < 0:
                        continue
                    g = QgsGeometry.fromWkt(f['geometry'])
                    if g is None or g.isEmpty() or not self._to_layer(g):
                        continue  # 変換できない形は範囲外として扱う
                    found = cities_hit(g)  # タイルの縁で分かれた形も、どの部分の市町も集める
                    if found:
                        inside.add(fid)
                        cities.setdefault(fid, set()).update(found)
                self.progress.emit(self.gen, i + 1, n)
            for fid, (lon, lat) in self.points.items():
                if self.cancelled:
                    return
                g = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
                if self._to_layer(g):
                    found = cities_hit(g)
                    if found:
                        inside.add(fid)
                        cities.setdefault(fid, set()).update(found)
            named = {fid: tuple(sorted(c for c in cs if c)) for fid, cs in cities.items()}
            self.finished_ok.emit(self.gen, (inside, named))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self.gen, str(e))

    def _to_layer(self, geom):
        try:
            geom.transform(self.transform)
            return True
        except QgsCsException:
            return False


# ── 表 ────────────────────────────────────────────────────────────────────

class _SeibiTableResizeFilter(QObject):
    """表の幅が変わったら、列の幅を分け直す。"""

    def __init__(self, table, on_resize):
        super().__init__(table)
        self._on_resize = on_resize

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self._on_resize()
        return False


class _SeibiModel(QAbstractTableModel):
    """1行＝(行番号, 行)。行は seibi_index の共通の行の形。市町は city_text(番号) で引く。"""

    def __init__(self, items, total, city_text=None, parent=None):
        super().__init__(parent)
        self._items = items
        self._total = total
        self._city_text = city_text

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal
                and 0 <= section < len(_HEADERS)):
            return _HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _CENTER_COLS:
                return int(Qt.AlignmentFlag.AlignCenter)
            if col in _RIGHT_COLS:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        rank, row = self._items[index.row()]
        if col == 0:
            return str(rank)
        if col == 1:
            return '' if self._total is None else str(self._total)
        if col == 2:
            return si.wareki_label(row['年度'])
        if col == _CITY_COL:
            return self._city_text(row['fid']) if self._city_text else ''
        if col == 4:
            return row['事業内容']
        if col == 5:
            return row['種類']
        if col == 6:
            return si.area_label(row['面積'])
        if col == 7:
            return row['樹種']
        return str(row['林齢'])

    def row_at(self, i):
        return self._items[i][1] if 0 <= i < len(self._items) else None


# ── タブ ──────────────────────────────────────────────────────────────────

class SeibiMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_seibi(self):
        self._seibi_index = None
        self._seibi_gen = 0             # 検索の世代
        self._seibi_hl_gen = 0          # 地図の表示の世代
        self._seibi_upd_gen = 0         # リスト更新の世代
        self._seibi_updating = False
        self._seibi_workers = []
        self._seibi_tile_rows = {}      # (x, y) -> (rows, points)  読んだタイルの覚え
        self._seibi_record_rows = None  # タイルに無い番号の保存済み行（初回利用時に読む）
        self._seibi_all_rows = {}       # fid -> row  いまの検索の行
        self._seibi_points = {}         # fid -> (lon, lat)
        self._seibi_total = None
        self._seibi_notes = []
        self._seibi_rng_gen = 0         # 範囲内の番号を求める処理の世代
        self._seibi_rng = None          # {'key': (レイヤーID, リストの作成日, 件数), 'fids': 範囲内の番号 or None}
        self._seibi_range_counts = None  # 範囲内の組み合わせごとの件数（グレーアウト用）
        self._seibi_sorted = []         # [(行番号, 行)]
        self._seibi_rendered_items = [] # 市町・範囲内まで絞って表に出している行
        self._seibi_completed_cond = None
        self._seibi_layer_gen = 0
        self._seibi_integrated_layer_id = None

        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row1 = QHBoxLayout()
        self.combo_seibi_year = QComboBox()
        self.combo_seibi_city = QComboBox()
        self.combo_seibi_city.setToolTip(
            '計画図レイヤーの小班に重なる整備事業を、その小班の市町で絞る（計画図レイヤーが無いときは選べない）')
        self.combo_seibi_naiyo = QComboBox()
        self.combo_seibi_shurui = QComboBox()
        self.combo_seibi_jushu = QComboBox()
        self.btn_seibi_shurui_reset = self._seibi_reset_button()
        self.btn_seibi_jushu_reset = self._seibi_reset_button()
        for label, combo, reset in (('年度:', self.combo_seibi_year, None),
                                    ('市町:', self.combo_seibi_city, None),
                                    ('事業内容:', self.combo_seibi_naiyo, None),
                                    ('種類:', self.combo_seibi_shurui, self.btn_seibi_shurui_reset),
                                    ('樹種:', self.combo_seibi_jushu, self.btn_seibi_jushu_reset)):
            row1.addWidget(QLabel(label))
            row1.addWidget(combo, 1)
            if reset is not None:
                row1.addWidget(reset)
        self.btn_seibi_search = QPushButton('検索')
        row1.addWidget(self.btn_seibi_search)
        self.btn_seibi_reset_all = QPushButton('全リセット')
        self.btn_seibi_reset_all.setToolTip('すべての絞り込み条件を解除して全件を検索する')
        row1.addWidget(self.btn_seibi_reset_all)
        v.addLayout(row1)

        for combo, values in ((self.combo_seibi_naiyo, si.JIGYOU_OPTIONS),
                              (self.combo_seibi_shurui, si.SHURUI_OPTIONS),
                              (self.combo_seibi_jushu, si.JUSHU_OPTIONS)):
            combo.addItem(_ALL, '')
            for val in values:
                combo.addItem(val, val)
        self.combo_seibi_year.addItem(_ALL, '')
        self.combo_seibi_city.addItem(_ALL, '')
        self.combo_seibi_city.setEnabled(False)  # 計画図レイヤーとの重ね合わせが済むまで選べない

        row2 = QHBoxLayout()
        self.chk_seibi_in_range = QCheckBox('計画図レイヤーの範囲内だけ')
        self.chk_seibi_in_range.setToolTip(
            '位置が計画図レイヤーの小班に入る整備事業だけを表に出す（件数は範囲内と全体の両方を出す）')
        row2.addWidget(self.chk_seibi_in_range)
        self.lbl_seibi_status = QLabel('')
        self.lbl_seibi_status.setStyleSheet('font-size: 11px;')
        self.lbl_seibi_status.setWordWrap(True)
        row2.addWidget(self.lbl_seibi_status, 1)
        self.btn_seibi_layer = QPushButton('統合してレイヤーで見る')
        self.btn_seibi_layer.setCheckable(True)
        self.btn_seibi_layer.setStyleSheet(_TOGGLE_BTN_QSS_LAYER)
        self.btn_seibi_layer.setToolTip(
            '年度・市町で絞った整備事業を、番号ごとにタイルの分割を統合して1つのレイヤーにする')
        self.btn_seibi_layer.setVisible(False)
        row2.addWidget(self.btn_seibi_layer)
        self.btn_seibi_update = QPushButton('リストを更新')
        self.btn_seibi_update.setToolTip(
            '検索軽量化リスト（検索条件と番号の対応表）を森林クラウドの今のデータに合わせる。\n'
            '増えた番号だけを取り出して書き足す。番号が減った・抜けたときは作り直す')
        row2.addWidget(self.btn_seibi_update)
        v.addLayout(row2)

        self.tbl_seibi = QTableView()
        self.tbl_seibi.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_seibi.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl_seibi.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_seibi.setAlternatingRowColors(True)
        self.tbl_seibi.setFrameShape(QFrame.Shape.NoFrame)
        self.tbl_seibi.setStyleSheet('QTableView { border: 1px solid palette(dark); }')
        self.tbl_seibi.horizontalHeader().setStretchLastSection(False)
        self.tbl_seibi.verticalHeader().setVisible(False)
        # 本家サイトのように、セルの左右に余白をとり、行を少し高くする
        self.tbl_seibi.setStyleSheet(
            'QTableView { border: 1px solid palette(dark); } QTableView::item { padding: 0px 8px; }')
        self.tbl_seibi.verticalHeader().setDefaultSectionSize(self.tbl_seibi.fontMetrics().height() + 12)
        self._seibi_resize_filter = _SeibiTableResizeFilter(self.tbl_seibi, self._seibi_apply_col_widths)
        self.tbl_seibi.viewport().installEventFilter(self._seibi_resize_filter)
        self.tbl_seibi.setWordWrap(False)
        self._enable_table_shortcuts(self.tbl_seibi)
        v.addWidget(self.tbl_seibi, 1)

        self._seibi_model_keep = ()
        self._seibi_set_model([], None)
        self._seibi_restore_conditions()

        self.btn_seibi_search.clicked.connect(self._seibi_search)
        self.btn_seibi_reset_all.clicked.connect(self._seibi_reset_all)
        self.btn_seibi_update.clicked.connect(self._seibi_on_update_clicked)
        self.btn_seibi_layer.toggled.connect(self._seibi_on_layer_toggled)
        self.chk_seibi_in_range.toggled.connect(self._seibi_on_range_toggled)
        self.btn_seibi_shurui_reset.clicked.connect(lambda: self.combo_seibi_shurui.setCurrentIndex(0))
        self.btn_seibi_jushu_reset.clicked.connect(lambda: self.combo_seibi_jushu.setCurrentIndex(0))
        for combo in (self.combo_seibi_year, self.combo_seibi_naiyo,
                      self.combo_seibi_shurui, self.combo_seibi_jushu):
            combo.currentIndexChanged.connect(self._seibi_update_option_states)
            combo.currentIndexChanged.connect(self._seibi_on_filter_condition_changed)
        # 市町は手元で絞るだけなので、選んだらすぐ表に出す（県には問い合わせない）
        self.combo_seibi_city.currentIndexChanged.connect(self._seibi_on_city_changed)
        self._seibi_update_layer_button()
        return w

    def _seibi_on_city_changed(self, *_args):
        self._seibi_remove_integrated_layer()
        QSettings().setValue('fcloud_shizuoka/seibi_city', self._seibi_city())
        self._seibi_update_option_states()
        self._seibi_render()

    def _seibi_on_filter_condition_changed(self, *_args):
        """年度・事業内容・種類・樹種の変更を、解除を含めてすぐ表へ反映する。"""
        self._seibi_search()

    def _seibi_reset_all(self):
        """すべての絞り込み条件を解除し、重複した検索を起こさず1回だけ全件検索する。"""
        combos = (
            self.combo_seibi_year, self.combo_seibi_city, self.combo_seibi_naiyo,
            self.combo_seibi_shurui, self.combo_seibi_jushu,
        )
        for combo in combos:
            combo.blockSignals(True)
        self.chk_seibi_in_range.blockSignals(True)
        try:
            for combo in combos:
                combo.setCurrentIndex(0)
            self.chk_seibi_in_range.setChecked(False)
        finally:
            for combo in combos:
                combo.blockSignals(False)
            self.chk_seibi_in_range.blockSignals(False)

        settings = QSettings()
        settings.setValue('fcloud_shizuoka/seibi_city', '')
        settings.setValue('fcloud_shizuoka/seibi_in_range', False)
        self._seibi_update_option_states()
        self._seibi_search()

    def _seibi_city(self):
        """選んでいる市町（選べない・選んでいないときは ''）。"""
        if not self.combo_seibi_city.isEnabled():
            return ''
        return self.combo_seibi_city.currentData() or ''

    def _seibi_fill_city_options(self):
        """計画図との重ね合わせで分かった市町を選択肢にする（県の市町CDの順）。計画図レイヤーが無い、
        または重ね合わせが済んでいないときは、選択欄ごとグレーにする。"""
        combo = self.combo_seibi_city
        ready = self._seibi_layer() is not None and self._seibi_layer_info_ready()
        names = set()
        if ready:
            for cs in self._seibi_rng.get('cities', {}).values():
                names.update(cs)
        order = {_API_CITY_MAP.get(full, full): cd for full, cd in _CD_CITY_ORDER}
        wanted = combo.currentData() or QSettings().value('fcloud_shizuoka/seibi_city', '', type=str)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(_ALL, '')
        for name in sorted(names, key=lambda n: (order.get(n, 999), n)):
            combo.addItem(name, name)
        i = combo.findData(wanted)
        combo.setCurrentIndex(i if i >= 0 else 0)
        combo.setEnabled(ready)
        combo.setToolTip(
            '計画図レイヤーの小班に重なる整備事業を、その小班の市町で絞る' if ready else
            ('計画図レイヤーとの重ね合わせが済むと選べます' if self._seibi_layer() is not None
             else _NO_PLAN_LAYER))
        combo.blockSignals(False)

    @staticmethod
    def _seibi_reset_button():
        btn = QPushButton('↺')
        btn.setFixedWidth(28)
        btn.setToolTip('（すべて）に戻す')
        # 別ウィンドウ（ダイアログ）のとき、エンターでこのボタンが押されないように
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setAutoDefault(False)
        return btn

    # ------------------------------------------------------------------
    # 選択肢のグレーアウト（件数の無いもの）
    # ------------------------------------------------------------------

    def _seibi_update_option_states(self, *_args):
        """年度・事業内容・種類・樹種のうち、ほかの条件の今の選択では件数が0になる選択肢をグレーにする。
        件数は検索軽量化リストで数える（県のサーバーには問い合わせない）。
        選んでいる項目が0件になっても、選択は変えない（何を検索したか分かるように）。"""
        idx = self._seibi_index
        counts = getattr(self, '_seibi_combo_counts', None)
        if idx is None or counts is None:
            return
        city = self._seibi_city()
        city_counts = getattr(self, '_seibi_city_counts', None) or {}
        if city:
            # 市町を選んでいるときは、その市町の件数で決める
            counts = {key: n for (key, c), n in city_counts.items() if c == city}
        elif self.chk_seibi_in_range.isChecked() and self._seibi_layer() is not None:
            # 範囲内だけのときは、範囲内の件数で決める（求め終えるまでは県全体の件数のまま）
            in_range = getattr(self, '_seibi_range_counts', None)
            if self._seibi_range_ready() and in_range is not None:
                counts = in_range
        cond = self._seibi_conditions()
        # 市町の選択肢：ほかの4つの条件の今の選択で、件数のある市町だけ選べる
        present_cities = {c for (key, c) in city_counts
                          if all(cond[k] == '' or cond[k] == key[k] for k in range(4))}
        cmodel = self.combo_seibi_city.model()
        for i in range(self.combo_seibi_city.count()):
            value = self.combo_seibi_city.itemData(i)
            item = cmodel.item(i) if hasattr(cmodel, 'item') else None
            if item is not None and value:
                item.setEnabled(value in present_cities)
        for field, combo in ((0, self.combo_seibi_year), (1, self.combo_seibi_naiyo),
                             (2, self.combo_seibi_shurui), (3, self.combo_seibi_jushu)):
            present = set()
            for combo_key in counts:
                if all(k == field or cond[k] == '' or cond[k] == combo_key[k] for k in range(4)):
                    present.add(combo_key[field])
            model = combo.model()
            for i in range(combo.count()):
                value = combo.itemData(i)
                item = model.item(i) if hasattr(model, 'item') else None
                if item is not None and value:
                    item.setEnabled(value in present)

    def _seibi_count_combos(self):
        idx = self._seibi_index
        if idx is None:
            self._seibi_combo_counts = None
            return
        counts = {}
        for ci in idx.combo_of_fid:
            if ci >= 0:
                key = idx.combos[ci]
                counts[key] = counts.get(key, 0) + 1
        self._seibi_combo_counts = counts

    def _seibi_restore_conditions(self):
        s = QSettings()
        for key, combo in (('naiyo', self.combo_seibi_naiyo), ('shurui', self.combo_seibi_shurui),
                           ('jushu', self.combo_seibi_jushu)):
            val = s.value(f'fcloud_shizuoka/seibi_{key}', '', type=str)
            i = combo.findData(val)
            if i >= 0:
                combo.setCurrentIndex(i)
        self._seibi_saved_year = s.value('fcloud_shizuoka/seibi_year', '', type=str)
        self.chk_seibi_in_range.setChecked(s.value('fcloud_shizuoka/seibi_in_range', False, type=bool))

    def _seibi_save_conditions(self):
        s = QSettings()
        s.setValue('fcloud_shizuoka/seibi_year', self.combo_seibi_year.currentData() or '')
        s.setValue('fcloud_shizuoka/seibi_naiyo', self.combo_seibi_naiyo.currentData() or '')
        s.setValue('fcloud_shizuoka/seibi_shurui', self.combo_seibi_shurui.currentData() or '')
        s.setValue('fcloud_shizuoka/seibi_jushu', self.combo_seibi_jushu.currentData() or '')

    def _seibi_set_model(self, items, total):
        table = self.tbl_seibi
        old_sel = table.selectionModel()
        old = getattr(self, '_seibi_model_keep', ())
        model = _SeibiModel(items, total, self._seibi_city_text, table)
        table.setModel(model)
        self._seibi_model_keep = (model,)
        table.selectionModel().selectionChanged.connect(self._seibi_on_row_selected)
        for obj in (old_sel,) + tuple(old):
            if obj is not None and not sip.isdeleted(obj):
                obj.deleteLater()
        # モデルを差し替えると列の幅の決め方が戻ることがあるので、毎回かけ直す
        self._seibi_apply_col_widths()

    def _seibi_apply_col_widths(self):
        """表の幅を、本家サイトの列幅の比率（_COL_WEIGHTS）で全部の列に分ける。
        どの列も、値と見出しが入る幅より狭くはしない（足りないときは横にスクロール）。
        パネルの幅が変わったら分け直す（_SeibiTableResizeFilter）。"""
        table = self.tbl_seibi
        hdr = table.horizontalHeader()
        cell_fm = table.fontMetrics()
        head_fm = hdr.fontMetrics()
        pad = 24  # セルの左右の余白（スタイルの padding 8px×2 ＋ 少し）
        mins = [max(cell_fm.horizontalAdvance(s), head_fm.horizontalAdvance(h)) + pad
                for h, s in zip(_HEADERS, _COL_SAMPLES)]
        avail = table.viewport().width()
        total_w = sum(_COL_WEIGHTS)
        widths = [max(m, int(avail * w / total_w)) for m, w in zip(mins, _COL_WEIGHTS)]
        if sum(widths) <= avail:
            widths[-1] += avail - sum(widths)  # 端数は最後の列に足して、右端までそろえる
        for col, wd in enumerate(widths):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(col, wd)

    # ------------------------------------------------------------------
    # タブの表示
    # ------------------------------------------------------------------

    def _seibi_on_tab_shown(self):
        idx = self._seibi_get_index()
        self._seibi_fill_year_options()
        self.chk_seibi_in_range.setEnabled(self._seibi_layer() is not None)
        self.lbl_cache_ts.setText(self._seibi_index_text(idx))
        self._seibi_ensure_layer_range()
        if self.btn_seibi_layer.isChecked() and self._seibi_integrated_layer_id:
            self._set_layer_visible(self._seibi_integrated_layer_id, True)
            self._refresh_map_canvas()
        self._seibi_update_layer_button()

    def _seibi_on_layer_changed(self):
        """計画図レイヤーが変わったら、範囲内の判定をやり直す。"""
        if not hasattr(self, 'chk_seibi_in_range'):
            return
        self._seibi_remove_integrated_layer()
        self.chk_seibi_in_range.setEnabled(self._seibi_layer() is not None)
        self._seibi_reset_layer_range()
        if self.cloud_tab.currentIndex() == _TAB_INDEX:
            self._seibi_ensure_layer_range()
        self._seibi_update_option_states()
        self._seibi_render()

    def _seibi_on_tab_hidden(self):
        self._seibi_hl_gen += 1
        self._clear_selection_highlights()
        if self._set_layer_visible(self._seibi_integrated_layer_id, False):
            self._refresh_map_canvas()

    @staticmethod
    def _seibi_index_text(idx):
        if idx is None:
            return '検索軽量化リスト: なし'
        where = '更新済み' if idx.source == _user_index_path() else '同梱'
        return f'検索軽量化リスト: {idx.built}（{idx.total:,}件・{where}）'

    def _seibi_layer(self):
        layer = self._connected_layer
        if layer is None or sip.isdeleted(layer):
            return None
        return layer

    def _seibi_get_index(self, reload=False):
        """同梱のリストと更新済みのリストのうち、新しい方（作った日、同じなら件数の多い方）を使う。"""
        if self._seibi_index is not None and not reload:
            return self._seibi_index
        if reload:
            self._seibi_record_rows = None
        best = None
        for path in (_BUNDLED_INDEX, _user_index_path()):
            if not os.path.exists(path):
                continue
            try:
                idx = si.SeibiIndex.load(path)
            except (OSError, si.SeibiIndexError) as e:
                print(f'[fcloud] 整備事業の検索軽量化リストを読めません（{path}）: {e}')
                continue
            if best is None or (idx.built, idx.total) >= (best.built, best.total):
                best = idx
        self._seibi_index = best
        self._seibi_count_combos()
        self._seibi_update_option_states()
        return best

    def _seibi_fill_year_options(self):
        """年度の選択肢。まずリストにある年度で作り、本家サイトと同じ口から取れたら差し替える。"""
        if getattr(self, '_seibi_years_loaded', False):
            return
        idx = self._seibi_index
        years = sorted({c[0] for c in idx.combos}, reverse=True) if idx else []
        self._seibi_set_year_items([(y, self._seibi_wareki_text(y)) for y in years])
        self._get_api(f'{_API_BASE}{si.API_YEAR_OPTIONS_PATH}', self._seibi_on_year_options)

    @staticmethod
    def _seibi_wareki_text(year):
        y = int(year)
        return f'令和{y - 2018}年度' if y >= 2019 else f'平成{y - 1988}年度'

    def _seibi_on_year_options(self, data):
        if not isinstance(data, list):
            return
        items = []
        for d in data:
            if isinstance(d, dict) and d.get('value'):
                items.append((str(d['value']), str(d.get('wareki') or self._seibi_wareki_text(d['value']))))
        if items:
            self._seibi_years_loaded = True
            self._seibi_set_year_items(items)

    def _seibi_set_year_items(self, items):
        combo = self.combo_seibi_year
        current = combo.currentData() or getattr(self, '_seibi_saved_year', '')
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(_ALL, '')
        for value, text in items:
            combo.addItem(text, value)
        i = combo.findData(current)
        combo.setCurrentIndex(i if i >= 0 else 0)
        combo.blockSignals(False)
        self._seibi_update_option_states()  # 作り直した年度の選択肢も、件数の無いものをグレーにする

    # ------------------------------------------------------------------
    # 検索
    # ------------------------------------------------------------------

    def _seibi_conditions(self):
        return (self.combo_seibi_year.currentData() or '', self.combo_seibi_naiyo.currentData() or '',
                self.combo_seibi_shurui.currentData() or '', self.combo_seibi_jushu.currentData() or '')

    def _seibi_search(self):
        idx = self._seibi_get_index()
        self._seibi_remove_integrated_layer()
        self._seibi_save_conditions()
        self._seibi_gen += 1
        gen = self._seibi_gen
        self._seibi_hl_gen += 1
        self._clear_selection_highlights()
        self._clear_cloud_record_info()
        self._seibi_cancel_workers()
        self._seibi_cond = self._seibi_conditions()
        self._seibi_all_rows = {}
        self._seibi_points = {}
        self._seibi_total = None
        self._seibi_notes = []
        self._seibi_sorted = []
        self._seibi_rendered_items = []
        self._seibi_completed_cond = None
        self._seibi_set_model([], None)
        self._seibi_update_layer_button()
        self._seibi_set_status('')
        self._seibi_progress('検索中…')
        if idx is None:
            self._seibi_notes.append('検索軽量化リストが無いため、501件目より後は取れません')
        y, j, s, t = self._seibi_cond
        self._post_api(f'{_API_BASE}{si.API_SEARCH_PATH}',
                       {'年度': y, '事業内容': j, '種類': s, '樹種': t},
                       lambda data, g=gen: self._seibi_on_search(g, data))

    def _seibi_on_search(self, gen, data):
        if gen != self._seibi_gen:
            return
        if not isinstance(data, list):
            self._seibi_progress_end()
            self._seibi_set_status('検索に失敗しました（ネットワークエラー）', warn=True)
            return
        total = int(data[-1].get('総行数', len(data))) if data else 0
        self._seibi_total = total
        idx = self._seibi_index
        changed = unknown = 0
        cache_rows = {}
        for rec in data:
            try:
                row = si.row_from_api_record(rec)
            except (KeyError, TypeError, ValueError):
                continue
            self._seibi_all_rows[row['fid']] = row
            if 'lon' in row:
                self._seibi_points[row['fid']] = (row['lon'], row['lat'])
            if idx is not None:
                res = idx.check_row(row)
                changed += res == 'changed'
                unknown += res == 'unknown'
                if res == 'ok' and row['fid'] in idx.tile_less:
                    cache_rows[row['fid']] = row
        if cache_rows:
            self._seibi_write_record_cache(cache_rows)
        if changed or unknown:
            self._seibi_notes.append(
                f'検索軽量化リストと違う行があります（値が違う {changed}件・リストに無い {unknown}件）。'
                '「リストを更新」を押してください')
        self._seibi_show_rows()
        if total <= len(self._seibi_all_rows) or idx is None:
            self._seibi_finish(gen)
            return
        fids = idx.fids_for(*self._seibi_cond)
        if len(fids) != total:
            self._seibi_notes.append(
                f'検索軽量化リストの件数（{len(fids):,}件）が総行数（{total:,}件）と違います。'
                '「リストを更新」を押してください')
        remaining = [f for f in fids if f not in self._seibi_all_rows]
        self._seibi_remaining = remaining
        self._seibi_fill_from_tiles(gen, remaining)

    def _seibi_fill_from_tiles(self, gen, remaining):
        """残りの番号が入っているタイルを読み、行を埋める。"""
        idx = self._seibi_index
        tiles = sorted(idx.tiles_for(remaining))
        todo = [t for t in tiles if t not in self._seibi_tile_rows]
        if not todo:
            self._seibi_take_tile_rows(gen, remaining)
            return
        self._seibi_tile_bytes = {}
        self._seibi_tile_queue = list(todo)
        self._seibi_tile_need = len(todo)
        self._seibi_tile_running = 0
        self._seibi_progress(f'整備事業のタイルを取得中… 0／{len(todo)}枚')
        for _ in range(min(_FETCH_PARALLEL, len(todo))):
            self._seibi_fetch_next_tile(gen, remaining)

    def _seibi_fetch_next_tile(self, gen, remaining):
        if gen != self._seibi_gen or not self._seibi_tile_queue:
            return
        x, y = self._seibi_tile_queue.pop(0)
        cached = self._seibi_read_tile_cache(si.TILE_ZOOM, x, y)
        if cached is not None:
            self._seibi_on_tile_bytes(gen, remaining, x, y, cached)
            return
        url = si.TILE_URL.format(z=si.TILE_ZOOM, x=x, y=y)
        self._get_binary(url, lambda data, xx=x, yy=y: self._seibi_on_tile_bytes(gen, remaining, xx, yy, data,
                                                                                 save=True))

    def _seibi_on_tile_bytes(self, gen, remaining, x, y, data, save=False):
        if gen != self._seibi_gen:
            return
        if data and save:
            self._seibi_write_tile_cache(si.TILE_ZOOM, x, y, data)
        self._seibi_tile_bytes[(x, y)] = data or b''
        done = len(self._seibi_tile_bytes)
        self._seibi_progress(f'整備事業のタイルを取得中… {done}／{self._seibi_tile_need}枚')
        if done < self._seibi_tile_need:
            QTimer.singleShot(0, lambda: self._seibi_fetch_next_tile(gen, remaining))
            return
        tiles = [(si.TILE_ZOOM, x, y, b) for (x, y), b in self._seibi_tile_bytes.items() if b]
        self._seibi_tile_bytes = {}
        worker = _TileParseWorker(gen, tiles)
        worker.kind = 'search'  # 新しい検索で止めてよいスレッド（範囲内の番号を求めるスレッドは止めない）
        worker.progress.connect(
            lambda g, d, n: g == self._seibi_gen and self._seibi_progress(f'整備事業のタイルを読み込み中… {d}／{n}枚'))
        worker.finished_ok.connect(lambda g, res: self._seibi_on_tiles_parsed(g, res, remaining))
        worker.failed.connect(lambda g, msg: self._seibi_on_tiles_failed(g, msg, remaining))
        self._seibi_start_worker(worker)

    def _seibi_on_tiles_failed(self, gen, msg, remaining):
        if gen != self._seibi_gen:
            return
        print(f'[fcloud] 整備事業のタイルを読めません: {msg}')
        self._seibi_notes.append('タイルを読めませんでした')
        self._seibi_take_tile_rows(gen, remaining)

    def _seibi_on_tiles_parsed(self, gen, result, remaining):
        self._seibi_tile_rows.update(result)  # 世代が変わっても、読んだタイルは次の検索に使える
        if gen != self._seibi_gen:
            return
        self._seibi_take_tile_rows(gen, remaining)

    def _seibi_take_tile_rows(self, gen, remaining):
        idx = self._seibi_index
        lacking = []
        for fid in remaining:
            found = None
            for ti in idx.tiles_of_fid[fid - 1]:
                entry = self._seibi_tile_rows.get(idx.tiles[ti])
                if entry is None:
                    continue
                rows, points = entry
                if fid in rows:
                    found = rows[fid]
                    if fid in points:
                        self._seibi_points[fid] = points[fid]
                    break
            if found is None:
                lacking.append(fid)
            else:
                self._seibi_all_rows[fid] = found
        self._seibi_show_rows()
        if lacking:
            self._seibi_fetch_records(gen, lacking)
        else:
            self._seibi_finish(gen)

    def _seibi_fetch_records(self, gen, fids):
        """タイルに無い番号を1件ずつ取り出す（間を空けて）。"""
        cached = self._seibi_read_record_cache()
        queue = []
        for fid in fids:
            row = cached.get(fid)
            if row is None:
                queue.append(fid)
                continue
            self._seibi_all_rows[fid] = row
            if 'lon' in row:
                self._seibi_points[fid] = (row['lon'], row['lat'])
        self._seibi_show_rows()
        if not queue:
            self._seibi_finish(gen)
            return
        self._seibi_rec_queue = queue
        self._seibi_rec_total = len(queue)
        self._seibi_rec_failed = 0
        self._seibi_fetch_next_record(gen)

    def _seibi_fetch_next_record(self, gen):
        if gen != self._seibi_gen:
            return
        if not self._seibi_rec_queue:
            if self._seibi_rec_failed:
                self._seibi_notes.append(f'{self._seibi_rec_failed}件を取り出せませんでした')
            self._seibi_finish(gen)
            return
        fid = self._seibi_rec_queue.pop(0)
        done = self._seibi_rec_total - len(self._seibi_rec_queue)
        self._seibi_progress(f'タイルに無い整備事業を取得中… {done}／{self._seibi_rec_total}件')
        self._get_api(f'{_API_BASE}{si.API_RECORD_PATH.format(fid=fid)}',
                      lambda data, f=fid: self._seibi_on_record(gen, f, data))

    def _seibi_on_record(self, gen, fid, data):
        rec = data[0] if isinstance(data, list) and data else data
        try:
            row = si.row_from_api_record(rec)
        except (KeyError, TypeError, ValueError):
            row = None
        # 条件が続けて変わり、前の検索になった応答でも、正しい1件なら次の検索で再利用する。
        if row is not None and row['fid'] == fid:
            self._seibi_write_record_cache({fid: row})
        if gen != self._seibi_gen:
            return
        if row is None or row['fid'] != fid:
            self._seibi_rec_failed += 1
        else:
            self._seibi_all_rows[fid] = row
            if 'lon' in row:
                self._seibi_points[fid] = (row['lon'], row['lat'])
        QTimer.singleShot(_RECORD_INTERVAL_MS, lambda: self._seibi_fetch_next_record(gen))

    def _seibi_finish(self, gen):
        if gen != self._seibi_gen:
            return
        self._seibi_progress_end()
        self._seibi_completed_cond = self._seibi_cond
        self._seibi_show_rows()

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------

    def _seibi_show_rows(self):
        rows = sorted(self._seibi_all_rows.values(), key=si.sort_key)
        self._seibi_sorted = [(i + 1, r) for i, r in enumerate(rows)]
        self._seibi_render()

    def _seibi_render(self):
        items = self._seibi_sorted
        city = self._seibi_city()
        if city:
            # 市町で絞る（市町は計画図の小班から求めたもの。範囲内だけの絞り込みも兼ねる）
            cities = self._seibi_rng.get('cities', {}) if self._seibi_layer_info_ready() else {}
            items = [(n, r) for n, r in items if city in cities.get(r['fid'], ())]
            self._seibi_rendered_items = list(items)
            self._seibi_set_model(items, self._seibi_total)
            self._seibi_update_status(len(items), city=city)
            self._seibi_update_layer_button()
            return
        if self.chk_seibi_in_range.isChecked() and self._seibi_layer() is not None:
            inside = self._seibi_range_fids()
            if inside is None:
                # 範囲内の番号を求めている途中。全体を出すと範囲外の行が混ざって見えるので、出さない
                self._seibi_rendered_items = []
                self._seibi_set_model([], self._seibi_total)
                self._seibi_update_status(None, waiting_range=True)
                self._seibi_update_layer_button()
                return
            items = [(n, r) for n, r in items if r['fid'] in inside]
            self._seibi_rendered_items = list(items)
            self._seibi_set_model(items, self._seibi_total)
            self._seibi_update_status(len(items))
            self._seibi_update_layer_button()
            return
        self._seibi_rendered_items = list(items)
        self._seibi_set_model(items, self._seibi_total)
        self._seibi_update_status(None)
        self._seibi_update_layer_button()

    def _seibi_city_text(self, fid):
        """市町の列。計画図レイヤーの小班に重なれば、その市町（複数なら「・」でつなぐ）。"""
        if self._seibi_layer() is None:
            return _NO_PLAN_LAYER
        if not self._seibi_layer_info_ready():
            return '…'
        names = self._seibi_rng.get('cities', {}).get(fid)
        if names is None:
            return _OUT_OF_PLAN
        return '・'.join(names)

    def _seibi_update_status(self, in_range_count=None, waiting_range=False, city=''):
        got, total = len(self._seibi_sorted), self._seibi_total
        if total is None:
            return
        if got == total:
            text = f'{total:,}件（総行数と一致）'
        else:
            text = f'{got:,}件／総行数 {total:,}件'
        if waiting_range:
            text = f'全体 {text}（範囲内は調べています）'
        elif city:
            text = f'{city} {in_range_count:,}件／全体 {text}'
        elif in_range_count is not None:
            text = f'範囲内 {in_range_count:,}件／全体 {text}'
        if self._seibi_notes:
            text += '　' + '　'.join(self._seibi_notes)
        self._seibi_set_status(text, warn=bool(self._seibi_notes) or (got != total and not self._seibi_busy()))

    # 状態の表示（表の上の1行）。検索の結果と、進行中の処理（検索・計画図との重ね合わせ・リストの更新・
    # 地図の知らせ）を同じ行に出す。処理ごとに分けて持ち、1つが終わってもほかの表示を消さない

    def _seibi_busy(self, kind='search'):
        return bool(getattr(self, '_seibi_busy_texts', {}).get(kind))

    def _seibi_set_status(self, text, warn=False):
        self._seibi_result_text = (text, warn)
        self._seibi_show_status()

    def _seibi_progress(self, text, kind='search'):
        self._seibi_busy_texts = getattr(self, '_seibi_busy_texts', {})
        self._seibi_busy_texts[kind] = text
        self._seibi_show_status()

    def _seibi_progress_end(self, kind='search'):
        getattr(self, '_seibi_busy_texts', {}).pop(kind, None)
        self._seibi_show_status()

    def _seibi_show_status(self):
        label = getattr(self, 'lbl_seibi_status', None)
        if label is None or sip.isdeleted(label):
            return
        busy = [t for k, t in getattr(self, '_seibi_busy_texts', {}).items() if t]
        text, warn = getattr(self, '_seibi_result_text', ('', False))
        if busy:
            # 進行中の処理を先に出す。結果があれば後ろに続ける
            text, warn = '　'.join(busy) + (f'　｜　{text}' if text else ''), False
        label.setText(text)
        label.setStyleSheet('font-size: 11px;' + (' color: #c0392b;' if warn else ''))

    # ------------------------------------------------------------------
    # 計画図レイヤーとの重ね合わせ（範囲内だけ・市町の列）
    #   計画図レイヤーにつないでタブを開いたときに一度だけ、レイヤーの範囲に重なるタイルを読み、
    #   整備事業の形が重なる小班から「範囲内の番号」と「市町」を求めて覚える。
    #   範囲内だけのときの選択肢のグレーアウトと表の絞り込み、市町の列に使う。
    #   タイルに無い番号は、目印のタイルが重なるものだけ1件取り出しで位置を見る
    # ------------------------------------------------------------------

    def _seibi_layer_info_ready(self):
        st = getattr(self, '_seibi_rng', None)
        return st is not None and st.get('key') == self._seibi_range_key() and st.get('fids') is not None

    def _seibi_range_ready(self):
        """範囲内だけで絞れる状態か（チェックが入っていて、重ね合わせが済んでいる）。"""
        return self.chk_seibi_in_range.isChecked() and self._seibi_layer_info_ready()

    def _seibi_range_fids(self):
        return self._seibi_rng['fids'] if self._seibi_range_ready() else None

    def _seibi_range_key(self):
        layer, idx = self._seibi_layer(), self._seibi_index
        if layer is None or idx is None:
            return None
        return (layer.id(), idx.built, idx.total)

    def _seibi_on_range_toggled(self, checked):
        self._seibi_remove_integrated_layer()
        QSettings().setValue('fcloud_shizuoka/seibi_in_range', checked)
        if checked:
            self._seibi_ensure_layer_range()
        self._seibi_update_option_states()
        self._seibi_render()

    # ------------------------------------------------------------------
    # 年度・市町で絞った結果 → 統合レイヤー
    # ------------------------------------------------------------------

    def _seibi_layer_conditions_selected(self):
        return bool(
            (self.combo_seibi_year.currentData() or '')
            and self._seibi_city()
        )

    def _seibi_update_layer_button(self):
        btn = getattr(self, 'btn_seibi_layer', None)
        if btn is None or sip.isdeleted(btn):
            return
        selected = self._seibi_layer_conditions_selected()
        btn.setVisible(selected)
        ready = (
            selected
            and self._seibi_completed_cond == self._seibi_conditions()
            and not self._seibi_busy('search')
            and not self._seibi_updating
            and bool(self._seibi_rendered_items)
        )
        # 作成中は、条件変更側からの解除だけ受け付ける。
        building = bool(getattr(self, '_seibi_layer_building', False))
        btn.setEnabled(ready and not building)

    def _seibi_on_layer_toggled(self, on):
        if not on:
            self._seibi_remove_integrated_layer()
            self._seibi_update_layer_button()
            return
        if not self._seibi_layer_conditions_selected():
            self._seibi_remove_integrated_layer()
            self._seibi_update_layer_button()
            return
        rows = {r['fid']: r for _n, r in self._seibi_rendered_items}
        idx = self._seibi_index
        if not rows or idx is None or self._seibi_completed_cond != self._seibi_conditions():
            self._seibi_remove_integrated_layer()
            self._seibi_update_layer_button()
            return

        self._seibi_remove_integrated_layer(reset_button=False)
        self._seibi_layer_gen += 1
        gen = self._seibi_layer_gen
        self._seibi_layer_building = True
        self._seibi_layer_target_rows = rows
        tiles = sorted(idx.tiles_for([fid for fid in rows if idx.has(fid)]))
        self._seibi_layer_queue = list(tiles)
        self._seibi_layer_bytes = {}
        self._seibi_layer_need = len(tiles)
        self._seibi_update_layer_button()
        if not tiles:
            self._seibi_layer_failed(gen, '統合できるポリゴンがありません')
            return
        self._seibi_progress(f'統合レイヤー用のタイルを取得中… 0／{len(tiles)}枚', 'layer')
        for _ in range(min(_FETCH_PARALLEL, len(tiles))):
            self._seibi_layer_fetch_next(gen)

    def _seibi_layer_fetch_next(self, gen):
        if gen != self._seibi_layer_gen or not self._seibi_layer_queue:
            return
        x, y = self._seibi_layer_queue.pop(0)
        cached = self._seibi_read_tile_cache(si.TILE_ZOOM, x, y)
        if cached is not None:
            QTimer.singleShot(0, lambda: self._seibi_layer_on_bytes(gen, x, y, cached))
            return

        def cb(data, xx=x, yy=y):
            if data:
                self._seibi_write_tile_cache(si.TILE_ZOOM, xx, yy, data)
            self._seibi_layer_on_bytes(gen, xx, yy, data)
        self._get_binary(si.TILE_URL.format(z=si.TILE_ZOOM, x=x, y=y), cb)

    def _seibi_layer_on_bytes(self, gen, x, y, data):
        if gen != self._seibi_layer_gen:
            return
        self._seibi_layer_bytes[(x, y)] = data or b''
        done = len(self._seibi_layer_bytes)
        self._seibi_progress(
            f'統合レイヤー用のタイルを取得中… {done}／{self._seibi_layer_need}枚', 'layer')
        if self._seibi_layer_queue:
            QTimer.singleShot(0, lambda: self._seibi_layer_fetch_next(gen))
        if done < self._seibi_layer_need:
            return
        tiles = [(si.TILE_ZOOM, xx, yy, raw)
                 for (xx, yy), raw in self._seibi_layer_bytes.items() if raw]
        self._seibi_layer_bytes = {}
        if not tiles:
            self._seibi_layer_failed(gen, 'タイルを取得できませんでした')
            return
        worker = _TileParseWorker(
            gen, tiles, target_fids=set(self._seibi_layer_target_rows))
        worker.kind = 'layer'
        worker.progress.connect(
            lambda g, d, n: g == self._seibi_layer_gen and self._seibi_progress(
                f'整備事業のポリゴンを統合中… {d}／{n}枚', 'layer'))
        worker.finished_ok.connect(self._seibi_layer_on_parsed)
        worker.failed.connect(lambda g, msg: self._seibi_layer_failed(g, f'ポリゴンを統合できませんでした: {msg}'))
        self._seibi_start_worker(worker)

    def _seibi_layer_on_parsed(self, gen, merged):
        if gen != self._seibi_layer_gen:
            return
        layer = QgsVectorLayer(f'MultiPolygon?crs={_WGS84.authid()}', _SEIBI_LAYER_NAME, 'memory')
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField('番号', QMetaType.Type.LongLong),
            QgsField('年度', QMetaType.Type.QString),
            QgsField('市町', QMetaType.Type.QString),
            QgsField('事業内容', QMetaType.Type.QString),
            QgsField('種類', QMetaType.Type.QString),
            QgsField('面積', QMetaType.Type.Double),
            QgsField('樹種', QMetaType.Type.QString),
            QgsField('林齢', QMetaType.Type.Int),
        ])
        layer.updateFields()
        features = []
        for fid, row in self._seibi_layer_target_rows.items():
            wkt = merged.get(fid)
            if not wkt:
                continue
            geom = QgsGeometry.fromWkt(wkt)
            if geom is None or geom.isEmpty():
                continue
            geom.convertToMultiType()
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes([
                fid, row['年度'], self._seibi_city_text(fid), row['事業内容'], row['種類'],
                float(row['面積']), row['樹種'], int(row['林齢']),
            ])
            features.append(feat)
        if not features:
            self._seibi_layer_failed(gen, '統合できるポリゴンがありません')
            return
        pr.addFeatures(features)
        layer.updateExtents()
        layer.setOpacity(0.60)
        layer.setCustomProperty('fcloud_shizuoka/generated', 'seibi')
        self._remove_layers_by_name(_SEIBI_LAYER_NAME)
        self._add_layer_above_gpkg(layer, visible=True)
        self._seibi_integrated_layer_id = layer.id()
        self._seibi_layer_building = False
        self._seibi_progress_end('layer')
        self._seibi_update_layer_button()
        missing = len(self._seibi_layer_target_rows) - len(features)
        text = f'統合レイヤーに {len(features):,}件を追加しました'
        if missing:
            text += f'（形のない {missing:,}件は未追加）'
        self._seibi_flash(text)
        self._refresh_map_canvas()

    def _seibi_layer_failed(self, gen, message):
        if gen != self._seibi_layer_gen:
            return
        self._seibi_layer_building = False
        self._seibi_progress_end('layer')
        btn = getattr(self, 'btn_seibi_layer', None)
        if btn is not None and not sip.isdeleted(btn):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        self._seibi_update_layer_button()
        self._seibi_flash(message)

    def _seibi_remove_integrated_layer(self, reset_button=True):
        self._seibi_layer_gen = getattr(self, '_seibi_layer_gen', 0) + 1
        self._seibi_layer_building = False
        for worker in list(getattr(self, '_seibi_workers', [])):
            if getattr(worker, 'kind', '') == 'layer':
                worker.cancelled = True
        removed = remove_project_layer(
            QgsProject.instance(), getattr(self, '_seibi_integrated_layer_id', None))
        self._seibi_integrated_layer_id = None
        self._seibi_progress_end('layer')
        btn = getattr(self, 'btn_seibi_layer', None)
        if reset_button and btn is not None and not sip.isdeleted(btn):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        if removed:
            self._refresh_map_canvas()

    def _seibi_reset_layer_range(self):
        self._seibi_rng_gen = getattr(self, '_seibi_rng_gen', 0) + 1
        self._seibi_rng = None
        self._seibi_range_counts = None
        self._seibi_city_counts = None
        self._seibi_fill_city_options()  # 重ね合わせが済むまで市町は選べない

    def _seibi_ensure_layer_range(self):
        key = self._seibi_range_key()
        if key is None:
            return
        st = getattr(self, '_seibi_rng', None)
        if st is not None and st.get('key') == key:
            return  # 求め終えた、または求めている途中
        self._seibi_reset_layer_range()
        gen = self._seibi_rng_gen
        self._seibi_rng = {'key': key, 'fids': None, 'cities': {}}
        layer, idx = self._seibi_layer(), self._seibi_index
        tr = QgsCoordinateTransform(layer.crs(), _WGS84, QgsProject.instance())
        try:
            ext = tr.transformBoundingBox(layer.extent())
        except QgsCsException as e:
            print(f'[fcloud] 整備事業: 計画図レイヤーの範囲を変換できません: {e}')
            self._seibi_rng['fids'] = set()
            return
        box = (ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())
        tiles, hinted = idx.candidates_in_box(box)
        self._seibi_rng_hinted = hinted
        need = list(tiles)  # 形で重ねるので、範囲に重なるタイルはすべて読む（保存したものは保存から）
        self._seibi_rng_bytes = {}
        if not need:
            self._seibi_rng_points(gen)
            return
        self._seibi_rng_queue = list(need)
        self._seibi_rng_need = len(need)
        self._seibi_progress(f'計画図レイヤーの範囲内の整備事業を数えています… タイル 0／{len(need)}枚', 'rng')
        for _ in range(min(_FETCH_PARALLEL, len(need))):
            self._seibi_rng_fetch_next(gen)

    def _seibi_rng_fetch_next(self, gen):
        if gen != self._seibi_rng_gen or not self._seibi_rng_queue:
            return
        x, y = self._seibi_rng_queue.pop(0)
        cached = self._seibi_read_tile_cache(si.TILE_ZOOM, x, y)
        if cached is not None:
            self._seibi_rng_on_bytes(gen, x, y, cached)
            return

        def cb(data, xx=x, yy=y):
            if data:
                self._seibi_write_tile_cache(si.TILE_ZOOM, xx, yy, data)
            self._seibi_rng_on_bytes(gen, xx, yy, data)
        self._get_binary(si.TILE_URL.format(z=si.TILE_ZOOM, x=x, y=y), cb)

    def _seibi_rng_on_bytes(self, gen, x, y, data):
        if gen != self._seibi_rng_gen:
            return
        self._seibi_rng_bytes[(x, y)] = data or b''
        done = len(self._seibi_rng_bytes)
        self._seibi_progress(f'計画図レイヤーの範囲内の整備事業を数えています… タイル {done}／{self._seibi_rng_need}枚', 'rng')
        if done < self._seibi_rng_need:
            QTimer.singleShot(0, lambda: self._seibi_rng_fetch_next(gen))
            return
        self._seibi_rng_points(gen)

    def _seibi_rng_points(self, gen):
        """タイルに無い番号のうち、目印のタイルが範囲に重なるものの位置を、1件取り出しで集める
        （前に取り出した位置は控えから使う）。"""
        for fid, pt in self._seibi_read_point_cache().items():
            self._seibi_points.setdefault(fid, pt)
        for fid, row in self._seibi_read_record_cache().items():
            if 'lon' in row:
                self._seibi_points.setdefault(fid, (row['lon'], row['lat']))
        self._seibi_rng_pts = {f: self._seibi_points[f] for f in self._seibi_rng_hinted if f in self._seibi_points}
        self._seibi_rng_hq = [f for f in self._seibi_rng_hinted if f not in self._seibi_points]
        self._seibi_rng_hn = len(self._seibi_rng_hq)
        self._seibi_rng_next_hint(gen)

    def _seibi_rng_next_hint(self, gen):
        if gen != self._seibi_rng_gen:
            return
        if not self._seibi_rng_hq:
            if self._seibi_rng_hn:
                self._seibi_write_point_cache(self._seibi_rng_pts)
            self._seibi_rng_judge(gen)
            return
        fid = self._seibi_rng_hq.pop(0)
        done = self._seibi_rng_hn - len(self._seibi_rng_hq)
        self._seibi_progress(f'計画図レイヤーの範囲内の整備事業を数えています… タイルに無いもの {done}／{self._seibi_rng_hn}件', 'rng')

        def cb(data, f=fid):
            if gen != self._seibi_rng_gen:
                return
            rec = data[0] if isinstance(data, list) and data else data
            try:
                r = si.row_from_api_record(rec)
            except (KeyError, TypeError, ValueError):
                r = None
            if r is not None and 'lon' in r:
                self._seibi_points[f] = (r['lon'], r['lat'])
                self._seibi_rng_pts[f] = (r['lon'], r['lat'])
            QTimer.singleShot(_RECORD_INTERVAL_MS, lambda: self._seibi_rng_next_hint(gen))
        self._get_api(f'{_API_BASE}{si.API_RECORD_PATH.format(fid=fid)}', cb)

    def _seibi_rng_judge(self, gen):
        layer = self._seibi_layer()
        if layer is None:
            return
        self._seibi_progress('計画図レイヤーの範囲内の整備事業を数えています… 小班と重ねています', 'rng')
        tr = QgsCoordinateTransform(_WGS84, layer.crs(), QgsProject.instance())
        tiles = [(si.TILE_ZOOM, x, y, b) for (x, y), b in self._seibi_rng_bytes.items() if b]
        self._seibi_rng_bytes = {}
        fields = layer.fields()
        worker = _RangeWorker(gen, QgsVectorLayerFeatureSource(layer), tr, tiles, dict(self._seibi_rng_pts),
                              fields.indexOf('市町村CD'), fields.indexOf('市町村名称'))
        worker.progress.connect(lambda g, d, n: g == self._seibi_rng_gen and self._seibi_progress(
            f'計画図レイヤーの範囲内の整備事業を数えています… 小班と重ねています {d}／{n}枚', 'rng'))
        worker.finished_ok.connect(self._seibi_rng_on_judged)
        worker.failed.connect(self._seibi_rng_on_failed)
        self._seibi_start_worker(worker)

    def _seibi_rng_on_failed(self, gen, msg):
        if gen != self._seibi_rng_gen:
            return
        print(f'[fcloud] 整備事業の範囲の判定に失敗: {msg}')
        self._seibi_progress_end('rng')
        self._seibi_rng['fids'] = set()
        self._seibi_rng['cities'] = {}
        self._seibi_city_counts = {}
        self._seibi_notes.append('計画図レイヤーとの重ね合わせに失敗しました')
        self._seibi_fill_city_options()
        self._seibi_render()

    def _seibi_rng_on_judged(self, gen, result):
        if gen != self._seibi_rng_gen:
            return
        inside, cities = result
        self._seibi_progress_end('rng')
        self._seibi_rng['fids'] = inside
        self._seibi_rng['cities'] = cities
        idx = self._seibi_index
        counts, city_counts = {}, {}
        for fid in inside:
            if idx.has(fid):
                key = idx.combos[idx.combo_of_fid[fid - 1]]
                counts[key] = counts.get(key, 0) + 1
                for c in cities.get(fid, ()):
                    city_counts[(key, c)] = city_counts.get((key, c), 0) + 1
        self._seibi_range_counts = counts
        self._seibi_city_counts = city_counts
        self._seibi_fill_city_options()
        self._seibi_update_option_states()
        self._seibi_render()

    # ------------------------------------------------------------------
    # 行の選択 → 地図
    # ------------------------------------------------------------------

    def _seibi_on_row_selected(self, *_args):
        self._seibi_hl_gen += 1
        gen = self._seibi_hl_gen
        self._clear_selection_highlights()
        sel = self.tbl_seibi.selectionModel()
        if sel is None or not sel.hasSelection():
            self._clear_cloud_record_info()
            return
        row_i = min(r.top() for r in sel.selection())
        row = self.tbl_seibi.model().row_at(row_i)
        if row is None:
            return
        self._show_cloud_table_row_info('整備事業', self.tbl_seibi, row_i)
        fid = row['fid']
        idx = self._seibi_index
        tiles = [idx.tiles[ti] for ti in idx.tiles_of_fid[fid - 1]] if idx and idx.has(fid) else []
        if tiles:
            self._seibi_find_shape(gen, fid, [(si.TILE_ZOOM, x, y) for x, y in tiles], deep=True)
        else:
            self._seibi_find_shape_deep(gen, fid)

    def _seibi_find_shape(self, gen, fid, tiles, deep):
        """tiles（[(z, x, y)]）から番号 fid の形を集めて出す。無ければ deep のとき細かいズームで探す。"""
        got = {}

        def on_bytes(key, data):
            if gen != self._seibi_hl_gen:
                return
            got[key] = data or b''
            if len(got) < len(tiles):
                return
            items = [(z, x, y, b) for (z, x, y), b in got.items() if b]
            worker = _TileParseWorker(gen, items, target_fid=fid)
            worker.finished_ok.connect(lambda g, wkts: self._seibi_on_shape(g, fid, wkts, deep))
            worker.failed.connect(lambda g, msg: self._seibi_on_shape(g, fid, [], deep))
            self._seibi_start_worker(worker)

        for z, x, y in tiles:
            cached = self._seibi_read_tile_cache(z, x, y)
            if cached is not None:
                on_bytes((z, x, y), cached)
                continue

            def cb(data, k=(z, x, y)):
                if data:
                    self._seibi_write_tile_cache(*k, data)
                on_bytes(k, data)
            self._get_binary(si.TILE_URL.format(z=z, x=x, y=y), cb)

    def _seibi_on_shape(self, gen, fid, wkts, deep):
        if gen != self._seibi_hl_gen:
            return
        geoms = [QgsGeometry.fromWkt(w) for w in wkts]
        geoms = [g for g in geoms if g is not None and not g.isEmpty()]
        if geoms:
            geom = QgsGeometry.unaryUnion(geoms) if len(geoms) > 1 else geoms[0]
            if geom is None or geom.isEmpty():
                geom = geoms[0]
            self._add_selection_highlight(geom, _WGS84)
            self._zoom_to_selection_highlights()
            self._refresh_map_canvas()
            return
        if deep:
            self._seibi_find_shape_deep(gen, fid)
        else:
            self._seibi_show_point(gen, fid)

    def _seibi_find_shape_deep(self, gen, fid, zi=0):
        """細かいズーム（13〜16）で、位置のタイルから形を探す。"""
        pt = self._seibi_points.get(fid)
        if pt is None:
            row = self._seibi_all_rows.get(fid, {})
            if 'lon' in row:
                pt = (row['lon'], row['lat'])
        if pt is None:
            row = self._seibi_read_record_cache().get(fid, {})
            if 'lon' in row:
                pt = (row['lon'], row['lat'])
        if pt is None:
            # 位置が分からない（タイルから取った行）。1件取り出しで位置を得る
            def on_rec(data):
                if gen != self._seibi_hl_gen:
                    return
                rec = data[0] if isinstance(data, list) and data else data
                try:
                    r = si.row_from_api_record(rec)
                except (KeyError, TypeError, ValueError):
                    r = None
                if r is None or 'lon' not in r:
                    self._seibi_no_shape(None)
                    return
                self._seibi_points[fid] = (r['lon'], r['lat'])
                self._seibi_find_shape_deep(gen, fid, zi)
            self._get_api(f'{_API_BASE}{si.API_RECORD_PATH.format(fid=fid)}', on_rec)
            return
        if zi >= len(_DEEP_ZOOMS):
            self._seibi_show_point(gen, fid)
            return
        z = _DEEP_ZOOMS[zi]
        x, y = si.tile_of(pt[0], pt[1], z)

        def on_parsed_shape(g, wkts):
            if g != self._seibi_hl_gen:
                return
            if wkts:
                self._seibi_on_shape(g, fid, wkts, deep=False)
            else:
                self._seibi_find_shape_deep(g, fid, zi + 1)

        def on_bytes(data):
            if gen != self._seibi_hl_gen:
                return
            if data:
                self._seibi_write_tile_cache(z, x, y, data)
            if not data:
                self._seibi_find_shape_deep(gen, fid, zi + 1)
                return
            worker = _TileParseWorker(gen, [(z, x, y, data)], target_fid=fid)
            worker.finished_ok.connect(on_parsed_shape)
            worker.failed.connect(lambda g, msg: on_parsed_shape(g, []))
            self._seibi_start_worker(worker)

        cached = self._seibi_read_tile_cache(z, x, y)
        if cached is not None:
            on_bytes(cached)
        else:
            self._get_binary(si.TILE_URL.format(z=z, x=x, y=y), on_bytes)

    def _seibi_show_point(self, gen, fid):
        if gen != self._seibi_hl_gen:
            return
        self._seibi_no_shape(self._seibi_points.get(fid))

    def _seibi_no_shape(self, pt):
        if pt is not None:
            self._add_selection_highlight(QgsGeometry.fromPointXY(QgsPointXY(*pt)), _WGS84)
            self._zoom_to_selection_highlights()
            self._refresh_map_canvas()
            self._seibi_flash('形がありません（森林クラウドの位置に印を出しました）')
        else:
            self._seibi_flash('形がありません（位置も分かりません）')

    def _seibi_flash(self, text):
        """表の上の状態の行に、しばらく出して消す（その間に別の知らせに変わっていたら、そちらは消さない）。"""
        self._seibi_progress(text, 'map')

        def end():
            if getattr(self, '_seibi_busy_texts', {}).get('map') == text:
                self._seibi_progress_end('map')
        QTimer.singleShot(5000, end)

    # ------------------------------------------------------------------
    # タイルの保存（ユーザー設定のフォルダ）
    # ------------------------------------------------------------------

    @staticmethod
    def _seibi_read_tile_cache(z, x, y):
        path = _tile_cache_path(z, x, y)
        try:
            with open(path, 'rb') as f:
                return f.read()
        except OSError:
            return None

    @staticmethod
    def _seibi_write_tile_cache(z, x, y, data):
        path = _tile_cache_path(z, x, y)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(data)
        except OSError as e:
            print(f'[fcloud] 整備事業のタイルを保存できません: {e}')

    @staticmethod
    def _seibi_read_point_cache():
        try:
            with open(_point_cache_path(), 'r', encoding='utf-8') as f:
                return {int(k): (float(v[0]), float(v[1])) for k, v in json.load(f).items()}
        except (OSError, ValueError, TypeError, IndexError, AttributeError):
            return {}

    def _seibi_write_point_cache(self, points):
        merged = self._seibi_read_point_cache()
        merged.update(points)
        path = _point_cache_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({str(k): [v[0], v[1]] for k, v in merged.items()}, f)
        except OSError as e:
            print(f'[fcloud] 整備事業の位置の控えを保存できません: {e}')

    def _seibi_read_record_cache(self):
        """現在の検索軽量化リストと内容が一致する、タイルに無い行だけを読む。"""
        if self._seibi_record_rows is not None:
            return self._seibi_record_rows
        rows = {}
        idx = self._seibi_index
        if idx is None:
            self._seibi_record_rows = rows
            return rows
        try:
            with open(_record_cache_path(), 'r', encoding='utf-8') as f:
                doc = json.load(f)
            if (doc.get('format') != 'fcloud_seibi_record_rows'
                    or int(doc.get('version', 0)) != 1
                    or doc.get('built') != idx.built
                    or int(doc.get('total', -1)) != idx.total):
                raise ValueError('検索軽量化リストと違う控え')
            for key, raw in doc.get('rows', {}).items():
                rec = dict(raw)
                if 'lon' in rec and 'lat' in rec:
                    rec['hilight_point_x'] = rec['lon']
                    rec['hilight_point_y'] = rec['lat']
                row = si.row_from_api_record(rec)
                fid = int(key)
                if (row['fid'] == fid and idx.has(fid) and fid in idx.tile_less
                        and idx.check_row(row) == 'ok'):
                    rows[fid] = row
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            rows = {}
        self._seibi_record_rows = rows
        return rows

    def _seibi_write_record_cache(self, new_rows):
        """タイルに無い行を、現在の検索軽量化リストで照合してから保存する。"""
        idx = self._seibi_index
        if idx is None or not new_rows:
            return
        rows = self._seibi_read_record_cache()
        changed = False
        for fid, row in new_rows.items():
            if (idx.has(fid) and fid in idx.tile_less and idx.check_row(row) == 'ok'):
                saved = dict(row)
                if rows.get(fid) != saved:
                    rows[fid] = saved
                    changed = True
        if not changed:
            return
        path = _record_cache_path()
        tmp = f'{path}.tmp'
        doc = {
            'format': 'fcloud_seibi_record_rows',
            'version': 1,
            'built': idx.built,
            'total': idx.total,
            'rows': {str(fid): row for fid, row in sorted(rows.items())},
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            print(f'[fcloud] 整備事業の行の控えを保存できません: {e}')

    def _seibi_clear_record_cache(self):
        self._seibi_record_rows = None
        try:
            os.remove(_record_cache_path())
        except OSError:
            pass

    def _seibi_clear_tile_cache(self):
        self._seibi_tile_rows = {}
        self._seibi_clear_record_cache()
        try:
            os.remove(_point_cache_path())
        except OSError:
            pass
        folder = os.path.join(_user_dir(), 'tiles')
        if not os.path.isdir(folder):
            return
        for name in os.listdir(folder):
            if name.endswith('.pbf'):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # リストの更新
    # ------------------------------------------------------------------

    def _seibi_on_update_clicked(self):
        if self._seibi_updating:
            self._seibi_update_stop('中止しました（前のリストのまま使います）')
            return
        self._seibi_remove_integrated_layer()
        self._seibi_updating = True
        self._seibi_update_layer_button()
        self._seibi_upd_gen += 1
        gen = self._seibi_upd_gen
        self.btn_seibi_update.setText('中止')
        self._seibi_progress('森林クラウドの総件数を確かめています…', 'upd')
        self._post_api(f'{_API_BASE}{si.API_SEARCH_PATH}',
                       {'年度': '', '事業内容': '', '種類': '', '樹種': ''},
                       lambda data: self._seibi_on_update_total(gen, data))

    def _seibi_update_stop(self, message=None, warn=False):
        self._seibi_upd_gen += 1
        self._seibi_updating = False
        self._seibi_update_layer_button()
        self.btn_seibi_update.setText('リストを更新')
        self._seibi_progress_end('upd')
        if message:
            box = QMessageBox.warning if warn else QMessageBox.information
            box(self, '検索軽量化リスト', message)

    def _seibi_on_update_total(self, gen, data):
        if gen != self._seibi_upd_gen:
            return
        if not isinstance(data, list):
            self._seibi_update_stop('総件数を取れませんでした（ネットワークエラー）', warn=True)
            return
        total = int(data[-1].get('総行数', 0)) if data else 0
        idx = self._seibi_get_index()
        if idx is None:
            self._seibi_ask_rebuild(gen, total, 'リストがありません。')
            return
        if total < idx.total:
            self._seibi_ask_rebuild(
                gen, total, f'総件数が {idx.total:,}件から {total:,}件に減っています（番号の振り直しや削除の可能性）。')
            return
        if total == idx.total:
            missing = idx.missing_fids()
            note = f'（リストに無い番号 {len(missing)}件）' if missing else ''
            self._seibi_update_stop(f'リストは森林クラウドの総件数（{total:,}件）と合っています{note}。')
            return
        self._seibi_add_queue = list(range(idx.total + 1, total + 1))
        self._seibi_add_total = total
        self._seibi_add_rows = []
        self._seibi_add_failed = []
        self._seibi_add_next(gen)

    def _seibi_add_next(self, gen):
        if gen != self._seibi_upd_gen:
            return
        if not self._seibi_add_queue:
            self._seibi_add_finish(gen)
            return
        fid = self._seibi_add_queue.pop(0)
        n = self._seibi_add_total - self._seibi_index.total
        done = n - len(self._seibi_add_queue)
        self._seibi_progress(f'増えた番号を取り出しています… {done}／{n}件', 'upd')
        self._get_api(f'{_API_BASE}{si.API_RECORD_PATH.format(fid=fid)}',
                      lambda data, f=fid: self._seibi_on_add_record(gen, f, data))

    def _seibi_on_add_record(self, gen, fid, data):
        if gen != self._seibi_upd_gen:
            return
        rec = data[0] if isinstance(data, list) and data else data
        try:
            row = si.row_from_api_record(rec)
        except (KeyError, TypeError, ValueError):
            row = None
        if row is None or row['fid'] != fid:
            self._seibi_add_failed.append(fid)
        else:
            self._seibi_add_rows.append(row)
        QTimer.singleShot(_RECORD_INTERVAL_MS, lambda: self._seibi_add_next(gen))

    def _seibi_add_finish(self, gen):
        idx = self._seibi_index
        total = self._seibi_add_total
        if self._seibi_add_failed:
            self._seibi_ask_rebuild(
                gen, total,
                f'増えた番号のうち {len(self._seibi_add_failed)}件を取り出せませんでした（番号の抜けの可能性）。')
            return
        try:
            new = idx.with_added_rows(self._seibi_add_rows, total, datetime.date.today().isoformat())
            self._seibi_save_index(new)
        except (OSError, si.SeibiIndexError) as e:
            self._seibi_update_stop(f'リストを保存できませんでした: {e}', warn=True)
            return
        self._seibi_clear_tile_cache()
        self._seibi_update_stop(f'{len(self._seibi_add_rows):,}件を書き足しました（総件数 {total:,}件）。')

    def _seibi_save_index(self, idx):
        os.makedirs(_user_dir(), exist_ok=True)
        idx.save(_user_index_path())
        self._seibi_get_index(reload=True)
        self.lbl_cache_ts.setText(self._seibi_index_text(self._seibi_index))
        self._seibi_reset_layer_range()  # リストが変わったので、範囲内の番号は求め直す
        self._seibi_ensure_layer_range()

    def _seibi_ask_rebuild(self, gen, total, reason):
        from .mvt_loader import shizuoka_tiles
        n_tiles = len(shizuoka_tiles(si.TILE_ZOOM))
        minutes = max(1, round(n_tiles * _REBUILD_INTERVAL_MS / 1000 / 60))
        ans = QMessageBox.question(
            self, '検索軽量化リスト',
            f'{reason}\n\nリストを作り直しますか？\n'
            f'静岡県全体のタイル {n_tiles}枚を読み直し、タイルに無い番号を1件ずつ取り出します'
            f'（{minutes}分ほど）。作り直しに失敗したときは前のリストのまま使います。')
        if ans != QMessageBox.StandardButton.Yes:
            self._seibi_update_stop()
            return
        self._seibi_rb_total = total
        self._seibi_rb_queue = list(shizuoka_tiles(si.TILE_ZOOM))
        self._seibi_rb_count = len(self._seibi_rb_queue)
        self._seibi_rb_tiles = []
        self._seibi_rb_next_tile(gen)

    def _seibi_rb_next_tile(self, gen):
        if gen != self._seibi_upd_gen:
            return
        if not self._seibi_rb_queue:
            self._seibi_rb_parse(gen)
            return
        x, y = self._seibi_rb_queue.pop(0)
        done = self._seibi_rb_count - len(self._seibi_rb_queue)
        self._seibi_progress(f'リストを作り直しています… タイル {done}／{self._seibi_rb_count}枚', 'upd')

        def cb(data, xx=x, yy=y):
            if gen != self._seibi_upd_gen:
                return
            if data:
                self._seibi_rb_tiles.append((si.TILE_ZOOM, xx, yy, data))
            QTimer.singleShot(_REBUILD_INTERVAL_MS, lambda: self._seibi_rb_next_tile(gen))
        self._get_binary(si.TILE_URL.format(z=si.TILE_ZOOM, x=x, y=y), cb)

    def _seibi_rb_parse(self, gen):
        worker = _TileParseWorker(gen, list(self._seibi_rb_tiles))
        worker.progress.connect(
            lambda g, d, n: g == self._seibi_upd_gen and self._seibi_progress(
                f'リストを作り直しています… タイルの読み込み {d}／{n}枚', 'upd'))
        worker.finished_ok.connect(self._seibi_rb_parsed)
        worker.failed.connect(lambda g, msg: g == self._seibi_upd_gen and self._seibi_update_stop(
            f'タイルを読めませんでした: {msg}', warn=True))
        self._seibi_start_worker(worker)

    def _seibi_rb_parsed(self, gen, result):
        if gen != self._seibi_upd_gen:
            return
        builder = si.SeibiIndexBuilder()
        for (x, y), (rows, _points) in result.items():
            for row in rows.values():
                builder.rows[row['fid']] = row
                builder.tiles_of.setdefault(row['fid'], set()).add((x, y))
        self._seibi_rb_builder = builder
        self._seibi_rb_result = result
        self._seibi_rb_gaps = builder.missing_fids(self._seibi_rb_total)
        self._seibi_rb_gap_count = len(self._seibi_rb_gaps)
        self._seibi_rb_failed = []
        self._seibi_rb_next_gap(gen)

    def _seibi_rb_next_gap(self, gen):
        if gen != self._seibi_upd_gen:
            return
        if not self._seibi_rb_gaps:
            self._seibi_rb_finish(gen)
            return
        fid = self._seibi_rb_gaps.pop(0)
        done = self._seibi_rb_gap_count - len(self._seibi_rb_gaps)
        self._seibi_progress(f'リストを作り直しています… タイルに無い番号 {done}／{self._seibi_rb_gap_count}件', 'upd')

        def cb(data, f=fid):
            if gen != self._seibi_upd_gen:
                return
            rec = data[0] if isinstance(data, list) and data else data
            try:
                row = si.row_from_api_record(rec)
            except (KeyError, TypeError, ValueError):
                row = None
            if row is None or row['fid'] != f:
                self._seibi_rb_failed.append(f)
            else:
                self._seibi_rb_builder.add_api_row(row)
            QTimer.singleShot(_RECORD_INTERVAL_MS, lambda: self._seibi_rb_next_gap(gen))
        self._get_api(f'{_API_BASE}{si.API_RECORD_PATH.format(fid=fid)}', cb)

    def _seibi_rb_finish(self, gen):
        try:
            new = self._seibi_rb_builder.build(self._seibi_rb_total, datetime.date.today().isoformat())
            self._seibi_save_index(new)
        except (OSError, si.SeibiIndexError) as e:
            self._seibi_update_stop(f'リストを作り直せませんでした（前のリストのまま使います）: {e}', warn=True)
            return
        # 読み直したタイルを、そのまま検索に使えるように残す
        self._seibi_clear_tile_cache()
        self._seibi_write_record_cache({
            fid: self._seibi_rb_builder.rows[fid]
            for fid in self._seibi_index.tile_less
            if fid in self._seibi_rb_builder.rows
        })
        for z, x, y, data in self._seibi_rb_tiles:
            self._seibi_write_tile_cache(z, x, y, data)
        self._seibi_tile_rows = dict(self._seibi_rb_result)
        self._seibi_rb_tiles = []
        missing = self._seibi_rb_failed
        note = (f'\n番号 {len(missing)}件は森林クラウドから取り出せませんでした（リストに無い番号として扱います）。'
                if missing else '')
        self._seibi_update_stop(f'リストを作り直しました（総件数 {self._seibi_rb_total:,}件）。{note}')

    # ------------------------------------------------------------------
    # スレッドの管理
    # ------------------------------------------------------------------

    def _seibi_start_worker(self, worker):
        _SEIBI_WORKERS.add(worker)
        self._seibi_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._seibi_worker_done(w))
        worker.start()

    def _seibi_worker_done(self, worker):
        _SEIBI_WORKERS.discard(worker)
        workers = getattr(self, '_seibi_workers', [])
        if worker in workers:
            workers.remove(worker)

    def _seibi_cancel_workers(self):
        """新しい検索を始めるとき、前の検索のスレッドだけを止める。"""
        for worker in list(getattr(self, '_seibi_workers', [])):
            if getattr(worker, 'kind', '') == 'search':
                worker.cancelled = True

    def _seibi_cancel_all(self):
        """プラグイン終了時：進行中の検索・更新を止め、スレッドから画面へ知らせないようにする。"""
        self._seibi_gen += 1
        self._seibi_hl_gen += 1
        self._seibi_upd_gen += 1
        self._seibi_updating = False
        for worker in list(getattr(self, '_seibi_workers', [])):
            worker.cancelled = True
            for signal in (getattr(worker, 'progress', None), worker.finished_ok, worker.failed):
                if signal is None:
                    continue
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass
            if worker.isRunning():
                worker.wait(3000)
