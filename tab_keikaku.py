# -*- coding: utf-8 -*-
import os
import colorsys
import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidgetItem, QHeaderView, QComboBox,
)
from qgis.PyQt.QtCore import Qt, QUrl, QVariant
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsField, QgsVectorFileWriter, QgsFeatureRequest,
    QgsCoordinateTransform, QgsNetworkAccessManager,
)

from .constants import (
    _API_BASE, _API_CITY_MAP,
    _CD_CITY, _SHIZUOKA_BBOX, _KEIKAKU_MVT_ZOOM,
)


class KeikakuMixin:

    def _keikaku_layer_requested_on(self):
        return self._keikaku_layer_requested is not False

    def _has_connected_keikaku_source(self):
        return (
            self._connected_layer is not None
            and not sip.isdeleted(self._connected_layer)
            and self._connected_layer.isValid()
        )

    def _ensure_keikaku_layer_loaded(self):
        if self._keikaku_vector_layer_id or not self.isVisible():
            return False
        if not self._has_connected_keikaku_source():
            return False
        if not self._keikaku_layer_requested_on():
            return False

        gpkg = self._get_keikaku_gpkg_path()
        if gpkg and os.path.exists(gpkg):
            self._load_keikaku_from_gpkg(gpkg)
            return bool(self._keikaku_vector_layer_id)

        if not self._keikaku_loading:
            self._load_keikaku()
        return False

    def _should_show_keikaku_layer(self):
        return (
            self._keikaku_layer_requested_on()
            and self.cloud_tab.currentIndex() == 3
            and self.isVisible()
        )

    def _sync_keikaku_layer_visibility(self, ensure_loaded=False):
        if ensure_loaded:
            self._ensure_keikaku_layer_loaded()

        if not self._keikaku_vector_layer_id:
            return False

        if not self._should_show_keikaku_layer():
            self._clear_selection_highlights()
        return self._set_layer_visible(
            self._keikaku_vector_layer_id,
            self._should_show_keikaku_layer(),
        )

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

        self.combo_keikaku_filter = QComboBox()
        self.combo_keikaku_filter.addItem('（全て）', None)
        self.combo_keikaku_filter.setEnabled(False)
        self.combo_keikaku_filter.setToolTip('認定権者で絞り込み')
        row.addWidget(self.combo_keikaku_filter, 1)

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

        self.btn_keikaku_load.setEnabled(False)
        self.btn_keikaku_layer.setEnabled(False)
        self.lbl_keikaku_count.setText('GPKGレイヤーを設定してください')

        self.btn_keikaku_load.clicked.connect(self._load_keikaku)
        self.btn_keikaku_layer.toggled.connect(self._on_keikaku_layer_toggled)
        self.combo_keikaku_filter.currentIndexChanged.connect(self._on_keikaku_filter_changed)
        self.tbl_keikaku.itemSelectionChanged.connect(self._on_keikaku_selected)
        return w

    def _update_keikaku_load_btn(self):
        if not self._has_connected_keikaku_source():
            self.btn_keikaku_load.setEnabled(False)
            self.btn_keikaku_layer.setEnabled(False)
            self.btn_keikaku_layer.blockSignals(True)
            self.btn_keikaku_layer.setChecked(False)
            self.btn_keikaku_layer.blockSignals(False)
            self.tbl_keikaku.setRowCount(0)
            self.lbl_keikaku_count.setStyleSheet('color: gray; font-size: 10px;')
            self.lbl_keikaku_count.setText('GPKGレイヤーを設定してください')
            return

        self.btn_keikaku_layer.blockSignals(True)
        self.btn_keikaku_layer.setChecked(self._keikaku_layer_requested_on())
        self.btn_keikaku_layer.blockSignals(False)

        if self._gpkg_covers_connected_layer():
            # 全県GPKGあり → タブ選択時に読み込む
            self.btn_keikaku_load.setEnabled(False)
            self.btn_keikaku_layer.setEnabled(True)
            if self.cloud_tab.currentIndex() == 3 and self.isVisible():
                self._sync_keikaku_layer_visibility(ensure_loaded=True)
            else:
                self._remove_keikaku_vector_layer()
                self.lbl_keikaku_count.setStyleSheet('color: gray; font-size: 10px;')
                self.lbl_keikaku_count.setText('経営計画タブを開くとレイヤーを表示します')
        else:
            # 未取得 → タブ選択時に取得、手動読み込みも可
            self.btn_keikaku_load.setEnabled(True)
            self.btn_keikaku_layer.setEnabled(self.cloud_tab.currentIndex() == 3)
            self._remove_keikaku_vector_layer()
            self.tbl_keikaku.setRowCount(0)
            self.lbl_keikaku_count.setStyleSheet('color: gray; font-size: 10px;')
            self.lbl_keikaku_count.setText('経営計画タブでデータ取得後にレイヤーを表示します')

    # ------------------------------------------------------------------
    # 読み込み
    # ------------------------------------------------------------------

    def _load_keikaku(self, force=False):
        """読み込みボタン（初回のみ）または更新ボタン（force=True）から呼ばれる。"""
        self._keikaku_loading = True
        self.btn_keikaku_load.setEnabled(False)
        self.btn_keikaku_layer.setEnabled(False)
        self.tbl_keikaku.setRowCount(0)
        self.lbl_keikaku_count.setText('読み込み中...')

        cache_key = '経営計画/all'
        if not force:
            db = self._get_db('経営計画')
            if db is not None:
                cached, ts = db.get(cache_key)
                if cached is not None:
                    self._current_raw_keikaku = cached
                    self.lbl_cache_ts.setText(f'取得日時: {ts}')
                    for rec in self._extract_records(cached):
                        if isinstance(rec, dict):
                            cd = rec.get('市町村cd')
                            name = rec.get('認定権者', '')
                            if cd is not None and name:
                                self._keikaku_cd_to_name[int(cd)] = str(name)
                    self._remove_keikaku_vector_layer()
                    self.lbl_keikaku_count.setText('MVT取得中...')
                    self._start_keikaku_mvt_fetch()
                    return

        self._post_api(
            f'{_API_BASE}/advanced-search/経営計画検索',
            {},
            lambda data: self._on_keikaku_result(data, cache_key),
        )

    def _on_keikaku_result(self, data, cache_key):
        if data is None:
            self._keikaku_loading = False
            self.lbl_keikaku_count.setText('取得失敗')
            self.btn_keikaku_load.setEnabled(True)
            self.btn_keikaku_layer.setEnabled(True)
            return
        self._current_raw_keikaku = data
        db = self._get_db('経営計画')
        if db is not None:
            ts = db.put(cache_key, data)
            self.lbl_cache_ts.setText(f'取得日時: {ts}')
        for rec in self._extract_records(data):
            if isinstance(rec, dict):
                cd = rec.get('市町村cd')
                name = rec.get('認定権者', '')
                if cd is not None and name:
                    self._keikaku_cd_to_name[int(cd)] = str(name)
        self._remove_keikaku_vector_layer()
        self.lbl_keikaku_count.setText('MVT取得中...')
        self._start_keikaku_mvt_fetch()

    def _gpkg_covers_connected_layer(self):
        """keiei_keikaku.gpkg にフィーチャーが1件でもあれば True（全県データのため範囲チェック不要）。"""
        gpkg = self._get_keikaku_gpkg_path()
        if not gpkg or not os.path.exists(gpkg):
            return False
        gpkg_layer = QgsVectorLayer(f'{gpkg}|layername=経営計画作成箇所', '', 'ogr')
        if not gpkg_layer.isValid():
            return False
        req = QgsFeatureRequest().setLimit(1)
        return any(True for _ in gpkg_layer.getFeatures(req))

    def _display_keikaku_table(self, data):
        self._current_raw_keikaku = data
        all_records = [r for r in self._extract_records(data) if isinstance(r, dict)]

        # 認定権者コンボ更新（選択を保持）
        current_val = self.combo_keikaku_filter.currentData()
        self.combo_keikaku_filter.blockSignals(True)
        self.combo_keikaku_filter.clear()
        self.combo_keikaku_filter.addItem('（全て）', None)
        for name in sorted({r.get('認定権者', '') for r in all_records if r.get('認定権者')}):
            self.combo_keikaku_filter.addItem(name, name)
        idx = self.combo_keikaku_filter.findData(current_val)
        self.combo_keikaku_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_keikaku_filter.setEnabled(self.combo_keikaku_filter.count() > 1)
        self.combo_keikaku_filter.blockSignals(False)

        self._render_keikaku_table(all_records)

    def _on_keikaku_filter_changed(self):
        if self._current_raw_keikaku is None:
            return
        all_records = [r for r in self._extract_records(self._current_raw_keikaku)
                       if isinstance(r, dict)]
        self._render_keikaku_table(all_records)
        self._apply_keikaku_layer_filter()

    def _apply_keikaku_layer_filter(self):
        """コンボ選択に従いレイヤーの表示範囲を setSubsetString で絞り込む。"""
        if not self._keikaku_vector_layer_id:
            return
        vl = QgsProject.instance().mapLayer(self._keikaku_vector_layer_id)
        if not vl or sip.isdeleted(vl):
            return
        selected = self.combo_keikaku_filter.currentData()
        vl.setSubsetString(f'"市町村名" = \'{selected}\'' if selected else '')
        self._refresh_map_canvas()

    def _render_keikaku_table(self, all_records):
        selected = self.combo_keikaku_filter.currentData()
        records = ([r for r in all_records if r.get('認定権者') == selected]
                   if selected else all_records)
        self.tbl_keikaku.setRowCount(len(records))
        for row_i, rec in enumerate(records):
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
        self.lbl_keikaku_count.setStyleSheet('color: gray; font-size: 10px;')
        self.lbl_keikaku_count.setText(f'{len(records)}件')

    # ------------------------------------------------------------------
    # レイヤー管理
    # ------------------------------------------------------------------

    def _get_keikaku_gpkg_path(self):
        home = QgsProject.instance().homePath()
        if not home:
            return None
        return os.path.join(home, 'fcloud_shizuoka', 'keiei_keikaku.gpkg')

    def _on_keikaku_layer_toggled(self, on):
        """表示ON/OFFのみ。_keikaku_vector_layer_id はリセットしない。"""
        self._keikaku_layer_requested = bool(on)
        changed = self._sync_keikaku_layer_visibility(ensure_loaded=on)
        if changed:
            self._refresh_map_canvas()

    def _load_keikaku_from_gpkg(self, gpkg_path):
        layer = QgsVectorLayer(f'{gpkg_path}|layername=経営計画作成箇所',
                               'fcloud_経営計画作成箇所', 'ogr')
        if not layer.isValid():
            self._keikaku_loading = False
            self.lbl_keikaku_count.setText('GPKGの読み込みに失敗しました')
            return
        self._apply_keikaku_style(layer)

        self._add_layer_above_gpkg(layer, visible=self._should_show_keikaku_layer())
        self._keikaku_vector_layer_id = layer.id()
        self.btn_keikaku_layer.blockSignals(True)
        self.btn_keikaku_layer.setChecked(self._keikaku_layer_requested_on())
        self.btn_keikaku_layer.setEnabled(True)
        self.btn_keikaku_layer.blockSignals(False)
        self._keikaku_loading = False

        # APIキャッシュが未ロードの場合はキャッシュから復元
        if self._current_raw_keikaku is None:
            db = self._get_db('経営計画')
            if db is not None:
                cached, ts = db.get('経営計画/all')
                if cached is not None:
                    self._current_raw_keikaku = cached
                    self.lbl_cache_ts.setText(f'取得日時: {ts}')
                    for rec in self._extract_records(cached):
                        if isinstance(rec, dict):
                            cd = rec.get('市町村cd')
                            name = rec.get('認定権者', '')
                            if cd is not None and name:
                                self._keikaku_cd_to_name[int(cd)] = str(name)

        if self._current_raw_keikaku is not None:
            self._display_keikaku_table(self._current_raw_keikaku)
        self._apply_keikaku_layer_filter()

    def _start_keikaku_mvt_fetch(self):
        from .mvt_loader import _lon_to_tile_x, _lat_to_tile_y
        self._keikaku_layer_features = []
        zoom = _KEIKAKU_MVT_ZOOM
        min_lon, min_lat, max_lon, max_lat = _SHIZUOKA_BBOX

        x0 = _lon_to_tile_x(min_lon, zoom)
        x1 = _lon_to_tile_x(max_lon, zoom)
        y0 = _lat_to_tile_y(max_lat, zoom)
        y1 = _lat_to_tile_y(min_lat, zoom)
        tiles = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

        self._keikaku_tiles_pending = len(tiles)
        self._keikaku_tiles_received = 0
        self.lbl_keikaku_count.setText(
            f'タイル取得中... (0/{self._keikaku_tiles_pending})')
        self.btn_keikaku_layer.setEnabled(False)

        mvt_url = (
            'https://fcloud.pref.shizuoka.jp/MAP/MVT/'
            f'SHINRIN_KEIEI_KEIKAKU_SAKUSEI_KASHO/{zoom}/{{x}}/{{y}}.pbf'
        )
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
                feats = parse_tile(raw, tile_x, tile_y, _KEIKAKU_MVT_ZOOM)
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
            self._build_keikaku_from_mvt()

    def _build_keikaku_from_mvt(self):
        """MVTフィーチャーから直接レイヤーを構築してGPKGに保存する（空間照合なし）。"""
        if not self._keikaku_layer_features:
            self._keikaku_loading = False
            self.lbl_keikaku_count.setText('取得データなし')
            self.btn_keikaku_layer.setEnabled(False)
            return

        self.lbl_keikaku_count.setText('GPKGに保存中...')
        layer = QgsVectorLayer('Polygon?crs=EPSG:4326', 'fcloud_経営計画作成箇所', 'memory')
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField('市町村cd', QVariant.Int),
            QgsField('市町村名', QVariant.String),
        ])
        layer.updateFields()

        feats_to_add = []
        for f in self._keikaku_layer_features:
            cd = int(f.get('OFFICE', 0))
            if cd == 0:
                continue
            geom = QgsGeometry.fromWkt(f['geometry'])
            if not geom or geom.isEmpty():
                continue
            name = self._keikaku_cd_to_name.get(cd) or _CD_CITY.get(cd, str(cd))
            qf = QgsFeature()
            qf.setGeometry(geom)
            qf.setAttributes([cd, name])
            feats_to_add.append(qf)

        if not feats_to_add:
            self._keikaku_loading = False
            self.lbl_keikaku_count.setText('取得データなし')
            self.btn_keikaku_layer.setEnabled(False)
            return

        pr.addFeatures(feats_to_add)
        layer.updateExtents()

        # タイル境界クリッピングで分割されたポリゴンを市町村cd単位でdissolve
        self.lbl_keikaku_count.setText('ポリゴン統合中...')
        try:
            import processing
            result = processing.run('native:dissolve', {
                'INPUT': layer,
                'FIELD': ['市町村cd'],
                'OUTPUT': 'memory:',
            })
            save_layer = result['OUTPUT']
        except Exception as e:
            print(f'[fcloud] dissolve failed, using raw layer: {e}')
            save_layer = layer

        self._save_keikaku_gpkg(save_layer)

        gpkg = self._get_keikaku_gpkg_path()
        if gpkg and os.path.exists(gpkg):
            self._load_keikaku_from_gpkg(gpkg)
        else:
            self._keikaku_loading = False

        self.btn_keikaku_load.setEnabled(False)
        self.btn_keikaku_layer.setEnabled(True)
        self._update_cache_btn_states()

    def _save_keikaku_gpkg(self, new_layer):
        """全県データで全上書き保存。"""
        gpkg = self._get_keikaku_gpkg_path()
        if not gpkg:
            return
        os.makedirs(os.path.dirname(gpkg), exist_ok=True)
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = 'GPKG'
        opts.fileEncoding = 'UTF-8'
        opts.layerName = '経営計画作成箇所'
        err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
            new_layer, gpkg, QgsProject.instance().transformContext(), opts)[:2]
        if err:
            print(f'[fcloud] keiei_keikaku GPKG save error: {msg}')

    def _apply_keikaku_style(self, layer):
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsFillSymbol
        cd_idx   = layer.fields().indexOf('市町村cd')
        name_idx = layer.fields().indexOf('市町村名')

        # レイヤーのデータから cd → 市町村名 を直接収集
        cd_to_name = {}
        req = QgsFeatureRequest().setSubsetOfAttributes([cd_idx, name_idx])
        for feat in layer.getFeatures(req):
            cd = feat.attribute(cd_idx)
            if cd is not None and int(cd) not in cd_to_name:
                name = feat.attribute(name_idx)
                cd_to_name[int(cd)] = str(name) if name else str(int(cd))

        cats = []
        phi = 0.618033988749895
        hue = 0.05
        for cd in sorted(cd_to_name):
            hue = (hue + phi) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.90)
            ri, gi, bi = int(r * 255), int(g * 255), int(b * 255)
            sym = QgsFillSymbol.createSimple({
                'color':         f'{ri},{gi},{bi},70',
                'outline_style': 'no',
            })
            cats.append(QgsRendererCategory(cd, sym, cd_to_name[cd]))
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
