# -*- coding: utf-8 -*-
import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QTableWidgetItem, QHeaderView,
)
from qgis.PyQt.QtCore import Qt, QUrl, QSettings
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import (
    QgsProject, QgsGeometry, QgsSpatialIndex,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsPointXY, QgsRectangle,
    QgsNetworkAccessManager,
)
from qgis.gui import QgsVertexMarker

from .constants import _API_BASE, _SEIBI_NENDO_LIST, _SEIBI_MVT_ZOOM

_WGS84 = QgsCoordinateReferenceSystem('EPSG:4326')


class SeibiMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_seibi(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel('年度:'))
        self.combo_seibi_year = QComboBox()
        self.combo_seibi_year.addItem('全年度', None)
        for label, yr in _SEIBI_NENDO_LIST:
            self.combo_seibi_year.addItem(label, yr)
        self.combo_seibi_year.setMaximumWidth(130)
        row.addWidget(self.combo_seibi_year)

        row.addWidget(QLabel('過去'))
        self.combo_seibi_year_span = QComboBox()
        for yr in (0, 5, 10):
            self.combo_seibi_year_span.addItem(f'{yr}年', yr)
        self.combo_seibi_year_span.setMaximumWidth(70)
        row.addWidget(self.combo_seibi_year_span)

        saved_year = QSettings().value('fcloud_shizuoka/seibi_year', None, type=str)
        if saved_year is not None:
            target = None if saved_year == '' else int(saved_year)
            idx = self.combo_seibi_year.findData(target)
            if idx >= 0:
                self.combo_seibi_year.setCurrentIndex(idx)
        saved_span = QSettings().value('fcloud_shizuoka/seibi_year_span', None, type=int)
        if saved_span is not None:
            idx = self.combo_seibi_year_span.findData(saved_span)
            if idx >= 0:
                self.combo_seibi_year_span.setCurrentIndex(idx)
        self.combo_seibi_year_span.setEnabled(self.combo_seibi_year.currentData() is not None)

        self.btn_seibi_search = QPushButton('検索')
        row.addWidget(self.btn_seibi_search)
        row.addStretch(1)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel('事業内容:'))
        self.combo_seibi_naiyo = QComboBox()
        self.combo_seibi_naiyo.addItem('（全て）', None)
        row2.addWidget(self.combo_seibi_naiyo, 1)
        row2.addWidget(QLabel('種類:'))
        self.combo_seibi_shurui = QComboBox()
        self.combo_seibi_shurui.addItem('（全て）', None)
        row2.addWidget(self.combo_seibi_shurui, 1)
        row2.addWidget(QLabel('樹種:'))
        self.combo_seibi_jushu = QComboBox()
        self.combo_seibi_jushu.addItem('（全て）', None)
        row2.addWidget(self.combo_seibi_jushu, 1)
        v.addLayout(row2)

        self.tbl_seibi = self._make_table(
            ['年度', '事業内容', '種類', '樹種', '林齢', '面積(ha)'])
        hdr = self.tbl_seibi.horizontalHeader()
        for col in (0, 4, 5):
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            self.tbl_seibi.setColumnWidth(col, 65)
        v.addWidget(self.tbl_seibi, 1)

        bottom = QHBoxLayout()
        self.lbl_seibi_count = QLabel('')
        self.lbl_seibi_count.setStyleSheet('color: gray; font-size: 10px;')
        bottom.addWidget(self.lbl_seibi_count, 1)
        v.addLayout(bottom)

        self.lbl_seibi_count.setText('GPKGレイヤーを設定してください')

        self.btn_seibi_search.clicked.connect(self._search_seibi)
        self.combo_seibi_year.currentIndexChanged.connect(self._on_seibi_year_changed)
        self.combo_seibi_naiyo.currentIndexChanged.connect(self._render_seibi_table)
        self.combo_seibi_shurui.currentIndexChanged.connect(self._render_seibi_table)
        self.combo_seibi_jushu.currentIndexChanged.connect(self._render_seibi_table)
        self.tbl_seibi.itemSelectionChanged.connect(self._on_seibi_selected)
        return w

    def _on_seibi_year_changed(self):
        self.combo_seibi_year_span.setEnabled(self.combo_seibi_year.currentData() is not None)

    def _update_seibi_layer_state(self):
        has_layer = (self._connected_layer is not None
                     and not sip.isdeleted(self._connected_layer))
        self.btn_seibi_search.setEnabled(has_layer)
        self._seibi_spatial_index = None
        self._seibi_index_layer_id = None
        if not has_layer:
            self.tbl_seibi.setRowCount(0)
            self._seibi_all_records = []
            self._seibi_unfiltered_records = []
            self.lbl_seibi_count.setText('GPKGレイヤーを設定してください')
        elif self.cloud_tab.currentIndex() == 2:
            self._ensure_seibi_auto_search()

    def _ensure_seibi_auto_search(self):
        """他タブ同様、タブを開いた時点で前回選択していた年度を自動的に再検索する
        （ローカルDBキャッシュがあればネットワーク再取得は発生しない）。"""
        if self._seibi_pending_years:
            return
        layer = self._connected_layer
        if layer is None or sip.isdeleted(layer):
            return
        if self._seibi_records_layer_id == layer.id():
            return
        self._search_seibi()

    # ------------------------------------------------------------------
    # 検索（選択年度でAPIを呼び、接続GPKGの範囲でクライアント側フィルタ）
    # ------------------------------------------------------------------

    def _search_seibi(self):
        if not (self._connected_layer is not None and not sip.isdeleted(self._connected_layer)):
            return
        sel = self.combo_seibi_year.currentData()
        span = self.combo_seibi_year_span.currentData() or 0
        QSettings().setValue('fcloud_shizuoka/seibi_year', '' if sel is None else str(int(sel)))
        QSettings().setValue('fcloud_shizuoka/seibi_year_span', int(span))

        if sel is None:
            keys = [None]
        else:
            hi = int(sel)
            lo = hi - int(span)
            keys = list(range(lo, hi + 1))

        self._seibi_search_gen += 1
        gen = self._seibi_search_gen
        self._seibi_raw_by_year = {}
        self._seibi_pending_years = list(keys)
        self._seibi_years_total = len(keys)
        self.tbl_seibi.setRowCount(0)
        self.lbl_seibi_count.setText(
            '検索中...' if len(keys) == 1 else f'検索中... (0/{len(keys)}年度)')
        self._seibi_ensure_spatial_index()

        for k in keys:
            self._fetch_seibi_year(k, gen)

    def _fetch_seibi_year(self, year, gen):
        cache_key = f'整備事業/{year if year is not None else "全年度"}'
        db = self._get_db('整備事業')
        if db is not None:
            cached, _ts = db.get(cache_key)
            if cached is not None:
                self._on_seibi_year_result(year, cached, gen, from_cache=True)
                return
        self._post_api(
            f'{_API_BASE}/advanced-search/整備事業検索',
            {'年度': '' if year is None else str(year), '事業内容': '', '種類': '', '樹種': ''},
            lambda data, y=year: self._on_seibi_year_result(y, data, gen),
        )

    def _on_seibi_year_result(self, year, data, gen, from_cache=False):
        if gen != self._seibi_search_gen:
            return
        cache_key = f'整備事業/{year if year is not None else "全年度"}'
        if data is not None:
            self._seibi_raw_by_year[cache_key] = data
            if not from_cache:
                db = self._get_db('整備事業')
                if db is not None:
                    db.put(cache_key, data)
        else:
            label = f'{year}年度' if year is not None else '全年度'
            print(f'[fcloud] 整備事業 {label} 取得失敗')
        if year in self._seibi_pending_years:
            self._seibi_pending_years.remove(year)
        if self._seibi_years_total > 1:
            done = self._seibi_years_total - len(self._seibi_pending_years)
            self.lbl_seibi_count.setText(f'検索中... ({done}/{self._seibi_years_total}年度)')
        if not self._seibi_pending_years:
            self._finalize_seibi_search()

    def _finalize_seibi_search(self):
        layer = self._connected_layer
        self._seibi_records_layer_id = (
            layer.id() if layer is not None and not sip.isdeleted(layer) else None)
        all_records = []
        for year in sorted(self._seibi_raw_by_year):
            all_records.extend(
                r for r in self._extract_records(self._seibi_raw_by_year[year])
                if isinstance(r, dict)
            )

        filtered = []
        for rec in all_records:
            x = rec.get('hilight_point_x')
            y = rec.get('hilight_point_y')
            if x is None or y is None:
                continue
            try:
                lon, lat = float(x), float(y)
            except (TypeError, ValueError):
                continue
            if self._seibi_point_in_connected_layer(lon, lat):
                filtered.append(rec)

        self._seibi_all_records = filtered
        self._seibi_unfiltered_records = all_records
        self._populate_seibi_filter_combos(filtered)
        self._render_seibi_table()

    # ------------------------------------------------------------------
    # 接続GPKG範囲での空間フィルタ（高精度: ポリゴン包含判定）
    # ------------------------------------------------------------------

    def _seibi_ensure_spatial_index(self):
        layer = self._connected_layer
        if layer is None or sip.isdeleted(layer):
            self._seibi_spatial_index = None
            self._seibi_index_layer_id = None
            return None
        if (self._seibi_spatial_index is not None
                and self._seibi_index_layer_id == layer.id()):
            return self._seibi_spatial_index
        index = QgsSpatialIndex(layer.getFeatures())
        self._seibi_spatial_index = index
        self._seibi_index_layer_id = layer.id()
        return index

    def _seibi_point_in_connected_layer(self, lon, lat):
        layer = self._connected_layer
        index = self._seibi_ensure_spatial_index()
        if layer is None or index is None or sip.isdeleted(layer):
            return False
        pt = QgsPointXY(lon, lat)
        if layer.crs() != _WGS84:
            tr = QgsCoordinateTransform(_WGS84, layer.crs(), QgsProject.instance())
            pt = tr.transform(pt)
        pt_geom = QgsGeometry.fromPointXY(pt)
        for fid in index.intersects(pt_geom.boundingBox()):
            feat = layer.getFeature(fid)
            geom = feat.geometry()
            if geom and geom.intersects(pt_geom):
                return True
        return False

    # ------------------------------------------------------------------
    # 表示
    # ------------------------------------------------------------------

    def _populate_seibi_filter_combos(self, records):
        for combo, key in (
            (self.combo_seibi_naiyo, '事業内容'),
            (self.combo_seibi_shurui, '種類'),
            (self.combo_seibi_jushu, '樹種'),
        ):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem('（全て）', None)
            for v in sorted({str(r.get(key, '') or '') for r in records if r.get(key)}):
                combo.addItem(v, v)
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    @staticmethod
    def _seibi_apply_combo_filter(records, naiyo, shurui, jushu):
        if naiyo:
            records = [r for r in records if r.get('事業内容') == naiyo]
        if shurui:
            records = [r for r in records if r.get('種類') == shurui]
        if jushu:
            records = [r for r in records if r.get('樹種') == jushu]
        return records

    def _render_seibi_table(self, *_):
        naiyo = self.combo_seibi_naiyo.currentData()
        shurui = self.combo_seibi_shurui.currentData()
        jushu = self.combo_seibi_jushu.currentData()

        records = self._seibi_apply_combo_filter(
            self._seibi_all_records, naiyo, shurui, jushu)
        total_records = self._seibi_apply_combo_filter(
            self._seibi_unfiltered_records, naiyo, shurui, jushu)
        records = sorted(records, key=lambda r: str(r.get('表示用_年度', '')), reverse=True)

        self.tbl_seibi.setRowCount(len(records))
        for row_i, rec in enumerate(records):
            vals = [
                str(rec.get('表示用_年度', '') or ''),
                str(rec.get('事業内容', '') or ''),
                str(rec.get('種類', '') or ''),
                str(rec.get('樹種', '') or ''),
                str(rec.get('林齢', '') or ''),
                str(rec.get('表示用_面積', rec.get('面積', '')) or ''),
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(' ' + v)
                item.setData(Qt.UserRole, rec)
                self.tbl_seibi.setItem(row_i, col, item)
        self.lbl_seibi_count.setText(
            f'{len(records)}件（接続レイヤー範囲内） / 全件{len(total_records)}件')

    @staticmethod
    def _seibi_match_geometry(features, lon, lat):
        """hilight_point座標を、タイルから取得したポリゴン群に突き合わせる
        （内包していればそれを採用、なければ最も近いものにフォールバック）。"""
        pt_geom = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
        best_geom = None
        best_dist = None
        for f in features:
            wkt = f.get('geometry')
            if not wkt:
                continue
            geom = QgsGeometry.fromWkt(wkt)
            if not geom or geom.isEmpty():
                continue
            if geom.contains(pt_geom):
                return geom
            d = geom.distance(pt_geom)
            if best_dist is None or d < best_dist:
                best_dist = d
                best_geom = geom
        return best_geom

    # ------------------------------------------------------------------
    # 行選択 → ズーム / ハイライト
    # ------------------------------------------------------------------

    def _clear_seibi_markers(self):
        canvas = self.iface.mapCanvas()
        scene = canvas.scene()
        for m in self._seibi_markers:
            if not sip.isdeleted(m):
                scene.removeItem(m)
        self._seibi_markers.clear()

    def _on_seibi_selected(self):
        self._clear_seibi_markers()
        self._clear_selection_highlights()
        self._seibi_highlight_gen += 1
        rows = self.tbl_seibi.selectionModel().selectedRows()
        if not rows:
            self._clear_cloud_record_info()
            return
        row = rows[0].row()
        item = self.tbl_seibi.item(row, 0)
        rec = item.data(Qt.UserRole) if item else None
        if not isinstance(rec, dict):
            self._clear_cloud_record_info()
            return
        self._show_cloud_table_row_info('整備事業', self.tbl_seibi, row)

        x = rec.get('hilight_point_x')
        y = rec.get('hilight_point_y')
        if x is None or y is None:
            return
        try:
            lon, lat = float(x), float(y)
        except (TypeError, ValueError):
            return

        self._seibi_zoom_point_fallback(lon, lat)
        self._fetch_seibi_highlight_tiles(lon, lat)

    def _seibi_zoom_point_fallback(self, lon, lat):
        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        tr = QgsCoordinateTransform(_WGS84, dst_crs, QgsProject.instance())
        pt = tr.transform(QgsPointXY(lon, lat))

        marker = QgsVertexMarker(canvas)
        marker.setCenter(pt)
        marker.setColor(QColor(255, 80, 0))
        marker.setIconSize(14)
        marker.setIconType(QgsVertexMarker.ICON_CROSS)
        marker.setPenWidth(3)
        self._seibi_markers.append(marker)

        buf = canvas.mapUnitsPerPixel() * 200
        extent = QgsRectangle(pt.x() - buf, pt.y() - buf, pt.x() + buf, pt.y() + buf)
        canvas.setExtent(extent)
        canvas.refresh()

    def _fetch_seibi_highlight_tiles(self, lon, lat):
        """対象地点を含むタイルとその周囲(3×3)を取得し、地点を内包する（または最も近い）
        ポリゴンをハイライト表示する。"""
        from .mvt_loader import _lon_to_tile_x, _lat_to_tile_y
        zoom = _SEIBI_MVT_ZOOM
        cx = _lon_to_tile_x(lon, zoom)
        cy = _lat_to_tile_y(lat, zoom)
        self._seibi_highlight_gen += 1
        gen = self._seibi_highlight_gen
        self._seibi_highlight_target = (lon, lat)
        self._seibi_highlight_features = []
        self._seibi_highlight_pending = 9

        mvt_url = f'https://fcloud.pref.shizuoka.jp/MAP/MVT/SHINRIN_SEIBI_JIGYOU/{zoom}/{{x}}/{{y}}.pbf'
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                tx, ty = cx + dx, cy + dy
                url = mvt_url.replace('{x}', str(tx)).replace('{y}', str(ty))
                req = QNetworkRequest(QUrl(url))
                reply = QgsNetworkAccessManager.instance().get(req)
                self._pending_replies.append(reply)
                reply.finished.connect(
                    lambda r=reply, x=tx, y=ty: self._on_seibi_highlight_tile(r, x, y, zoom, gen))

    def _on_seibi_highlight_tile(self, reply, tile_x, tile_y, zoom, gen):
        from .mvt_loader import parse_tile
        if gen == self._seibi_highlight_gen and reply.error() == QNetworkReply.NoError:
            raw = bytes(reply.readAll())
            try:
                feats = parse_tile(raw, tile_x, tile_y, zoom, 'SHINRIN_SEIBI_JIGYOU')
                self._seibi_highlight_features.extend(feats)
            except Exception as e:
                print(f'[fcloud] seibi highlight tile parse error ({tile_x},{tile_y}): {e}')
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()

        if gen != self._seibi_highlight_gen:
            return
        self._seibi_highlight_pending -= 1
        if self._seibi_highlight_pending <= 0:
            self._finalize_seibi_highlight(gen)

    def _finalize_seibi_highlight(self, gen):
        if gen != self._seibi_highlight_gen or self._seibi_highlight_target is None:
            return
        lon, lat = self._seibi_highlight_target

        # THE_FID属性はタイル内でのローカルな採番の可能性が高く、JSON側のfidとは
        # 対応しないことが実地確認で判明したため使用しない。hilight_point座標に対する
        # 点含有判定（内包していなければ最近傍）のみでポリゴンを特定する。
        best_geom = self._seibi_match_geometry(self._seibi_highlight_features, lon, lat)

        if best_geom is not None:
            self._add_selection_highlight(best_geom, _WGS84)
            self._zoom_to_selection_highlights()
            self._refresh_map_canvas()
        else:
            print('[fcloud] seibi highlight: 周囲タイルにポリゴンが見つかりませんでした')
