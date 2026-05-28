# -*- coding: utf-8 -*-
import json
import os
import sip
import urllib.parse

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLabel, QTabWidget, QTextBrowser,
    QPushButton, QFrame,
)
from qgis.PyQt.QtCore import Qt, QUrl, QByteArray, QSettings
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeatureRequest,
    QgsNetworkAccessManager, QgsCoordinateTransform, QgsWkbTypes,
    QgsCoordinateReferenceSystem, QgsLayerTreeLayer,
)
from qgis.gui import QgsRubberBand

from .cache_db import CacheDB
from .constants import _API_BASE, _CITY_TO_NORIN, _PRIMARY_FIELDS, _HISTORY_FIELDS
from .tab_hoanrin import HoanrinMixin
from .tab_mori    import MoriMixin
from .tab_keikaku import KeikakuMixin
from .tab_rinchi  import RinchiMixin

# 複数タブで共有する選択ハイライト色
_HL_SEL_BORDER = QColor(210,  30,  30, 220)
_HL_SEL_FILL   = QColor(210,  30,  30,  25)


class FcloudDockWidget(HoanrinMixin, MoriMixin, KeikakuMixin, RinchiMixin, QDockWidget):

    def __init__(self, iface, highlights=None, parent=None):
        super().__init__('静岡県森林クラウド', parent or iface.mainWindow())
        self.iface = iface

        # 全タブ共有の状態変数
        self._connected_layer        = None
        self._hoanrin_highlights     = highlights if highlights is not None else []
        self._selection_highlights   = []
        self._mori_markers           = []
        self._pending_replies        = []
        self._current_raw_hoanrin    = None
        self._current_raw_mori       = None
        self._current_mori_cache_key = ''
        self._mori_vector_layer_id   = None
        self._mori_layer_features    = []
        self._mori_tiles_pending     = 0
        self._mori_tiles_received    = 0
        self._current_raw_keikaku    = None
        self._keikaku_vector_layer_id = None
        self._keikaku_layer_features  = []
        self._keikaku_cd_to_name      = {}
        self._keikaku_tiles_pending   = 0
        self._keikaku_tiles_received  = 0
        self._current_raw_rinchi      = None
        self._current_rinchi_cache_key = ''
        self._prev_tab_index          = -1
        self._first_show              = True
        self._layer_type              = 'gpkg'  # 'gpkg' | 'shp'

        self.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        self._build_ui()
        self._remove_rinchi_layers()
        self._connect_project_signals()

    # ------------------------------------------------------------------
    # UI 構築
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setWidget(root)
        main = QHBoxLayout(root)
        main.setContentsMargins(4, 4, 4, 4)
        main.setSpacing(4)

        # ── 左パネル ──────────────────────────────────────────────────
        left_w = QWidget()
        left_w.setMinimumWidth(290)
        left_w.setMaximumWidth(360)
        left_v = QVBoxLayout(left_w)
        left_v.setContentsMargins(0, 0, 0, 0)
        left_v.setSpacing(4)

        row_a = QHBoxLayout()
        row_a.addWidget(QLabel('GPKGレイヤー:'))
        self.layer_combo = QComboBox()
        row_a.addWidget(self.layer_combo, 1)
        left_v.addLayout(row_a)

        self.left_tab = QTabWidget()

        selected_tab = QWidget()
        selected_v = QVBoxLayout(selected_tab)
        selected_v.setContentsMargins(0, 0, 0, 3)
        selected_v.setSpacing(4)
        self.info_browser = QTextBrowser()
        self.info_browser.setOpenExternalLinks(False)
        selected_v.addWidget(self.info_browser, 1)
        self.lbl_selected = QLabel('GPKGレイヤーで選択してください')
        self.lbl_selected.setStyleSheet('font-weight: bold; padding: 2px;')
        selected_v.addWidget(self.lbl_selected)
        self.left_tab.addTab(selected_tab, '選択中の小班')

        cloud_info_tab = QWidget()
        cloud_info_v = QVBoxLayout(cloud_info_tab)
        cloud_info_v.setContentsMargins(0, 0, 0, 3)
        cloud_info_v.setSpacing(4)
        self.lbl_cloud_selected = QLabel('右側の表で選択してください')
        self.lbl_cloud_selected.setStyleSheet('font-weight: bold; padding: 2px;')
        cloud_info_v.addWidget(self.lbl_cloud_selected)
        self.cloud_info_browser = QTextBrowser()
        self.cloud_info_browser.setOpenExternalLinks(False)
        cloud_info_v.addWidget(self.cloud_info_browser, 1)
        self.left_tab.addTab(cloud_info_tab, 'クラウド情報')

        _help_lbl = QLabel('<a href="#">マニュアル</a>')
        _help_lbl.setStyleSheet('font-size: 11px; padding-right: 4px; padding-bottom: 4px;')
        _help_lbl.linkActivated.connect(lambda _: self._open_manual())
        self.left_tab.setCornerWidget(_help_lbl, Qt.TopRightCorner)

        left_v.addWidget(self.left_tab, 1)
        main.addWidget(left_w)

        sep = QFrame()
        sep.setFrameShape(QFrame.NoFrame)
        sep.setFixedWidth(1)
        main.addWidget(sep)

        # ── 右パネル ──────────────────────────────────────────────────
        right_w = QWidget()
        right_v = QVBoxLayout(right_w)
        right_v.setContentsMargins(0, 0, 0, 0)
        right_v.setSpacing(0)

        self.cloud_tab = QTabWidget()
        self.cloud_tab.addTab(self._build_tab_hoanrin(), '保安林台帳')
        self.cloud_tab.addTab(self._build_tab_seibi(),   '整備事業')
        self.cloud_tab.addTab(self._build_tab_mori(),    '森の力')
        self.cloud_tab.addTab(self._build_tab_keikaku(), '経営計画')
        self.cloud_tab.addTab(self._build_tab_rinchi(),  '林地開発')

        self.btn_mori_fullscreen = QPushButton('全画面')
        self.btn_mori_fullscreen.setCheckable(True)
        self.btn_mori_fullscreen.setToolTip('全画面 / 格納')
        self.btn_mori_fullscreen.toggled.connect(self._toggle_mori_fullscreen)
        _corner_w = QWidget()
        _corner_l = QHBoxLayout(_corner_w)
        _corner_l.setContentsMargins(0, 3, 0, 3)
        _corner_l.addWidget(self.btn_mori_fullscreen)
        self.cloud_tab.setCornerWidget(_corner_w, Qt.TopRightCorner)

        right_v.addWidget(self.cloud_tab, 1)

        _btn_style = 'padding: 2px 10px;'
        _lbl_style = 'color: gray; font-size: 10px;'

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(2)

        bottom_row.addWidget(QLabel('レイヤー追加:'))
        self.btn_rindo = QPushButton('林道')
        self.btn_rindo.setCheckable(True)
        self.btn_rindo.setToolTip('林道 MVT レイヤーを追加/除去')
        self.btn_rindo.setStyleSheet(_btn_style)
        bottom_row.addWidget(self.btn_rindo)
        bottom_row.addStretch()

        self.btn_cache_save = QPushButton('ローカル保存')
        self.btn_cache_save.setToolTip('現在のデータをローカルDBに保存')
        self.btn_cache_save.setStyleSheet(_btn_style)
        self.lbl_cache_ts = QLabel('取得日時: —')
        self.lbl_cache_ts.setStyleSheet(_lbl_style)
        self.btn_cache_update = QPushButton('更新')
        self.btn_cache_update.setToolTip('APIから再取得してローカルDBを更新')
        self.btn_cache_update.setStyleSheet(_btn_style)
        bottom_row.addWidget(self.btn_cache_save)
        bottom_row.addSpacing(6)
        bottom_row.addWidget(self.lbl_cache_ts)
        bottom_row.addSpacing(4)
        bottom_row.addWidget(self.btn_cache_update)

        right_v.addLayout(bottom_row)
        main.addWidget(right_w, 1)

        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        self.btn_rindo.toggled.connect(lambda on: self._toggle_mvt_layer(
            'https://fcloud.pref.shizuoka.jp/MAP/MVT/MAGIS.RINDO/{z}/{x}/{y}.pbf',
            'fcloud_林道', on))
        self.btn_cache_save.clicked.connect(self._save_current_cache)
        self.btn_cache_update.clicked.connect(self._update_current_cache)
        self.cloud_tab.currentChanged.connect(self._on_tab_changed)

        self._refresh_layer_combo()
        self._on_tab_changed(0)
        self._restore_state()

    def _open_manual(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manual.html')
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _build_tab_seibi(self):
        from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel
        w = QWidget()
        v = QVBoxLayout(w)
        lbl = QLabel('森林クラウド側未実装')
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet('color: gray; font-size: 13px; padding: 20px;')
        v.addWidget(lbl)
        return w

    # ------------------------------------------------------------------
    # 共通ユーティリティ
    # ------------------------------------------------------------------

    def _make_table(self, headers):
        from qgis.PyQt.QtWidgets import QTableWidget, QHeaderView, QFrame
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setAlternatingRowColors(True)
        # Dock の端でネイティブ枠線が見切れることがあるため、表の外枠を明示する。
        t.setFrameShape(QFrame.NoFrame)
        t.setStyleSheet('QTableWidget { border: 1px solid palette(mid); }')
        return t

    def _post_api(self, url, params, callback):
        body = QByteArray(urllib.parse.urlencode(params).encode('utf-8'))
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.ContentTypeHeader,
                      'application/x-www-form-urlencoded')
        req.setRawHeader(b'Accept', b'application/json, text/plain, */*')
        req.setRawHeader(b'Origin', b'https://fcloud.pref.shizuoka.jp')
        req.setRawHeader(
            b'Referer',
            b'https://fcloud.pref.shizuoka.jp/fgis/?version=1.26.0220.a')
        reply = QgsNetworkAccessManager.instance().post(req, body)
        self._pending_replies.append(reply)
        reply.finished.connect(lambda: self._handle_reply(reply, callback))

    def _handle_reply(self, reply, callback):
        data = None
        if reply.error() == QNetworkReply.NoError:
            try:
                data = json.loads(bytes(reply.readAll()).decode('utf-8'))
            except Exception as e:
                print(f'[fcloud_shizuoka] JSON parse error: {e}')
        else:
            print(f'[fcloud_shizuoka] network error: {reply.errorString()}')
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()
        callback(data)

    @staticmethod
    def _extract_records(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ('data', 'list', 'result', 'items'):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []

    def _get_db(self, data_name):
        home = QgsProject.instance().homePath()
        if not home:
            return None
        db_dir = os.path.join(home, 'fcloud_shizuoka')
        return CacheDB(os.path.join(db_dir, f'fcloud_shizuoka_{data_name}.db'))

    # ------------------------------------------------------------------
    # レイヤーツリー / 表示制御
    # ------------------------------------------------------------------

    def _add_layer_above_gpkg(self, layer, visible=True):
        QgsProject.instance().addMapLayer(layer, False)
        root = QgsProject.instance().layerTreeRoot()
        node = QgsLayerTreeLayer(layer)
        node.setItemVisibilityChecked(visible)
        connected = self._connected_layer
        if connected and not sip.isdeleted(connected):
            gpkg_node = root.findLayer(connected.id())
            if gpkg_node:
                parent = gpkg_node.parent()
                idx = list(parent.children()).index(gpkg_node)
                parent.insertChildNode(idx, node)
                return
        root.insertChildNode(0, node)

    def _refresh_map_canvas(self):
        canvas = self.iface.mapCanvas()
        try:
            canvas.clearCache()
        except Exception:
            pass
        canvas.refresh()
        canvas.repaint()

    def _remove_layers_by_name(self, *names):
        for target in names:
            while True:
                found_id = None
                for lid, layer in list(QgsProject.instance().mapLayers().items()):
                    if layer.name() == target:
                        found_id = lid
                        break
                if found_id is None:
                    break
                QgsProject.instance().removeMapLayer(found_id)

    def _cleanup_plugin_layers(self):
        self._remove_mori_vector_layer()
        self._remove_keikaku_vector_layer()
        self._remove_rinchi_layers()
        self._toggle_mvt_layer('', 'fcloud_林道', False)
        for btn in (self.btn_mori_layer, self.btn_keikaku_layer, self.btn_rinchi_layer, self.btn_rindo):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        QSettings().setValue('fcloud_shizuoka/mori_layer_on', False)
        QSettings().setValue('fcloud_shizuoka/keikaku_layer_on', False)
        self._remove_layers_by_name(
            'fcloud_林道', 'fcloud_森の力実施箇所',
            'fcloud_経営計画作成箇所', '林地開発_許可', '林地開発_連絡調整',
        )
        self._refresh_map_canvas()

    def _set_layer_visible(self, layer_id, visible):
        if not layer_id:
            return False
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer_id)
        if node:
            node.setItemVisibilityChecked(visible)
            return True
        return False

    def _toggle_mvt_layer(self, url, name, on):
        from qgis.core import QgsVectorTileLayer
        if on:
            layer = QgsVectorTileLayer(f'type=xyz&url={url}', name)
            if layer.isValid():
                self._add_layer_above_gpkg(layer, visible=True)
        else:
            for lid, layer in list(QgsProject.instance().mapLayers().items()):
                if layer.name() == name:
                    QgsProject.instance().removeMapLayer(lid)
                    break

    # ------------------------------------------------------------------
    # タブ切替（接続部分のクリーンアップを統括）
    # ------------------------------------------------------------------

    def _restore_state(self):
        s = QSettings()
        tab = s.value('fcloud_shizuoka/tab_index', 0, type=int)
        if 0 <= tab < self.cloud_tab.count():
            self.cloud_tab.setCurrentIndex(tab)

    def _on_tab_changed(self, index):
        prev = self._prev_tab_index
        needs_refresh = False
        if prev == 2 and index != 2:
            self._clear_mori_markers()
            needs_refresh = self._set_layer_visible(self._mori_vector_layer_id, False) or needs_refresh
        elif prev == 3 and index != 3:
            needs_refresh = self._set_layer_visible(self._keikaku_vector_layer_id, False) or needs_refresh
        if index == 2 and prev != 2:
            if self.btn_mori_layer.isChecked():
                needs_refresh = self._set_layer_visible(self._mori_vector_layer_id, True) or needs_refresh
        elif index == 3 and prev != 3:
            if self.btn_keikaku_layer.isChecked():
                needs_refresh = self._set_layer_visible(self._keikaku_vector_layer_id, True) or needs_refresh
        self._prev_tab_index = index
        QSettings().setValue('fcloud_shizuoka/tab_index', index)
        if needs_refresh:
            self._refresh_map_canvas()

        if index == 0:
            db = self._get_db('保安林台帳')
            if db is not None:
                ts = db.get_fetched_at('保安林/all')
                if ts:
                    self.lbl_cache_ts.setText(f'取得日時: {ts}')
                    return
            self.lbl_cache_ts.setText('取得日時: —')
        elif index == 2:
            city = self.combo_hoanrin_city.currentText().strip()
            norin = _CITY_TO_NORIN.get(city, '')
            if norin:
                self.combo_mori_norin.setCurrentText(norin)
            gpkg = self._get_mori_gpkg_path()
            if gpkg and os.path.exists(gpkg):
                import datetime
                mtime = os.path.getmtime(gpkg)
                ts = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                self.lbl_cache_ts.setText(f'レイヤーキャッシュ: {ts}')
            else:
                self.lbl_cache_ts.setText('レイヤーキャッシュ: なし')
        elif index == 3:
            db = self._get_db('経営計画')
            if db is not None:
                ts = db.get_fetched_at('経営計画/all')
                if ts:
                    self.lbl_cache_ts.setText(f'取得日時: {ts}')
                    return
            self.lbl_cache_ts.setText('取得日時: —')
        elif index == 4:
            city = self.combo_hoanrin_city.currentText().strip()
            if city:
                i = self.combo_rinchi_city.findData(city)
                if i >= 0:
                    self.combo_rinchi_city.setCurrentIndex(i)
            if self._current_rinchi_cache_key:
                db = self._get_db('林地開発')
                if db is not None:
                    ts = db.get_fetched_at(self._current_rinchi_cache_key)
                    if ts:
                        self.lbl_cache_ts.setText(f'取得日時: {ts}')
                        return
            self.lbl_cache_ts.setText('取得日時: —')
        else:
            self.lbl_cache_ts.setText('取得日時: —')

    # ------------------------------------------------------------------
    # キャッシュ操作
    # ------------------------------------------------------------------

    def _save_current_cache(self):
        tab = self.cloud_tab.currentIndex()
        if tab == 0:
            if self._current_raw_hoanrin is None:
                return
            db = self._get_db('保安林台帳')
            if db is None:
                return
            ts = db.put('保安林/all', self._current_raw_hoanrin)
            self.lbl_cache_ts.setText(f'取得日時: {ts}')
        elif tab == 2:
            if self._current_raw_mori is None or not self._current_mori_cache_key:
                return
            db = self._get_db('森の力')
            if db is None:
                return
            ts = db.put(self._current_mori_cache_key, self._current_raw_mori)
            self.lbl_cache_ts.setText(f'取得日時: {ts}')
        elif tab == 4:
            if self._current_raw_rinchi is None or not self._current_rinchi_cache_key:
                return
            db = self._get_db('林地開発')
            if db is None:
                return
            ts = db.put(self._current_rinchi_cache_key, self._current_raw_rinchi)
            self.lbl_cache_ts.setText(f'取得日時: {ts}')

    def _update_current_cache(self):
        tab = self.cloud_tab.currentIndex()
        if tab == 0:
            self.btn_cache_update.setEnabled(False)
            self.btn_hoanrin_search.setEnabled(False)
            self.lbl_cache_ts.setText('取得日時: 更新中...')
            self._post_api(
                f'{_API_BASE}/advanced-search/保安林検索',
                {},
                self._on_hoanrin_update_result,
            )
        elif tab == 2:
            gpkg = self._get_mori_gpkg_path()
            if gpkg and os.path.exists(gpkg):
                try:
                    os.remove(gpkg)
                except OSError:
                    pass
            self._remove_mori_vector_layer()
            self.btn_mori_layer.setChecked(True)
            self._on_mori_layer_toggled(True)
        elif tab == 3:
            gpkg = self._get_keikaku_gpkg_path()
            if gpkg and os.path.exists(gpkg):
                try:
                    os.remove(gpkg)
                except OSError:
                    pass
            self._remove_keikaku_vector_layer()
            self._keikaku_cd_to_name.clear()
            self.lbl_cache_ts.setText('取得日時: 更新中...')
            self._load_keikaku(force=True)
        elif tab == 4:
            self._search_rinchi(force=True)

    # ------------------------------------------------------------------
    # レイヤー管理（プロジェクト・GPKG 接続）
    # ------------------------------------------------------------------

    def _connect_project_signals(self):
        QgsProject.instance().layersAdded.connect(self._refresh_layer_combo)
        QgsProject.instance().layersRemoved.connect(self._refresh_layer_combo)

    def _refresh_layer_combo(self, *_):
        settings = QSettings()
        current_id = (self.layer_combo.currentData()
                      or settings.value('fcloud_shizuoka/layer_id', ''))
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            src = layer.source().lower()
            if '.gpkg' not in src and '.shp' not in src:
                continue
            if layer.fields().indexOf('KEY1') < 0:
                continue
            self.layer_combo.addItem(layer.name(), layer.id())
        if current_id:
            idx = self.layer_combo.findData(current_id)
            if idx >= 0:
                self.layer_combo.setCurrentIndex(idx)
        self.layer_combo.blockSignals(False)
        self._on_layer_changed()

    def _on_layer_changed(self):
        if self._connected_layer is not None and not sip.isdeleted(self._connected_layer):
            try:
                self._connected_layer.selectionChanged.disconnect(self._on_selection_changed)
            except Exception:
                pass
        self._connected_layer = None

        layer_id = self.layer_combo.currentData()
        if not layer_id:
            self.combo_hoanrin_city.clear()
            self.combo_rinchi_city.blockSignals(True)
            self.combo_rinchi_city.clear()
            self.combo_rinchi_city.addItem('（全て）', '')
            self.combo_rinchi_city.blockSignals(False)
            return
        QSettings().setValue('fcloud_shizuoka/layer_id', layer_id)
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer:
            self._connected_layer = layer
            layer.selectionChanged.connect(self._on_selection_changed)
            flds = [f.name() for f in layer.fields()]
            self._layer_type = 'shp' if '市町村名称' not in flds and '市町村CD' in flds else 'gpkg'
            self._refresh_city_combo(layer)

    def _refresh_city_combo(self, layer):
        from .constants import _CITY_API_MAP, _API_CITY_MAP, _CD_CITY

        if '市町村名称' in [f.name() for f in layer.fields()]:
            idx = layer.fields().indexOf('市町村名称')
            cities = sorted(
                str(v) for v in layer.uniqueValues(idx)
                if v is not None and str(v) not in ('NULL', '')
            )
        elif '市町村CD' in [f.name() for f in layer.fields()]:
            idx = layer.fields().indexOf('市町村CD')
            raw = []
            for v in layer.uniqueValues(idx):
                if v is None or str(v) in ('NULL', ''):
                    continue
                try:
                    api_name = _CD_CITY.get(int(str(v).strip()), '')
                except ValueError:
                    continue
                if api_name:
                    raw.append(_API_CITY_MAP.get(api_name, api_name))
            cities = sorted(raw)
        else:
            return

        current = self.combo_hoanrin_city.currentText()
        self.combo_hoanrin_city.blockSignals(True)
        self.combo_hoanrin_city.clear()
        for city in cities:
            self.combo_hoanrin_city.addItem(city)
        if current:
            i = self.combo_hoanrin_city.findText(current)
            if i >= 0:
                self.combo_hoanrin_city.setCurrentIndex(i)
        self.combo_hoanrin_city.blockSignals(False)
        self._on_hoanrin_city_changed(self.combo_hoanrin_city.currentText())

        from .constants import _API_CITY_MAP
        prev = self.combo_rinchi_city.currentData() or ''
        self.combo_rinchi_city.blockSignals(True)
        self.combo_rinchi_city.clear()
        self.combo_rinchi_city.addItem('（全て）', '')
        for city in cities:
            display_name = _API_CITY_MAP.get(city, city)
            self.combo_rinchi_city.addItem(display_name, city)
        if prev:
            i = self.combo_rinchi_city.findData(prev)
            if i >= 0:
                self.combo_rinchi_city.setCurrentIndex(i)
        self.combo_rinchi_city.blockSignals(False)

    # ------------------------------------------------------------------
    # 選択小班 → 情報表示
    # ------------------------------------------------------------------

    def _on_selection_changed(self, selected_ids, deselected_ids, clear_and_select):
        if not self._connected_layer or sip.isdeleted(self._connected_layer):
            self._connected_layer = None
            return
        features = list(self._connected_layer.getSelectedFeatures())
        if not features:
            self.lbl_selected.setText('GPKGレイヤーで選択してください')
            self.info_browser.clear()
            return
        feat = features[0]
        fnames = feat.fields().names()
        if self._layer_type == 'shp':
            key1 = feat['KEY1'] if 'KEY1' in fnames else ''
            self.lbl_selected.setText(f'小班: {key1}')
        else:
            rinpan = feat['林班_森林簿']     if '林班_森林簿'     in fnames else ''
            junrin = feat['準林班名称']       if '準林班名称'       in fnames else ''
            kohan  = feat['小班_親番_森林簿'] if '小班_親番_森林簿' in fnames else ''
            self.lbl_selected.setText(f'小班: {rinpan}-{junrin}-{kohan}')
        self._show_feature_info(feat)
        self.left_tab.setCurrentIndex(0)

    def _show_feature_info(self, feat):
        if self._layer_type == 'shp':
            self.info_browser.setHtml(
                '<p style="color:gray;padding:8px;line-height:1.6;">'
                '計画図SHPレイヤーのため森林属性は表示されません。<br>'
                'Shinrinbo Code Converter で森林簿と結合したGPKGを作成すると'
                '詳細属性が表示されます。</p>'
            )
            return
        fnames = feat.fields().names()
        parts = ['<table style="border-collapse:collapse;width:100%;">']

        parts.append(
            '<tr><td colspan="2" style="background:#e8f4e8;font-weight:bold;'
            'padding:3px;">基本情報</td></tr>')
        for src, label in _PRIMARY_FIELDS:
            if src not in fnames:
                continue
            val = feat[src]
            if val is None or str(val) in ('', 'NULL'):
                continue
            parts.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;">'
                f'{label}</td><td style="padding:1px 4px;">{val}</td></tr>')

        hist_rows = []
        for y_f, m_f, e_f in _HISTORY_FIELDS:
            yr = feat[y_f] if y_f in fnames else None
            if not yr or str(yr) in ('0', '', 'NULL', 'None'):
                continue
            method = feat[m_f] if m_f in fnames else ''
            etype  = feat[e_f] if e_f in fnames else ''
            hist_rows.append(
                f'<tr><td colspan="2" style="padding:2px 4px;'
                f'border-bottom:1px solid #eee;">'
                f'{yr}年度: {method}（{etype}）</td></tr>')
        if hist_rows:
            parts.append(
                '<tr><td colspan="2" style="background:#e8f4e8;font-weight:bold;'
                'padding:3px;">施業履歴</td></tr>')
            parts.extend(hist_rows)

        parts.append('</table>')
        self.info_browser.setHtml(''.join(parts))

    def _show_cloud_table_row_info(self, title, table, row):
        self.lbl_cloud_selected.setText(title)
        if table is None or row < 0:
            self.cloud_info_browser.clear()
            return
        parts = ['<table style="border-collapse:collapse;width:100%;">']
        for col in range(table.columnCount()):
            header_item = table.horizontalHeaderItem(col)
            item = table.item(row, col)
            if header_item is None or item is None:
                continue
            label = header_item.text().strip()
            val = item.text().strip()
            if not label or val == '':
                continue
            parts.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                f'{label}</td><td style="padding:1px 4px;">{val}</td></tr>')
        parts.append('</table>')
        self.cloud_info_browser.setHtml(''.join(parts))
        self.left_tab.setCurrentIndex(1)

    def _clear_cloud_record_info(self):
        self.lbl_cloud_selected.setText('右側の表で選択してください')
        self.cloud_info_browser.clear()

    # ------------------------------------------------------------------
    # 複数タブで共有する選択ハイライト
    # ------------------------------------------------------------------

    def _clear_selection_highlights(self):
        scene = self.iface.mapCanvas().scene()
        for rb in self._selection_highlights:
            if not sip.isdeleted(rb):
                scene.removeItem(rb)
        self._selection_highlights.clear()

    def _add_selection_highlight(self, geom, src_crs=None):
        from qgis.core import QgsGeometry as _G
        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        g = _G(geom)
        if src_crs and src_crs != dst_crs:
            g.transform(QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance()))
        geom_type = g.type()
        rb_type = (QgsWkbTypes.PolygonGeometry if geom_type == 2
                   else QgsWkbTypes.LineGeometry if geom_type == 1
                   else QgsWkbTypes.PointGeometry)
        rb = QgsRubberBand(canvas, rb_type)
        rb.setColor(_HL_SEL_BORDER)
        rb.setFillColor(_HL_SEL_FILL)
        rb.setWidth(2)
        rb.setToGeometry(g)
        rb.show()
        self._selection_highlights.append(rb)
        return g.boundingBox()

    def _zoom_to_selection_highlights(self, margin_ratio=0.60, min_padding=5):
        bbox = None
        for rb in self._selection_highlights:
            if sip.isdeleted(rb):
                continue
            try:
                geom = rb.asGeometry()
            except Exception:
                geom = None
            if not geom or geom.isEmpty():
                continue
            b = geom.boundingBox()
            if bbox is None:
                bbox = b
            else:
                bbox.combineExtentWith(b)
        if bbox and not bbox.isEmpty():
            pad = max(bbox.width(), bbox.height()) * margin_ratio + min_padding
            bbox.grow(pad)
            canvas = self.iface.mapCanvas()
            canvas.setExtent(bbox)
            canvas.refresh()
            return True
        return False

    # ------------------------------------------------------------------
    # アンロード / クローズ
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._clear_hoanrin_highlights()
        self._clear_selection_highlights()
        self._clear_mori_markers()
        self._cleanup_plugin_layers()
        for reply in list(self._pending_replies):
            reply.abort()
        super().closeEvent(event)

    def cleanup_on_unload(self):
        self._clear_hoanrin_highlights()
        self._clear_selection_highlights()
        self._clear_mori_markers()
        self._cleanup_plugin_layers()
        for reply in list(self._pending_replies):
            try:
                reply.abort()
            except Exception:
                pass
