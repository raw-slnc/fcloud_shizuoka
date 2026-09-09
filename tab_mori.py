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
    Qgis, QgsMessageLog,
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsField, QgsVectorFileWriter, QgsFeatureRequest,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsNetworkAccessManager,
)
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import QVariant

from .constants import (
    _API_BASE, _MORI_MVT_ZOOM, _NORIN_OFFICES, _NENDO_LIST, _TOGGLE_BTN_QSS_LAYER,
    _MORI_FIELDS, _MORI_COL_W, _MORI_COL_W_DEFAULT,
)
from .layer_cleanup import remove_project_layer


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
        self.btn_mori_layer.setStyleSheet(_TOGGLE_BTN_QSS_LAYER)
        self.btn_mori_layer.setToolTip('森の力実施箇所のMVTポリゴンレイヤーを追加/除去')
        row.addWidget(self.btn_mori_layer)
        v.addLayout(row)

        self.tbl_mori = self._make_table([lbl for lbl, *_ in _MORI_FIELDS])
        hdr = self.tbl_mori.horizontalHeader()
        # 列数が多いので Stretch をやめ、既定幅＋横スクロールで扱う
        hdr.setSectionResizeMode(hdr.Interactive)
        hdr.setStretchLastSection(False)
        for i, (lbl, *_keys) in enumerate(_MORI_FIELDS):
            self.tbl_mori.setColumnWidth(i, _MORI_COL_W.get(lbl, _MORI_COL_W_DEFAULT))
        v.addWidget(self.tbl_mori, 1)

        bottom = QHBoxLayout()
        self.lbl_mori_count = QLabel('')
        self.lbl_mori_count.setStyleSheet('color: gray; font-size: 10px;')
        bottom.addWidget(self.lbl_mori_count, 1)
        v.addLayout(bottom)

        self.btn_mori_search.clicked.connect(self._search_mori)
        self.combo_mori_norin.activated.connect(self._on_mori_condition_changed)
        self.combo_mori_nendo.activated.connect(self._on_mori_condition_changed)
        self.combo_mori_kubun.activated.connect(self._on_mori_condition_changed)
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
        # 検索条件が変わったら形状追補の試行済みマーク・実行中の年度スイープを破棄
        self._mori_geom_fetch_attempted = set()
        self._mori_sweep_active = False
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

        self._current_mori_filter = {
            '農林事務所': norin,
            '年度': nendo,
            '事業区分': kubun or '',
        }
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
        records = self._extract_records(data)
        try:
            office_total = int(records[0].get('総行数'))
        except (TypeError, ValueError, IndexError, AttributeError):
            office_total = len(records)
        # 「（全て）」検索がサーバー上限(500)で切れている → 年度別にスイープして全件化する
        year_active = self._current_mori_filter.get('年度') is not None
        if (not year_active) and office_total > len(records) and len(records) >= 500:
            self._start_mori_year_sweep(records, office_total)
            return
        self._current_raw_mori = data
        self.lbl_cache_ts.setText('取得日時: 未保存')
        total = self._display_mori_table(data)
        self._auto_show_mori_layer(total)
        self._update_cache_btn_states()

    # ------------------------------------------------------------------
    # 年度別スイープ（500件上限の回避）
    #
    # 森の力検索 API は農林事務所単位で最大500件しか返さない（レスポンスの
    # 「総行数」に真の件数が入る。page/offset/limit 等のページング引数は全て無効）。
    # 「（全て）」検索が上限に当たったら、その事務所の年度を1年ずつ問い合わせて
    # 管理番号で重複除去しながら結合し、全件を得る。上限に当たらない事務所・
    # 年度指定済みの検索では従来どおり1リクエストで完了する。
    # ------------------------------------------------------------------

    def _start_mori_year_sweep(self, first_records, office_total):
        self._mori_sweep_active = True
        self._mori_sweep_gen += 1
        gen = self._mori_sweep_gen
        self._mori_sweep_office_total = office_total
        # 先頭500件を種として投入（年度表記が揺れて年度検索で拾えないレコードの保険）
        self._mori_sweep_records = {}
        for rec in first_records:
            if isinstance(rec, dict):
                self._mori_sweep_records.setdefault(self._mori_rec_key(rec), rec)

        base = {}
        norin = self._current_mori_filter.get('農林事務所') or ''
        if norin:
            base['農林事務所'] = norin
        kubun = self._current_mori_filter.get('事業区分') or ''
        if kubun:
            base['事業区分'] = kubun

        years = [wareki for _label, wareki in _NENDO_LIST]
        # _NENDO_LIST は 2019 を「令和1年度」とするが森の力データは「平成31年度」表記。
        # 表記揺れ対策として代替表記と、種レコードに現れた年度表記を追加する。
        for alt in ('平成31年度', '令和元年度'):
            if alt not in years:
                years.append(alt)
        for rec in first_records:
            y = str(rec.get('年度', '') or '').strip()
            if y and y not in years:
                years.append(y)
        self._mori_sweep_pending = len(years)
        self._mori_sweep_done = 0
        self.btn_mori_search.setEnabled(False)
        self.lbl_mori_count.setText(f'全年度取得中... (0/{len(years)})')
        for wareki in years:
            params = dict(base, **{'年度': str(wareki)})
            self._post_api(
                f'{_API_BASE}/advanced-search/森の力検索',
                params,
                lambda d, w=wareki, g=gen: self._on_mori_sweep_year(d, w, g),
            )

    @staticmethod
    def _mori_rec_key(rec):
        # fid はレコード（行）ごとに一意。同一管理番号で複数行あるケース
        # （環境伐＋倒木等処理など）も取りこぼさず、種と年度結果を正しく統合できる。
        fid = rec.get('fid')
        if fid not in (None, '', 'NULL'):
            return f'fid:{fid}'
        k = str(rec.get('管理番号', '') or '').strip()
        return k if k else f'row:{id(rec)}'

    def _on_mori_sweep_year(self, data, wareki, gen):
        if not self._mori_sweep_active or gen != self._mori_sweep_gen:
            return
        for rec in self._extract_records(data or []):
            if isinstance(rec, dict):
                self._mori_sweep_records.setdefault(self._mori_rec_key(rec), rec)
        self._mori_sweep_done += 1
        self.lbl_mori_count.setText(
            f'全年度取得中... ({self._mori_sweep_done}/{self._mori_sweep_pending})')
        if self._mori_sweep_done >= self._mori_sweep_pending:
            self._finish_mori_year_sweep()

    def _finish_mori_year_sweep(self):
        self._mori_sweep_active = False
        self.btn_mori_search.setEnabled(True)
        merged = list(self._mori_sweep_records.values())
        self._mori_sweep_records = {}
        # 「総行数」を事務所総数で統一（キャッシュ再読込時も分母が安定する）
        ot = self._mori_sweep_office_total or len(merged)
        for rec in merged:
            rec['総行数'] = ot
        self._current_raw_mori = merged
        self.lbl_cache_ts.setText('取得日時: 未保存')
        total = self._display_mori_table(merged)
        self._auto_show_mori_layer(total)
        self._update_cache_btn_states()

    def _on_mori_condition_changed(self, *_):
        self._mori_sweep_active = False
        self.lbl_mori_count.setText('条件変更後は検索を押してください')
        self.lbl_cache_ts.setText('取得日時: —')
        self.btn_cache_save.setEnabled(False)
        self.btn_cache_update.setEnabled(False)

    def _display_mori_table(self, data):
        records = self._extract_records(data)

        # 管轄事務所の総数（＝件数表示の分母）。整備者による絞り込み前に確定する。
        try:
            office_total = int(records[0].get('総行数'))
        except (TypeError, ValueError, IndexError, AttributeError):
            office_total = len(records)
        self._current_mori_office_total = office_total

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

        self._current_mori_display_kanri = [
            str(r.get('管理番号', '') or '').strip()
            for r in records
            if isinstance(r, dict) and str(r.get('管理番号', '') or '').strip()
        ]

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
            for col, (_lbl, *keys) in enumerate(_MORI_FIELDS):
                item = QTableWidgetItem(' ' + _get(rec, *keys))
                item.setData(Qt.UserRole, rec)
                self.tbl_mori.setItem(row_i, col, item)

        self.lbl_mori_count.setText(self._mori_count_label())
        self._apply_mori_layer_filter()
        return total

    def _mori_count_label(self, note=''):
        """件数ラベル文字列: 「表示件数/管轄事務所の総数件」（＋補足）。"""
        rows = self.tbl_mori.rowCount()
        denom = getattr(self, '_current_mori_office_total', 0) or rows
        return f'{rows}/{denom}件{note}'

    def _auto_show_mori_layer(self, total):
        if total <= 0:
            return
        if not self.btn_mori_layer.isChecked():
            self.btn_mori_layer.setChecked(True)
            return
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

        kanri_values = list(dict.fromkeys(
            getattr(self, '_current_mori_display_kanri', []) or []
        ))
        if '管理番号' in field_names and kanri_values:
            quoted = ','.join(
                f"'{self._escape_mori_sql(value)}'" for value in kanri_values
            )
            clauses.append(f'"管理番号" IN ({quoted})')
        elif '管理番号' in field_names:
            clauses.append('"管理番号" IS NULL AND "管理番号" IS NOT NULL')

        # 先にサブセットを適用してから凡例を組む。逆にすると _apply_mori_style 内の
        # layer.uniqueValues('管理番号') が「全県 or 前回検索」の管理番号を拾い、
        # 今回検索に無いものが全部「（整備者不明）」グループへ入ってしまう。
        layer.setSubsetString(' AND '.join(clauses))
        self._apply_mori_style(layer)
        self._refresh_map_canvas()
        self._fill_missing_mori_geometries()

    # ------------------------------------------------------------------
    # 検索結果にあるがレイヤー(キャッシュGPKG)に形状が無い管理番号の追補
    #
    # 実施箇所レイヤーは MVT を zoom13 で全県プリフェッチした GPKG キャッシュ。
    # 県が新年度分を MVT へ追加しても、GPKG があると再取得されないため、表には
    # 出るのに地図に形状が出ない管理番号が生じる（クラウドでは出る）。
    # ここで「表示中だがレイヤーに無い管理番号」を検出し、その hilight 座標の
    # タイルだけ取得してレイヤー／GPKG へ追記する。
    # ------------------------------------------------------------------

    def _fill_missing_mori_geometries(self):
        if getattr(self, '_mori_fill_pending', 0):
            return  # 追補取得が進行中
        if not self._mori_vector_layer_id:
            return
        layer = QgsProject.instance().mapLayer(self._mori_vector_layer_id)
        if not layer or sip.isdeleted(layer):
            return
        if layer.fields().indexOf('管理番号') < 0:
            return

        expected = [k for k in dict.fromkeys(
            getattr(self, '_current_mori_display_kanri', []) or []) if k]
        if not expected:
            return

        attempted = self._mori_geom_fetch_attempted
        # subsetString は expected の IN 句なので、走査結果 = 実在する expected 集合
        present = {str(f['管理番号'] or '').strip() for f in layer.getFeatures()}
        missing = [k for k in expected if k not in present and k not in attempted]
        if not missing:
            return
        attempted.update(missing)

        # 管理番号 -> (lon, lat)（API 生データの hilight 座標）
        pts = {}
        for rec in self._extract_records(getattr(self, '_current_raw_mori', None) or []):
            if not isinstance(rec, dict):
                continue
            k = str(rec.get('管理番号', '') or '').strip()
            if k in missing and k not in pts:
                x, y = rec.get('hilight_point_x'), rec.get('hilight_point_y')
                if x is None or y is None:
                    continue
                try:
                    pts[k] = (float(x), float(y))
                except (TypeError, ValueError):
                    pass  # 座標が数値でないレコードは追補対象外（想定内）
        if not pts:
            return

        from .mvt_loader import _lon_to_tile_x, _lat_to_tile_y
        z = _MORI_MVT_ZOOM
        want_tiles = set()
        for x, y in pts.values():
            cx, cy = _lon_to_tile_x(x, z), _lat_to_tile_y(y, z)
            # 形状がタイル境界をまたぐことがあるので 3x3 で取る
            for tx in (cx - 1, cx, cx + 1):
                for ty in (cy - 1, cy, cy + 1):
                    want_tiles.add((tx, ty))

        if len(want_tiles) > 400:
            self.lbl_mori_count.setText(
                self._mori_count_label('（未取得の形状が多数 — 「更新」で再取得してください）'))
            return

        self._mori_fill_missing = set(pts.keys())
        self._mori_fill_pending = len(want_tiles)
        self._mori_fill_received = 0
        self._mori_fill_feats = []
        self.lbl_mori_count.setText(
            self._mori_count_label(f'（形状 {len(self._mori_fill_missing)}件を追加取得中…）'))

        mvt_url = ('https://fcloud.pref.shizuoka.jp/MAP/MVT/'
                   'MAGIS.MORI_NO_CHIKARA/{z}/{x}/{y}.pbf')
        for tx, ty in want_tiles:
            url = mvt_url.replace('{z}', str(z)).replace('{x}', str(tx)).replace('{y}', str(ty))
            reply = QgsNetworkAccessManager.instance().get(QNetworkRequest(QUrl(url)))
            self._pending_replies.append(reply)
            reply.finished.connect(
                lambda r=reply, x=tx, y=ty: self._on_mori_fill_tile(r, x, y))

    def _on_mori_fill_tile(self, reply, tile_x, tile_y):
        from .mvt_loader import parse_tile
        if not self._mori_fill_pending:
            # レイヤー除去などでバッチが破棄済み
            if reply in self._pending_replies:
                self._pending_replies.remove(reply)
            reply.deleteLater()
            return
        if reply.error() == QNetworkReply.NoError:
            raw = bytes(reply.readAll())
            try:
                for f in parse_tile(raw, tile_x, tile_y, _MORI_MVT_ZOOM,
                                    'MAGIS.MORI_NO_CHIKARA'):
                    if str(f.get('管理番号', '') or '').strip() in self._mori_fill_missing:
                        self._mori_fill_feats.append(f)
            except Exception as e:
                QgsMessageLog.logMessage(
                    f'[fcloud] mori fill parse error tile({tile_x},{tile_y}): {e}',
                    level=Qgis.Warning)
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()
        self._mori_fill_received += 1
        if self._mori_fill_received >= self._mori_fill_pending:
            self._commit_mori_fill()

    def _commit_mori_fill(self):
        from collections import defaultdict
        pending_missing = set(self._mori_fill_missing)
        feats = self._mori_fill_feats
        self._mori_fill_pending = 0
        self._mori_fill_received = 0
        self._mori_fill_feats = []
        self._mori_fill_missing = set()

        layer = (QgsProject.instance().mapLayer(self._mori_vector_layer_id)
                 if self._mori_vector_layer_id else None)
        if not layer or sip.isdeleted(layer):
            return

        groups = defaultdict(list)
        meta = {}
        for f in feats:
            k = str(f.get('管理番号', '') or '').strip()
            if k not in pending_missing:
                continue
            g = QgsGeometry.fromWkt(f.get('geometry', '') or '')
            if not g or g.isEmpty():
                continue
            groups[k].append(g)
            meta.setdefault(k, f)

        fields = layer.fields()
        i_kanri = fields.indexOf('管理番号')
        i_ku = fields.indexOf('事業区分')
        i_sho = fields.indexOf('詳細区分')
        i_nen = fields.indexOf('年度')
        i_nor = fields.indexOf('農林事務所')
        i_sei = fields.indexOf('整備者名')

        new_feats = []
        for k, geoms in groups.items():
            try:
                geom = QgsGeometry.unaryUnion(geoms)
            except Exception as e:
                QgsMessageLog.logMessage(
                    f'[fcloud] mori fill unaryUnion failed for {k}: {e}',
                    level=Qgis.Warning)
                geom = geoms[0]
                for extra in geoms[1:]:
                    try:
                        geom = geom.combine(extra)
                    except Exception as e:
                        QgsMessageLog.logMessage(
                            f'[fcloud] mori fill combine failed for {k}: {e}',
                            level=Qgis.Warning)
            if not geom or geom.isEmpty():
                continue
            a = meta[k]
            qf = QgsFeature(fields)
            qf.setGeometry(geom)
            if i_kanri >= 0:
                qf.setAttribute(i_kanri, k)
            if i_ku >= 0:
                qf.setAttribute(i_ku, str(a.get('事業区分', '') or ''))
            if i_sho >= 0:
                qf.setAttribute(i_sho, str(a.get('詳細区分', '') or ''))
            if i_nen >= 0:
                qf.setAttribute(i_nen, str(a.get('年度', '') or ''))
            if i_nor >= 0:
                qf.setAttribute(i_nor, str(a.get('農林事務所', '') or ''))
            if i_sei >= 0:
                qf.setAttribute(i_sei, str(
                    a.get('申請者(整備者)_氏名', a.get('申請者_整備者_氏名', '')) or ''))
            new_feats.append(qf)

        if new_feats:
            layer.dataProvider().addFeatures(new_feats)
            layer.updateExtents()
            layer.reload()
            # スタイル・サブセットを貼り直して追加分を反映（管理番号は既に
            # IN 句・スタイル規則に含まれるため、再帰しても missing は空になる）
            self._apply_mori_layer_filter()

        got = {k for k in groups}
        still = sorted(pending_missing - got)
        if still:
            self.lbl_mori_count.setText(
                self._mori_count_label(f'（形状データなし {len(still)}件）'))
            QgsMessageLog.logMessage(
                '[fcloud] 森の力: MVT に形状が見つからない管理番号: ' + ', '.join(still),
                level=Qgis.Info)
        else:
            self.lbl_mori_count.setText(self._mori_count_label())

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
            layer = QgsProject.instance().mapLayer(self._mori_vector_layer_id)
            if layer is not None and layer.featureCount() == 0:
                remove_project_layer(QgsProject.instance(), self._mori_vector_layer_id)
                layer = None
                self._mori_vector_layer_id = None
            if layer is not None and self._set_layer_visible(self._mori_vector_layer_id, True):
                self._apply_mori_layer_filter()
                self._refresh_map_canvas()
                return
            self._mori_vector_layer_id = None
        if self._mori_loading:
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
        if layer.featureCount() == 0:
            self._start_mori_mvt_fetch()
            return
        # スタイル付与は _apply_mori_layer_filter() に任せる（サブセット適用後）
        visible = (self.btn_mori_layer.isChecked()
                   and self.cloud_tab.currentIndex() == 3)
        self._add_layer_above_gpkg(layer, visible=visible)
        self._mori_vector_layer_id = layer.id()
        try:
            import datetime
            mt = datetime.datetime.fromtimestamp(os.path.getmtime(gpkg_path))
            self.lbl_cache_ts.setText(f'レイヤーキャッシュ: {mt:%Y-%m-%d %H:%M}')
        except OSError:
            pass  # mtime が取れなくてもラベルを更新しないだけ（想定内）
        self._apply_mori_layer_filter()

    def _start_mori_mvt_fetch(self):
        if self._mori_loading:
            return
        self._mori_loading = True
        # 全県フルフェッチなので、ピンポイント追補の試行済みマークは意味を失う
        self._mori_geom_fetch_attempted = set()
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
        if not self._mori_loading:
            if reply in self._pending_replies:
                self._pending_replies.remove(reply)
            reply.deleteLater()
            return
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
        self._mori_loading = False
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

        save_layer = self._dissolve_mori_features_by_kanri(layer, feats_to_add)
        save_layer.setName('fcloud_森の力実施箇所')
        # スタイル付与は _apply_mori_layer_filter() に任せる（サブセット適用後）

        gpkg = self._get_mori_gpkg_path()
        if gpkg and not feats_to_add:
            print('[fcloud] 森の力MVT: 0件取得のためGPKGキャッシュへの保存をスキップしました（タイル取得に失敗した可能性があります）')
        elif gpkg:
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
                   and self.cloud_tab.currentIndex() == 3)
        self._add_layer_above_gpkg(save_layer, visible=visible)
        self._mori_vector_layer_id = save_layer.id()
        self._apply_mori_layer_filter()
        self.btn_mori_layer.setEnabled(True)
        self.lbl_mori_count.setText(
            self._mori_count_label() if self.tbl_mori.rowCount() else '')

    def _dissolve_mori_features_by_kanri(self, template_layer, features):
        # Processing の一時出力を使わず、管理番号単位で統合して名前付きメモリレイヤーを作る。
        self.lbl_mori_count.setText('ポリゴン統合中...')
        layer = QgsVectorLayer('Polygon?crs=EPSG:4326', 'fcloud_森の力実施箇所', 'memory')
        pr = layer.dataProvider()
        pr.addAttributes(template_layer.fields())
        layer.updateFields()

        groups = {}
        order = []
        for feat in features:
            attrs = feat.attributes()
            kanri = str(attrs[0] if attrs else '').strip()
            if not kanri:
                kanri = f'__empty_{len(order)}'
            if kanri not in groups:
                groups[kanri] = {'attrs': attrs, 'geoms': []}
                order.append(kanri)
            geom = feat.geometry()
            if geom and not geom.isEmpty():
                groups[kanri]['geoms'].append(QgsGeometry(geom))

        out_features = []
        for kanri in order:
            item = groups[kanri]
            geoms = item['geoms']
            if not geoms:
                continue
            try:
                geom = QgsGeometry.unaryUnion(geoms)
            except Exception as e:
                print(f'[fcloud] mori unaryUnion failed for {kanri}: {e}')
                geom = geoms[0]
                for extra in geoms[1:]:
                    try:
                        geom = geom.combine(extra)
                    except Exception as e:
                        QgsMessageLog.logMessage(
                            f'[fcloud] mori geometry combine failed for {kanri}: {e}',
                            level=Qgis.Warning)
            if not geom or geom.isEmpty():
                continue
            qf = QgsFeature(layer.fields())
            qf.setGeometry(geom)
            qf.setAttributes(item['attrs'])
            out_features.append(qf)

        pr.addFeatures(out_features)
        layer.updateExtents()
        return layer

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
            remove_project_layer(QgsProject.instance(), self._mori_vector_layer_id)
            self._mori_vector_layer_id = None
        self._mori_layer_features = []
        self._mori_tiles_pending = 0
        self._mori_tiles_received = 0
        self._mori_loading = False
        self._mori_fill_missing = set()
        self._mori_fill_pending = 0
        self._mori_fill_received = 0
        self._mori_fill_feats = []
        self._refresh_map_canvas()

    def _clear_mori_markers(self):
        canvas = self.iface.mapCanvas()
        scene = canvas.scene()
        for m in self._mori_markers:
            if not sip.isdeleted(m):
                scene.removeItem(m)
        self._mori_markers.clear()

    # ------------------------------------------------------------------
    # 行選択 → 情報パネル / ズーム
    # ------------------------------------------------------------------

    def _show_mori_record_info(self, rec):
        """森の力レコードを左「クラウド情報」パネルへ表示する。
        森林クラウド公開システムに合わせ、_MORI_FIELDS の全項目を
        （空欄もそのまま）固定の並びで出す。"""
        from html import escape
        self.lbl_cloud_selected.setText('森の力')
        if not isinstance(rec, dict):
            self.cloud_info_browser.clear()
            return

        def _val(keys):
            for k in keys:
                v = rec.get(k)
                if v is not None and str(v) not in ('', 'NULL', 'None'):
                    return str(v)
            return ''

        parts = ['<table style="border-collapse:collapse;width:100%;">']
        for lbl, *keys in _MORI_FIELDS:
            parts.append(
                '<tr>'
                '<td style="color:gray;padding:2px 8px 2px 4px;white-space:nowrap;'
                f'vertical-align:top;">{escape(lbl)}</td>'
                f'<td style="padding:2px 4px;">{escape(_val(keys))}</td>'
                '</tr>')
        parts.append('</table>')
        self.cloud_info_browser.setHtml(''.join(parts))
        self.left_tab.setCurrentIndex(1)

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
        rec = item.data(Qt.UserRole)
        if not isinstance(rec, dict):
            self._clear_cloud_record_info()
            return
        self._show_mori_record_info(rec)

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
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f'[fcloud] mori bbox unaryUnion failed: {e}',
                        level=Qgis.Warning)
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
