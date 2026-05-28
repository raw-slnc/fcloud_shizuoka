# -*- coding: utf-8 -*-
import os
import colorsys
import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidgetItem, QHeaderView,
)
from qgis.PyQt.QtCore import Qt, QUrl, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsField, QgsVectorFileWriter, QgsFeatureRequest,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsNetworkAccessManager,
)

from .constants import _API_BASE, _API_CITY_MAP


class KeikakuMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_keikaku(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        self.btn_keikaku_load = QPushButton('読み込み')
        row.addWidget(self.btn_keikaku_load)
        row.addStretch(1)

        self.btn_keikaku_layer = QPushButton('計画箇所レイヤー')
        self.btn_keikaku_layer.setCheckable(True)
        self.btn_keikaku_layer.setToolTip('経営計画作成箇所を市町村別色分けで表示/非表示')
        row.addWidget(self.btn_keikaku_layer)
        v.addLayout(row)

        self.tbl_keikaku = self._make_table([
            '認定権者', '面積(ha)', '造林面積', '主伐面積', '主伐材積',
            '間伐面積', '間伐材積', '保育面積',
        ])
        hdr = self.tbl_keikaku.horizontalHeader()
        for col in range(1, 8):
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            self.tbl_keikaku.setColumnWidth(col, 75)
        v.addWidget(self.tbl_keikaku, 1)

        bottom = QHBoxLayout()
        self.lbl_keikaku_count = QLabel('')
        self.lbl_keikaku_count.setStyleSheet('color: gray; font-size: 10px;')
        bottom.addWidget(self.lbl_keikaku_count, 1)
        v.addLayout(bottom)

        self.btn_keikaku_load.clicked.connect(self._load_keikaku)
        self.btn_keikaku_layer.toggled.connect(self._on_keikaku_layer_toggled)
        self.tbl_keikaku.itemSelectionChanged.connect(self._on_keikaku_selected)
        return w

    # ------------------------------------------------------------------
    # 読み込み・表示
    # ------------------------------------------------------------------

    def _load_keikaku(self, force=False):
        self.tbl_keikaku.setRowCount(0)
        self.lbl_keikaku_count.setText('読み込み中...')
        self.btn_keikaku_load.setEnabled(False)

        cache_key = '経営計画/all'
        if not force:
            db = self._get_db('経営計画')
            if db is not None:
                cached, ts = db.get(cache_key)
                if cached is not None:
                    self.btn_keikaku_load.setEnabled(True)
                    self.lbl_cache_ts.setText(f'取得日時: {ts}')
                    for rec in self._extract_records(cached):
                        if isinstance(rec, dict):
                            cd = rec.get('市町村cd')
                            name = rec.get('認定権者', '')
                            if cd is not None and name:
                                self._keikaku_cd_to_name[int(cd)] = str(name)
                    self._display_keikaku_table(cached)
                    self._auto_build_keikaku_layer()
                    return

        self._post_api(
            f'{_API_BASE}/advanced-search/経営計画検索',
            {},
            lambda data: self._on_keikaku_result(data, cache_key),
        )

    def _on_keikaku_result(self, data, cache_key):
        self.btn_keikaku_load.setEnabled(True)
        if data is None:
            self.lbl_keikaku_count.setText('取得失敗')
            return
        self._current_raw_keikaku = data
        db = self._get_db('経営計画')
        if db is not None:
            ts = db.put(cache_key, data)
            self.lbl_cache_ts.setText(f'取得日時: {ts}')
        records = self._extract_records(data)
        for rec in records:
            if isinstance(rec, dict):
                cd = rec.get('市町村cd')
                name = rec.get('認定権者', '')
                if cd is not None and name:
                    self._keikaku_cd_to_name[int(cd)] = str(name)
        self._display_keikaku_table(data)
        self._auto_build_keikaku_layer()

    def _display_keikaku_table(self, data):
        self._current_raw_keikaku = data
        records = self._extract_records(data)
        self.tbl_keikaku.setRowCount(len(records))
        for row_i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            vals = [
                str(rec.get('認定権者', '') or ''),
                str(rec.get('表示用_面積',     rec.get('面積',     '')) or ''),
                str(rec.get('表示用_造林面積', rec.get('造林面積', '')) or ''),
                str(rec.get('表示用_主伐面積', rec.get('主伐面積', '')) or ''),
                str(rec.get('表示用_主伐材積', rec.get('主伐材積', '')) or ''),
                str(rec.get('表示用_間伐面積', rec.get('間伐面積', '')) or ''),
                str(rec.get('表示用_間伐材積', rec.get('間伐材積', '')) or ''),
                str(rec.get('表示用_保育面積', rec.get('保育面積', '')) or ''),
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(' ' + v)
                item.setData(Qt.UserRole, rec)
                self.tbl_keikaku.setItem(row_i, col, item)
        self.lbl_keikaku_count.setText(f'{len(records)}件')

    # ------------------------------------------------------------------
    # レイヤー管理
    # ------------------------------------------------------------------

    def _get_keikaku_gpkg_path(self):
        home = QgsProject.instance().homePath()
        if not home:
            return None
        return os.path.join(home, 'fcloud_shizuoka', 'keikaku_chikara.gpkg')

    def _on_keikaku_layer_toggled(self, on):
        if not on:
            self._remove_keikaku_vector_layer()
            return
        if self._current_raw_keikaku is None:
            self.btn_keikaku_layer.blockSignals(True)
            self.btn_keikaku_layer.setChecked(False)
            self.btn_keikaku_layer.blockSignals(False)
            return
        if self._keikaku_vector_layer_id:
            self._set_layer_visible(self._keikaku_vector_layer_id, True)
            self._refresh_map_canvas()
            return
        gpkg = self._get_keikaku_gpkg_path()
        if gpkg and os.path.exists(gpkg):
            self._load_keikaku_from_gpkg(gpkg)
        elif self._keikaku_cd_to_name:
            self._start_keikaku_mvt_fetch()
        else:
            self._load_keikaku()
        self._refresh_map_canvas()

    def _load_keikaku_from_gpkg(self, gpkg_path):
        layer = QgsVectorLayer(f'{gpkg_path}|layername=経営計画作成箇所',
                               'fcloud_経営計画作成箇所', 'ogr')
        if not layer.isValid():
            self._start_keikaku_mvt_fetch()
            return
        self._apply_keikaku_style(layer)
        visible = (self.btn_keikaku_layer.isChecked()
                   and self.cloud_tab.currentIndex() == 3)
        self._add_layer_above_gpkg(layer, visible=visible)
        self._keikaku_vector_layer_id = layer.id()

    def _start_keikaku_mvt_fetch(self):
        from .mvt_loader import shizuoka_tiles
        self._keikaku_layer_features = []
        tiles = shizuoka_tiles(zoom=9)
        self._keikaku_tiles_pending = len(tiles)
        self._keikaku_tiles_received = 0
        self.lbl_keikaku_count.setText(f'タイル取得中... (0/{self._keikaku_tiles_pending})')
        self.btn_keikaku_layer.setEnabled(False)
        mvt_url = ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
                   'SHINRIN_KEIEI_KEIKAKU_SAKUSEI_KASHO/9/{x}/{y}.pbf')
        for tx, ty in tiles:
            url = mvt_url.replace('{x}', str(tx)).replace('{y}', str(ty))
            req = QNetworkRequest(QUrl(url))
            reply = QgsNetworkAccessManager.instance().get(req)
            self._pending_replies.append(reply)
            reply.finished.connect(
                lambda r=reply, x=tx, y=ty: self._on_keikaku_mvt_tile(r, x, y))

    def _on_keikaku_mvt_tile(self, reply, tile_x, tile_y):
        from .mvt_loader import parse_tile
        if reply.error() == QNetworkReply.NoError:
            raw = bytes(reply.readAll())
            try:
                feats = parse_tile(raw, tile_x, tile_y, 9)
                self._keikaku_layer_features.extend(feats)
            except Exception as e:
                print(f'[fcloud] keikaku MVT parse error ({tile_x},{tile_y}): {e}')
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()
        self._keikaku_tiles_received += 1
        self.lbl_keikaku_count.setText(
            f'タイル取得中... ({self._keikaku_tiles_received}/{self._keikaku_tiles_pending})')
        if self._keikaku_tiles_received >= self._keikaku_tiles_pending:
            self._build_keikaku_vector_layer()

    def _build_keikaku_vector_layer(self):
        layer = QgsVectorLayer('Polygon?crs=EPSG:4326', 'fcloud_経営計画作成箇所', 'memory')
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField('市町村cd',  QVariant.Int),
            QgsField('市町村名',  QVariant.String),
            QgsField('THE_FID',   QVariant.LongLong),
        ])
        layer.updateFields()

        feats_to_add = []
        for f in self._keikaku_layer_features:
            geom = QgsGeometry.fromWkt(f['geometry'])
            if geom is None or geom.isEmpty():
                continue
            cd = int(f.get('OFFICE', 0))
            name = self._keikaku_cd_to_name.get(cd, '')
            qf = QgsFeature()
            qf.setGeometry(geom)
            qf.setAttributes([cd, name, int(f.get('THE_FID', 0))])
            feats_to_add.append(qf)

        pr.addFeatures(feats_to_add)
        layer.updateExtents()
        self._apply_keikaku_style(layer)

        gpkg = self._get_keikaku_gpkg_path()
        if gpkg:
            os.makedirs(os.path.dirname(gpkg), exist_ok=True)
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = 'GPKG'
            opts.fileEncoding = 'UTF-8'
            opts.layerName = '経営計画作成箇所'
            err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
                layer, gpkg, QgsProject.instance().transformContext(), opts)[:2]
            if err:
                print(f'[fcloud] keikaku GPKG save error: {msg}')

        visible = (self.btn_keikaku_layer.isChecked()
                   and self.cloud_tab.currentIndex() == 3)
        self._add_layer_above_gpkg(layer, visible=visible)
        self._keikaku_vector_layer_id = layer.id()
        self.btn_keikaku_layer.setEnabled(True)
        total = self.tbl_keikaku.rowCount()
        self.lbl_keikaku_count.setText(f'{total}件' if total else '')

    def _apply_keikaku_style(self, layer):
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsFillSymbol
        idx = layer.fields().indexOf('市町村cd')
        unique_cds = sorted(int(v) for v in layer.uniqueValues(idx)
                            if v is not None)
        cats = []
        phi = 0.618033988749895
        hue = 0.05
        for cd in unique_cds:
            hue = (hue + phi) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.90)
            ri, gi, bi = int(r * 255), int(g * 255), int(b * 255)
            label = self._keikaku_cd_to_name.get(cd, str(cd))
            sym = QgsFillSymbol.createSimple({
                'color':         f'{ri},{gi},{bi},150',
                'outline_color': f'{max(0,ri-50)},{max(0,gi-50)},{max(0,bi-50)},200',
                'outline_width': '0.3',
            })
            cats.append(QgsRendererCategory(cd, sym, label))
        layer.setRenderer(QgsCategorizedSymbolRenderer('市町村cd', cats))

    def _remove_keikaku_vector_layer(self):
        self._clear_selection_highlights()
        if self._keikaku_vector_layer_id:
            layer = QgsProject.instance().mapLayer(self._keikaku_vector_layer_id)
            if layer and not sip.isdeleted(layer):
                QgsProject.instance().removeMapLayer(self._keikaku_vector_layer_id)
            self._keikaku_vector_layer_id = None
        self._keikaku_layer_features = []
        self._refresh_map_canvas()

    def _auto_build_keikaku_layer(self):
        if self._keikaku_vector_layer_id:
            vl = QgsProject.instance().mapLayer(self._keikaku_vector_layer_id)
            if vl and not sip.isdeleted(vl):
                return
            self._keikaku_vector_layer_id = None
        gpkg = self._get_keikaku_gpkg_path()
        if gpkg and os.path.exists(gpkg):
            self._load_keikaku_from_gpkg(gpkg)
        elif self._keikaku_cd_to_name:
            self._start_keikaku_mvt_fetch()

    # ------------------------------------------------------------------
    # 行選択 → ズーム
    # ------------------------------------------------------------------

    def _on_keikaku_selected(self):
        self._clear_selection_highlights()
        rows = self.tbl_keikaku.selectionModel().selectedRows()
        if not rows:
            self._clear_cloud_record_info()
            return
        item = self.tbl_keikaku.item(rows[0].row(), 0)
        if not item:
            self._clear_cloud_record_info()
            return
        row = rows[0].row()
        rec = item.data(Qt.UserRole)
        if not isinstance(rec, dict):
            self._clear_cloud_record_info()
            return
        self._show_cloud_table_row_info('経営計画', self.tbl_keikaku, row)

        if not self.btn_keikaku_layer.isChecked():
            return

        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        cd = rec.get('市町村cd')
        show_feature = self.btn_keikaku_layer.isChecked()

        def _req_with_geom(expr=None):
            req = QgsFeatureRequest()
            if expr:
                req.setFilterExpression(expr)
            return req

        def _bbox_from_layer(layer, expr):
            bbox = None
            for feat in layer.getFeatures(_req_with_geom(expr)):
                g = feat.geometry()
                if g and not g.isEmpty():
                    if show_feature:
                        b = self._add_selection_highlight(g, layer.crs())
                    else:
                        b = g.boundingBox()
                        if layer.crs() != dst_crs:
                            tr = QgsCoordinateTransform(layer.crs(), dst_crs, QgsProject.instance())
                            b = tr.transformBoundingBox(b)
                    if bbox is None:
                        bbox = b
                    else:
                        bbox.combineExtentWith(b)
            if bbox and not bbox.isEmpty():
                bbox.grow(max(bbox.width(), bbox.height()) * 0.10)
                canvas.setExtent(bbox)
                canvas.refresh()
                return True
            return False

        if cd is not None and self._keikaku_vector_layer_id:
            vl = QgsProject.instance().mapLayer(self._keikaku_vector_layer_id)
            if vl and not sip.isdeleted(vl):
                if _bbox_from_layer(vl, f'"市町村cd" = {int(cd)}'):
                    return

        if cd is not None:
            gpkg = self._get_keikaku_gpkg_path()
            if gpkg and os.path.exists(gpkg):
                temp = QgsVectorLayer(f'{gpkg}|layername=経営計画作成箇所', 'temp', 'ogr')
                if temp.isValid():
                    if _bbox_from_layer(temp, f'"市町村cd" = {int(cd)}'):
                        return

        city_name = str(rec.get('認定権者', '') or '')
        gpkg_city = _API_CITY_MAP.get(city_name, city_name)
        layer = self._connected_layer
        if not layer or sip.isdeleted(layer) or not gpkg_city:
            return
        _bbox_from_layer(layer, f'"市町村名称" = \'{gpkg_city}\'')


