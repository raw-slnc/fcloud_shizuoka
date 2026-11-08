# -*- coding: utf-8 -*-
import csv
import math
import os
from qgis.PyQt import sip
import struct

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView,
    QHeaderView, QFrame, QPushButton, QAbstractItemView, QLabel,
    QFileDialog, QMessageBox,
)
from qgis.PyQt.QtCore import Qt, QAbstractTableModel, QModelIndex
from qgis.core import (
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsProject, QgsPointXY, QgsFeatureRequest,
)

from .constants import (
    _API_BASE, _TOGGLE_BTN_QSS_ONOFF,
    _PRIMARY_FIELDS, _HISTORY_FIELDS, _MORINBO_OLD_CD_TO_CITY, _SHINRINBO_COL_WIDTHS,
    _SHINRINBO_CLOUD_FIELDS,
)

_MVT_BASE = 'https://fcloud.pref.shizuoka.jp'
_MVT_PATH = '/MAP/MVT/MAGIS.SHOHAN_SHINRINBO.5JOU'
_MAX_SHINRINBO_REQUESTS = 6

# API応答は、本家サイトと同じ列順の共通定義で表示する。
_API_TABLE_COLS = _SHINRINBO_CLOUD_FIELDS

_API_CENTER_SOURCES = {'林班', '準林班', '小班ラベル', '小班_枝番'}
_API_RIGHT_SOURCES = {
    '混合歩合', '林齢', '齢級',
    '表示用_面積', '表示用_材積', '表示用_成長量',
    '道からの距離', '施業年度',
    '表示用_割合１', '表示用_割合２', '表示用_割合３',
}

_NULL_DISPLAY = {'', 'NULL', 'None', None}


class _ShinrinboApiModel(QAbstractTableModel):
    """森林クラウドから取った森林簿の表（1行＝1小班の dict）。
    値は表が描くときに文字にする（セルごとの部品を作らないので、行が多くても軽い）。"""

    def __init__(self, col_map, rows, parent=None):
        super().__init__(parent)
        self._cols = list(col_map)
        self._rows = list(rows)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._cols)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal
                and 0 <= section < len(self._cols)):
            return self._cols[section][1]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        src = self._cols[index.column()][0]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if src in _API_CENTER_SOURCES:
                return int(Qt.AlignmentFlag.AlignCenter)
            if src in _API_RIGHT_SOURCES:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        d = self._rows[index.row()] or {}
        raw = d.get(src)
        text = '' if raw is None or str(raw) in _NULL_DISPLAY else str(raw)
        if '年度' in src and text == '0':
            text = ''
        return text

    def row_data(self, row):
        return (self._rows[row] or {}) if 0 <= row < len(self._rows) else None


class _ShinrinboLayerModel(QAbstractTableModel):
    """計画図レイヤーの森林簿の列の表。最初に持つのは選択中の小班の ID（fid）の一覧だけで、
    属性は表が描く行の分だけ、100 行ずつまとめて読む（読んだ行は覚えておく）。
    行が多くても表はすぐ出て、スクロールしたところだけ読む。値は属性の値をそのまま文字にする（NULL は空欄）。"""

    _BLOCK = 100  # 一度に読む行数

    def __init__(self, layer, fids, col_map, parent=None):
        super().__init__(parent)
        self._layer = layer
        self._fids = list(fids)
        self._labels = [label for _, label in col_map]
        self._idx = [layer.fields().indexOf(src) for src, _ in col_map]
        self._values = {}     # fid → 列の値
        self._loaded = set()  # 読み終えたまとまりの番号
        # レイヤーが編集などで変わったら、読んだ値を捨てて描き直す（描くときに読み直す）
        layer.dataChanged.connect(self._on_layer_changed)

    def _on_layer_changed(self):
        self.beginResetModel()
        self._values.clear()
        self._loaded.clear()
        self.endResetModel()

    def _load_block(self, block):
        ids = self._fids[block * self._BLOCK:(block + 1) * self._BLOCK]
        req = (QgsFeatureRequest().setFilterFids(ids)
               .setSubsetOfAttributes(self._idx).setFlags(QgsFeatureRequest.Flag.NoGeometry))
        try:
            for f in self._layer.getFeatures(req):
                attrs = f.attributes()
                self._values[f.id()] = [attrs[i] for i in self._idx]
        except RuntimeError:
            pass  # レイヤーが削除済み（表はすぐ差し替えられる）
        self._loaded.add(block)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._fids)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._labels)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal
                and 0 <= section < len(self._labels)):
            return self._labels[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = index.row()
        block = row // self._BLOCK
        if block not in self._loaded:
            self._load_block(block)
        values = self._values.get(self._fids[row])
        v = values[index.column()] if values else None
        if v is None or (hasattr(v, 'isNull') and v.isNull()) or str(v) == 'NULL':
            return ''
        return str(v)

    def fid(self, row):
        return self._fids[row]


class ShinrinboMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_shinrinbo(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        self.lbl_shinrinbo_progress = QLabel()
        self.lbl_shinrinbo_progress.setVisible(False)
        self.lbl_shinrinbo_progress.setToolTip('森林クラウド情報の読み込み状況')
        self.btn_cancel_shinrinbo = QPushButton('中止')
        self.btn_cancel_shinrinbo.setToolTip('森林クラウド情報の読み込みを中止')
        self.btn_cancel_shinrinbo.setVisible(False)
        self.btn_shinrinbo_source = QPushButton('森林クラウド')
        self.btn_shinrinbo_source.setToolTip(
            '現在の表示元です。押すたびに「森林クラウド」と「森林簿」を切り替えます。')
        # 表示名の長さが変わっても、公図連携ボタンの位置を動かさない。
        cloud_width = self.btn_shinrinbo_source.sizeHint().width()
        self.btn_shinrinbo_source.setText('森林簿')
        local_width = self.btn_shinrinbo_source.sizeHint().width()
        self.btn_shinrinbo_source.setFixedWidth(max(cloud_width, local_width))
        self.btn_shinrinbo_source.setText('森林クラウド')
        row.addWidget(self.btn_shinrinbo_source)
        row.addWidget(self.lbl_shinrinbo_progress)
        row.addWidget(self.btn_cancel_shinrinbo)
        row.addStretch(1)
        self.btn_export_shinrinbo = QPushButton('CSVエクスポート')
        self.btn_export_shinrinbo.setToolTip(
            '現在の森林簿テーブルをCSVファイルに保存')
        self.btn_export_shinrinbo.setEnabled(False)
        row.addWidget(self.btn_export_shinrinbo)
        self.btn_kozu_shinrinbo = QPushButton('公図連携OFF')
        self.btn_kozu_shinrinbo.setCheckable(True)
        self.btn_kozu_shinrinbo.setStyleSheet(_TOGGLE_BTN_QSS_ONOFF)
        # 幅は広い方（公図連携OFF）で固定して ON/OFF 切替で伸縮させない
        self.btn_kozu_shinrinbo.setFixedWidth(self.btn_kozu_shinrinbo.sizeHint().width())
        self.btn_kozu_shinrinbo.setToolTip('ONにすると小班選択のたびに自動で Kozu XML Integrator へ送信')
        row.addWidget(self.btn_kozu_shinrinbo)
        self._update_kozu_btn(self.btn_kozu_shinrinbo)
        v.addLayout(row)

        # 表はモデルとビューを分ける作り（行が多くても、描くセルの分だけ値を読む）。
        # モデルは型で違う：計画図の属性（gpkg 型）は _ShinrinboLayerModel、森林クラウドの森林簿は _ShinrinboApiModel
        self.tbl_shinrinbo = QTableView()
        self.tbl_shinrinbo.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_shinrinbo.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_shinrinbo.setAlternatingRowColors(True)
        self.tbl_shinrinbo.setFrameShape(QFrame.Shape.NoFrame)
        self.tbl_shinrinbo.setStyleSheet(
            'QTableView { border: 1px solid palette(dark); }')
        hdr = self.tbl_shinrinbo.horizontalHeader()
        # 列幅は列ごとに決めた幅（_SHINRINBO_COL_WIDTHS）。データや選択では変えない（利用者は手で変えられる）
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(False)
        self.tbl_shinrinbo.verticalHeader().setVisible(False)
        # 入りきらない値は折り返さず「…」で省略する（行の高さは1行のまま）
        self.tbl_shinrinbo.setWordWrap(False)
        # ピクセルスクロール／Shift+ホイール水平／Ctrl+C・右クリックでコピー
        self._enable_table_shortcuts(self.tbl_shinrinbo)

        v.addWidget(self.tbl_shinrinbo, 1)

        self.btn_kozu_shinrinbo.toggled.connect(
            lambda checked: self._on_kozu_toggled(checked, self.btn_kozu_shinrinbo))
        self.btn_shinrinbo_source.clicked.connect(
            self._on_shinrinbo_source_clicked)
        self.btn_export_shinrinbo.clicked.connect(
            self._export_shinrinbo_csv)
        self.btn_cancel_shinrinbo.clicked.connect(self._cancel_shinrinbo_loading)
        self._shinrinbo_model_keep = ()
        self._shinrinbo_source = 'cloud'
        self._shinrinbo_active_source = 'cloud'
        self._shinrinbo_replies = []
        self._shinrinbo_progress_done = 0
        self._shinrinbo_progress_total = 0
        self._shinrinbo_loaded_feature_indexes = set()
        self._shinrinbo_partial_results = []
        self._shinrinbo_batch_api_ids = []
        self._shinrinbo_cancel_requested = False
        self._clear_shinrinbo_table()
        return w

    def _set_shinrinbo_progress(self, text='', done=None, total=None,
                                cancellable=True):
        """森林簿タブ上部の空き領域に、クラウド読込の件数と進捗を表示する。"""
        label = getattr(self, 'lbl_shinrinbo_progress', None)
        if label is None or sip.isdeleted(label):
            return
        cancel = getattr(self, 'btn_cancel_shinrinbo', None)
        if not text:
            self._shinrinbo_progress_done = 0
            self._shinrinbo_progress_total = 0
            label.clear()
            label.setVisible(False)
            if cancel is not None and not sip.isdeleted(cancel):
                cancel.setVisible(False)
            return
        if total:
            done = max(0, min(int(done or 0), int(total)))
            pct = int(done * 100 / total)
            self._shinrinbo_progress_done = done
            self._shinrinbo_progress_total = int(total)
            text = f'{text}… {done:,} / {total:,}件（{pct}%）'
        label.setText(text)
        label.setVisible(True)
        if cancel is not None and not sip.isdeleted(cancel):
            cancel.setVisible(cancellable)

    def _track_shinrinbo_reply(self, reply):
        """森林簿だけの通信を追跡し、ほかのタブを巻き込まず中止できるようにする。"""
        if reply is None:
            return
        try:
            if reply.isFinished():
                return
        except RuntimeError:
            return
        self._shinrinbo_replies.append(reply)

        def forget_reply():
            if reply in self._shinrinbo_replies:
                self._shinrinbo_replies.remove(reply)

        reply.finished.connect(forget_reply)

    def _abort_shinrinbo_replies(self):
        replies = list(getattr(self, '_shinrinbo_replies', []))
        self._shinrinbo_replies = []
        for reply in replies:
            try:
                if not sip.isdeleted(reply) and not reply.isFinished():
                    reply.abort()
            except RuntimeError:
                pass
        return len(replies)

    def _cancel_shinrinbo_loading(self):
        """中止を予約する。通信中のまとまりは完了させ、新しい要求は送らない。"""
        if getattr(self, '_shinrinbo_cancel_requested', False):
            return
        self._shinrinbo_cancel_requested = True
        done = getattr(self, '_shinrinbo_progress_done', 0)
        total = getattr(self, '_shinrinbo_progress_total', 0)
        self._set_shinrinbo_progress('中止処理中', done, total, cancellable=False)

    def _finish_shinrinbo_cancellation(self):
        """通信中だったまとまりが完了した時点で、未取得フィーチャーを選択解除する。"""
        if not getattr(self, '_shinrinbo_cancel_requested', False):
            return
        total = getattr(self, '_shinrinbo_progress_total', 0)
        self._shinrinbo_cancel_requested = False
        self._shinrinbo_generation = getattr(self, '_shinrinbo_generation', 0) + 1
        kept = self._keep_loaded_shinrinbo_selection()
        text = f'読み込みを中止しました（{kept:,} / {total:,}件を取得）'
        self._set_shinrinbo_progress(text, cancellable=False)
        self._progress_end()
        self._update_cache_btn_states()

    def _keep_loaded_shinrinbo_selection(self):
        """中止時、クラウド情報を取得済みの行だけ地図選択と表に残す。"""
        layer = getattr(self, '_connected_layer', None)
        row_features = list(getattr(self, '_shinrinbo_row_features', []))
        results = list(getattr(self, '_shinrinbo_partial_results', []))
        api_ids = list(getattr(self, '_shinrinbo_batch_api_ids', []))
        indexes = sorted(getattr(self, '_shinrinbo_loaded_feature_indexes', set()))
        indexes = [i for i in indexes
                   if i < len(row_features) and i < len(results) and bool(results[i])]
        keep_features = [row_features[i] for i in indexes]
        keep_results = [results[i] for i in indexes]
        keep_api_ids = [api_ids[i] for i in indexes if i < len(api_ids) and api_ids[i]]

        if layer is not None and not sip.isdeleted(layer):
            # selectionChanged を一時停止し、未取得フィーチャーの解除が
            # 重なり小班の再選択として扱われないようにする。
            layer.blockSignals(True)
            try:
                layer.removeSelection()
                layer.selectByIds([f.id() for f in keep_features])
            finally:
                layer.blockSignals(False)

        self._clicked_fid = keep_features[0].id() if keep_features else None
        self._shinrinbo_row_features = keep_features
        self._shinrinbo_api_ids = keep_api_ids
        self._current_shinrinbo_key = (
            f'森林簿/{keep_api_ids[-1]}' if keep_api_ids else '')
        self._clear_selection_highlights()
        self._clear_cloud_record_info()

        if keep_features:
            feat = keep_features[0]
            fnames = feat.fields().names()
            if self._layer_type == 'shp':
                key1 = feat['KEY1'] if 'KEY1' in fnames else ''
                self.lbl_selected.setText(f'小班: {key1}')
            elif self._layer_type == 'cd_gpkg':
                rinpan = feat['林班'] if '林班' in fnames else ''
                junrin = feat['準林班CD'] if '準林班CD' in fnames else ''
                kohan = feat['小班_親番'] if '小班_親番' in fnames else ''
                self.lbl_selected.setText(f'小班: {rinpan}-{junrin}-{kohan}')
            else:
                rinpan = feat['林班_森林簿'] if '林班_森林簿' in fnames else ''
                junrin = feat['準林班名称'] if '準林班名称' in fnames else ''
                kohan = feat['小班_親番_森林簿'] if '小班_親番_森林簿' in fnames else ''
                self.lbl_selected.setText(f'小班: {rinpan}-{junrin}-{kohan}')
            if self._layer_type in ('cd_gpkg', 'shp'):
                self._track_owner_info(None)
                self._on_cd_api_result(keep_results[0])
            else:
                self._show_feature_info(feat)
            self.left_tab.setCurrentIndex(0)
            self._shinrinbo_active_source = 'cloud'
            self._build_shinrinbo_table_api(keep_results)
        else:
            self.lbl_selected.setText('計画図レイヤーで選択してください')
            self.info_browser.clear()
            self._track_owner_info(None)
            self._clear_shinrinbo_table()

        self._od_on_plan_selection()
        return len(keep_features)

    def _shinrinbo_uses_local_layer(self):
        """完全な森林簿GPKGで、利用者がローカル表示を選んだときだけ True。"""
        return (self._layer_type == 'gpkg'
                and getattr(self, '_shinrinbo_source', 'cloud') == 'local')

    def _set_shinrinbo_source(self, source):
        """表示元を保持し、ボタンに現在のモードを表示する。"""
        self._shinrinbo_source = 'local' if source == 'local' else 'cloud'
        btn = getattr(self, 'btn_shinrinbo_source', None)
        if btn is not None:
            btn.setText('森林簿' if self._shinrinbo_source == 'local'
                        else '森林クラウド')

    def _on_shinrinbo_source_clicked(self):
        """表示元を切り替える。地図の選択は変えず、表だけを作り直す。"""
        self._set_shinrinbo_source(
            'cloud' if self._shinrinbo_uses_local_layer() else 'local')
        self._init_shinrinbo_headers()
        layer = getattr(self, '_connected_layer', None)
        if layer is None or sip.isdeleted(layer) or not layer.selectedFeatureIds():
            self._refresh_shinrinbo_tab([])
            return
        features = (self._selected_features_head()
                    if self._shinrinbo_uses_local_layer()
                    else self._selected_features_sorted())
        self._refresh_shinrinbo_tab(features)

    # ------------------------------------------------------------------
    # 表のモデルの差し替え
    # ------------------------------------------------------------------

    def _set_shinrinbo_model(self, model, keep=()):
        """表のモデルを差し替える。keep：モデルと一緒に持っておくもの（読み込みのキャッシュなど）。
        前のモデルなどは、差し替えたあとに片付ける。"""
        table = self.tbl_shinrinbo
        old_sel = table.selectionModel()
        old = getattr(self, '_shinrinbo_model_keep', ())
        table.setModel(model)
        self.btn_export_shinrinbo.setEnabled(
            model.rowCount(QModelIndex()) > 0
            and model.columnCount(QModelIndex()) > 0)
        self._shinrinbo_model_keep = (model,) + tuple(keep)
        table.selectionModel().selectionChanged.connect(self._on_shinrinbo_row_selected)
        for obj in (old_sel,) + tuple(old):
            if obj is not None and not sip.isdeleted(obj):
                obj.deleteLater()
        self._apply_shinrinbo_col_widths()

    def _export_shinrinbo_csv(self):
        """現在表示中の森林簿テーブルを、表示順のままCSVに書き出す。"""
        model = self.tbl_shinrinbo.model()
        rows = model.rowCount(QModelIndex()) if model is not None else 0
        cols = model.columnCount(QModelIndex()) if model is not None else 0
        if rows <= 0 or cols <= 0:
            return

        source_name = getattr(self, 'btn_shinrinbo_source', None)
        source_name = source_name.text() if source_name is not None else '森林簿'
        initial_path = os.path.join(os.path.expanduser('~'), f'{source_name}.csv')
        path, _ = QFileDialog.getSaveFileName(
            self, 'CSVエクスポート', initial_path, 'CSVファイル (*.csv)')
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.csv'

        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    model.headerData(c, Qt.Orientation.Horizontal,
                                     Qt.ItemDataRole.DisplayRole) or ''
                    for c in range(cols)
                ])
                for r in range(rows):
                    writer.writerow([
                        model.data(model.index(r, c), Qt.ItemDataRole.DisplayRole) or ''
                        for c in range(cols)
                    ])
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(
                self, 'CSVエクスポート',
                f'CSVファイルを保存できませんでした。\n\n{exc}')
            return

        QMessageBox.information(
            self, 'CSVエクスポート',
            f'{rows}件を保存しました。\n\n{path}')

    def _apply_shinrinbo_col_widths(self):
        """列幅を、列ごとに決めた幅（_SHINRINBO_COL_WIDTHS の文字列が入る幅）にする。
        見出しの方が長い列は見出しに合わせる。決めていない列だけ、中身に合わせる。"""
        table = self.tbl_shinrinbo
        model = table.model()
        cell_fm = table.fontMetrics()
        head_fm = table.horizontalHeader().fontMetrics()
        pad = 16  # セルの左右の余白
        for col in range(model.columnCount(QModelIndex())):
            label = str(model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or '')
            sample = _SHINRINBO_COL_WIDTHS.get(label) or _SHINRINBO_COL_WIDTHS.get(label.rstrip('0123456789'))
            if sample is None:
                table.resizeColumnToContents(col)
                continue
            table.setColumnWidth(col, max(cell_fm.horizontalAdvance(sample),
                                          head_fm.horizontalAdvance(label)) + pad)

    def _clear_shinrinbo_table(self):
        """表を空にする（見出しだけ出す）。"""
        self._set_shinrinbo_model(_ShinrinboApiModel(
            getattr(self, '_shinrinbo_col_map', []), [], self.tbl_shinrinbo))

    # ------------------------------------------------------------------
    # ヘッダー確定（レイヤー選択時に一度だけ呼ぶ）
    # ------------------------------------------------------------------

    def _local_shinrinbo_col_map(self):
        """接続中の完全な森林簿GPKGに実在する、ローカル表示用の列。"""
        if self._layer_type != 'gpkg':
            return []
        layer = self._connected_layer
        existing = set()
        if layer is not None and not sip.isdeleted(layer):
            try:
                existing = {f.name() for f in layer.fields()}
            except RuntimeError:
                pass  # チェック後にレイヤーが削除された場合のみ発生
        col_map = [
            (src, label) for src, label in _PRIMARY_FIELDS
            if src in existing
        ]
        for i, (y_f, m_f, e_f) in enumerate(_HISTORY_FIELDS):
            if y_f in existing:
                n = str(i + 1)
                col_map += [
                    (y_f, f'施業年度{n}'),
                    (m_f, f'施業方法{n}'),
                    (e_f, f'事業種別{n}'),
                ]
        return col_map

    def _init_shinrinbo_headers(self):
        """表示元とレイヤー種別に基づきヘッダー列を確定する。"""
        btn = getattr(self, 'btn_shinrinbo_source', None)
        if btn is not None:
            btn.setEnabled(self._layer_type == 'gpkg')
            if self._layer_type != 'gpkg':
                self._set_shinrinbo_source('cloud')

        if self._shinrinbo_uses_local_layer():
            self._shinrinbo_col_map = self._local_shinrinbo_col_map()
        elif self._layer_type in ('gpkg', 'cd_gpkg', 'shp'):
            self._shinrinbo_col_map = list(_API_TABLE_COLS)
        else:
            self._shinrinbo_col_map = []

        self._clear_shinrinbo_table()

    # ------------------------------------------------------------------
    # 更新エントリポイント（features = list[QgsFeature]、選択変更時）
    # ------------------------------------------------------------------

    def _refresh_shinrinbo_tab(self, features):
        """features：選択中の小班。gpkg 型は表を選択中の小班すべてから作るので、空かどうかだけを見る
        （呼び出し元は先頭の1件だけ読めばよい）。cd_gpkg 型は、この地物から森林クラウドの森林簿を取る。"""
        self._shinrinbo_generation = getattr(self, '_shinrinbo_generation', 0) + 1
        gen = self._shinrinbo_generation
        self._shinrinbo_cancel_requested = False
        self._abort_shinrinbo_replies()
        self._update_kozu_btn(self.btn_kozu_shinrinbo)
        self._current_shinrinbo_key = ''
        self._shinrinbo_api_ids = []
        self._shinrinbo_loaded_feature_indexes = set()
        self._shinrinbo_partial_results = []
        self._shinrinbo_batch_api_ids = []
        use_local = self._shinrinbo_uses_local_layer()
        # 森林クラウドの各行を、元になった計画図フィーチャーへ対応付ける。
        # 行選択時のハイライトと、ローカル所有者情報の追記に使う。
        self._shinrinbo_row_features = [] if use_local else list(features or [])
        if not features:
            self._set_shinrinbo_progress()
            self._clear_shinrinbo_table()
            self._clear_selection_highlights()
            self._clear_cloud_record_info()
            self._update_cache_btn_states()
            self._progress_end()
            return

        # 地図上の選択はテーブルの行選択と連動させない。
        # ラバーバンド/ズームはテーブルで明示的に行を選んだ時だけ行う。
        self._clear_selection_highlights()
        self._clear_cloud_record_info()

        if use_local:
            self._set_shinrinbo_progress()
            self._shinrinbo_active_source = 'local'
            self._build_shinrinbo_table_layer()
            self._sync_shinrinbo_cache_ts('ローカルデータ')
            self._update_cache_btn_states()
            self._progress_end()
            return

        # cd_gpkg: 2段階
        # Stage 1: KEY1 → THE_FID (MVT タイル)
        self._sync_shinrinbo_cache_ts('取得日時: —')
        self._set_shinrinbo_progress('小班IDを確認中', 0, len(features))

        def on_fids_resolved(fid_list):
            if self._shinrinbo_generation != gen:
                return
            if self._shinrinbo_cancel_requested:
                self._finish_shinrinbo_cancellation()
                return
            api_ids = [str(f) if f is not None else None for f in fid_list]
            self._shinrinbo_api_ids = [a for a in api_ids if a]
            if self._shinrinbo_api_ids:
                self._current_shinrinbo_key = f'森林簿/{self._shinrinbo_api_ids[-1]}'
            # Stage 2: 森林簿データ取得
            self._fetch_shinrinbo_batch(api_ids, gen, force=False)

        self._resolve_fids_via_mvt(features, on_fids_resolved)
        self._progress_end()  # 同期処理用の共通表示はここまで。以後の通信進捗は森林簿タブ上部に出す

    # ------------------------------------------------------------------
    # MVT タイルで KEY1 → THE_FID を一括解決
    # ------------------------------------------------------------------

    def _resolve_fids_via_mvt(self, features, on_done):
        """KEY1 → THE_FID を MVT タイルで解決。完了後 on_done(fid_list) を呼ぶ。
        世代チェックは呼び出し元の on_done 内で行うこと。"""
        n = len(features)
        fid_list = [None] * n
        resolve_gen = self._shinrinbo_generation

        tile_groups = {}
        for i, feat in enumerate(features):
            self._progress_step(i, n, 50, 50)
            fnames = feat.fields().names()
            if 'KEY1' not in fnames:
                continue
            key1 = str(feat['KEY1'])
            if not key1 or key1 in ('NULL', 'None', ''):
                continue
            centroid = self._feature_centroid_wgs84(feat)
            if centroid is None:
                continue
            lon, lat = centroid
            tx, ty = self._tile_xyz(lon, lat, 13)
            tile_key = (13, tx, ty)
            tile_groups.setdefault(tile_key, []).append((i, key1))

        if not tile_groups:
            self._set_shinrinbo_progress('小班IDを確認中', n, n)
            on_done(fid_list)
            return

        pending = [len(tile_groups)]
        resolved = [n - sum(len(items) for items in tile_groups.values())]
        mvt_cache = getattr(self, '_mvt_tile_cache', {})

        def process_tile(tile_key, mapping):
            if self._shinrinbo_generation != resolve_gen:
                return
            for idx, key1 in tile_groups[tile_key]:
                if key1 in mapping:
                    fid_list[idx] = mapping[key1]
            resolved[0] += len(tile_groups[tile_key])
            progress_text = ('中止処理中' if self._shinrinbo_cancel_requested
                             else '小班IDを確認中')
            self._set_shinrinbo_progress(
                progress_text, resolved[0], n,
                cancellable=not self._shinrinbo_cancel_requested)
            pending[0] -= 1
            if pending[0] == 0:
                on_done(fid_list)

        for tile_key in tile_groups:
            if tile_key in mvt_cache:
                process_tile(tile_key, mvt_cache[tile_key])
            else:
                z, tx, ty = tile_key
                url = f'{_MVT_BASE}{_MVT_PATH}/{z}/{tx}/{ty}.pbf'

                def make_cb(tk):
                    def cb(data):
                        mapping = self._parse_mvt_key1_to_fid(data) if data else {}
                        self._mvt_tile_cache[tk] = mapping
                        process_tile(tk, mapping)
                    return cb

                reply = self._get_binary(url, make_cb(tile_key))
                self._track_shinrinbo_reply(reply)

    def _feature_centroid_wgs84(self, feat):
        try:
            layer = self._connected_layer
            if layer is None or sip.isdeleted(layer):
                return None
            geom = feat.geometry()
            cp = geom.centroid().asPoint()
            src_crs = layer.crs()
            dst_crs = QgsCoordinateReferenceSystem('EPSG:4326')
            xf = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
            pt = xf.transform(QgsPointXY(cp.x(), cp.y()))
            return pt.x(), pt.y()
        except Exception:
            return None

    @staticmethod
    def _tile_xyz(lon, lat, zoom=13):
        n = 2 ** zoom
        x = int((lon + 180) / 360 * n)
        lat_r = math.radians(lat)
        y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
        return x, y

    @staticmethod
    def _parse_mvt_key1_to_fid(raw_bytes):
        """MVT PBF を解析して {key1: the_fid} マッピングを返す"""
        def read_varint(buf, pos):
            result = shift = 0
            while True:
                b = buf[pos]
                pos += 1
                result |= (b & 0x7F) << shift
                if not (b & 0x80):
                    return result, pos
                shift += 7

        def parse_value(buf):
            pos = 0
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                wtype = tag & 7
                if wtype == 2:
                    l, pos = read_varint(buf, pos)
                    return buf[pos:pos + l].decode('utf-8', errors='replace')
                elif wtype == 0:
                    v, pos = read_varint(buf, pos)
                    return v
                elif wtype == 5:
                    return struct.unpack_from('<f', buf, pos)[0]
                elif wtype == 1:
                    return struct.unpack_from('<d', buf, pos)[0]
            return None

        def parse_feature_tags(buf):
            pos = 0
            tags = []
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                field, wtype = tag >> 3, tag & 7
                if field == 2 and wtype == 2:
                    l, pos = read_varint(buf, pos)
                    chunk = buf[pos:pos + l]
                    pos += l
                    p2 = 0
                    while p2 < len(chunk):
                        v, p2 = read_varint(chunk, p2)
                        tags.append(v)
                elif wtype == 2:
                    l, pos = read_varint(buf, pos)
                    pos += l
                elif wtype == 0:
                    _, pos = read_varint(buf, pos)
                elif wtype in (1, 5):
                    pos += (8 if wtype == 1 else 4)
                else:
                    break
            return tags

        def parse_layer(buf):
            pos = 0
            keys = []
            vals = []
            feat_tags_list = []
            while pos < len(buf):
                tag, pos = read_varint(buf, pos)
                field, wtype = tag >> 3, tag & 7
                if wtype == 2:
                    l, pos = read_varint(buf, pos)
                    chunk = buf[pos:pos + l]
                    pos += l
                    if field == 3:
                        keys.append(chunk.decode('utf-8', errors='replace'))
                    elif field == 4:
                        vals.append(parse_value(chunk))
                    elif field == 2:
                        feat_tags_list.append(parse_feature_tags(chunk))
                elif wtype == 0:
                    _, pos = read_varint(buf, pos)
                elif wtype in (1, 5):
                    pos += (8 if wtype == 1 else 4)
                else:
                    break
            return keys, vals, feat_tags_list

        buf = raw_bytes
        pos = 0
        while pos < len(buf):
            tag, pos = read_varint(buf, pos)
            field, wtype = tag >> 3, tag & 7
            if field == 3 and wtype == 2:
                l, pos = read_varint(buf, pos)
                keys, vals, feat_tags_list = parse_layer(buf[pos:pos + l])
                pos += l
                if 'KEY1' not in keys or 'THE_FID' not in keys:
                    continue
                mapping = {}
                for tags in feat_tags_list:
                    prop = {keys[tags[i]]: vals[tags[i + 1]]
                            for i in range(0, len(tags) - 1, 2)}
                    k1 = prop.get('KEY1')
                    fid = prop.get('THE_FID')
                    if k1 and fid is not None and k1 not in mapping:
                        mapping[k1] = int(fid)
                return mapping
            elif wtype == 2:
                l, pos = read_varint(buf, pos)
                pos += l
            elif wtype == 0:
                _, pos = read_varint(buf, pos)
            elif wtype in (1, 5):
                pos += (8 if wtype == 1 else 4)
            else:
                break
        return {}

    # ------------------------------------------------------------------
    # バッチ取得（Stage 2）
    # ------------------------------------------------------------------

    def _fetch_shinrinbo_batch(self, api_ids, gen, force=False):
        n = len(api_ids)
        results = [None] * n
        self._shinrinbo_partial_results = results
        self._shinrinbo_batch_api_ids = list(api_ids)
        self._shinrinbo_loaded_feature_indexes = set()
        completed = [0]
        active = [0]
        next_request = [0]
        finished = [False]
        requests = []
        progress_text = '森林クラウドを更新中' if force else '森林クラウドを取得中'
        self._set_shinrinbo_progress(progress_text, 0, n)

        def on_done():
            if finished[0] or self._shinrinbo_generation != gen:
                return
            finished[0] = True
            self._set_shinrinbo_progress()
            # 完全な森林簿GPKGでは、クラウドから1件も取れなかった場合だけ
            # ローカル属性へ自動的に退避する。表示元は状態欄で明示する。
            if (self._layer_type == 'gpkg' and not any(bool(r) for r in results)):
                self._shinrinbo_active_source = 'local_fallback'
                self._set_shinrinbo_source('local')
                self._build_shinrinbo_table_layer(self._local_shinrinbo_col_map())
                self._sync_shinrinbo_cache_ts('ローカルデータ（森林クラウド取得不可）')
                self._update_cache_btn_states()
                return
            self._shinrinbo_active_source = 'cloud'
            self._build_shinrinbo_table_api(results)
            last_id = next((aid for aid in reversed(api_ids) if aid), None)
            ts_text = '取得日時: —'
            if last_id:
                db = self._get_db('森林簿')
                if db:
                    ts = db.get_fetched_at(f'森林簿/{last_id}')
                    if ts:
                        ts_text = f'取得日時: {ts}'
            self._sync_shinrinbo_cache_ts(ts_text)
            self._update_cache_btn_states()

        db = self._get_db('森林簿')
        for i, api_id in enumerate(api_ids):
            if api_id is None:
                results[i] = {}
                completed[0] += 1
                self._set_shinrinbo_progress(progress_text, completed[0], n)
                continue
            cache_key = f'森林簿/{api_id}'
            if not force and db is not None:
                cached, _ = db.get(cache_key)
                if cached is not None:
                    results[i] = cached
                    if cached:
                        self._shinrinbo_loaded_feature_indexes.add(i)
                    completed[0] += 1
                    self._set_shinrinbo_progress(progress_text, completed[0], n)
                    continue
            requests.append((i, api_id, cache_key))

        def launch_more():
            if (self._shinrinbo_generation != gen
                    or self._shinrinbo_cancel_requested):
                return
            while (active[0] < _MAX_SHINRINBO_REQUESTS
                   and next_request[0] < len(requests)):
                idx, api_id, cache_key = requests[next_request[0]]
                next_request[0] += 1
                active[0] += 1

                def cb(data, idx=idx, key=cache_key):
                    if self._shinrinbo_generation != gen:
                        return
                    results[idx] = data or {}
                    if data and db is not None:
                        db.put(key, data)
                    if data:
                        self._shinrinbo_loaded_feature_indexes.add(idx)
                    completed[0] += 1
                    active[0] -= 1
                    stopping = self._shinrinbo_cancel_requested
                    self._set_shinrinbo_progress(
                        '中止処理中' if stopping else progress_text,
                        completed[0], n, cancellable=not stopping)
                    if stopping:
                        if active[0] == 0:
                            self._finish_shinrinbo_cancellation()
                        return
                    launch_more()
                    if completed[0] == n:
                        on_done()

                reply = self._get_api(
                    f'{_API_BASE}/advanced-search/森林簿/{api_id}', cb)
                self._track_shinrinbo_reply(reply)

        if completed[0] == n:
            on_done()
        else:
            launch_more()

    # ------------------------------------------------------------------
    # 更新ボタン
    # ------------------------------------------------------------------

    def _update_shinrinbo_cache(self):
        if not self._shinrinbo_api_ids:
            return
        self._shinrinbo_generation = getattr(self, '_shinrinbo_generation', 0) + 1
        self._shinrinbo_cancel_requested = False
        self._abort_shinrinbo_replies()
        self._fetch_shinrinbo_batch(
            self._shinrinbo_api_ids,
            self._shinrinbo_generation,
            force=True,
        )

    # ------------------------------------------------------------------
    # テーブル行更新（ヘッダーは変えない）
    # ------------------------------------------------------------------

    def _build_shinrinbo_table_layer(self, col_map=None):
        """計画図レイヤー（gpkg 型）で選択中の小班の森林簿の表を作る。表のモデルが持つのは小班の ID の一覧だけで、
        属性は見えている行の分だけ読むので、選んだ小班が多くても表はすぐ出る。
        形状は読まない（行を選んだときにその小班だけ読む）。行の並びは地物の番号順。"""
        layer = self._connected_layer
        if layer is None or sip.isdeleted(layer):
            self._clear_shinrinbo_table()
            return
        table = self.tbl_shinrinbo
        model = _ShinrinboLayerModel(
            layer, sorted(layer.selectedFeatureIds()),
            col_map if col_map is not None else getattr(self, '_shinrinbo_col_map', []), table)
        self._set_shinrinbo_model(model)
        table.clearSelection()

    def _build_shinrinbo_table_api(self, results):
        """森林クラウドから取った森林簿の表を作る（値は表が描くときに文字にする）。"""
        self._set_shinrinbo_model(_ShinrinboApiModel(
            getattr(self, '_shinrinbo_col_map', []), results, self.tbl_shinrinbo))
        self.tbl_shinrinbo.clearSelection()

    # ------------------------------------------------------------------
    # 行選択 → kozu連携
    # ------------------------------------------------------------------

    def _on_shinrinbo_row_selected(self, *_args):
        self._clear_selection_highlights()
        # 選ばれた行の一覧（selectedRows）は作らない。表で Ctrl+A を押すと全行分になり、止まるため。
        # 使うのは選ばれた範囲の先頭の行だけ
        sel = self.tbl_shinrinbo.selectionModel()
        if sel is None or not sel.hasSelection():
            self._clear_cloud_record_info()
            return
        row = min(r.top() for r in sel.selection())
        model = self.tbl_shinrinbo.model()
        layer = self._connected_layer
        feature = None
        if isinstance(model, _ShinrinboLayerModel):
            # gpkg 型：その行の小班を、所有者情報・ハイライト・公図連携のために1件だけ読む
            if layer is None or sip.isdeleted(layer):
                return
            feature = next(layer.getFeatures(QgsFeatureRequest().setFilterFid(model.fid(row))), None)
            row_data = feature
        else:
            row_data = model.row_data(row) if isinstance(model, _ShinrinboApiModel) else None
            row_features = getattr(self, '_shinrinbo_row_features', [])
            if 0 <= row < len(row_features):
                feature = row_features[row]
        if row_data is None:
            self._clear_cloud_record_info()
            return
        # 森林クラウドの行にも、対応するローカル小班の所有者情報だけを出どころを分けて続ける。
        owner_feat = feature if self._layer_type == 'gpkg' else None
        is_cloud = isinstance(model, _ShinrinboApiModel)
        self._show_cloud_table_row_info(
            '森林クラウド情報' if is_cloud else 'ローカル森林簿情報',
            self.tbl_shinrinbo, row, owner_feat, show_empty=is_cloud)
        if feature is not None:
            try:
                geom = feature.geometry()
            except Exception:
                geom = None
            layer = self._connected_layer
            if geom and not geom.isEmpty() and layer is not None and not sip.isdeleted(layer):
                self._add_selection_highlight(geom, layer.crs())
                self._zoom_to_selection_highlights()

        if not self.btn_kozu_shinrinbo.isChecked():
            return

        if self._layer_type == 'gpkg':
            # 表示中の行が森林クラウドの dict でも、公図連携に使うのは
            # その行へ対応付けて保持しているローカル森林簿フィーチャー。
            feat = feature
            if feat is None:
                return
            fnames = feat.fields().names()
            city  = str(feat['市町村名称'] or '').strip() if '市町村名称' in fnames else ''
            # 旧市町村区分（旧井川村・旧静岡市… の飾り付き表記）は市町村CDで
            # 現行市名へ変換してから Kozu へ渡す。表に無いCDは市町村名称のまま。
            if '市町村CD' in fnames:
                try:
                    _cur = _MORINBO_OLD_CD_TO_CITY.get(int(str(feat['市町村CD']).strip()))
                except (TypeError, ValueError):
                    _cur = None
                if _cur:
                    city = _cur
            daiji = str(feat['大字名称']  or '').strip() if '大字名称'  in fnames else ''
            oban  = str(feat['地番_親番'] or '').strip() if '地番_親番' in fnames else ''
            eda   = str(feat['地番_枝番'] or '').strip() if '地番_枝番' in fnames else ''
            # 親番・枝番は別フィールドのため、kozu側のchiban形式（親番-枝番）に合わせて結合
            chiban = f'{oban}-{eda}' if eda and eda != '0' else oban
        else:
            d = row_data or {}
            city  = str(d.get('市町村', '') or '').strip()
            daiji = str(d.get('大字', '') or '').strip()
            # 表示用_地番は既に「親番-枝番」形式で提供されるためそのまま使用
            chiban = str(d.get('表示用_地番', '') or '').strip()

        if chiban:
            self._send_to_kozu(city, daiji, chiban)

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    def _sync_shinrinbo_cache_ts(self, text):
        if self.cloud_tab.currentIndex() == self._shinrinbo_tab_index:
            self.lbl_cache_ts.setText(text)
