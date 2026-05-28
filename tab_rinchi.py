# -*- coding: utf-8 -*-
import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLabel, QLineEdit, QPushButton,
    QTableWidgetItem, QHeaderView,
)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import (
    QgsProject, QgsFeatureRequest, QgsGeometry,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsPointXY, QgsRectangle, QgsNetworkAccessManager,
)

from .constants import _API_BASE, _API_CITY_MAP, _RINCHI_MOKUTEKI


class RinchiMixin:

    _RINCHI_LAYERS = [
        ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
         'MAGIS.RINCHI_KAIHATSU_KYOKA/{z}/{x}/{y}.pbf',            '林地開発_許可'),
        ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
         'MAGIS.RINCHI_KAIHATSU_RENRAKU_CHOUSEI/{z}/{x}/{y}.pbf',  '林地開発_連絡調整'),
    ]

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
        self.edit_rinchi_shinseisha = QLineEdit()
        self.edit_rinchi_shinseisha.setPlaceholderText('申請者名')
        row1.addWidget(self.edit_rinchi_shinseisha, 2)
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
        self.btn_rinchi_layer.setToolTip('林地開発（許可・連絡調整）のMVTレイヤーを追加/除去')
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
        self.tbl_rinchi.itemSelectionChanged.connect(self._on_rinchi_selected)
        self.btn_rinchi_layer.toggled.connect(self._on_rinchi_layer_toggled)
        return w

    # ------------------------------------------------------------------
    # 検索・表示
    # ------------------------------------------------------------------

    def _search_rinchi(self, force=False):
        self.tbl_rinchi.setRowCount(0)
        self.lbl_rinchi_count.setText('検索中...')
        self.btn_rinchi_search.setEnabled(False)

        city_name  = self.combo_rinchi_city.currentData() or ''
        kubun      = self.combo_rinchi_kubun.currentData() or ''
        mokuteki   = self.combo_rinchi_mokuteki.currentData() or ''
        shinseisha = self.edit_rinchi_shinseisha.text().strip()
        shozaichi  = self.edit_rinchi_shozaichi.text().strip()

        api_city_name = _API_CITY_MAP.get(city_name, city_name)

        params = {}
        if api_city_name:
            params['所在市町村'] = api_city_name
        if kubun:
            params['開発区分'] = kubun
        if mokuteki:
            params['開発目的'] = mokuteki

        self._current_rinchi_cache_key = (
            f'林地開発/市={city_name}&区分={kubun}&目的={mokuteki}')

        if not force:
            db = self._get_db('林地開発')
            if db is not None:
                cached, ts = db.get(self._current_rinchi_cache_key)
                if cached is not None:
                    self.btn_rinchi_search.setEnabled(True)
                    self._current_raw_rinchi = cached
                    self.lbl_cache_ts.setText(f'取得日時: {ts}')
                    self._display_rinchi_table(cached, shinseisha, shozaichi)
                    return

        self._post_api(
            f'{_API_BASE}/advanced-search/林地開発検索',
            params,
            lambda data: self._on_rinchi_result(data, shinseisha, shozaichi),
        )

    def _on_rinchi_result(self, data, shinseisha='', shozaichi=''):
        self.btn_rinchi_search.setEnabled(True)
        if data is None:
            self.lbl_rinchi_count.setText('取得失敗')
            return
        self._current_raw_rinchi = data
        self.lbl_cache_ts.setText('取得日時: 未保存')
        self._display_rinchi_table(data, shinseisha, shozaichi)

    def _display_rinchi_table(self, data, shinseisha='', shozaichi=''):
        records = self._extract_records(data)

        if shinseisha:
            records = [r for r in records if isinstance(r, dict)
                       and shinseisha in str(r.get('申請者_法人名', '') or '')]
        if shozaichi:
            records = [r for r in records if isinstance(r, dict)
                       and shozaichi in str(r.get('所在地', '') or '')]

        total = len(records)
        self.tbl_rinchi.setRowCount(total)

        def _get(rec, *keys):
            for k in keys:
                v = rec.get(k)
                if v is not None and str(v) not in ('', 'NULL', 'None'):
                    return str(v)
            return ''

        for row_i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
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

    # ------------------------------------------------------------------
    # レイヤー管理
    # ------------------------------------------------------------------

    def _remove_rinchi_layers(self):
        for _, name in self._RINCHI_LAYERS:
            self._toggle_mvt_layer('', name, False)
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
            self._clear_selection_highlights()
            self._remove_rinchi_layers()
            return
        if self._current_raw_rinchi is None:
            self.btn_rinchi_layer.blockSignals(True)
            self.btn_rinchi_layer.setChecked(False)
            self.btn_rinchi_layer.blockSignals(False)
            return
        for _, name in self._RINCHI_LAYERS:
            self._toggle_mvt_layer('', name, False)
        for url, name in self._RINCHI_LAYERS:
            self._toggle_mvt_layer(url, name, True)
        self._refresh_map_canvas()

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

        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        crs_4326 = QgsCoordinateReferenceSystem('EPSG:4326')
        show_feature = self.btn_rinchi_layer.isChecked()

        city_raw  = str(rec.get('所在市町村', '') or rec.get('市町村', '') or '')
        gpkg_city = _API_CITY_MAP.get(city_raw, city_raw)
        shinseisha = str(rec.get('申請者_法人名', '') or '')
        kubun      = str(rec.get('開発区分', '') or '')

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

        layer = self._connected_layer
        city_bbox_4326 = None
        if layer and not sip.isdeleted(layer) and gpkg_city:
            src_crs = layer.crs()
            freq = QgsFeatureRequest()
            freq.setFilterExpression(f'"市町村名称" = \'{gpkg_city}\'')
            cb = None
            for feat in layer.getFeatures(freq):
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
            zoom = 10
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
                        for f in parse_tile(raw, tx, ty, zoom):
                            mvt_name = str(f.get('申請者_法人名等', '') or '')
                            if mvt_name and (mvt_name == shinseisha
                                            or shinseisha in mvt_name
                                            or mvt_name in shinseisha):
                                geom = QgsGeometry.fromWkt(f['geometry'])
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
