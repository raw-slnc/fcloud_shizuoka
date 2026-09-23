# -*- coding: utf-8 -*-
import collections

import sip

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton,
)
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QColor, QBrush
from qgis.core import (
    QgsProject, QgsFeatureRequest,
    QgsCoordinateTransform, QgsWkbTypes,
)
from qgis.gui import QgsRubberBand

from .constants import (
    _API_BASE, _CITY_API_MAP, _API_CITY_MAP, _CD_CITY, _CITY_CD,
    _HOANRIN_CITIES, _TOGGLE_BTN_QSS_ONOFF,
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

# テーブル「一致データの有無」列の文字色
_MATCH_BRUSH = {
    '一致':     QBrush(QColor(30,  100, 255)),
    '近似一致': QBrush(QColor(180, 140,   0)),
    '近傍一致': QBrush(QColor(200,  50,  50)),
    '一致無し': QBrush(QColor(0,     0,   0)),
}


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
        # 現行市町村の固定リスト。接続レイヤーの市町村属性からは作らない
        # （「計画図情報で絞込」ONのときだけレイヤーに出る市町村へ絞る）
        self._apply_hoanrin_city_filter(None)
        row.addWidget(self.combo_hoanrin_city, 3)
        row.addWidget(QLabel('大字:'))
        self.combo_hoanrin_daiji = QComboBox()
        self.combo_hoanrin_daiji.setEditable(True)
        self.combo_hoanrin_daiji.setInsertPolicy(QComboBox.NoInsert)
        self.combo_hoanrin_daiji.addItem('（全て）', '')
        row.addWidget(self.combo_hoanrin_daiji, 3)
        row.addWidget(QLabel('字:'))
        self.combo_hoanrin_koaza = QComboBox()
        self.combo_hoanrin_koaza.setEditable(True)
        self.combo_hoanrin_koaza.setInsertPolicy(QComboBox.NoInsert)
        self.combo_hoanrin_koaza.addItem('（全て）', '')
        self.combo_hoanrin_koaza.setToolTip(
            '大字の中をさらに字で絞り込む。検索を実行すると、その大字に含まれる'
            '字が候補に入る（字は打ち込みでも可）')
        row.addWidget(self.combo_hoanrin_koaza, 2)
        row.addWidget(QLabel('種別:'))
        self.combo_hoanrin_shubetsu = QComboBox()
        self.combo_hoanrin_shubetsu.setEditable(True)
        self.combo_hoanrin_shubetsu.setInsertPolicy(QComboBox.NoInsert)
        self.combo_hoanrin_shubetsu.addItem('（全て）', '')
        self.combo_hoanrin_shubetsu.setToolTip(
            '保安林種別で絞り込む。検索を実行すると、その市町村にある種別が'
            '候補に入る（多い順）')
        row.addWidget(self.combo_hoanrin_shubetsu, 2)
        row.addWidget(QLabel('地番:'))
        self.edit_hoanrin_chiban = QLineEdit()
        self.edit_hoanrin_chiban.setPlaceholderText('番号')
        self.edit_hoanrin_chiban.setFixedWidth(60)
        self.edit_hoanrin_chiban.returnPressed.connect(self._search_hoanrin)
        row.addWidget(self.edit_hoanrin_chiban)
        self.btn_hoanrin_search = QPushButton('検索')
        row.addWidget(self.btn_hoanrin_search)
        _kinbo_init = QSettings().value('fcloud_shizuoka/kinbo_enabled', False, type=bool)
        self.btn_kinbo = QPushButton('近傍データOFF')
        self.btn_kinbo.setCheckable(True)
        self.btn_kinbo.setStyleSheet(_TOGGLE_BTN_QSS_ONOFF)
        # ラベルは「近傍データON」⇔「近傍データOFF」で切り替わるが、幅は広い方（OFF）で固定
        self.btn_kinbo.setFixedWidth(self.btn_kinbo.sizeHint().width())
        self.btn_kinbo.setChecked(_kinbo_init)
        self.btn_kinbo.setText('近傍データON' if _kinbo_init else '近傍データOFF')
        self.btn_kinbo.setToolTip('黄・赤のハイライト（近似/近傍一致）を表示する')
        row.addWidget(self.btn_kinbo)
        self.btn_kozu = QPushButton('公図連携OFF')
        self.btn_kozu.setCheckable(True)
        self.btn_kozu.setStyleSheet(_TOGGLE_BTN_QSS_ONOFF)
        # ラベルは「公図連携ON」⇔「公図連携OFF」で切り替わるが、幅は広い方（OFF）で固定
        self.btn_kozu.setFixedWidth(self.btn_kozu.sizeHint().width())
        self.btn_kozu.setToolTip('ONにすると行選択のたびに自動で Kozu XML Integrator へ送信')
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
        self.chk_hoanrin_city_in_layer = QCheckBox('計画図情報で絞込')
        self.chk_hoanrin_city_in_layer.setChecked(True)
        self.chk_hoanrin_city_in_layer.setToolTip(
            'ONのとき、接続した計画図レイヤーに含まれる市町村だけを市町村の'
            '候補に出す。OFFで静岡県の全市町村を出す。')
        bottom_row.addWidget(self.chk_hoanrin_city_in_layer)
        hint = QLabel('行選択/矢印キー → ハイライト')
        hint.setStyleSheet('color: gray; font-size: 10px;')
        bottom_row.addWidget(hint)
        v.addLayout(bottom_row)

        self.combo_hoanrin_city.currentTextChanged.connect(self._on_hoanrin_city_changed)
        # 市町村・大字・字・種別コンボで Enter → 検索実行（検索ボタンを押しに行かなくてよい）
        self.combo_hoanrin_city.lineEdit().returnPressed.connect(self._search_hoanrin)
        self.combo_hoanrin_daiji.lineEdit().returnPressed.connect(self._search_hoanrin)
        self.combo_hoanrin_koaza.lineEdit().returnPressed.connect(self._search_hoanrin)
        self.combo_hoanrin_shubetsu.lineEdit().returnPressed.connect(self._search_hoanrin)
        self.combo_hoanrin_daiji.currentTextChanged.connect(self._on_hoanrin_daiji_changed)
        self.combo_hoanrin_koaza.currentTextChanged.connect(self._on_hoanrin_koaza_changed)
        self.chk_hoanrin_city_in_layer.toggled.connect(self._on_hoanrin_city_scope_toggled)
        self.btn_hoanrin_search.clicked.connect(self._search_hoanrin)
        self.btn_kinbo.toggled.connect(self._on_kinbo_toggled)
        self.btn_kozu.toggled.connect(self._on_kozu_toggled)
        self.tbl_hoanrin.itemSelectionChanged.connect(self._on_hoanrin_selected)
        return w

    def _on_hoanrin_city_scope_toggled(self, _checked):
        self._apply_hoanrin_city_filter(getattr(self, '_connected_layer', None))

    def _apply_hoanrin_city_filter(self, layer):
        """市町村コンボを現行市町村の固定リスト（_HOANRIN_CITIES）で作り直す。
        「計画図情報で絞込」チェックONかつ layer が渡された場合は、そのレイヤーの
        市町村属性に現れる市町村だけに絞る（判定は図形でなく属性の文字列一致。
        市町村名称＝固定名がその値に含まれるか、市町村CD＝_CD_CITY で名前へ変換）。
        チェックOFF・市町村属性なし・一致ゼロ・レイヤー未接続は絞らず全件。"""
        allowed = None
        chk = getattr(self, 'chk_hoanrin_city_in_layer', None)
        scope_on = chk is None or chk.isChecked()
        if scope_on and layer is not None and not sip.isdeleted(layer):
            fnames = [f.name() for f in layer.fields()]
            if '市町村名称' in fnames:
                idx = layer.fields().indexOf('市町村名称')
                vals = [
                    str(v) for v in layer.uniqueValues(idx)
                    if v is not None and str(v) not in ('', 'NULL')
                ]
                allowed = {c for c in _HOANRIN_CITIES if any(c in v for v in vals)}
            elif '市町村CD' in fnames:
                idx = layer.fields().indexOf('市町村CD')
                here = set()
                for v in layer.uniqueValues(idx):
                    try:
                        nm = _CD_CITY.get(int(str(v).strip()), '')
                    except (TypeError, ValueError):
                        continue
                    if nm:
                        here.add(_API_CITY_MAP.get(nm, nm))
                allowed = {c for c in _HOANRIN_CITIES if c in here}
            if not allowed:
                allowed = None

        prev = self.combo_hoanrin_city.currentText()
        self.combo_hoanrin_city.blockSignals(True)
        self.combo_hoanrin_city.clear()
        for c in _HOANRIN_CITIES:
            if allowed is None or c in allowed:
                self.combo_hoanrin_city.addItem(c)
        i = self.combo_hoanrin_city.findText(prev)
        self.combo_hoanrin_city.setCurrentIndex(i if i >= 0 else -1)
        if i < 0:
            self.combo_hoanrin_city.setEditText('')
        self.combo_hoanrin_city.blockSignals(False)
        # 初回ビルド時は大字コンボ未生成のためスキップ（レイヤー接続時に呼ばれる）
        if hasattr(self, 'combo_hoanrin_daiji'):
            self._on_hoanrin_city_changed(self.combo_hoanrin_city.currentText())

    @staticmethod
    def _hoanrin_chiban_disp(rec):
        """保安林レコードの地番を「親番[-枝番1[-枝番2]]」で表す（地番1/2/3）。"""
        p1 = str(rec.get('地番1', '') or rec.get('地番_親番', '') or rec.get('地番', '')).strip()
        p2 = str(rec.get('地番2', '') or '').strip()
        p3 = str(rec.get('地番3', '') or '').strip()
        parts = [p1]
        if p2 and p2 != '0':
            parts.append(p2)
            if p3 and p3 != '0':
                parts.append(p3)
        return '-'.join(p for p in parts if p)

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
        self._reset_hoanrin_combo(getattr(self, 'combo_hoanrin_koaza', None))
        self._reset_hoanrin_combo(getattr(self, 'combo_hoanrin_shubetsu', None))
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

    def _on_hoanrin_daiji_changed(self, _text):
        # 大字が変わったら字も種別もリセット。次の検索で、その大字に実在する
        # 字／種別だけが候補に入り直す。
        self._reset_hoanrin_combo(getattr(self, 'combo_hoanrin_koaza', None))
        self._reset_hoanrin_combo(getattr(self, 'combo_hoanrin_shubetsu', None))

    def _on_hoanrin_koaza_changed(self, _text):
        # 字が変わったら種別をリセット。次の検索で、その字に実在する種別だけが
        # 候補に入り直す。
        self._reset_hoanrin_combo(getattr(self, 'combo_hoanrin_shubetsu', None))

    @staticmethod
    def _reset_hoanrin_combo(combo):
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('（全て）', '')
        combo.setCurrentIndex(0)
        combo.blockSignals(False)

    @staticmethod
    def _refill_hoanrin_combo(combo, scope_records, field, by_count=False):
        """scope_records に出てくる field の値をコンボの候補にする。
        現在の選択が新しい候補にもあれば維持、なければ（全て）へ戻す。
        by_count=True で件数の多い順、False で名前順。"""
        if combo is None:
            return
        cur = combo.currentText().strip()
        ctr = collections.Counter(
            str(r.get(field, '') or '').strip()
            for r in scope_records if isinstance(r, dict)
        )
        ctr.pop('', None)
        names = [k for k, _ in ctr.most_common()] if by_count else sorted(ctr)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('（全て）', '')
        for n in names:
            combo.addItem(n, n)
        if cur and cur not in ('', '（全て）') and cur in ctr:
            combo.setCurrentText(cur)
        else:
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

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

    @classmethod
    def _hoanrin_match_label(cls, rec, gpkg_map, near_set, kinbo_on):
        """1レコードの「一致データの有無」ラベルを返す。
        近傍一致は「近傍データ」ボタンON時のみ（地図ハイライトと連動）。"""
        chiban1 = str(rec.get('地番1', '') or rec.get('地番_親番', '') or '')
        chiban2 = str(rec.get('地番2', '') or '')
        rec_daiji = cls._strip_ward_prefix(str(rec.get('大字', '') or '').strip())
        key = (rec_daiji, chiban1)
        if key in gpkg_map:
            return '一致' if chiban2 in gpkg_map[key] else '近似一致'
        if kinbo_on and len(chiban1) > 1 and (rec_daiji, chiban1[:-1]) in near_set:
            return '近傍一致'
        return '一致無し'

    def _resync_hoanrin_match_column(self):
        """近傍データON/OFF切り替え時に、テーブルを作り直さず「一致データの有無」
        列だけ塗り替える（地図ハイライトと表示を即時に合わせる）。"""
        gpkg_map = getattr(self, '_hoanrin_gpkg_map', None)
        near_set = getattr(self, '_hoanrin_near_set', None)
        if gpkg_map is None or near_set is None:
            return
        kinbo_on = self.btn_kinbo.isChecked()
        tbl = self.tbl_hoanrin
        for row_i in range(tbl.rowCount()):
            it = tbl.item(row_i, 7)
            if it is None:
                continue
            rec = it.data(Qt.UserRole)
            if not isinstance(rec, dict):
                continue
            text = self._hoanrin_match_label(rec, gpkg_map, near_set, kinbo_on)
            it.setText(' ' + text)
            it.setForeground(_MATCH_BRUSH[text])

    def _filter_and_display_hoanrin(self):
        all_records = self._extract_records(self._current_raw_hoanrin)

        city = self.combo_hoanrin_city.currentText().strip()
        api_city = _CITY_API_MAP.get(city, city)
        if api_city:
            all_records = [r for r in all_records
                           if isinstance(r, dict) and r.get('市町村') == api_city]

        # 全件数（下部ラベルの分母）＝市町村で検索しただけの筆数。以降の絞り込みで不変
        city_scope = list(all_records)

        daiji = (self.combo_hoanrin_daiji.currentData()
                 or self.combo_hoanrin_daiji.currentText().strip())
        if daiji and daiji not in ('', '（全て）'):
            # 大字コンボは区名なし（「相俣」）だが保安林台帳のレコードは政令市だと
            # 区名付き（「葵区相俣」）。区名を落とした比較も許容する。
            all_records = [
                r for r in all_records
                if isinstance(r, dict)
                and (r.get('大字') == daiji
                     or self._strip_ward_prefix(str(r.get('大字', '') or '')) == daiji)
            ]

        # 字コンボの候補は「市町村＋大字」で絞った集合から（字で絞る前）
        koaza_scope = list(all_records)
        koaza = (self.combo_hoanrin_koaza.currentData()
                 or self.combo_hoanrin_koaza.currentText().strip())
        if koaza and koaza not in ('', '（全て）'):
            all_records = [
                r for r in all_records
                if isinstance(r, dict)
                and str(r.get('字', '') or '').strip() == koaza
            ]

        # 種別コンボの候補は「市町村＋大字＋字」で絞った集合から（種別で絞る前）。
        # 大字・字を変えるたびに、実在する種別だけに入れ替わる
        shubetsu_scope = list(all_records)
        shubetsu = (self.combo_hoanrin_shubetsu.currentData()
                    or self.combo_hoanrin_shubetsu.currentText().strip())
        if shubetsu and shubetsu not in ('', '（全て）'):
            all_records = [
                r for r in all_records
                if isinstance(r, dict)
                and str(r.get('保安林種別', '') or '').strip() == shubetsu
            ]

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
        if (layer and not sip.isdeleted(layer) and records
                and self._layer_type == 'gpkg'
                and '大字名称' in layer.fields().names()):
            # 森林簿GPKGの市町村名称は「旧清沢村」等の飾り付きで city と一致しないため、
            # 市町村でなく大字で GPKG を絞る。保安林レコードの大字は「葵区相俣」等の
            # 区名付き、GPKGの大字名称は「相俣」のことがあるので、両綴りで問い合わせ、
            # gpkg_map のキーは区名を落とした大字にそろえる。
            want = set()
            for r in records:
                if not isinstance(r, dict):
                    continue
                d = str(r.get('大字', '') or '').strip()
                if d:
                    want.add(d)
                    want.add(self._strip_ward_prefix(d))
            want.discard('')
            if want:
                quoted = ','.join(
                    "'" + d.replace("'", "''") + "'" for d in want)
                req = QgsFeatureRequest().setFilterExpression(
                    f'"大字名称" IN ({quoted})')
                has_edaban = '地番_枝番' in layer.fields().names()
                for feat in layer.getFeatures(req):
                    dj   = self._strip_ward_prefix(str(feat['大字名称'] or '').strip())
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

        self.tbl_hoanrin.setRowCount(len(records))
        kinbo_on = self.btn_kinbo.isChecked()
        for row_i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            chiban1 = str(rec.get('地番1', '') or rec.get('地番_親番', '') or '')
            chiban2 = str(rec.get('地番2', '') or '')
            # 表示は親番-枝番1-枝番2（地番3）まで。GPKG照合キーは親番/枝番1のみ
            chiban_disp = self._hoanrin_chiban_disp(rec)
            # GPKG側の大字名称（区名なし）にそろえるため区名を落とす
            rec_daiji = self._strip_ward_prefix(str(rec.get('大字', '') or '').strip())

            match_text = self._hoanrin_match_label(rec, gpkg_map, near_set, kinbo_on)
            match_brush = _MATCH_BRUSH[match_text]

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

        # 全件数＝市町村で検索しただけ（大字・字・種別・地番で絞る前）の筆数
        grand = len(city_scope)
        if total > 500:
            self.lbl_hoanrin_count.setText(
                f'表示上限 500件/{grand}件 ； ※絞り込みを行ってください')
        else:
            self.lbl_hoanrin_count.setText(f'{total}件/{grand}件')

        self._refill_hoanrin_combo(self.combo_hoanrin_koaza, koaza_scope, '字')
        self._refill_hoanrin_combo(
            self.combo_hoanrin_shubetsu, shubetsu_scope, '保安林種別', by_count=True)

        # 近傍データON/OFFトグル時に列だけ塗り替えるための素材を保持
        self._hoanrin_gpkg_map = gpkg_map
        self._hoanrin_near_set = near_set

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
            # kozu側のchiban形式に合わせて親番-枝番1-枝番2（地番3）まで合成して送信
            self._send_to_kozu(gpkg_city, daiji, self._hoanrin_chiban_disp(rec))

    def _highlight_by_chiban(self, chiban, city='', daiji='', chiban2=''):
        if self._layer_type != 'gpkg':
            return
        layer  = self._connected_layer
        canvas = self.iface.mapCanvas()
        has_edaban = '地番_枝番' in layer.fields().names()
        kinbo_on   = self.btn_kinbo.isChecked()

        # 森林簿型GPKGの市町村名称は「旧静岡市、旧美和村（旧静岡市）」等の飾り付きの
        # ことがあり、現行名 city と完全一致しない。その場合は市町村条件を外して
        # 大字名称だけで照合する（森林簿の大字名称は区名付きで市町村を特定できる）。
        _city_ok = False
        if city and '市町村名称' in layer.fields().names():
            fid = layer.id()
            if getattr(self, '_hoanrin_city_val_cache_id', None) != fid:
                _idx = layer.fields().indexOf('市町村名称')
                self._hoanrin_city_val_cache = {
                    str(v) for v in layer.uniqueValues(_idx) if v is not None}
                self._hoanrin_city_val_cache_id = fid
            _city_ok = city in self._hoanrin_city_val_cache

        _daiji_bare = self._strip_ward_prefix(daiji)

        def _loc():
            p = []
            if city and _city_ok:
                p.append(f'"市町村名称" = \'{city}\'')
            if daiji:
                # レコードの大字は「葵区相俣」等の区名付き、計画図側は「相俣」の
                # ことがある。両綴りで照合する。OGR/GPKG では
                # （A OR B）と LIKE を併用すると0件になる不具合があるため IN を使う。
                if _daiji_bare != daiji:
                    p.append(f'"大字名称" IN (\'{daiji}\', \'{_daiji_bare}\')')
                else:
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
        if not isinstance(rec, dict):
            self._clear_cloud_record_info()
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

        chiban_disp = self._hoanrin_chiban_disp(rec)

        ordered_keys = []
        seen = set()
        for key in priority_keys + list(rec.keys()):
            if key in seen:
                continue
            if key in ('地番1', '地番2', '地番3', '地番_親番', '地番_枝番'):
                continue
            if key in hidden_exact or key.startswith('表示用_'):
                continue
            val = chiban_disp if key == '地番' else rec.get(key)
            if val is None or str(val) in ('', 'NULL', 'None'):
                continue
            ordered_keys.append(key)
            seen.add(key)

        parts = ['<table style="border-collapse:collapse;width:100%;">',
                 self._info_title_row('保安林台帳')]
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
        else:
            btn.setText('公図連携ON' if btn.isChecked() else '公図連携OFF')

    def _on_kozu_toggled(self, checked, btn=None):
        btn = btn or self.btn_kozu
        btn.setText('公図連携ON' if checked else '公図連携OFF')
        if checked:
            from qgis.utils import plugins as _qplugins
            kozu = _qplugins.get('kozu_xml_integrator')
            if kozu:
                kozu.run()

    @staticmethod
    def _norm_muni(name):
        """市町村名を正規化して Kozu の市町村名と突き合わせられる形にする。
          ・郡/県プレフィックス除去（賀茂郡西伊豆町→西伊豆町、静岡県下田市→下田市）
          ・森林簿GPKGの飾り付き値を現行名へ寄せる:
              「島田市　　※旧島田市（金谷町含む）」→ 島田市
              「旧静岡市、旧美和村（旧静岡市）」   → 静岡市
            （※/、/, で区切って先頭、末尾の（…）(…) を除去、先頭の「旧」を除去）
        Kozu の normalize_municipality_name 相当＋森林簿の表記ゆれ吸収。
        なお「旧井川村」のように現行名の手掛かりが無い値は「井川村」になり、
        現行市町村（静岡市）とは一致しない（＝安全側で連携見送り）。"""
        import re
        s = str(name or '').strip()
        s = re.split(r'[、,※]', s, 1)[0]
        s = re.sub(r'[（(].*?[）)]', '', s)
        s = re.sub(r'^.*?郡', '', s)
        s = re.sub(r'^.*?県', '', s)
        s = s.replace(' ', '').replace('　', '')
        s = re.sub(r'^旧', '', s)
        return s

    _KOZU_WARDS = ('葵区', '駿河区', '清水区', '天竜区', '中央区', '浜名区',
                   '中区', '東区', '西区', '南区', '北区', '浜北区')

    @classmethod
    def _strip_ward_prefix(cls, daiji):
        """保安林台帳の大字は「葵区井川」のように政令市の区名が付く。Kozu（法務局
        公図XML）側の大字は区名なし（井川）なので、照合前に先頭の区名を落とす。
        森林簿GPKGの大字名称は元々区名なしのため無影響。"""
        d = str(daiji or '')
        for w in cls._KOZU_WARDS:
            if d.startswith(w):
                return d[len(w):]
        return d

    def _kozu_target_status(self, kozu, city, daiji):
        """Kozu が現在読み込んでいる DB に (city, daiji) の筆ポリゴンがあるか判定する。
        戻り値 (ok, message, keep_ids)。
          ok=False : message を案内ダイアログに使う（送信キャンセル）
          ok=True  : keep_ids は「対象市町村×対象大字」の xml_meta_id 集合。
                     None のときは絞り込みなし（市町村不明／判定不能）。"""
        win = getattr(kozu, 'main_window', None)
        db = getattr(win, 'db', None) if win is not None else None
        if db is None:
            return False, 'Kozu XML Integrator に地図データ（DB）が読み込まれていません。', None
        try:
            with db.connection() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT m.id, m.municipality_name "
                    "FROM t_xml_meta m JOIN t_fude_poly f ON f.xml_meta_id = m.id "
                    "WHERE f.oaza_name = ?",
                    (daiji,),
                ).fetchall()
        except Exception:
            return True, '', None
        pairs = [(r[0], r[1]) for r in rows if r and r[0] is not None]
        munis = sorted({m for _, m in pairs if m})
        disp_city = self._norm_muni(city)  # 飾りを落とした表示用の市町村名
        where = f'市町村「{disp_city}」大字「{daiji}」' if disp_city else f'大字「{daiji}」'
        if not munis:
            return False, f'{where}が現在の Kozu XML Integrator の DB に存在しません。', None
        if not city:
            return True, '', None
        tgt = disp_city
        keep = set()
        for mid, m in pairs:
            nm = self._norm_muni(m)
            if not nm or not tgt:
                continue
            # 完全一致／政令市＋区（静岡市⊂静岡市葵区）／その逆向き
            if nm == tgt or nm.startswith(tgt) or tgt.startswith(nm):
                keep.add(mid)
        if keep:
            return True, '', keep
        return False, (f'{where}が現在の Kozu XML Integrator の DB に存在しません'
                       f'（同名大字は {"・".join(munis)} の分のみ）。'), None

    def _send_to_kozu(self, city, daiji, chiban):
        from qgis.utils import plugins as _qplugins
        from qgis.PyQt.QtWidgets import QMessageBox
        kozu = _qplugins.get('kozu_xml_integrator')
        if not kozu or not kozu.main_window:
            return
        daiji = self._strip_ward_prefix(daiji)  # 「葵区井川」→「井川」（Kozu側は区名なし）
        ok, message, keep_ids = self._kozu_target_status(kozu, city, daiji)
        if not ok:
            QMessageBox.information(
                self, '公図連携',
                message + '\n\n該当地域の XML を取り込んだ DB を Kozu XML Integrator 側で'
                '作成・追加してから再度お試しください。')
            return
        win = kozu.main_window
        win.comboOaza.setCurrentText(daiji)
        # 同名大字が複数市町村にある場合に他市町村の筆を拾わないよう、
        # 対象市町村の XML だけをツリーに残してから検索させる
        if keep_ids is not None:
            tree = getattr(win, 'treeXmlFiles', None)
            if tree is not None:
                for i in range(tree.topLevelItemCount() - 1, -1, -1):
                    it = tree.topLevelItem(i)
                    if it.data(0, Qt.UserRole) not in keep_ids:
                        tree.takeTopLevelItem(i)
        win.lineEditXmlSearch.setText(chiban)
        win._on_xml_search()

    def _on_kinbo_toggled(self, checked):
        QSettings().setValue('fcloud_shizuoka/kinbo_enabled', checked)
        self.btn_kinbo.setText('近傍データON' if checked else '近傍データOFF')
        # テーブルの「一致データの有無」列と地図ハイライトを即時に合わせる
        self._resync_hoanrin_match_column()
        self._on_hoanrin_selected()
