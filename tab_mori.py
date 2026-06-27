# -*- coding: utf-8 -*-
import os
import colorsys
import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLabel, QPushButton, QTableWidgetItem,
)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsField, QgsVectorFileWriter, QgsFeatureRequest,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsNetworkAccessManager,
)
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import QVariant

from .constants import _API_BASE, _MORI_MVT_ZOOM, _NORIN_OFFICES, _NENDO_LIST


class MoriMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_mori(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel('農林事務所:'))
        self.combo_mori_norin = QComboBox()
        for office in _NORIN_OFFICES:
            self.combo_mori_norin.addItem(office)
        row.addWidget(self.combo_mori_norin, 2)
        row.addWidget(QLabel('年度:'))
        self.combo_mori_nendo = QComboBox()
        self.combo_mori_nendo.addItem('（全て）', None)
        for label, yr in _NENDO_LIST:
            self.combo_mori_nendo.addItem(label, yr)
        self.combo_mori_nendo.setMaximumWidth(110)
        row.addWidget(self.combo_mori_nendo, 1)
        row.addWidget(QLabel('事業区分:'))
        self.combo_mori_kubun = QComboBox()
        self.combo_mori_kubun.addItem('（全て）', '')
        self.combo_mori_kubun.addItem('人工林再生整備', '人工林再生整備')
        self.combo_mori_kubun.addItem('広葉樹林等再生整備', '広葉樹林等再生整備')
        self.combo_mori_kubun.setMaximumWidth(130)
        row.addWidget(self.combo_mori_kubun, 1)
        row.addWidget(QLabel('整備者:'))
        self.combo_mori_seibi = QComboBox()
        self.combo_mori_seibi.setEditable(True)
        self.combo_mori_seibi.setInsertPolicy(QComboBox.NoInsert)
        self.combo_mori_seibi.addItem('（全て）', '')
        self.combo_mori_seibi.setEnabled(False)
        row.addWidget(self.combo_mori_seibi, 2)
        self.btn_mori_search = QPushButton('検索')
        row.addWidget(self.btn_mori_search)

        self.btn_mori_layer = QPushButton('実施箇所レイヤー')
        self.btn_mori_layer.setCheckable(True)
        self.btn_mori_layer.setToolTip('森の力実施箇所のMVTポリゴンレイヤーを追加/除去')
        row.addWidget(self.btn_mori_layer)
        v.addLayout(row)

        self.tbl_mori = self._make_table(
            ['農林事務所', '年度', 'モデル林', '整備者住所', '整備者名', '整備者代表', '総面積(ha)', '所在地', '林小班'])
        hdr = self.tbl_mori.horizontalHeader()
        hdr.setSectionResizeMode(0, hdr.Fixed)
        hdr.setSectionResizeMode(1, hdr.Fixed)
        hdr.setSectionResizeMode(2, hdr.Fixed)
        hdr.setSectionResizeMode(6, hdr.Fixed)
        self.tbl_mori.setColumnWidth(0, 130)
        self.tbl_mori.setColumnWidth(1, 90)
        self.tbl_mori.setColumnWidth(2, 60)
        self.tbl_mori.setColumnWidth(6, 70)
        v.addWidget(self.tbl_mori, 1)

        bottom = QHBoxLayout()
        self.lbl_mori_count = QLabel('')
        self.lbl_mori_count.setStyleSheet('color: gray; font-size: 10px;')
        bottom.addWidget(self.lbl_mori_count, 1)
        v.addLayout(bottom)

        self.btn_mori_search.clicked.connect(self._search_mori)
        self.combo_mori_norin.activated.connect(self._on_mori_search_filter_changed)
        self.combo_mori_nendo.activated.connect(self._on_mori_search_filter_changed)
        self.combo_mori_kubun.activated.connect(self._on_mori_search_filter_changed)
        self.btn_mori_layer.toggled.connect(self._on_mori_layer_toggled)
        self.combo_mori_seibi.currentIndexChanged.connect(self._on_mori_seibi_changed)
        self.tbl_mori.itemSelectionChanged.connect(self._on_mori_selected)
        return w

    # ------------------------------------------------------------------
    # 検索・表示
    # ------------------------------------------------------------------

    def _search_mori(self):
        self.tbl_mori.setRowCount(0)
        self.lbl_mori_count.setText('検索中...')
        self.btn_mori_search.setEnabled(False)
        params = {}
        norin = self.combo_mori_norin.currentText().strip()
        if norin:
            params['農林事務所'] = norin
        nendo = self.combo_mori_nendo.currentData()
        if nendo is not None:
            params['年度'] = str(nendo)
        kubun = self.combo_mori_kubun.currentData()
        if kubun:
            params['事業区分'] = kubun

        self._current_mori_cache_key = (
            f'森の力/農林={norin}&年度={nendo or ""}&区分={kubun or ""}')

        db = self._get_db('森の力')
        if db is not None:
            cached, ts = db.get(self._current_mori_cache_key)
            if cached is not None:
                self.btn_mori_search.setEnabled(True)
                self._current_raw_mori = cached
                self.lbl_cache_ts.setText(f'取得日時: {ts}')
                total = self._display_mori_table(cached)
                self._auto_show_mori_layer(total)
                return

        self._post_api(
            f'{_API_BASE}/advanced-search/森の力検索',
            params,
            self._on_mori_result,
        )

    def _on_mori_result(self, data):
        self.btn_mori_search.setEnabled(True)
        if data is None:
            self.lbl_mori_count.setText('取得失敗')
            self._update_cache_btn_states()
            return
        self._current_raw_mori = data
        self.lbl_cache_ts.setText('取得日時: 未保存')
        total = self._display_mori_table(data)
        self._auto_show_mori_layer(total)
        self._update_cache_btn_states()

    def _on_mori_search_filter_changed(self, *_):
        self._search_mori()

    def _display_mori_table(self, data):
        records = self._extract_records(data)

        seibi_vals = sorted(set(
            str(r.get('申請者_整備者_氏名', '') or '')
            for r in records if isinstance(r, dict)
        ) - {'', 'NULL', 'None'})
        prev_seibi = self.combo_mori_seibi.currentData() or ''
        self.combo_mori_seibi.blockSignals(True)
        self.combo_mori_seibi.clear()
        self.combo_mori_seibi.addItem('（全て）', '')
        for s in seibi_vals:
            self.combo_mori_seibi.addItem(s, s)
        if prev_seibi:
            idx = self.combo_mori_seibi.findData(prev_seibi)
            if idx >= 0:
                self.combo_mori_seibi.setCurrentIndex(idx)
        self.combo_mori_seibi.blockSignals(False)
        self.combo_mori_seibi.setEnabled(bool(seibi_vals))

        seibi_filter = self.combo_mori_seibi.currentData() or ''
        if seibi_filter:
            records = [r for r in records
                       if isinstance(r, dict)
                       and str(r.get('申請者_整備者_氏名', '') or '') == seibi_filter]

        total = len(records)
        self.tbl_mori.setRowCount(total)

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
                _get(rec, '農林事務所'),
                _get(rec, '年度'),
                _get(rec, 'モデル林フラグ', 'モデル林'),
                _get(rec, '申請者_整備者_住所'),
                _get(rec, '申請者_整備者_氏名'),
                _get(rec, '申請者_整備者_氏名_代表者'),
                _get(rec, '対象森林_総面積', '表示用_対象森林_総面積'),
                _get(rec, '対象森林_所在地'),
                _get(rec, '対象森林_林小班'),
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(' ' + v)
                item.setData(Qt.UserRole, rec)
                self.tbl_mori.setItem(row_i, col, item)

        self.lbl_mori_count.setText(f'{total}件')
        self._apply_mori_layer_filter()
        return total

    def _auto_show_mori_layer(self, total):
        if total <= 0:
            return
        if not self.btn_mori_layer.isChecked():
            self.btn_mori_layer.setChecked(True)
            return
        if not self._mori_vector_layer_id:
            self._on_mori_layer_toggled(True)

    def _on_mori_seibi_changed(self):
        if self._current_raw_mori is not None:
            self._display_mori_table(self._current_raw_mori)

    @staticmethod
    def _escape_mori_sql(value):
        return str(value).replace("'", "''")

    def _apply_mori_layer_filter(self):
        if not self._mori_vector_layer_id:
            return
        layer = QgsProject.instance().mapLayer(self._mori_vector_layer_id)
        if not layer or sip.isdeleted(layer):
            return

        field_names = {f.name() for f in layer.fields()}
        clauses = []

        norin = self.combo_mori_norin.currentText().strip()
        nendo = self.combo_mori_nendo.currentData()
        kubun = self.combo_mori_kubun.currentData() or ''
        seibi = self.combo_mori_seibi.currentData() or ''

        if norin and '農林事務所' in field_names:
            clauses.append(f'"農林事務所" = \'{self._escape_mori_sql(norin)}\'')
        if nendo is not None and '年度' in field_names:
            clauses.append(f'"年度" = \'{self._escape_mori_sql(nendo)}\'')
        if kubun and '事業区分' in field_names:
            clauses.append(f'"事業区分" = \'{self._escape_mori_sql(kubun)}\'')
        if seibi and '整備者名' in field_names:
            clauses.append(f'"整備者名" = \'{self._escape_mori_sql(seibi)}\'')

        layer.setSubsetString(' AND '.join(clauses))
        self._refresh_map_canvas()

    # ------------------------------------------------------------------
    # レイヤー管理
    # ------------------------------------------------------------------

    def _get_mori_gpkg_path(self):
        home = QgsProject.instance().homePath()
        if not home:
            return None
        return os.path.join(home, 'fcloud_shizuoka', 'mori_chikara_v4.gpkg')

    def _on_mori_layer_toggled(self, on):
        if not on:
            self._remove_mori_vector_layer()
            return
        if self._current_raw_mori is None:
            self.btn_mori_layer.blockSignals(True)
            self.btn_mori_layer.setChecked(False)
            self.btn_mori_layer.blockSignals(False)
            return
        if self._mori_vector_layer_id:
            self._set_layer_visible(self._mori_vector_layer_id, True)
            self._refresh_map_canvas()
            return
        gpkg = self._get_mori_gpkg_path()
        if gpkg and os.path.exists(gpkg):
            self._load_mori_from_gpkg(gpkg)
        else:
            self._start_mori_mvt_fetch()
        self._refresh_map_canvas()

    def _load_mori_from_gpkg(self, gpkg_path):
        layer = QgsVectorLayer(f'{gpkg_path}|layername=森の力実施箇所',
                               'fcloud_森の力実施箇所', 'ogr')
        if not layer.isValid():
            self._start_mori_mvt_fetch()
            return
        if layer.fields().indexOf('整備者名') < 0:
            self._start_mori_mvt_fetch()
            return
        self._apply_mori_style(layer)
        visible = (self.btn_mori_layer.isChecked()
                   and self.cloud_tab.currentIndex() == 2)
        self._add_layer_above_gpkg(layer, visible=visible)
        self._mori_vector_layer_id = layer.id()
        self._apply_mori_layer_filter()

    def _start_mori_mvt_fetch(self):
        from .mvt_loader import shizuoka_tiles
        self._mori_layer_features = []
        tiles = shizuoka_tiles(zoom=_MORI_MVT_ZOOM)
        self._mori_tiles_pending = len(tiles)
        self._mori_tiles_received = 0
        self.lbl_mori_count.setText(f'タイル取得中... (0/{self._mori_tiles_pending})')
        self.btn_mori_layer.setEnabled(False)
        mvt_url = ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
                   'MAGIS.MORI_NO_CHIKARA/{z}/{x}/{y}.pbf')
        for tx, ty in tiles:
            url = mvt_url.replace('{z}', str(_MORI_MVT_ZOOM)).replace('{x}', str(tx)).replace('{y}', str(ty))
            req = QNetworkRequest(QUrl(url))
            reply = QgsNetworkAccessManager.instance().get(req)
            self._pending_replies.append(reply)
            reply.finished.connect(
                lambda r=reply, x=tx, y=ty: self._on_mori_mvt_tile(r, x, y))

    def _on_mori_mvt_tile(self, reply, tile_x, tile_y):
        from .mvt_loader import parse_tile
        if reply.error() == QNetworkReply.NoError:
            raw = bytes(reply.readAll())
            try:
                feats = parse_tile(raw, tile_x, tile_y, _MORI_MVT_ZOOM, 'MAGIS.MORI_NO_CHIKARA')
                self._mori_layer_features.extend(feats)
            except Exception as e:
                print(f'[fcloud] MVT parse error tile({tile_x},{tile_y}): {e}')
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()
        self._mori_tiles_received += 1
        self.lbl_mori_count.setText(
            f'タイル取得中... ({self._mori_tiles_received}/{self._mori_tiles_pending})')
        if self._mori_tiles_received >= self._mori_tiles_pending:
            self._build_mori_vector_layer()

    def _build_mori_vector_layer(self):
        layer = QgsVectorLayer('Polygon?crs=EPSG:4326', 'fcloud_森の力実施箇所', 'memory')
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField('管理番号',   QVariant.String),
            QgsField('事業区分',   QVariant.String),
            QgsField('詳細区分',   QVariant.String),
            QgsField('年度',       QVariant.String),
            QgsField('農林事務所', QVariant.String),
            QgsField('整備者名',   QVariant.String),
        ])
        layer.updateFields()

        feats_to_add = []

        def _make_feat(attrs, geom):
            if geom is None or geom.isEmpty():
                return None
            qf = QgsFeature()
            qf.setGeometry(geom)
            qf.setAttributes([
                str(attrs.get('管理番号', '')),
                str(attrs.get('事業区分', '')),
                str(attrs.get('詳細区分', '')),
                str(attrs.get('年度', '')),
                str(attrs.get('農林事務所', '')),
                str(attrs.get('申請者_整備者_氏名', '')),
            ])
            return qf

        for f in self._mori_layer_features:
            g = QgsGeometry.fromWkt(f['geometry'])
            qf = _make_feat(f, g)
            if qf:
                feats_to_add.append(qf)

        pr.addFeatures(feats_to_add)
        layer.updateExtents()

        # タイル境界クリッピングで分割されたポリゴンを管理番号単位でdissolve
        self.lbl_mori_count.setText('ポリゴン統合中...')
        try:
            import processing
            from qgis.core import QgsProcessingContext, QgsProcessingFeedback
            ctx = QgsProcessingContext()
            result = processing.run('native:dissolve', {
                'INPUT': layer,
                'FIELD': ['管理番号'],
                'OUTPUT': 'memory:',
            }, context=ctx, feedback=QgsProcessingFeedback())
            save_layer = result['OUTPUT']
        except Exception as e:
            print(f'[fcloud] mori dissolve failed, using raw layer: {e}')
            save_layer = layer

        self._apply_mori_style(save_layer)

        gpkg = self._get_mori_gpkg_path()
        if gpkg:
            os.makedirs(os.path.dirname(gpkg), exist_ok=True)
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = 'GPKG'
            opts.fileEncoding = 'UTF-8'
            opts.layerName = '森の力実施箇所'
            err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
                save_layer, gpkg, QgsProject.instance().transformContext(), opts)[:2]
            if err:
                print(f'[fcloud] GPKG save error: {msg}')
            else:
                import datetime
                ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
                self.lbl_cache_ts.setText(f'レイヤーキャッシュ: {ts}')

        visible = (self.btn_mori_layer.isChecked()
                   and self.cloud_tab.currentIndex() == 2)
        self._add_layer_above_gpkg(save_layer, visible=visible)
        self._mori_vector_layer_id = save_layer.id()
        self._apply_mori_layer_filter()
        self.btn_mori_layer.setEnabled(True)
        total = self.tbl_mori.rowCount()
        self.lbl_mori_count.setText(f'{total}件' if total else '')

    def _apply_mori_style(self, layer):
        from collections import defaultdict
        from qgis.core import QgsRuleBasedRenderer, QgsFillSymbol

        _null = {'', 'NULL', 'None'}
        # {seibi: {nendo: [kanri, ...]}}
        groups = defaultdict(lambda: defaultdict(list))
        kanri_seen = set()

        raw = getattr(self, '_current_raw_mori', None)
        if raw is not None:
            for rec in self._extract_records(raw):
                if not isinstance(rec, dict):
                    continue
                kanri = str(rec.get('管理番号', '') or '')
                if not kanri or kanri in _null or kanri in kanri_seen:
                    continue
                kanri_seen.add(kanri)
                nendo = str(rec.get('年度', '') or '')
                seibi = str(rec.get('申請者_整備者_氏名', '') or '')
                nendo = '' if nendo in _null else nendo
                seibi = '' if seibi in _null else seibi
                groups[seibi][nendo].append(kanri)

        idx_kanri = layer.fields().indexOf('管理番号')
        if idx_kanri >= 0:
            for val in layer.uniqueValues(idx_kanri):
                kanri = str(val) if val is not None else ''
                if kanri and kanri not in _null and kanri not in kanri_seen:
                    groups[''][''].append(kanri)

        def _nendo_key(n):
            try:
                return -int(n)
            except (ValueError, TypeError):
                return 0

        root = QgsRuleBasedRenderer.Rule(None)
        phi = 0.618033988749895
        hue = 0.17

        for seibi in sorted(groups):
            seibi_rule = QgsRuleBasedRenderer.Rule(None)
            seibi_rule.setLabel(seibi if seibi else '（整備者不明）')

            for nendo in sorted(groups[seibi], key=_nendo_key):
                nendo_rule = QgsRuleBasedRenderer.Rule(None)
                nendo_rule.setLabel(nendo if nendo else '（年度不明）')

                for kanri in sorted(groups[seibi][nendo]):
                    hue = (hue + phi) % 1.0
                    r, g, b = colorsys.hsv_to_rgb(hue, 0.60, 0.88)
                    ri, gi, bi = int(r * 255), int(g * 255), int(b * 255)
                    sym = QgsFillSymbol.createSimple({
                        'color':         f'{ri},{gi},{bi},160',
                        'outline_color': f'{max(0,ri-50)},{max(0,gi-50)},{max(0,bi-50)},220',
                        'outline_width': '0.4',
                    })
                    kanri_rule = QgsRuleBasedRenderer.Rule(sym)
                    kanri_rule.setLabel(kanri)
                    kanri_rule.setFilterExpression(
                        f"\"管理番号\" = '{kanri.replace(chr(39), chr(39)*2)}'")
                    nendo_rule.appendChild(kanri_rule)

                seibi_rule.appendChild(nendo_rule)

            root.appendChild(seibi_rule)

        layer.setRenderer(QgsRuleBasedRenderer(root))

    def _remove_mori_vector_layer(self):
        self._clear_mori_markers()
        self._clear_selection_highlights()
        if self._mori_vector_layer_id:
            layer = QgsProject.instance().mapLayer(self._mori_vector_layer_id)
            if layer and not sip.isdeleted(layer):
                QgsProject.instance().removeMapLayer(self._mori_vector_layer_id)
            self._mori_vector_layer_id = None
        self._mori_layer_features = []
        self._mori_tiles_pending = 0
        self._mori_tiles_received = 0
        self._refresh_map_canvas()

    def _clear_mori_markers(self):
        canvas = self.iface.mapCanvas()
        scene = canvas.scene()
        for m in self._mori_markers:
            if not sip.isdeleted(m):
                scene.removeItem(m)
        self._mori_markers.clear()

    # ------------------------------------------------------------------
    # 行選択 → ズーム
    # ------------------------------------------------------------------

    def _on_mori_selected(self):
        self._clear_mori_markers()
        self._clear_selection_highlights()
        rows = self.tbl_mori.selectionModel().selectedRows()
        if not rows:
            self._clear_cloud_record_info()
            return
        item = self.tbl_mori.item(rows[0].row(), 0)
        if not item:
            self._clear_cloud_record_info()
            return
        row = rows[0].row()
        rec = item.data(Qt.UserRole)
        if not isinstance(rec, dict):
            self._clear_cloud_record_info()
            return
        self._show_cloud_table_row_info('森の力', self.tbl_mori, row)

        if not self.btn_mori_layer.isChecked():
            return

        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        show_feature = self.btn_mori_layer.isChecked()

        def _zoom_geometries(geoms, src_crs):
            tr = (QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                  if src_crs != dst_crs else None)
            bbox = None
            transformed_geoms = []
            for geom in geoms:
                if not geom or geom.isEmpty():
                    continue
                tg = QgsGeometry(geom)
                if tr is not None:
                    tg.transform(tr)
                if show_feature:
                    b = self._add_selection_highlight(geom, src_crs)
                    if bbox is None:
                        bbox = b
                    else:
                        bbox.combineExtentWith(b)
                transformed_geoms.append(tg)
            if show_feature and self._zoom_to_selection_highlights():
                return True
            if transformed_geoms:
                try:
                    merged = QgsGeometry.unaryUnion(transformed_geoms)
                    if merged and not merged.isEmpty():
                        bbox = merged.boundingBox()
                except Exception:
                    pass
            if bbox and not bbox.isEmpty():
                buf = max(bbox.width(), bbox.height()) * 0.60 + 5
                bbox.grow(buf)
                canvas.setExtent(bbox)
                canvas.refresh()
                return True
            return False

        def _zoom_layer_matches(layer, expr):
            req = QgsFeatureRequest()
            req.setFilterExpression(expr)
            geoms = [feat.geometry() for feat in layer.getFeatures(req)]
            return _zoom_geometries(geoms, layer.crs())

        kanri = str(rec.get('管理番号', '')).strip()
        if kanri and self._mori_vector_layer_id:
            vl = QgsProject.instance().mapLayer(self._mori_vector_layer_id)
            if vl and not sip.isdeleted(vl):
                if _zoom_layer_matches(vl, f'"管理番号" = \'{kanri}\''):
                    return

        if kanri:
            gpkg = self._get_mori_gpkg_path()
            if gpkg and os.path.exists(gpkg):
                temp = QgsVectorLayer(f'{gpkg}|layername=森の力実施箇所', 'temp', 'ogr')
                if temp.isValid():
                    if _zoom_layer_matches(temp, f'"管理番号" = \'{kanri}\''):
                        return

        if kanri and self._mori_layer_features:
            geoms = []
            for feat in self._mori_layer_features:
                if str(feat.get('管理番号', '')).strip() != kanri:
                    continue
                geom = QgsGeometry.fromWkt(feat.get('geometry', ''))
                if geom and not geom.isEmpty():
                    geoms.append(geom)
            if geoms and _zoom_geometries(geoms, QgsCoordinateReferenceSystem('EPSG:4326')):
                return

        x = rec.get('hilight_point_x')
        y = rec.get('hilight_point_y')
        if x is None or y is None:
            return
        src_crs = QgsCoordinateReferenceSystem('EPSG:4326')
        from qgis.core import QgsPointXY, QgsRectangle
        tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        pt = tr.transform(QgsPointXY(float(x), float(y)))

        if show_feature:
            marker = QgsVertexMarker(canvas)
            marker.setCenter(pt)
            marker.setColor(QColor(255, 80, 0))
            marker.setIconSize(14)
            marker.setIconType(QgsVertexMarker.ICON_CROSS)
            marker.setPenWidth(3)
            self._mori_markers.append(marker)

        buf = canvas.mapUnitsPerPixel() * 200
        extent = QgsRectangle(pt.x() - buf, pt.y() - buf, pt.x() + buf, pt.y() + buf)
        canvas.setExtent(extent)
        canvas.refresh()

    # ------------------------------------------------------------------
    # 全画面トグル
    # ------------------------------------------------------------------

    def _toggle_mori_fullscreen(self, on):
        if on:
            self.setFloating(True)
            self.showMaximized()
            self.btn_mori_fullscreen.setText('格納')
        else:
            self.showNormal()
            self.setFloating(False)
            self.btn_mori_fullscreen.setText('全画面')
