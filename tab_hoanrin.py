# -*- coding: utf-8 -*-
import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLabel, QLineEdit, QPushButton,
)
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QColor, QBrush
from qgis.core import (
    QgsProject, QgsFeatureRequest,
    QgsCoordinateTransform, QgsWkbTypes,
)
from qgis.gui import QgsRubberBand

from .constants import (
    _API_BASE, _CITY_API_MAP, _API_CITY_MAP, _CITY_CD,
)

# 保安林台帳専用ハイライト色
_HL_BLUE_BORDER   = QColor(30,  100, 255, 200)
_HL_BLUE_FILL     = QColor(30,  100, 255,   8)
_HL_YELLOW_BORDER = QColor(210, 170,   0, 200)
_HL_YELLOW_FILL   = QColor(210, 170,   0,  13)
_HL_RED_BORDER    = QColor(220,  50,  50, 200)
_HL_RED_FILL      = QColor(220,  50,  50,   8)
_HL_ORANGE_BORDER = QColor(255, 140,   0, 200)
_HL_ORANGE_FILL   = QColor(255, 140,   0,   8)


class HoanrinMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_hoanrin(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel('市町村:'))
        self.combo_hoanrin_city = QComboBox()
        self.combo_hoanrin_city.setEditable(True)
        self.combo_hoanrin_city.setInsertPolicy(QComboBox.NoInsert)
        row.addWidget(self.combo_hoanrin_city, 3)
        row.addWidget(QLabel('大字:'))
        self.combo_hoanrin_daiji = QComboBox()
        self.combo_hoanrin_daiji.setEditable(True)
        self.combo_hoanrin_daiji.setInsertPolicy(QComboBox.NoInsert)
        self.combo_hoanrin_daiji.addItem('（全て）', '')
        row.addWidget(self.combo_hoanrin_daiji, 3)
        row.addWidget(QLabel('地番:'))
        self.edit_hoanrin_chiban = QLineEdit()
        self.edit_hoanrin_chiban.setPlaceholderText('番号')
        self.edit_hoanrin_chiban.setFixedWidth(60)
        self.edit_hoanrin_chiban.returnPressed.connect(self._search_hoanrin)
        row.addWidget(self.edit_hoanrin_chiban)
        self.btn_hoanrin_search = QPushButton('検索')
        row.addWidget(self.btn_hoanrin_search)
        _kinbo_init = QSettings().value('fcloud_shizuoka/kinbo_enabled', False, type=bool)
        self.btn_kinbo = QPushButton('近傍データON' if _kinbo_init else '近傍データOFF')
        self.btn_kinbo.setCheckable(True)
        self.btn_kinbo.setChecked(_kinbo_init)
        self.btn_kinbo.setStyleSheet('color: gray;' if _kinbo_init else '')
        self.btn_kinbo.setToolTip('黄・赤のハイライト（近似/近傍一致）を表示する')
        row.addWidget(self.btn_kinbo)
        self.btn_kozu = QPushButton('公図連携OFF')
        self.btn_kozu.setCheckable(True)
        self.btn_kozu.setToolTip('ONにすると行選択のたびに自動でkozu_xml_integratorへ送信')
        row.addWidget(self.btn_kozu)
        self._update_kozu_btn()
        v.addLayout(row)

        self.tbl_hoanrin = self._make_table(
            ['大字', '字', '地番', '保安林種', '面積(ha)', 'GPKG面積(ha)', '面積割合', '一致データの有無'])
        v.addWidget(self.tbl_hoanrin, 1)

        bottom_row = QHBoxLayout()
        self.lbl_hoanrin_count = QLabel('')
        self.lbl_hoanrin_count.setStyleSheet('color: gray; font-size: 10px;')
        bottom_row.addWidget(self.lbl_hoanrin_count, 1)
        hint = QLabel('行選択/矢印キー → ハイライト')
        hint.setStyleSheet('color: gray; font-size: 10px;')
        bottom_row.addWidget(hint)
        v.addLayout(bottom_row)

        self.combo_hoanrin_city.currentTextChanged.connect(self._on_hoanrin_city_changed)
        self.btn_hoanrin_search.clicked.connect(self._search_hoanrin)
        self.btn_kinbo.toggled.connect(self._on_kinbo_toggled)
        self.btn_kozu.toggled.connect(self._on_kozu_toggled)
        self.tbl_hoanrin.itemSelectionChanged.connect(self._on_hoanrin_selected)
        return w

    # ------------------------------------------------------------------
    # 大字コンボ更新
    # ------------------------------------------------------------------

    def _hoanrin_api_city(self, city=None):
        city = (city if city is not None else self.combo_hoanrin_city.currentText()).strip()
        return _CITY_API_MAP.get(city, city)

    def _hoanrin_city_cache_key(self, city=None):
        api_city = self._hoanrin_api_city(city)
        return f'保安林/city/{api_city}' if api_city else ''

    def _filter_hoanrin_data_by_city(self, data, city=None):
        api_city = self._hoanrin_api_city(city)
        if data is None or not api_city:
            return data
        return [
            r for r in self._extract_records(data)
            if isinstance(r, dict) and r.get('市町村') == api_city
        ]

    def _hoanrin_scope_cities(self):
        combo = getattr(self, 'combo_hoanrin_city', None)
        cities = []
        seen = set()
        if combo is not None:
            for i in range(combo.count()):
                city = combo.itemText(i).strip()
                if city and city not in seen:
                    cities.append(city)
                    seen.add(city)
        current = combo.currentText().strip() if combo is not None else ''
        if current and current not in seen:
            cities.append(current)
        return cities

    def _hoanrin_cache_status_text(self, cities=None):
        db = self._get_db('保安林台帳')
        if db is None:
            return '取得日時: —'
        cities = cities or [self.combo_hoanrin_city.currentText().strip()]
        cities = [c for c in cities if c]
        if not cities:
            return '取得日時: —'
        timestamps = []
        for city in cities:
            key = self._hoanrin_city_cache_key(city)
            if not key:
                continue
            ts = db.get_fetched_at(key)
            if ts:
                timestamps.append(ts)
        if not timestamps:
            return '取得日時: —'
        if len(cities) == 1:
            return f'取得日時: {timestamps[0]}'
        if len(timestamps) < len(cities):
            return f'取得日時: 一部保存 {len(timestamps)}/{len(cities)}市町村'
        oldest = min(timestamps)
        newest = max(timestamps)
        if oldest == newest:
            return f'取得日時: {newest}（{len(cities)}市町村）'
        return f'取得日時: {oldest}〜{newest}（{len(cities)}市町村）'

    def _on_hoanrin_city_changed(self, city):
        self.combo_hoanrin_daiji.blockSignals(True)
        self.combo_hoanrin_daiji.clear()
        self.combo_hoanrin_daiji.addItem('（全て）', '')
        self.combo_hoanrin_daiji.blockSignals(False)
        self.lbl_hoanrin_count.setText('')
        city = city.strip()
        if not city:
            return
        api_city = _CITY_API_MAP.get(city, city)
        cd = _CITY_CD.get(api_city)
        if cd is None:
            return

        db = self._get_db('保安林台帳')
        if db is not None:
            cached, _ = db.get(f'大字/{api_city}')
            if cached is not None:
                self._on_daiji_loaded(cached)
                return

        self._post_api(
            f'{_API_BASE}/search-option-value/保安林大字',
            {'市町村CD': str(cd)},
            self._on_daiji_loaded,
        )

    def _on_daiji_loaded(self, data):
        self.combo_hoanrin_daiji.blockSignals(True)
        self.combo_hoanrin_daiji.clear()
        self.combo_hoanrin_daiji.addItem('（全て）', '')
        for item in (data or []):
            if not isinstance(item, dict):
                continue
            name = item.get('大字', '')
            if not name or '選択してください' in name:
                continue
            self.combo_hoanrin_daiji.addItem(name, name)
        self.combo_hoanrin_daiji.blockSignals(False)

        city = self.combo_hoanrin_city.currentText().strip()
        if city and data:
            api_city = _CITY_API_MAP.get(city, city)
            db = self._get_db('保安林台帳')
            if db is not None:
                db.put(f'大字/{api_city}', data)

    # ------------------------------------------------------------------
    # 検索
    # ------------------------------------------------------------------

    def _search_hoanrin(self):
        city = self.combo_hoanrin_city.currentText().strip()
        if not city:
            return
        self.tbl_hoanrin.setRowCount(0)
        self._clear_hoanrin_highlights()

        db = self._get_db('保安林台帳')
        if db is not None:
            cached, ts = db.get(self._hoanrin_city_cache_key(city))
            if cached is not None:
                self._current_raw_hoanrin = cached
                self.lbl_cache_ts.setText(f'取得日時: {ts}')
                self._filter_and_display_hoanrin()
                return
            legacy_cached, legacy_ts = db.get('保安林/all')
            if legacy_cached is not None:
                self._current_raw_hoanrin = self._filter_hoanrin_data_by_city(legacy_cached, city)
                self.lbl_cache_ts.setText(f'取得日時: 未保存（旧全体: {legacy_ts}）')
                self._filter_and_display_hoanrin()
                return

        self.btn_hoanrin_search.setEnabled(False)
        self._post_api(
            f'{_API_BASE}/advanced-search/保安林検索',
            {},
            lambda data, city=city: self._on_hoanrin_result(data, city),
        )

    def _on_hoanrin_result(self, data, city=None):
        self.btn_hoanrin_search.setEnabled(True)
        self._current_raw_hoanrin = self._filter_hoanrin_data_by_city(data, city)
        self.lbl_cache_ts.setText('取得日時: 未保存')
        self._filter_and_display_hoanrin()
        self._update_cache_btn_states()

    def _save_hoanrin_city_cache(self, city, data):
        db = self._get_db('保安林台帳')
        key = self._hoanrin_city_cache_key(city)
        if db is None or not key:
            return None
        return db.put(key, self._filter_hoanrin_data_by_city(data, city))

    def _update_hoanrin_scope_cache(self):
        cities = self._hoanrin_scope_cities()
        if not cities:
            return
        self._hoanrin_update_generation = getattr(self, '_hoanrin_update_generation', 0) + 1
        gen = self._hoanrin_update_generation
        self._hoanrin_update_total = len(cities)
        self._hoanrin_update_failed = 0
        self.btn_cache_update.setEnabled(False)
        self.btn_hoanrin_search.setEnabled(False)
        self.lbl_cache_ts.setText(f'取得日時: 更新中...（{len(cities)}市町村）')
        self._post_api(
            f'{_API_BASE}/advanced-search/保安林検索',
            {},
            lambda data, cities=cities, gen=gen: self._on_hoanrin_scope_update_result(cities, data, gen),
        )

    def _on_hoanrin_scope_update_result(self, cities, data, gen):
        if gen != getattr(self, '_hoanrin_update_generation', 0):
            return
        if data is None:
            self._hoanrin_update_failed = len(cities)
        else:
            for city in cities:
                if self._save_hoanrin_city_cache(city, data) is None:
                    self._hoanrin_update_failed += 1

        self.btn_hoanrin_search.setEnabled(True)
        current_city = self.combo_hoanrin_city.currentText().strip()
        db = self._get_db('保安林台帳')
        if db is not None and current_city:
            cached, _ = db.get(self._hoanrin_city_cache_key(current_city))
            if cached is not None:
                self._current_raw_hoanrin = cached
                self._filter_and_display_hoanrin()
        if self._hoanrin_update_failed == self._hoanrin_update_total:
            self.lbl_cache_ts.setText('取得日時: 取得失敗')
        else:
            text = self._hoanrin_cache_status_text(self._hoanrin_scope_cities())
            if self._hoanrin_update_failed:
                text += f'（{self._hoanrin_update_failed}市町村失敗）'
            self.lbl_cache_ts.setText(text)
        self._update_cache_btn_states()

    def _filter_and_display_hoanrin(self):
        all_records = self._extract_records(self._current_raw_hoanrin)

        city = self.combo_hoanrin_city.currentText().strip()
        api_city = _CITY_API_MAP.get(city, city)
        if api_city:
            all_records = [r for r in all_records
                           if isinstance(r, dict) and r.get('市町村') == api_city]

        daiji = (self.combo_hoanrin_daiji.currentData()
                 or self.combo_hoanrin_daiji.currentText().strip())
        if daiji and daiji not in ('', '（全て）'):
            all_records = [r for r in all_records
                           if isinstance(r, dict) and r.get('大字') == daiji]

        chiban = self.edit_hoanrin_chiban.text().strip()
        if chiban:
            all_records = [r for r in all_records
                           if isinstance(r, dict) and str(r.get('地番1', '')) == chiban]

        total = len(all_records)
        records = all_records[:500]

        gpkg_map = {}
        near_set = set()
        gpkg_area = {}
        layer = self._connected_layer
        if layer and not sip.isdeleted(layer) and city and self._layer_type == 'gpkg':
            req = QgsFeatureRequest().setFilterExpression(
                f'"市町村名称" = \'{city}\'')
            has_edaban = '地番_枝番' in layer.fields().names()
            for feat in layer.getFeatures(req):
                dj   = str(feat['大字名称']  or '').strip()
                pnum = str(feat['地番_親番'] or '').strip()
                enum = str(feat['地番_枝番'] or '').strip() if has_edaban else ''
                if pnum:
                    key = (dj, pnum)
                    gpkg_map.setdefault(key, set()).add(enum)
                    if len(pnum) > 1:
                        near_set.add((dj, pnum[:-1]))
                    geom = feat.geometry()
                    if geom and not geom.isEmpty():
                        ha = geom.area() / 10000
                        ak = (dj, pnum, enum)
                        gpkg_area[ak] = gpkg_area.get(ak, 0.0) + ha

        _C_BLUE   = QBrush(QColor(30,  100, 255))
        _C_YELLOW = QBrush(QColor(180, 140,   0))
        _C_RED    = QBrush(QColor(200,  50,  50))
        _C_BLACK  = QBrush(QColor(0,     0,   0))

        self.tbl_hoanrin.setRowCount(len(records))
        for row_i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            chiban1 = str(rec.get('地番1', '') or rec.get('地番_親番', '') or '')
            chiban2 = str(rec.get('地番2', '') or '')
            chiban_disp = f'{chiban1}-{chiban2}' if chiban2 and chiban2 != '0' else chiban1
            rec_daiji = str(rec.get('大字', '') or '').strip()

            key = (rec_daiji, chiban1)
            if key in gpkg_map:
                if chiban2 in gpkg_map[key]:
                    match_text, match_brush = '一致',     _C_BLUE
                else:
                    match_text, match_brush = '近似一致', _C_YELLOW
            elif len(chiban1) > 1 and (rec_daiji, chiban1[:-1]) in near_set:
                match_text, match_brush = '近傍一致', _C_RED
            else:
                match_text, match_brush = '一致無し', _C_BLACK

            cloud_area_str = str(rec.get('面積', '') or rec.get('指定面積', ''))
            gpkg_ha = gpkg_area.get((rec_daiji, chiban1, chiban2),
                      gpkg_area.get((rec_daiji, chiban1, ''), None))
            gpkg_ha_str = f'{gpkg_ha:.4f}' if gpkg_ha is not None else '—'
            try:
                ratio = float(cloud_area_str) / gpkg_ha if gpkg_ha else None
                ratio_str = f'{ratio * 100:.1f}%' if ratio is not None else '—'
            except (ValueError, TypeError):
                ratio_str = '—'

            vals = [
                str(rec.get('大字', '') or rec.get('大字名称', '')),
                str(rec.get('字',   '') or rec.get('字名称',   '')),
                chiban_disp,
                str(rec.get('保安林種別', '') or rec.get('保安林種名称', '') or rec.get('保安林種', '')),
                cloud_area_str,
                gpkg_ha_str,
                ratio_str,
            ]
            from qgis.PyQt.QtWidgets import QTableWidgetItem as _TWI
            for col, v in enumerate(vals):
                item = _TWI(' ' + v)
                item.setData(Qt.UserRole, rec)
                self.tbl_hoanrin.setItem(row_i, col, item)

            st_item = _TWI(' ' + match_text)
            st_item.setForeground(match_brush)
            st_item.setData(Qt.UserRole, rec)
            self.tbl_hoanrin.setItem(row_i, 7, st_item)

        if total > 500:
            self.lbl_hoanrin_count.setText(f'{total}件（先頭500件表示）')
        else:
            self.lbl_hoanrin_count.setText(f'{total}件')

    # ------------------------------------------------------------------
    # 行選択 → ハイライト
    # ------------------------------------------------------------------

    def _on_hoanrin_selected(self):
        self._update_kozu_btn()
        self._clear_hoanrin_highlights()
        self._clear_selection_highlights()
        rows = self.tbl_hoanrin.selectionModel().selectedRows()
        if not rows:
            self._clear_cloud_record_info()
            return
        item = self.tbl_hoanrin.item(rows[0].row(), 0)
        if not item:
            self._clear_cloud_record_info()
            return
        rec = item.data(Qt.UserRole)
        if not isinstance(rec, dict):
            self._clear_cloud_record_info()
            return
        self._show_hoanrin_record_info(rec)
        if not self._connected_layer or sip.isdeleted(self._connected_layer):
            return
        chiban  = str(rec.get('地番1', '') or rec.get('地番_親番', '') or rec.get('地番', '')).strip()
        chiban2 = str(rec.get('地番2', '') or '').strip()
        api_city  = str(rec.get('市町村', '')).strip()
        gpkg_city = _API_CITY_MAP.get(api_city, api_city)
        daiji = str(rec.get('大字', '')).strip()
        if chiban:
            self._highlight_by_chiban(chiban, gpkg_city, daiji, chiban2)
        if self.btn_kozu.isChecked():
            # kozu側のchiban形式（親番-枝番）に合わせて合成してから送信
            kozu_chiban = f'{chiban}-{chiban2}' if chiban2 and chiban2 != '0' else chiban
            self._send_to_kozu(daiji, kozu_chiban)

    def _highlight_by_chiban(self, chiban, city='', daiji='', chiban2=''):
        if self._layer_type != 'gpkg':
            return
        layer  = self._connected_layer
        canvas = self.iface.mapCanvas()
        has_edaban = '地番_枝番' in layer.fields().names()
        kinbo_on   = self.btn_kinbo.isChecked()

        def _loc():
            p = []
            if city:
                p.append(f'"市町村名称" = \'{city}\'')
            if daiji:
                p.append(f'"大字名称" = \'{daiji}\'')
            return p

        blue_feats, yellow_feats = [], []
        exact_expr = ' AND '.join([f'"地番_親番" = \'{chiban}\''] + _loc())
        for feat in layer.getFeatures(
                QgsFeatureRequest().setFilterExpression(exact_expr)):
            gpkg2 = str(feat['地番_枝番'] or '').strip() if has_edaban else ''
            (blue_feats if chiban2 == gpkg2 else yellow_feats).append(feat)

        red_feats = []
        if kinbo_on and len(chiban) > 1:
            prefix = chiban[:-1]
            red_expr = ' AND '.join(
                [f'"地番_親番" LIKE \'{prefix}_\'',
                 f'"地番_親番" != \'{chiban}\''] + _loc())
            red_feats = list(layer.getFeatures(
                QgsFeatureRequest().setFilterExpression(red_expr)))

        orange_feats = []
        if kinbo_on and len(chiban) > 2:
            prefix2 = chiban[:-2]
            orange_expr = ' AND '.join(
                [f'"地番_親番" LIKE \'{prefix2}__\'',
                 f'"地番_親番" NOT LIKE \'{chiban[:-1]}%\''] + _loc())
            orange_feats = list(layer.getFeatures(
                QgsFeatureRequest().setFilterExpression(orange_expr)))

        if blue_feats:
            to_show = [(f, _HL_BLUE_BORDER,   _HL_BLUE_FILL)   for f in blue_feats]
        elif yellow_feats or (kinbo_on and red_feats):
            to_show = ([(f, _HL_YELLOW_BORDER, _HL_YELLOW_FILL) for f in yellow_feats]
                     + ([(f, _HL_RED_BORDER,   _HL_RED_FILL)    for f in red_feats]
                        if kinbo_on else []))
        elif kinbo_on and orange_feats:
            to_show = [(f, _HL_ORANGE_BORDER, _HL_ORANGE_FILL) for f in orange_feats]
        else:
            to_show = []

        bbox = None
        for feat, border, fill in to_show:
            rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            rb.setColor(border)
            rb.setFillColor(fill)
            rb.setWidth(1)
            rb.addGeometry(feat.geometry(), layer)
            rb.show()
            self._hoanrin_highlights.append(rb)
            geom = feat.geometry()
            if geom and not geom.isEmpty():
                if bbox is None:
                    bbox = geom.boundingBox()
                else:
                    bbox.combineExtentWith(geom.boundingBox())

        if bbox and not bbox.isEmpty():
            src_crs = layer.crs()
            dst_crs = canvas.mapSettings().destinationCrs()
            if src_crs != dst_crs:
                tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                bbox = tr.transformBoundingBox(bbox)
            buf = max(bbox.width(), bbox.height()) * 0.30 + 15
            bbox.grow(buf)
            canvas.setExtent(bbox)
            canvas.refresh()

    def _clear_hoanrin_highlights(self):
        scene = self.iface.mapCanvas().scene()
        for hl in self._hoanrin_highlights:
            if not sip.isdeleted(hl):
                scene.removeItem(hl)
        self._hoanrin_highlights.clear()

    # ------------------------------------------------------------------
    # 詳細表示
    # ------------------------------------------------------------------

    def _show_hoanrin_record_info(self, rec):
        self.lbl_cloud_selected.setText('保安林台帳')
        if not isinstance(rec, dict):
            self.cloud_info_browser.clear()
            return

        hidden_exact = {
            'fid', 'FID', 'THE_FID',
            'hilight_key', 'hilight_key_name', 'hilight_layer_name',
            'hilight_point_x', 'hilight_point_y',
            'highlight_key', 'highlight_key_name', 'highlight_layer_name',
            'highlight_point_x', 'highlight_point_y',
            '総行数', '行番号',
        }
        priority_keys = [
            '保安林ID', '市町村', '大字', '字', '地番',
            '地目', '所有形態', '検索番号',
            '保安林種別', '面積',
            '皆伐_択伐限度30', '皆伐_択伐限度40', '皆伐限度',
        ]

        chiban1 = str(rec.get('地番1', '') or rec.get('地番_親番', '') or rec.get('地番', '')).strip()
        chiban2 = str(rec.get('地番2', '') or '').strip()
        chiban_disp = f'{chiban1}-{chiban2}' if chiban2 and chiban2 != '0' else chiban1

        ordered_keys = []
        seen = set()
        for key in priority_keys + list(rec.keys()):
            if key in seen:
                continue
            if key in ('地番1', '地番2', '地番_親番', '地番_枝番'):
                continue
            if key in hidden_exact or key.startswith('表示用_'):
                continue
            val = chiban_disp if key == '地番' else rec.get(key)
            if val is None or str(val) in ('', 'NULL', 'None'):
                continue
            ordered_keys.append(key)
            seen.add(key)

        parts = ['<table style="border-collapse:collapse;width:100%;">']
        for key in ordered_keys:
            val = chiban_disp if key == '地番' else rec.get(key)
            parts.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                f'{key}</td><td style="padding:1px 4px;">{val}</td></tr>')
        parts.append('</table>')
        self.cloud_info_browser.setHtml(''.join(parts))
        self.left_tab.setCurrentIndex(1)

    # ------------------------------------------------------------------
    # 公図連携
    # ------------------------------------------------------------------

    def _update_kozu_btn(self, btn=None):
        from qgis.utils import plugins as _qplugins
        btn = btn or self.btn_kozu
        installed = 'kozu_xml_integrator' in _qplugins
        btn.setEnabled(installed)
        if not installed:
            btn.setChecked(False)
            btn.setStyleSheet('color: gray;')
        else:
            on = btn.isChecked()
            btn.setText('公図連携ON' if on else '公図連携OFF')
            btn.setStyleSheet('' if on else 'color: gray;')

    def _on_kozu_toggled(self, checked, btn=None):
        btn = btn or self.btn_kozu
        btn.setText('公図連携ON' if checked else '公図連携OFF')
        btn.setStyleSheet('' if checked else 'color: gray;')
        if checked:
            from qgis.utils import plugins as _qplugins
            kozu = _qplugins.get('kozu_xml_integrator')
            if kozu:
                kozu.run()

    def _send_to_kozu(self, daiji, chiban):
        from qgis.utils import plugins as _qplugins
        kozu = _qplugins.get('kozu_xml_integrator')
        if not kozu or not kozu.main_window:
            return
        win = kozu.main_window
        win.comboOaza.setCurrentText(daiji)
        win.lineEditXmlSearch.setText(chiban)
        win._on_xml_search()

    def _on_kinbo_toggled(self, checked):
        QSettings().setValue('fcloud_shizuoka/kinbo_enabled', checked)
        self.btn_kinbo.setText('近傍データON' if checked else '近傍データOFF')
        self.btn_kinbo.setStyleSheet('color: gray;' if checked else '')
        self._on_hoanrin_selected()
