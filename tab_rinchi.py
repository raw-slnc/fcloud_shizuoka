# -*- coding: utf-8 -*-
import colorsys
import os
import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLabel, QLineEdit, QPushButton,
    QTableWidgetItem, QHeaderView,
)
from qgis.PyQt.QtCore import Qt, QTimer, QUrl, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import (
    QgsProject, QgsFeatureRequest, QgsGeometry,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsPointXY, QgsRectangle, QgsNetworkAccessManager,
    QgsVectorLayer, QgsFeature, QgsField, QgsVectorFileWriter,
)

from .constants import (
    _API_BASE, _API_CITY_MAP, _CITY_CD, _RINCHI_MOKUTEKI,
    _SHIZUOKA_BBOX,
)


class RinchiMixin:

    _RINCHI_ALL_CACHE_KEY = '林地開発/all'
    _RINCHI_LAYER_NAME = 'fcloud_林地開発'
    _RINCHI_GPKG_LAYER_NAME = '林地開発'
    _RINCHI_MVT_ZOOM = 10
    _RINCHI_TILE_SOURCES = [
        ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
         'MAGIS.RINCHI_KAIHATSU_KYOKA/{z}/{x}/{y}.pbf', '許可'),
        ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
         'MAGIS.RINCHI_KAIHATSU_RENRAKU_CHOUSEI/{z}/{x}/{y}.pbf', '連絡調整'),
    ]
    _RINCHI_OLD_LAYER_NAMES = ('林地開発_許可', '林地開発_連絡調整')

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_rinchi(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel('開発区分:'))
        self.combo_rinchi_kubun = QComboBox()
        self.combo_rinchi_kubun.addItem('（全て）', '')
        self.combo_rinchi_kubun.addItem('許可', '許可')
        self.combo_rinchi_kubun.addItem('連絡調整', '連絡調整')
        self.combo_rinchi_kubun.setMaximumWidth(100)
        row1.addWidget(self.combo_rinchi_kubun)
        row1.addSpacing(4)
        row1.addWidget(QLabel('申請者:'))
        self.combo_rinchi_shinseisha = QComboBox()
        self.combo_rinchi_shinseisha.setEditable(True)
        self.combo_rinchi_shinseisha.setInsertPolicy(QComboBox.NoInsert)
        self.combo_rinchi_shinseisha.addItem('（全て）', '')
        self.combo_rinchi_shinseisha.setEnabled(False)
        row1.addWidget(self.combo_rinchi_shinseisha, 2)
        row1.addSpacing(4)
        row1.addWidget(QLabel('開発目的:'))
        self.combo_rinchi_mokuteki = QComboBox()
        self.combo_rinchi_mokuteki.addItem('（全て）', '')
        for m in _RINCHI_MOKUTEKI:
            self.combo_rinchi_mokuteki.addItem(m, m)
        row1.addWidget(self.combo_rinchi_mokuteki, 2)
        self.btn_rinchi_search = QPushButton('検索')
        row1.addWidget(self.btn_rinchi_search)
        row1.addStretch()
        self.btn_rinchi_layer = QPushButton('林地開発レイヤー')
        self.btn_rinchi_layer.setCheckable(True)
        self.btn_rinchi_layer.setToolTip('林地開発の絞り込み結果を地図に表示/非表示')
        row1.addWidget(self.btn_rinchi_layer)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel('所在市町村:'))
        self.combo_rinchi_city = QComboBox()
        self.combo_rinchi_city.addItem('（全て）', '')
        row2.addWidget(self.combo_rinchi_city, 2)
        row2.addSpacing(4)
        row2.addWidget(QLabel('所在地:'))
        self.edit_rinchi_shozaichi = QLineEdit()
        self.edit_rinchi_shozaichi.setPlaceholderText('所在地（任意）')
        row2.addWidget(self.edit_rinchi_shozaichi, 3)
        self._rinchi_shozaichi_timer = QTimer(w)
        self._rinchi_shozaichi_timer.setSingleShot(True)
        self._rinchi_shozaichi_timer.setInterval(180)
        self._rinchi_shozaichi_timer.timeout.connect(self._apply_rinchi_shozaichi_filter)
        v.addLayout(row2)

        self.tbl_rinchi = self._make_table([
            '開発区分', '許可年月日', '申請者', '開発目的', '開発目的(詳細)',
            '所在市町村', '所在地',
            '事業区域面積(ha)', '事業区域内森林面積(ha)', '許可面積',
            '着手年月日', '完了(予定)年月日', 'その他',
        ])
        hdr = self.tbl_rinchi.horizontalHeader()
        for col, w_ in [(0, 80), (1, 90), (7, 110), (8, 140), (9, 70), (10, 90), (11, 105)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            self.tbl_rinchi.setColumnWidth(col, w_)
        v.addWidget(self.tbl_rinchi, 1)

        bottom = QHBoxLayout()
        self.lbl_rinchi_count = QLabel('')
        self.lbl_rinchi_count.setStyleSheet('color: gray; font-size: 10px;')
        bottom.addWidget(self.lbl_rinchi_count, 1)
        v.addLayout(bottom)

        self.btn_rinchi_search.clicked.connect(self._search_rinchi)
        self.combo_rinchi_kubun.activated.connect(self._on_rinchi_search_filter_changed)
        self.combo_rinchi_mokuteki.activated.connect(self._on_rinchi_search_filter_changed)
        self.combo_rinchi_city.activated.connect(self._on_rinchi_search_filter_changed)
        self.combo_rinchi_shinseisha.currentIndexChanged.connect(self._on_rinchi_shinseisha_changed)
        self.edit_rinchi_shozaichi.textChanged.connect(self._on_rinchi_shozaichi_text_changed)
        self.tbl_rinchi.itemSelectionChanged.connect(self._on_rinchi_selected)
        self.btn_rinchi_layer.toggled.connect(self._on_rinchi_layer_toggled)
        return w

    # ------------------------------------------------------------------
    # 検索・表示
    # ------------------------------------------------------------------

    def _search_rinchi(self, force=False, save_to_db=False):
        self.tbl_rinchi.setRowCount(0)
        self.lbl_rinchi_count.setText('検索中...')
        self.btn_rinchi_search.setEnabled(False)
        self._current_rinchi_cache_key = self._RINCHI_ALL_CACHE_KEY

        if not force and self._current_raw_rinchi is not None:
            self.btn_rinchi_search.setEnabled(True)
            total = self._display_rinchi_table(self._current_raw_rinchi)
            self._auto_show_rinchi_layer(total)
            return

        if not force:
            db = self._get_db('林地開発')
            if db is not None:
                cached, ts = db.get(self._current_rinchi_cache_key)
                if cached is not None:
                    self.btn_rinchi_search.setEnabled(True)
                    self._current_raw_rinchi = cached
                    self.lbl_cache_ts.setText(f'取得日時: {ts}')
                    total = self._display_rinchi_table(cached)
                    self._auto_show_rinchi_layer(total)
                    return

        self._search_rinchi_all_cities(force=force, save_to_db=save_to_db)

    def _search_rinchi_all_cities(self, force=False, save_to_db=False):
        cache_key = self._current_rinchi_cache_key
        db = self._get_db('林地開発')
        if not force and db is not None:
            cached, ts = db.get(cache_key)
            if cached is not None and self._extract_records(cached):
                self.btn_rinchi_search.setEnabled(True)
                self._current_raw_rinchi = cached
                self.lbl_cache_ts.setText(f'取得日時: {ts}')
                total = self._display_rinchi_table(cached)
                self._auto_show_rinchi_layer(total)
                return

        cities = []
        for i in range(1, self.combo_rinchi_city.count()):
            city = self.combo_rinchi_city.itemData(i) or ''
            if city and city not in cities:
                cities.append(city)
        if not cities:
            cities = sorted({
                _API_CITY_MAP.get(api_city, api_city)
                for api_city in _CITY_CD.keys()
            })

        if not cities:
            self.btn_rinchi_search.setEnabled(True)
            self._current_raw_rinchi = []
            self.lbl_cache_ts.setText('取得日時: —')
            self.tbl_rinchi.setRowCount(0)
            self.lbl_rinchi_count.setText('0件')
            self._apply_rinchi_layer_filter()
            self._update_cache_btn_states()
            return

        aggregate = []

        def _append_records(data):
            for rec in self._extract_records(data):
                if isinstance(rec, dict):
                    aggregate.append(rec)

        fetch_cities = list(cities)
        if not force and db is not None:
            missing = []
            for city in cities:
                city_key = f'林地開発/市={city}&区分=&目的='
                cached, _ = db.get(city_key)
                if cached is None:
                    missing.append(city)
                else:
                    _append_records(cached)
            if not missing:
                self.btn_rinchi_search.setEnabled(True)
                self._current_raw_rinchi = aggregate
                self.lbl_cache_ts.setText(
                    f'取得日時: {ts}'
                    if (ts := db.get_fetched_at(cache_key))
                    else '取得日時: 未保存')
                total = self._display_rinchi_table(aggregate)
                self._auto_show_rinchi_layer(total)
                self._update_cache_btn_states()
                return
            fetch_cities = missing

        self._rinchi_all_city_total = len(fetch_cities)
        self._rinchi_all_city_pending = len(fetch_cities)
        self._rinchi_all_city_results = aggregate
        self._rinchi_save_after_fetch = save_to_db
        self.lbl_rinchi_count.setText(f'検索中... (0/{self._rinchi_all_city_total})')

        for city in fetch_cities:
            params = {'所在市町村': _API_CITY_MAP.get(city, city)}
            self._post_api(
                f'{_API_BASE}/advanced-search/林地開発検索',
                params,
                lambda data, city_name=city: self._on_rinchi_all_city_result(data, city_name),
            )

    def _on_rinchi_all_city_result(self, data, city_name=''):
        if data is not None:
            for rec in self._extract_records(data):
                if isinstance(rec, dict):
                    self._rinchi_all_city_results.append(rec)
        self._rinchi_all_city_pending -= 1
        self.lbl_rinchi_count.setText(
            f'検索中... ({self._rinchi_all_city_total - self._rinchi_all_city_pending}/'
            f'{self._rinchi_all_city_total})')
        if self._rinchi_all_city_pending > 0:
            return
        self.btn_rinchi_search.setEnabled(True)
        self._current_raw_rinchi = list(self._rinchi_all_city_results)
        if getattr(self, '_rinchi_save_after_fetch', False):
            ts = self._save_rinchi_cache_to_db()
            self.lbl_cache_ts.setText(f'取得日時: {ts}' if ts else '取得日時: —')
        else:
            self.lbl_cache_ts.setText('取得日時: 未保存')
        total_rows = self._display_rinchi_table(self._current_raw_rinchi)
        self._auto_show_rinchi_layer(total_rows)
        self._update_cache_btn_states()

    def _save_rinchi_cache_to_db(self):
        if self._current_raw_rinchi is None:
            return None
        db = self._get_db('林地開発')
        if db is None:
            return None
        ts = db.put(self._RINCHI_ALL_CACHE_KEY, self._current_raw_rinchi)
        for city in sorted({
            _API_CITY_MAP.get(
                str(rec.get('所在市町村', '') or '').strip(),
                str(rec.get('所在市町村', '') or '').strip(),
            )
            for rec in self._current_raw_rinchi if isinstance(rec, dict)
        }):
            if not city:
                continue
            city_records = [
                rec for rec in self._current_raw_rinchi
                if isinstance(rec, dict)
                and _API_CITY_MAP.get(
                    str(rec.get('所在市町村', '') or '').strip(),
                    str(rec.get('所在市町村', '') or '').strip(),
                ) == city
            ]
            db.put(f'林地開発/市={city}&区分=&目的=', city_records)
        self._current_rinchi_cache_key = self._RINCHI_ALL_CACHE_KEY
        return ts

    def _on_rinchi_search_filter_changed(self, *_):
        self.combo_rinchi_shinseisha.blockSignals(True)
        self.combo_rinchi_shinseisha.setCurrentIndex(0)
        self.combo_rinchi_shinseisha.blockSignals(False)
        if self._current_raw_rinchi is None:
            self._search_rinchi()
        else:
            total = self._display_rinchi_table(self._current_raw_rinchi)
            self._auto_show_rinchi_layer(total)

    def _on_rinchi_shinseisha_changed(self, *_):
        if self._current_raw_rinchi is not None:
            total = self._display_rinchi_table(self._current_raw_rinchi)
            self._auto_show_rinchi_layer(total)

    def _on_rinchi_shozaichi_text_changed(self, *_):
        if self._current_raw_rinchi is not None:
            self._rinchi_shozaichi_timer.start()

    def _apply_rinchi_shozaichi_filter(self):
        if self._current_raw_rinchi is not None:
            total = self._display_rinchi_table(self._current_raw_rinchi)
            self._auto_show_rinchi_layer(total)

    def _current_rinchi_shinseisha_filter(self):
        if self.combo_rinchi_shinseisha.currentIndex() <= 0:
            return ''
        data = self.combo_rinchi_shinseisha.currentData()
        if data not in (None, ''):
            return str(data).strip()
        return ''

    def _display_rinchi_table(self, data):
        records = [r for r in self._extract_records(data) if isinstance(r, dict)]
        city_name = self.combo_rinchi_city.currentData() or ''
        kubun = self.combo_rinchi_kubun.currentData() or ''
        mokuteki = self.combo_rinchi_mokuteki.currentData() or ''
        shozaichi = self.edit_rinchi_shozaichi.text().strip()

        if city_name:
            city_keys = {city_name, _API_CITY_MAP.get(city_name, city_name)}
            records = [
                r for r in records
                if str(r.get('所在市町村', '') or '') in city_keys
            ]
        if kubun:
            records = [
                r for r in records
                if str(r.get('開発区分', '') or '') == kubun
            ]
        if mokuteki:
            records = [
                r for r in records
                if str(r.get('開発目的', '') or '') == mokuteki
            ]

        shinseisha_vals = sorted(set(
            str(r.get('申請者_法人名', '') or '')
            for r in records
        ) - {'', 'NULL', 'None'})
        prev_shinseisha = self._current_rinchi_shinseisha_filter()
        self.combo_rinchi_shinseisha.blockSignals(True)
        self.combo_rinchi_shinseisha.clear()
        self.combo_rinchi_shinseisha.addItem('（全て）', '')
        for name in shinseisha_vals:
            self.combo_rinchi_shinseisha.addItem(name, name)
        if prev_shinseisha:
            idx = self.combo_rinchi_shinseisha.findData(prev_shinseisha)
            if idx < 0:
                idx = self.combo_rinchi_shinseisha.findText(prev_shinseisha)
            if idx >= 0:
                self.combo_rinchi_shinseisha.setCurrentIndex(idx)
        self.combo_rinchi_shinseisha.blockSignals(False)
        self.combo_rinchi_shinseisha.setEnabled(bool(shinseisha_vals))

        shinseisha = self._current_rinchi_shinseisha_filter()
        if shinseisha:
            records = [
                r for r in records
                if str(r.get('申請者_法人名', '') or '') == shinseisha
            ]
        if shozaichi:
            records = [
                r for r in records
                if shozaichi in str(r.get('所在地', '') or '')
            ]

        self._current_filtered_rinchi = list(records)
        total = len(records)
        self.tbl_rinchi.setRowCount(total)

        def _get(rec, *keys):
            for k in keys:
                v = rec.get(k)
                if v is not None and str(v) not in ('', 'NULL', 'None'):
                    return str(v)
            return ''

        for row_i, rec in enumerate(records):
            vals = [
                _get(rec, '開発区分'),
                _get(rec, '表示用_許可年月日'),
                _get(rec, '申請者_法人名'),
                _get(rec, '開発目的'),
                _get(rec, '表示用_開発目的詳細', '開発目的詳細'),
                _get(rec, '所在市町村'),
                _get(rec, '所在地'),
                _get(rec, '表示用_事業区域面積', '事業区域面積'),
                _get(rec, '表示用_事業区域内森林面積', '事業区域内森林面積'),
                _get(rec, '表示用_許可面積', '許可面積'),
                _get(rec, '表示用_着手年月日'),
                _get(rec, '表示用_完了予定年月日'),
                _get(rec, '表示用_施行実施状況', '施工実施状況'),
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(' ' + v)
                item.setData(Qt.UserRole, rec)
                self.tbl_rinchi.setItem(row_i, col, item)

        self.lbl_rinchi_count.setText(f'{total}件')
        return total

    def _auto_show_rinchi_layer(self, total):
        if total <= 0:
            if self.btn_rinchi_layer.isChecked():
                self._apply_rinchi_layer_filter()
                self._refresh_map_canvas()
            return
        if not self.btn_rinchi_layer.isChecked():
            self.btn_rinchi_layer.setChecked(True)
            return
        if self._rinchi_vector_layer_id:
            self._apply_rinchi_layer_filter()
            self._refresh_map_canvas()
            return
        self._on_rinchi_layer_toggled(True)

    # ------------------------------------------------------------------
    # レイヤー管理
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_sql_value(value):
        return str(value).replace("'", "''")

    def _rinchi_city_short(self, city_text):
        text = str(city_text or '').strip()
        if not text:
            return ''
        candidates = sorted({
            _API_CITY_MAP.get(api_city, api_city)
            for api_city in _CITY_CD.keys()
        }, key=len, reverse=True)
        for city in candidates:
            if text.startswith(city):
                return city
        return text

    def _get_rinchi_gpkg_path(self):
        home = QgsProject.instance().homePath()
        if not home:
            return None
        return os.path.join(home, 'fcloud_shizuoka', 'rinchi_kaihatsu.gpkg')

    def _invalidate_rinchi_layer_cache(self):
        self._remove_rinchi_vector_layer()
        gpkg = self._get_rinchi_gpkg_path()
        if gpkg and os.path.exists(gpkg):
            try:
                os.remove(gpkg)
            except OSError:
                pass

    def _remove_rinchi_vector_layer(self):
        self._clear_selection_highlights()
        if self._rinchi_vector_layer_id:
            layer = QgsProject.instance().mapLayer(self._rinchi_vector_layer_id)
            if layer and not sip.isdeleted(layer):
                QgsProject.instance().removeMapLayer(self._rinchi_vector_layer_id)
            self._rinchi_vector_layer_id = None
        self._rinchi_layer_features = []
        self._rinchi_tiles_pending = 0
        self._rinchi_tiles_received = 0
        self._rinchi_loading = False

    def _remove_rinchi_layers(self):
        self._remove_rinchi_vector_layer()
        self._remove_layers_by_name(self._RINCHI_LAYER_NAME, *self._RINCHI_OLD_LAYER_NAMES)
        root = QgsProject.instance().layerTreeRoot()
        while root.findGroup('林地開発'):
            grp = root.findGroup('林地開発')
            for child in grp.findLayers():
                QgsProject.instance().removeMapLayer(child.layerId())
            root.removeChildNode(grp)
        self.btn_rinchi_layer.blockSignals(True)
        self.btn_rinchi_layer.setChecked(False)
        self.btn_rinchi_layer.blockSignals(False)
        self._refresh_map_canvas()

    def _on_rinchi_layer_toggled(self, on):
        if not on:
            self._remove_rinchi_layers()
            return
        if self._current_raw_rinchi is None:
            self.btn_rinchi_layer.blockSignals(True)
            self.btn_rinchi_layer.setChecked(False)
            self.btn_rinchi_layer.blockSignals(False)
            return
        if self._rinchi_vector_layer_id:
            self._set_layer_visible(self._rinchi_vector_layer_id, True)
            self._apply_rinchi_layer_filter()
            self._refresh_map_canvas()
            return
        gpkg = self._get_rinchi_gpkg_path()
        if gpkg and os.path.exists(gpkg):
            self._load_rinchi_from_gpkg(gpkg)
        else:
            self._start_rinchi_mvt_fetch()
        self._refresh_map_canvas()

    def _load_rinchi_from_gpkg(self, gpkg_path):
        layer = QgsVectorLayer(
            f'{gpkg_path}|layername={self._RINCHI_GPKG_LAYER_NAME}',
            self._RINCHI_LAYER_NAME,
            'ogr',
        )
        if not layer.isValid():
            self._start_rinchi_mvt_fetch()
            return
        self._apply_rinchi_style(layer)
        self._add_layer_above_gpkg(layer, visible=self.btn_rinchi_layer.isChecked())
        self._rinchi_vector_layer_id = layer.id()
        self._apply_rinchi_layer_filter()

    def _start_rinchi_mvt_fetch(self):
        from .mvt_loader import _lon_to_tile_x, _lat_to_tile_y

        zoom = self._RINCHI_MVT_ZOOM
        min_lon, min_lat, max_lon, max_lat = _SHIZUOKA_BBOX
        x0 = _lon_to_tile_x(min_lon, zoom)
        x1 = _lon_to_tile_x(max_lon, zoom)
        y0 = _lat_to_tile_y(max_lat, zoom)
        y1 = _lat_to_tile_y(min_lat, zoom)
        tiles = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

        self._rinchi_layer_features = []
        self._rinchi_tiles_pending = len(tiles) * len(self._RINCHI_TILE_SOURCES)
        self._rinchi_tiles_received = 0
        self._rinchi_loading = True
        self.lbl_rinchi_count.setText(f'タイル取得中... (0/{self._rinchi_tiles_pending})')
        self.btn_rinchi_layer.setEnabled(False)

        for url_template, kubun in self._RINCHI_TILE_SOURCES:
            for tx, ty in tiles:
                url = (url_template.replace('{z}', str(zoom))
                                   .replace('{x}', str(tx))
                                   .replace('{y}', str(ty)))
                req = QNetworkRequest(QUrl(url))
                reply = QgsNetworkAccessManager.instance().get(req)
                self._pending_replies.append(reply)
                reply.finished.connect(
                    lambda r=reply, x=tx, y=ty, k=kubun: self._on_rinchi_mvt_tile(r, x, y, k))

    def _on_rinchi_mvt_tile(self, reply, tile_x, tile_y, kubun):
        from .mvt_loader import parse_tile

        if reply.error() == QNetworkReply.NoError:
            raw = bytes(reply.readAll())
            try:
                feats = parse_tile(raw, tile_x, tile_y, self._RINCHI_MVT_ZOOM)
                for feat in feats:
                    feat['開発区分'] = kubun
                self._rinchi_layer_features.extend(feats)
            except Exception as e:
                print(f'[fcloud] rinchi MVT parse error ({tile_x},{tile_y},{kubun}): {e}')
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()
        self._rinchi_tiles_received += 1
        self.lbl_rinchi_count.setText(
            f'タイル取得中... ({self._rinchi_tiles_received}/{self._rinchi_tiles_pending})')
        if self._rinchi_tiles_received >= self._rinchi_tiles_pending:
            self._build_rinchi_vector_layer()

    def _build_rinchi_vector_layer(self):
        if not self._rinchi_layer_features:
            self._rinchi_loading = False
            self.lbl_rinchi_count.setText('取得データなし')
            self.btn_rinchi_layer.setEnabled(True)
            return

        layer = QgsVectorLayer('Polygon?crs=EPSG:4326', self._RINCHI_LAYER_NAME, 'memory')
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField('kubun', QVariant.String),
            QgsField('permit_no', QVariant.String),
            QgsField('applicant', QVariant.String),
            QgsField('purpose', QVariant.String),
            QgsField('purpose_detail', QVariant.String),
            QgsField('city', QVariant.String),
            QgsField('city_short', QVariant.String),
            QgsField('address', QVariant.String),
            QgsField('norin', QVariant.String),
            QgsField('permit_date', QVariant.String),
            QgsField('project_ha', QVariant.Double),
            QgsField('forest_ha', QVariant.Double),
            QgsField('permit_ha', QVariant.Double),
        ])
        layer.updateFields()

        def _pick(attrs, changed_key, initial_key):
            return str(attrs.get(changed_key) or attrs.get(initial_key) or '')

        feats_to_add = []
        for attrs in self._rinchi_layer_features:
            geom = QgsGeometry.fromWkt(attrs.get('geometry', ''))
            if not geom or geom.isEmpty():
                continue
            city = _pick(attrs, '所在場所（変更最終）_市町村', '所在場所（当初）_市町村')
            address = _pick(attrs, '所在場所（変更最終）_住所', '所在場所（当初）_住所')
            purpose = _pick(attrs, '開発行為の目的名称_変更最終（目的）', '開発行為の目的名称_当初（目的）')
            purpose_detail = _pick(
                attrs,
                '開発行為の目的名称_変更最終（詳細）',
                '開発行為の目的名称_当初（詳細）',
            )

            qf = QgsFeature()
            qf.setGeometry(geom)
            qf.setAttributes([
                str(attrs.get('開発区分', '') or ''),
                str(attrs.get('許可No.', '') or ''),
                str(attrs.get('申請者_法人名等', '') or ''),
                purpose,
                purpose_detail,
                city,
                self._rinchi_city_short(city),
                address,
                str(attrs.get('農林事務所', '') or ''),
                str(attrs.get('許可日', '') or ''),
                float(attrs.get('全体面積（ha）', 0) or 0),
                float(attrs.get('森林面積（ha）', 0) or 0),
                float(attrs.get('許可面積（ha）', 0) or 0),
            ])
            feats_to_add.append(qf)

        pr.addFeatures(feats_to_add)
        layer.updateExtents()
        self._apply_rinchi_style(layer)

        gpkg = self._get_rinchi_gpkg_path()
        loaded = False
        if gpkg:
            os.makedirs(os.path.dirname(gpkg), exist_ok=True)
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = 'GPKG'
            opts.fileEncoding = 'UTF-8'
            opts.layerName = self._RINCHI_GPKG_LAYER_NAME
            err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
                layer, gpkg, QgsProject.instance().transformContext(), opts)[:2]
            if err:
                print(f'[fcloud] rinchi GPKG save error: {msg}')
            elif os.path.exists(gpkg):
                self._load_rinchi_from_gpkg(gpkg)
                loaded = bool(self._rinchi_vector_layer_id)

        if not loaded:
            self._add_layer_above_gpkg(layer, visible=self.btn_rinchi_layer.isChecked())
            self._rinchi_vector_layer_id = layer.id()
            self._apply_rinchi_layer_filter()

        self._rinchi_loading = False
        self.btn_rinchi_layer.setEnabled(True)
        self.lbl_rinchi_count.setText(f'{self.tbl_rinchi.rowCount()}件')
        self._refresh_map_canvas()

    def _apply_rinchi_style(self, layer):
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsFillSymbol

        color_map = {
            '許可': (220, 114, 42),
            '連絡調整': (41, 132, 178),
        }
        cats = []
        for kubun in ('許可', '連絡調整'):
            r, g, b = color_map.get(kubun, (90, 140, 90))
            sym = QgsFillSymbol.createSimple({
                'color': f'{r},{g},{b},75',
                'outline_color': f'{max(0, r - 40)},{max(0, g - 40)},{max(0, b - 40)},200',
                'outline_width': '0.35',
            })
            cats.append(QgsRendererCategory(kubun, sym, kubun))

        extra_vals = sorted(str(v) for v in layer.uniqueValues(layer.fields().indexOf('kubun'))
                            if v is not None and str(v) not in ('', '許可', '連絡調整'))
        hue = 0.11
        for val in extra_vals:
            hue = (hue + 0.618033988749895) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.88)
            ri, gi, bi = int(r * 255), int(g * 255), int(b * 255)
            sym = QgsFillSymbol.createSimple({
                'color': f'{ri},{gi},{bi},75',
                'outline_color': f'{max(0, ri - 40)},{max(0, gi - 40)},{max(0, bi - 40)},200',
                'outline_width': '0.35',
            })
            cats.append(QgsRendererCategory(val, sym, val))

        layer.setRenderer(QgsCategorizedSymbolRenderer('kubun', cats))

    def _apply_rinchi_layer_filter(self):
        if not self._rinchi_vector_layer_id:
            return
        layer = QgsProject.instance().mapLayer(self._rinchi_vector_layer_id)
        if not layer or sip.isdeleted(layer):
            return

        city_name = (self.combo_rinchi_city.currentData() or '').strip()
        kubun = (self.combo_rinchi_kubun.currentData() or '').strip()
        mokuteki = (self.combo_rinchi_mokuteki.currentData() or '').strip()
        shinseisha = self._current_rinchi_shinseisha_filter()
        shozaichi = self.edit_rinchi_shozaichi.text().strip()

        clauses = []
        if city_name:
            clauses.append(f"\"city_short\" = '{self._escape_sql_value(city_name)}'")
        if kubun:
            clauses.append(f"\"kubun\" = '{self._escape_sql_value(kubun)}'")
        if mokuteki:
            clauses.append(f"\"purpose\" = '{self._escape_sql_value(mokuteki)}'")
        if shinseisha:
            clauses.append(
                f"\"applicant\" LIKE '%{self._escape_sql_value(shinseisha)}%'")
        if shozaichi:
            kw = self._escape_sql_value(shozaichi)
            clauses.append(
                f"(\"city\" LIKE '%{kw}%' OR \"address\" LIKE '%{kw}%')"
            )

        layer.setSubsetString(' AND '.join(clauses))

    def _feature_matches_rinchi_record(self, feat, rec):
        kubun = str(rec.get('開発区分', '') or '').strip()
        applicant = str(rec.get('申請者_法人名', '') or '').strip()
        mokuteki = str(rec.get('開発目的', '') or '').strip()
        city = self._rinchi_city_short(str(rec.get('所在市町村', '') or '').strip())
        address = str(rec.get('所在地', '') or '').strip()

        if kubun and str(feat['kubun'] or '').strip() != kubun:
            return False
        feat_applicant = str(feat['applicant'] or '').strip()
        if applicant and feat_applicant:
            if applicant != feat_applicant and applicant not in feat_applicant and feat_applicant not in applicant:
                return False
        if mokuteki and str(feat['purpose'] or '').strip() != mokuteki:
            return False
        if city and str(feat['city_short'] or '').strip() != city:
            return False
        if address:
            feat_city = str(feat['city'] or '').strip()
            feat_addr = str(feat['address'] or '').strip()
            if address not in feat_addr and address not in feat_city:
                return False
        return True

    # ------------------------------------------------------------------
    # 行選択 → ズーム
    # ------------------------------------------------------------------

    def _on_rinchi_selected(self):
        self._clear_selection_highlights()
        rows = self.tbl_rinchi.selectionModel().selectedRows()
        if not rows:
            self._clear_cloud_record_info()
            return
        item = self.tbl_rinchi.item(rows[0].row(), 0)
        if not item:
            self._clear_cloud_record_info()
            return
        row = rows[0].row()
        rec = item.data(Qt.UserRole)
        if not isinstance(rec, dict):
            self._clear_cloud_record_info()
            return
        self._show_cloud_table_row_info('林地開発', self.tbl_rinchi, row)

        if not self.btn_rinchi_layer.isChecked():
            return

        layer = QgsProject.instance().mapLayer(self._rinchi_vector_layer_id)
        if layer and not sip.isdeleted(layer):
            matched = False
            for feat in layer.getFeatures(QgsFeatureRequest()):
                if not self._feature_matches_rinchi_record(feat, rec):
                    continue
                geom = feat.geometry()
                if geom and not geom.isEmpty():
                    self._add_selection_highlight(geom, layer.crs())
                    matched = True
            if matched and self._zoom_to_selection_highlights(margin_ratio=0.30):
                return

        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        crs_4326 = QgsCoordinateReferenceSystem('EPSG:4326')
        show_feature = self.btn_rinchi_layer.isChecked()

        def _apply_bbox(bbox_4326, margin=0.10):
            b = bbox_4326
            if crs_4326 != dst_crs:
                tr = QgsCoordinateTransform(crs_4326, dst_crs, QgsProject.instance())
                b = tr.transformBoundingBox(b)
            b.grow(max(b.width(), b.height()) * margin)
            canvas.setExtent(b)
            canvas.refresh()

        def _feature_bbox(geom, src_crs):
            if show_feature:
                return self._add_selection_highlight(geom, src_crs)
            g = QgsGeometry(geom)
            if src_crs != dst_crs:
                tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                g.transform(tr)
            return g.boundingBox()

        city_raw = str(rec.get('所在市町村', '') or rec.get('市町村', '') or '')
        gpkg_city = _API_CITY_MAP.get(city_raw, city_raw)
        shinseisha = str(rec.get('申請者_法人名', '') or '')
        kubun = str(rec.get('開発区分', '') or '')

        connected = self._connected_layer
        city_bbox_4326 = None
        if connected and not sip.isdeleted(connected) and gpkg_city:
            src_crs = connected.crs()
            freq = QgsFeatureRequest()
            freq.setFilterExpression(f'"市町村名称" = \'{gpkg_city}\'')
            cb = None
            for feat in connected.getFeatures(freq):
                g = feat.geometry()
                if g and not g.isEmpty():
                    b = g.boundingBox()
                    if cb is None:
                        cb = b
                    else:
                        cb.combineExtentWith(b)
            if cb and not cb.isEmpty():
                if src_crs != crs_4326:
                    tr = QgsCoordinateTransform(src_crs, crs_4326, QgsProject.instance())
                    city_bbox_4326 = tr.transformBoundingBox(cb)
                else:
                    city_bbox_4326 = cb

        if city_bbox_4326 and shinseisha:
            if '連絡調整' in kubun:
                mvt_url = ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
                           'MAGIS.RINCHI_KAIHATSU_RENRAKU_CHOUSEI/{z}/{x}/{y}.pbf')
            else:
                mvt_url = ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
                           'MAGIS.RINCHI_KAIHATSU_KYOKA/{z}/{x}/{y}.pbf')

            from .mvt_loader import _lon_to_tile_x, _lat_to_tile_y, parse_tile
            import urllib.request as _ur

            zoom = self._RINCHI_MVT_ZOOM
            x0 = _lon_to_tile_x(city_bbox_4326.xMinimum(), zoom)
            x1 = _lon_to_tile_x(city_bbox_4326.xMaximum(), zoom)
            y0 = _lat_to_tile_y(city_bbox_4326.yMaximum(), zoom)
            y1 = _lat_to_tile_y(city_bbox_4326.yMinimum(), zoom)

            match_bbox = None
            for tx in range(x0, x1 + 1):
                for ty in range(y0, y1 + 1):
                    url = mvt_url.replace('{z}', str(zoom)) \
                                 .replace('{x}', str(tx)) \
                                 .replace('{y}', str(ty))
                    try:
                        raw = _ur.urlopen(url, timeout=5).read()
                        for feat in parse_tile(raw, tx, ty, zoom):
                            mvt_name = str(feat.get('申請者_法人名等', '') or '')
                            if mvt_name and (mvt_name == shinseisha
                                             or shinseisha in mvt_name
                                             or mvt_name in shinseisha):
                                geom = QgsGeometry.fromWkt(feat['geometry'])
                                if geom and not geom.isEmpty():
                                    b = _feature_bbox(geom, crs_4326)
                                    if match_bbox is None:
                                        match_bbox = b
                                    else:
                                        match_bbox.combineExtentWith(b)
                    except Exception:
                        pass

            if match_bbox and not match_bbox.isEmpty():
                match_bbox.grow(max(match_bbox.width(), match_bbox.height()) * 0.30)
                canvas.setExtent(match_bbox)
                canvas.refresh()
                return

        if city_bbox_4326:
            _apply_bbox(city_bbox_4326, margin=0.10)
            return

        x = rec.get('hilight_point_x')
        y = rec.get('hilight_point_y')
        if x is not None and y is not None:
            try:
                tr = QgsCoordinateTransform(crs_4326, dst_crs, QgsProject.instance())
                pt = tr.transform(QgsPointXY(float(x), float(y)))
                buf = canvas.mapUnitsPerPixel() * 300
                canvas.setExtent(QgsRectangle(
                    pt.x() - buf, pt.y() - buf, pt.x() + buf, pt.y() + buf))
                canvas.refresh()
            except Exception:
                pass
