# -*- coding: utf-8 -*-
import html
import json
import os
from qgis.PyQt import sip
import urllib.parse

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QComboBox, QLabel, QTabWidget, QTextBrowser,
    QPushButton, QFrame, QMessageBox, QCheckBox,
)
from qgis.PyQt.QtCore import (
    Qt, QUrl, QByteArray, QSettings, QTimer, pyqtSignal, QObject, QEvent,
)
from qgis.PyQt.QtGui import (
    QColor, QDesktopServices, QKeySequence, QTextCursor, QTextTable,
)
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer,
    QgsNetworkAccessManager, QgsCoordinateTransform, QgsWkbTypes,
    QgsLayerTreeLayer, QgsFeatureRequest,
)
from qgis.gui import QgsRubberBand

from .cache_db import CacheDB
from .constants import (
    _API_BASE, _CITY_TO_NORIN, _TOGGLE_BTN_QSS_LAYER,
    _PRIMARY_FIELDS, _HISTORY_FIELDS,
    _CD_API_PRIMARY_FIELDS, _CD_API_HISTORY_FIELDS,
    _OWNER_FIELDS, _OWNER_LABEL, _OWNER_SOURCE_TITLE,
)
from .layer_cleanup import remove_project_layer
from .privacy_guard import IdleWatcher, IDLE_TIMEOUT_MS, MASK_TEXT
from .tab_hoanrin   import HoanrinMixin
from .tab_mori      import MoriMixin
from .tab_keikaku   import KeikakuMixin
from .tab_rinchi    import RinchiMixin
from .tab_shinrinbo import ShinrinboMixin

# 複数タブで共有する選択ハイライト色
_HL_SEL_BORDER = QColor(210,  30,  30, 220)
_HL_SEL_FILL   = QColor(210,  30,  30,  25)

# 背景タイル「静岡県 微地形表現図」（森林クラウドでの表示名。CS立体図）
_BG_CS3D_NAME = '静岡県 微地形表現図'
_BG_CS3D_URL  = 'https://fcloud.pref.shizuoka.jp/MAP/raster/CS_3D_MAP/{z}/{x}/{y}.png'


class _TableInputFilter(QObject):
    """クラウド各表の共通操作:
      * Shift+ホイール → 水平スクロール
      * Ctrl+C        → 選択セルをタブ区切りでコピー（列構造を保ったまま）
    ホイールは viewport、キーは表本体からイベントが来るので両方に入れる。
    """

    def __init__(self, table):
        super().__init__(table)
        self._table = table

    def eventFilter(self, obj, event):
        et = event.type()
        if (et == QEvent.Type.Wheel
                and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            sb = self._table.horizontalScrollBar()
            sb.setValue(sb.value() - event.angleDelta().y())
            return True
        if (et == QEvent.Type.KeyPress
                and event.matches(QKeySequence.StandardKey.Copy)):
            _copy_table_selection(self._table)
            return True
        return False


def _copy_table_selection(table):
    """見出し行＋選択行を TSV（行=改行 / 列=タブ）でクリップボードへコピーする。
    見出しもデータ行と同じタブ区切り1行として先頭に入る。"""
    from qgis.PyQt.QtWidgets import QApplication
    idxs = table.selectedIndexes()
    if not idxs:
        return
    rows = sorted({i.row() for i in idxs})
    cols = sorted({i.column() for i in idxs})

    def _header(c):
        hi = table.horizontalHeaderItem(c)
        return hi.text().strip() if hi is not None else ''

    lines = ['\t'.join(_header(c) for c in cols)]
    for r in rows:
        cells = []
        for c in cols:
            it = table.item(r, c)
            cells.append(it.text().strip() if it is not None else '')
        lines.append('\t'.join(cells))
    QApplication.clipboard().setText('\n'.join(lines))


def _info_browser_tsv(browser):
    """情報パネル（QTextBrowser）に表示中の label/value テーブルを
    TSV（行=改行 / 列=タブ）で返す。空欄行も残す。値は無加工。"""
    doc = browser.document()
    table = next((f for f in doc.rootFrame().childFrames()
                  if isinstance(f, QTextTable)), None)
    _brk = {0x2028: ' ', 0x2029: ' ', 0x0a: ' ', 0x0d: ' '}
    if table is None:
        return browser.toPlainText().strip()
    lines = []
    for r in range(table.rows()):
        cells, c = [], 0
        while c < table.columns():
            cell = table.cellAt(r, c)
            cur = QTextCursor(cell.firstCursorPosition())
            cur.setPosition(cell.lastCursorPosition().position(),
                            QTextCursor.MoveMode.KeepAnchor)
            cells.append(cur.selectedText().translate(_brk).strip())
            c += max(1, cell.columnSpan())
        lines.append('\t'.join(cells))
    return '\n'.join(lines)


class _InfoBrowser(QTextBrowser):
    """右クリックメニューに「全項目をコピー」を足した QTextBrowser。
    標準メニュー（選択部分のコピー等）はそのまま残す。"""

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu(event.pos())
        text = _info_browser_tsv(self)
        if text:
            from qgis.PyQt.QtWidgets import QApplication
            menu.addSeparator()
            menu.addAction(
                '全項目をコピー',
                lambda: QApplication.clipboard().setText(text))
        menu.exec(event.globalPos())


class FcloudDock(QDockWidget):
    """QGIS 格納用の薄いドックコンテナ。ネイティブ floating は使わず
    Closable / Movable のみを許可する（フロート⇔格納の状態遷移で Windows/Qt6 に
    透過残像バグがあるため。別ウィンドウ表示は QDialog 側で行う）。"""

    closed = pyqtSignal()

    def closeEvent(self, event):
        # ドック純正の ✕ ボタン経由のみ発火（ツールバートグルは setVisible(False) で
        # closeEvent を経由しない）。プラグイン側で本格的なクリーンアップに使う。
        self.closed.emit()
        super().closeEvent(event)


class FcloudWindow(HoanrinMixin, MoriMixin, KeikakuMixin, RinchiMixin, ShinrinboMixin, QWidget):
    _TAB_ORDER_VERSION = 2

    def __init__(self, iface, highlights=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._window_mode_callback = None
        self._suspend_hide_cleanup = False

        # 全タブ共有の状態変数
        self._connected_layer        = None
        self._sel_color_layer_id     = None  # 選択ハイライト色を変更中のレイヤーID
        self._sel_color_orig         = None  # 変更前の選択色（復元用）
        self._sel_mode_orig          = None  # 変更前のselectionRenderingMode（復元用）
        self._hoanrin_highlights     = highlights if highlights is not None else []
        self._selection_highlights   = []
        self._expanding_selection    = False  # 重なり地物の自動追加中フラグ（selectionChanged 再帰防止）
        self._clicked_fid            = None   # 直近に地図でクリックした地物 fid（左パネル表示の先頭固定用）
        self._mori_markers           = []
        self._pending_replies        = []
        self._current_raw_hoanrin    = None
        self._hoanrin_update_generation = 0
        self._hoanrin_update_total   = 0
        self._hoanrin_update_failed  = 0
        self._current_raw_mori       = None
        self._current_mori_cache_key = ''
        self._current_mori_filter    = {}
        self._current_mori_display_kanri = []
        self._mori_vector_layer_id   = None
        self._mori_layer_features    = []
        self._mori_tiles_pending     = 0
        self._mori_tiles_received    = 0
        self._mori_loading           = False
        # 検索結果にあるがレイヤー(GPKGキャッシュ)に形状が無い管理番号の
        # ピンポイント追補取得用。検索条件が変わるたび _search_mori でリセットする。
        self._mori_geom_fetch_attempted = set()
        self._mori_fill_missing      = set()
        self._mori_fill_pending      = 0
        self._mori_fill_received     = 0
        self._mori_fill_feats        = []
        # 森の力検索 API の500件上限を年度別スイープで回避するための状態
        self._current_mori_office_total = 0
        self._mori_sweep_active      = False
        self._mori_sweep_gen         = 0
        self._mori_sweep_office_total = 0
        self._mori_sweep_records     = {}
        self._mori_sweep_pending     = 0
        self._mori_sweep_done        = 0
        self._current_raw_keikaku    = None
        self._keikaku_vector_layer_id = None
        self._keikaku_layer_features  = []
        self._keikaku_layer_requested = False
        self._keikaku_loading         = False
        self._keikaku_cd_to_name      = {}
        self._keikaku_tiles_pending   = 0
        self._keikaku_tiles_received  = 0
        self._current_raw_rinchi      = None
        self._current_rinchi_cache_key = ''
        self._rinchi_vector_layer_id   = None
        self._rinchi_layer_features    = []
        self._rinchi_tiles_pending     = 0
        self._rinchi_tiles_received    = 0
        self._rinchi_loading           = False
        self._prev_tab_index           = -1
        self._first_show               = True
        self._layer_type               = 'gpkg'  # 'gpkg' | 'cd_gpkg' | 'shp'
        self._current_shinrinbo_key    = ''
        self._shinrinbo_api_ids        = []
        self._shinrinbo_generation     = 0
        self._shinrinbo_tab_index      = 0
        self._shinrinbo_col_map        = []
        self._mvt_tile_cache           = {}  # (z,x,y) → {key1: fid}
        self._info_gen                 = 0
        self._owner_feat               = None  # 選択中の小班タブで所有者情報を表示中の地物（伏せ字の再描画用）
        self._cloud_owner_feat         = None  # クラウド情報タブで所有者情報を続けている地物（森林簿の行の小班）
        self._cloud_info_parts         = []    # クラウド情報タブに表示中の表（'</table>' で終わる HTML 断片）
        self._privacy_masked           = False
        self._idle_watcher             = None
        self._layer_refresh_timer      = QTimer(self)
        self._layer_refresh_timer.setSingleShot(True)
        self._layer_refresh_timer.timeout.connect(self._refresh_layer_combo)

        self._build_ui()
        self._remove_rinchi_layers()
        self._connect_project_signals()

    # ------------------------------------------------------------------
    # コンテナ（ドック / 別ウィンドウ）切り替え
    # ------------------------------------------------------------------

    def set_window_mode_callback(self, callback):
        self._window_mode_callback = callback

    def set_window_mode_checked(self, checked):
        self.chk_separate_window.blockSignals(True)
        self.chk_separate_window.setChecked(checked)
        self.chk_separate_window.blockSignals(False)

    def set_hide_cleanup_suspended(self, suspended):
        self._suspend_hide_cleanup = suspended

    def _on_window_mode_toggled(self, checked):
        if self._window_mode_callback is not None:
            self._window_mode_callback(checked)

    # ------------------------------------------------------------------
    # UI 構築
    # ------------------------------------------------------------------

    def _build_ui(self):
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        root = QWidget()
        _outer.addWidget(root)
        main = QHBoxLayout(root)
        main.setContentsMargins(4, 4, 4, 4)
        main.setSpacing(4)

        # ── 左パネル ──────────────────────────────────────────────────
        left_w = QWidget()
        # 森の力の情報パネルが全項目（空欄含む）を出すので少し広めに確保する
        left_w.setMinimumWidth(330)
        left_w.setMaximumWidth(440)
        left_v = QVBoxLayout(left_w)
        left_v.setContentsMargins(0, 0, 0, 0)
        left_v.setSpacing(4)

        row_a = QHBoxLayout()
        row_a.addWidget(QLabel('計画図レイヤー:'))
        self.layer_combo = QComboBox()
        row_a.addWidget(self.layer_combo, 1)
        left_v.addLayout(row_a)

        self.left_tab = QTabWidget()

        selected_tab = QWidget()
        selected_v = QVBoxLayout(selected_tab)
        selected_v.setContentsMargins(0, 0, 0, 3)
        selected_v.setSpacing(4)
        self.info_browser = _InfoBrowser()
        self.info_browser.setOpenExternalLinks(False)
        selected_v.addWidget(self.info_browser, 1)
        self.lbl_selected = QLabel('計画図レイヤーで選択してください')
        self.lbl_selected.setStyleSheet('font-weight: bold; padding: 2px;')
        selected_v.addWidget(self.lbl_selected)
        self.left_tab.addTab(selected_tab, '選択中の小班')

        cloud_info_tab = QWidget()
        cloud_info_v = QVBoxLayout(cloud_info_tab)
        cloud_info_v.setContentsMargins(0, 0, 0, 3)
        cloud_info_v.setSpacing(4)
        self.cloud_info_browser = _InfoBrowser()
        self.cloud_info_browser.setOpenExternalLinks(False)
        self.cloud_info_browser.setPlaceholderText('右側の表で選択してください')
        cloud_info_v.addWidget(self.cloud_info_browser, 1)
        self.left_tab.addTab(cloud_info_tab, 'クラウド情報')

        _help_lbl = QLabel('<a href="#">マニュアル</a>')
        _help_lbl.setStyleSheet('font-size: 11px; padding-right: 4px; padding-bottom: 4px;')
        _help_lbl.linkActivated.connect(lambda _: self._open_manual())
        self.left_tab.setCornerWidget(_help_lbl, Qt.Corner.TopRightCorner)

        left_v.addWidget(self.left_tab, 1)

        _credit_lbl = QLabel('Developed by Avid Tree Work')
        _credit_lbl.setStyleSheet('color: gray; font-size: 10px;')
        _credit_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        left_v.addWidget(_credit_lbl)

        main.addWidget(left_w)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.NoFrame)
        sep.setFixedWidth(1)
        main.addWidget(sep)

        # ── 右パネル ──────────────────────────────────────────────────
        right_w = QWidget()
        right_v = QVBoxLayout(right_w)
        right_v.setContentsMargins(0, 0, 0, 0)
        right_v.setSpacing(0)

        self.cloud_tab = QTabWidget()
        self.cloud_tab.addTab(self._build_tab_shinrinbo(), '森林簿')
        self.cloud_tab.addTab(self._build_tab_hoanrin(),   '保安林台帳')
        self.cloud_tab.addTab(self._build_tab_seibi(),     '整備事業')
        self.cloud_tab.addTab(self._build_tab_mori(),      '森の力')
        self.cloud_tab.addTab(self._build_tab_keikaku(),   '経営計画')
        self.cloud_tab.addTab(self._build_tab_rinchi(),    '林地開発')

        self.chk_disable_zoom = QCheckBox('選択時のズームを無効化')
        self.chk_disable_zoom.setToolTip('表の行を選択したとき、地図の縮尺は変えず、中心だけを選択位置に合わせる')
        self.chk_disable_zoom.toggled.connect(self._on_disable_zoom_toggled)
        self.chk_separate_window = QCheckBox('別ウィンドウ')
        self.chk_separate_window.setToolTip('パネルを QGIS 本体から独立した別ウィンドウで表示する')
        self.chk_separate_window.toggled.connect(self._on_window_mode_toggled)
        _corner_w = QWidget()
        _corner_l = QHBoxLayout(_corner_w)
        _corner_l.setContentsMargins(0, 3, 0, 3)
        _corner_l.addWidget(self.chk_disable_zoom)
        _corner_l.addWidget(self.chk_separate_window)
        self.cloud_tab.setCornerWidget(_corner_w, Qt.Corner.TopRightCorner)

        right_v.addWidget(self.cloud_tab, 1)

        _btn_style = 'padding: 2px 10px;'
        _lbl_style = 'color: gray; font-size: 10px;'

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(2)

        bottom_row.addWidget(QLabel('レイヤー追加:'))
        self.btn_rindo = QPushButton('林道')
        self.btn_rindo.setCheckable(True)
        self.btn_rindo.setToolTip('林道 MVT レイヤーを追加/除去')
        self.btn_rindo.setStyleSheet(_TOGGLE_BTN_QSS_LAYER)
        bottom_row.addWidget(self.btn_rindo)

        self.btn_bg_cs3d = QPushButton(_BG_CS3D_NAME)
        self.btn_bg_cs3d.setToolTip(
            '森林クラウドの背景タイル「静岡県 微地形表現図」（CS立体図）を'
            'レイヤーとして追加する。追加済みならグレーアウト。\n'
            '通常のレイヤーとして残る（トグルではなく、プラグイン終了時に自動では消えない）')
        self.btn_bg_cs3d.setStyleSheet(_btn_style)
        bottom_row.addWidget(self.btn_bg_cs3d)
        bottom_row.addStretch()

        btn_data_info = QPushButton('データについて')
        btn_data_info.setToolTip('データについて')
        btn_data_info.setStyleSheet(_btn_style)
        btn_data_info.clicked.connect(self._show_data_info)
        bottom_row.addWidget(btn_data_info)
        bottom_row.addSpacing(8)

        self.btn_cache_save = QPushButton('ローカル保存')
        self.btn_cache_save.setToolTip('現在のデータをローカルDBに保存')
        self.btn_cache_save.setStyleSheet(_btn_style)
        self.lbl_cache_ts = QLabel('取得日時: —')
        self.lbl_cache_ts.setStyleSheet(_lbl_style)
        self.btn_cache_update = QPushButton('更新')
        self.btn_cache_update.setToolTip('APIから再取得してローカルDBを更新')
        self.btn_cache_update.setStyleSheet(_btn_style)
        bottom_row.addWidget(self.btn_cache_save)
        bottom_row.addSpacing(6)
        bottom_row.addWidget(self.lbl_cache_ts)
        bottom_row.addSpacing(4)
        bottom_row.addWidget(self.btn_cache_update)

        right_v.addLayout(bottom_row)
        main.addWidget(right_w, 1)

        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        self.btn_rindo.toggled.connect(lambda on: self._toggle_mvt_layer(
            'https://fcloud.pref.shizuoka.jp/MAP/MVT/MAGIS.RINDO/{z}/{x}/{y}.pbf',
            'fcloud_林道', on))
        self.btn_bg_cs3d.clicked.connect(self._add_bg_cs3d_layer)
        self.btn_cache_save.clicked.connect(self._save_current_cache)
        self.btn_cache_update.clicked.connect(self._update_current_cache)
        self.cloud_tab.currentChanged.connect(self._on_tab_changed)

        self._refresh_layer_combo()
        self._schedule_layer_combo_refresh()
        QTimer.singleShot(500, self._schedule_layer_combo_refresh)
        self._on_tab_changed(0)
        self._restore_state()

    def _show_data_info(self):
        QMessageBox.information(
            self,
            'データについて',
            '表示する保安林台帳・森の力再生事業・経営計画・林地開発などの情報は、'
            '静岡県が公開するオープンデータおよび静岡県森林クラウドの公開データをもとに、'
            'QGIS 上で参照しやすいよう取得・加工・編集して表示しています。',
        )

    def _open_manual(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manual.html')
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _build_tab_seibi(self):
        from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel
        w = QWidget()
        v = QVBoxLayout(w)
        lbl = QLabel('森林クラウド側未実装')
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet('color: gray; font-size: 13px; padding: 20px;')
        v.addWidget(lbl)
        return w

    # ------------------------------------------------------------------
    # 共通ユーティリティ
    # ------------------------------------------------------------------

    def _make_table(self, headers):
        from qgis.PyQt.QtWidgets import QTableWidget, QHeaderView, QFrame, QAbstractItemView
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        # Dock の端でネイティブ枠線が見切れることがあるため、表の外枠を明示する。
        t.setFrameShape(QFrame.Shape.NoFrame)
        t.setStyleSheet('QTableWidget { border: 1px solid palette(dark); }')
        self._enable_table_shortcuts(t)
        return t

    def _enable_table_shortcuts(self, table):
        """クラウド各表に共通の操作を付与する:
        ピクセル単位スクロール／Shift+ホイールで水平スクロール／
        Ctrl+C・右クリック「選択列をコピー」で見出し行＋選択行を TSV コピー。"""
        from qgis.PyQt.QtWidgets import QAbstractItemView
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        filt = _TableInputFilter(table)
        table.installEventFilter(filt)
        table.viewport().installEventFilter(filt)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table: self._show_table_context_menu(t, pos))

    def _show_table_context_menu(self, table, pos):
        from qgis.PyQt.QtWidgets import QMenu
        if not table.selectedIndexes():
            return
        menu = QMenu(table)
        menu.addAction('選択列をコピー', lambda: _copy_table_selection(table))
        menu.exec(table.viewport().mapToGlobal(pos))

    def _post_api(self, url, params, callback):
        body = QByteArray(urllib.parse.urlencode(params).encode('utf-8'))
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader,
                      'application/x-www-form-urlencoded')
        req.setRawHeader(b'Accept', b'application/json, text/plain, */*')
        req.setRawHeader(b'Origin', b'https://fcloud.pref.shizuoka.jp')
        req.setRawHeader(
            b'Referer',
            b'https://fcloud.pref.shizuoka.jp/fgis/?version=1.26.0220.a')
        reply = QgsNetworkAccessManager.instance().post(req, body)
        self._pending_replies.append(reply)
        reply.finished.connect(lambda: self._handle_reply(reply, callback))

    def _get_binary(self, url, callback):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b'Accept', b'*/*')
        req.setRawHeader(
            b'Referer',
            b'https://fcloud.pref.shizuoka.jp/fgis/?version=1.26.0525.a')
        reply = QgsNetworkAccessManager.instance().get(req)
        self._pending_replies.append(reply)
        reply.finished.connect(lambda: self._handle_binary_reply(reply, callback))

    def _handle_binary_reply(self, reply, callback):
        data = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = bytes(reply.readAll())
        else:
            print(f'[fcloud_shizuoka] MVT error: {reply.errorString()}')
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()
        callback(data)

    def _get_api(self, url, callback):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b'Accept', b'application/json, text/plain, */*')
        req.setRawHeader(b'Origin', b'https://fcloud.pref.shizuoka.jp')
        req.setRawHeader(
            b'Referer',
            b'https://fcloud.pref.shizuoka.jp/fgis/?version=1.26.0220.a')
        reply = QgsNetworkAccessManager.instance().get(req)
        self._pending_replies.append(reply)
        reply.finished.connect(lambda: self._handle_reply(reply, callback))

    def _on_cd_api_result(self, data):
        if data is None:
            self.info_browser.setHtml(
                '<p style="color:red;padding:8px;">取得失敗（ネットワークエラー）</p>'
            )
            return
        parts = ['<table style="border-collapse:collapse;width:100%;">']
        parts.append(self._info_title_row('基本情報（森林クラウド）'))
        for src, label in _CD_API_PRIMARY_FIELDS:
            val = data.get(src)
            if val is None or str(val) in ('', 'NULL', '0', 'None'):
                continue
            parts.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;">'
                f'{label}</td><td style="padding:1px 4px;">{val}</td></tr>')
        hist_rows = []
        for y_f, m_f, e_f in _CD_API_HISTORY_FIELDS:
            yr = data.get(y_f)
            if not yr or str(yr) in ('0', '', 'NULL', 'None'):
                continue
            method = data.get(m_f) or ''
            etype  = data.get(e_f) or ''
            if not method and not etype:
                continue
            hist_rows.append(
                f'<tr><td colspan="2" style="padding:2px 4px;'
                f'border-bottom:1px solid #eee;">'
                f'{yr}年度: {method}（{etype}）</td></tr>')
        if hist_rows:
            parts.append(self._info_title_row('施業履歴'))
            parts.extend(hist_rows)
        parts.append('</table>')
        self.info_browser.setHtml(''.join(parts))

    def _handle_reply(self, reply, callback):
        data = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            try:
                data = json.loads(bytes(reply.readAll()).decode('utf-8'))
            except Exception as e:
                print(f'[fcloud_shizuoka] JSON parse error: {e}')
        else:
            print(f'[fcloud_shizuoka] network error: {reply.errorString()}')
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        reply.deleteLater()
        callback(data)

    @staticmethod
    def _extract_records(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ('data', 'list', 'result', 'items'):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []

    def _get_db(self, data_name):
        home = QgsProject.instance().homePath()
        if not home:
            return None
        db_dir = os.path.join(home, 'fcloud_shizuoka')
        return CacheDB(os.path.join(db_dir, f'fcloud_shizuoka_{data_name}.db'))

    # ------------------------------------------------------------------
    # レイヤーツリー / 表示制御
    # ------------------------------------------------------------------

    def _add_layer_above_gpkg(self, layer, visible=True):
        QgsProject.instance().addMapLayer(layer, False)
        root = QgsProject.instance().layerTreeRoot()
        node = QgsLayerTreeLayer(layer)
        node.setItemVisibilityChecked(visible)
        connected = self._connected_layer
        if connected and not sip.isdeleted(connected):
            gpkg_node = root.findLayer(connected.id())
            if gpkg_node:
                parent = gpkg_node.parent()
                idx = list(parent.children()).index(gpkg_node)
                parent.insertChildNode(idx, node)
                return
        root.insertChildNode(0, node)

    def _refresh_map_canvas(self):
        QTimer.singleShot(0, self.iface.mapCanvas().refresh)

    def _remove_layers_by_name(self, *names):
        project = QgsProject.instance()
        for target in names:
            while True:
                found_id = None
                for lid, layer in list(project.mapLayers().items()):
                    if layer.name() == target:
                        found_id = lid
                        break
                if found_id is None:
                    break
                remove_project_layer(project, found_id)

    def _cleanup_plugin_layers(self):
        self._remove_mori_vector_layer()
        self._remove_keikaku_vector_layer()
        self._remove_rinchi_layers()
        self._toggle_mvt_layer('', 'fcloud_林道', False)
        for btn in (self.btn_mori_layer, self.btn_keikaku_layer, self.btn_rinchi_layer, self.btn_rindo):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        QSettings().setValue('fcloud_shizuoka/mori_layer_on', False)
        QSettings().setValue('fcloud_shizuoka/keikaku_layer_on', False)
        self._remove_layers_by_name(
            'fcloud_林道', 'fcloud_森の力実施箇所',
            'fcloud_経営計画作成箇所', '林地開発_許可', '林地開発_連絡調整',
        )
        self._refresh_map_canvas()

    def _set_layer_visible(self, layer_id, visible):
        if not layer_id:
            return False
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer_id)
        if node:
            node.setItemVisibilityChecked(visible)
            return True
        return False

    def _toggle_mvt_layer(self, url, name, on):
        from qgis.core import QgsVectorTileLayer
        if on:
            layer = QgsVectorTileLayer(f'type=xyz&url={url}', name)
            if layer.isValid():
                self._add_layer_above_gpkg(layer, visible=True)
        else:
            project = QgsProject.instance()
            for lid, layer in list(project.mapLayers().items()):
                if layer.name() == name:
                    remove_project_layer(project, lid)
                    break

    @staticmethod
    def _bg_cs3d_layer_exists():
        """「静岡県 微地形表現図」のタイルURLを使うレイヤーがプロジェクト内に
        既にあるか（このボタンで追加したかどうかは問わない。名前で判定すると
        手動で改名された場合に見失うため、ソースURLで判定する）。"""
        for layer in QgsProject.instance().mapLayers().values():
            try:
                uri = layer.dataProvider().dataSourceUri()
            except (AttributeError, RuntimeError):
                # RuntimeError: プロジェクト終了処理中でレイヤーのC++実体が
                # 既に破棄されている場合（sip: wrapped C/C++ object has been deleted）
                continue
            if 'CS_3D_MAP' in uri:
                return True
        return False

    def _update_bg_cs3d_btn_state(self, *_args):
        # layersAdded/layersRemoved 経由で呼ばれるため、QGIS終了処理中に
        # ボタン（ひいてはこのウィジェット自体）が既に破棄されている場合がある
        btn = getattr(self, 'btn_bg_cs3d', None)
        if btn is None or sip.isdeleted(btn):
            return
        btn.setEnabled(not self._bg_cs3d_layer_exists())

    def _add_layer_at_bottom(self, layer, visible=True):
        """背景タイル用。既存レイヤーの下（描画順で最背面）に追加する。"""
        QgsProject.instance().addMapLayer(layer, False)
        root = QgsProject.instance().layerTreeRoot()
        node = QgsLayerTreeLayer(layer)
        node.setItemVisibilityChecked(visible)
        root.insertChildNode(len(list(root.children())), node)

    def _add_bg_cs3d_layer(self):
        if self._bg_cs3d_layer_exists():
            self._update_bg_cs3d_btn_state()
            return
        encoded = urllib.parse.quote(_BG_CS3D_URL, safe='')
        layer = QgsRasterLayer(
            f'type=xyz&url={encoded}&zmax=18&zmin=4', _BG_CS3D_NAME, 'wms')
        if not layer.isValid():
            QMessageBox.warning(self, _BG_CS3D_NAME, 'レイヤーの追加に失敗しました。')
            return
        self._add_layer_at_bottom(layer)
        self._update_bg_cs3d_btn_state()
        self._refresh_map_canvas()

    # ------------------------------------------------------------------
    # タブ切替（接続部分のクリーンアップを統括）
    # ------------------------------------------------------------------

    def _restore_state(self):
        s = QSettings()
        tab = s.value('fcloud_shizuoka/tab_index', None, type=int)
        order_version = s.value('fcloud_shizuoka/tab_order_version', 0, type=int)
        if tab is None:
            tab = 0
        elif order_version < self._TAB_ORDER_VERSION:
            tab = self._migrate_tab_index(tab)
            s.setValue('fcloud_shizuoka/tab_index', tab)
        s.setValue('fcloud_shizuoka/tab_order_version', self._TAB_ORDER_VERSION)
        if 0 <= tab < self.cloud_tab.count():
            self.cloud_tab.setCurrentIndex(tab)
        # 状態の復元だけなので、保存処理（toggled）は走らせない
        self.chk_disable_zoom.blockSignals(True)
        self.chk_disable_zoom.setChecked(
            s.value('fcloud_shizuoka/disable_zoom', False, type=bool))
        self.chk_disable_zoom.blockSignals(False)

    @staticmethod
    def _migrate_tab_index(tab):
        old_to_new = {
            0: 1,  # 保安林台帳
            1: 2,  # 整備事業
            2: 3,  # 森の力
            3: 4,  # 経営計画
            4: 5,  # 林地開発
            5: 0,  # 森林簿
        }
        return old_to_new.get(tab, 0)

    def _on_tab_changed(self, index):
        prev = self._prev_tab_index
        needs_refresh = False
        if prev == 0 and index != 0:
            self._clear_selection_highlights()
        elif prev == 3 and index != 3:
            self._clear_mori_markers()
            needs_refresh = self._set_layer_visible(self._mori_vector_layer_id, False) or needs_refresh
        elif prev == 4 and index != 4:
            self._clear_selection_highlights()
            needs_refresh = self._set_layer_visible(self._keikaku_vector_layer_id, False) or needs_refresh
        if index == 3 and prev != 3:
            if self.btn_mori_layer.isChecked():
                needs_refresh = self._set_layer_visible(self._mori_vector_layer_id, True) or needs_refresh
        elif index == 4 and prev != 4:
            self._update_keikaku_load_btn()
            needs_refresh = self._sync_keikaku_layer_visibility(
                ensure_loaded=True) or needs_refresh
        self._prev_tab_index = index
        QSettings().setValue('fcloud_shizuoka/tab_index', index)
        QSettings().setValue('fcloud_shizuoka/tab_order_version', self._TAB_ORDER_VERSION)
        if needs_refresh:
            self._refresh_map_canvas()

        if index == 0:  # 森林簿
            if self._layer_type == 'gpkg':
                self.lbl_cache_ts.setText('ローカルデータ')
            elif self._current_shinrinbo_key:
                db = self._get_db('森林簿')
                ts = db.get_fetched_at(self._current_shinrinbo_key) if db else None
                self.lbl_cache_ts.setText(f'取得日時: {ts}' if ts else '取得日時: —')
            else:
                self.lbl_cache_ts.setText('取得日時: —')
        elif index == 1:
            self.lbl_cache_ts.setText(
                self._hoanrin_cache_status_text(self._hoanrin_scope_cities())
            )
        elif index == 3:
            city = self.combo_hoanrin_city.currentText().strip()
            norin = _CITY_TO_NORIN.get(city, '')
            if norin:
                self.combo_mori_norin.setCurrentText(norin)
            gpkg = self._get_mori_gpkg_path()
            if gpkg and os.path.exists(gpkg):
                import datetime
                mtime = os.path.getmtime(gpkg)
                ts = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                self.lbl_cache_ts.setText(f'レイヤーキャッシュ: {ts}')
            else:
                self.lbl_cache_ts.setText('レイヤーキャッシュ: なし')
        elif index == 4:
            db = self._get_db('経営計画')
            ts = db.get_fetched_at('経営計画/all') if db else None
            self.lbl_cache_ts.setText(f'取得日時: {ts}' if ts else '取得日時: —')
        elif index == 5:
            city = self.combo_hoanrin_city.currentText().strip()
            if city:
                i = self.combo_rinchi_city.findData(city)
                if i >= 0:
                    self.combo_rinchi_city.setCurrentIndex(i)
            self._current_rinchi_cache_key = self._RINCHI_ALL_CACHE_KEY
            db = self._get_db('林地開発')
            ts = db.get_fetched_at(self._RINCHI_ALL_CACHE_KEY) if db else None
            self.lbl_cache_ts.setText(f'取得日時: {ts}' if ts else '取得日時: —')
        else:
            self.lbl_cache_ts.setText('取得日時: —')
        self._update_cache_btn_states()

    # ------------------------------------------------------------------
    # キャッシュ操作
    # ------------------------------------------------------------------

    def _save_current_cache(self):
        tab = self.cloud_tab.currentIndex()
        if tab == 1:
            if self._current_raw_hoanrin is None:
                return
            city = self.combo_hoanrin_city.currentText().strip()
            if not city:
                return
            ts = self._save_hoanrin_city_cache(city, self._current_raw_hoanrin)
            if ts is None:
                return
            self.lbl_cache_ts.setText(f'取得日時: {ts}')
        elif tab == 3:
            if self._current_raw_mori is None or not self._current_mori_cache_key:
                return
            db = self._get_db('森の力')
            if db is None:
                return
            ts = db.put(self._current_mori_cache_key, self._current_raw_mori)
            self.lbl_cache_ts.setText(f'取得日時: {ts}')
        elif tab == 5:
            if self._current_raw_rinchi is None or not self._current_rinchi_cache_key:
                return
            ts = self._save_rinchi_cache_to_db()
            if ts:
                self.lbl_cache_ts.setText(f'取得日時: {ts}')
        self._update_cache_btn_states()

    def _update_cache_btn_states(self):
        ts = self.lbl_cache_ts.text()
        no_data = '—' in ts or ts.strip() == ''
        unsaved = '未保存' in ts
        tab = self.cloud_tab.currentIndex()

        if tab == 4:
            # 経営計画: APIから取得すると同時にGPKGへ自動保存 → ローカル保存は常に無効
            self.btn_cache_save.setEnabled(False)
            self.btn_cache_update.setEnabled(not no_data)
        elif tab == 2:
            # 整備事業: 未実装
            self.btn_cache_save.setEnabled(False)
            self.btn_cache_update.setEnabled(False)
        elif tab == 0:
            # 森林簿: 自動保存のためローカル保存不要。更新はAPIレイヤー選択時のみ有効
            self.btn_cache_save.setEnabled(False)
            can_update = (self._layer_type != 'gpkg'
                          and bool(self._current_shinrinbo_key)
                          and not no_data)
            self.btn_cache_update.setEnabled(can_update)
        elif tab == 1:
            # 保安林: 連携GPKGに含まれる市町村範囲をまとめて取得・更新できる。
            updating = '更新中' in ts
            can_update = (not updating
                          and self._get_db('保安林台帳') is not None
                          and bool(self._hoanrin_scope_cities()))
            self.btn_cache_save.setEnabled(unsaved and not updating)
            self.btn_cache_update.setEnabled(can_update)
        else:
            # 森の力・林地開発: 未保存なら保存可、保存済みなら更新可
            self.btn_cache_save.setEnabled(unsaved)
            self.btn_cache_update.setEnabled(not unsaved and not no_data)

    def _update_current_cache(self):
        tab = self.cloud_tab.currentIndex()
        if tab == 1:
            self._update_hoanrin_scope_cache()
        elif tab == 3:
            gpkg = self._get_mori_gpkg_path()
            if gpkg and os.path.exists(gpkg):
                try:
                    os.remove(gpkg)
                except OSError:
                    pass
            self._remove_mori_vector_layer()
            self.btn_mori_layer.setChecked(True)
            self._on_mori_layer_toggled(True)
        elif tab == 4:
            # GPKGは他地域のデータを保持するため削除せず、地域単位で上書きする
            self._remove_keikaku_vector_layer()
            self._keikaku_cd_to_name.clear()
            self.lbl_cache_ts.setText('取得日時: 更新中...')
            self._load_keikaku(force=True)
        elif tab == 5:
            self._invalidate_rinchi_layer_cache()
            self.lbl_cache_ts.setText('取得日時: 更新中...')
            self._search_rinchi(force=True, save_to_db=True)
        elif tab == 0:
            if self._layer_type != 'gpkg' and self._shinrinbo_api_ids:
                self._update_shinrinbo_cache()

    # ------------------------------------------------------------------
    # レイヤー管理（プロジェクト・GPKG 接続）
    # ------------------------------------------------------------------

    def _connect_project_signals(self):
        QgsProject.instance().layersAdded.connect(self._refresh_layer_combo)
        QgsProject.instance().layersRemoved.connect(self._refresh_layer_combo)
        QgsProject.instance().layersAdded.connect(self._update_bg_cs3d_btn_state)
        QgsProject.instance().layersRemoved.connect(self._update_bg_cs3d_btn_state)
        QgsProject.instance().readProject.connect(self._on_project_read)
        # 注意: iface.currentLayerChanged にはあえて接続しない。
        # レイヤーパネルでのクリックにプラグインの接続先が追従すると、
        # 意図せず接続GPKGが切り替わってしまうため。
        self._update_bg_cs3d_btn_state()

    def _on_project_read(self, *_):
        self._schedule_layer_combo_refresh()
        QTimer.singleShot(300, self._schedule_layer_combo_refresh)
        self._update_bg_cs3d_btn_state()

    def _schedule_layer_combo_refresh(self, delay_ms=0):
        self._layer_refresh_timer.start(max(0, int(delay_ms)))

    def _iter_selectable_layers(self):
        seen = set()
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            layer = node.layer()
            if layer is None:
                continue
            seen.add(layer.id())
            yield layer
        for layer in QgsProject.instance().mapLayers().values():
            if layer is None or layer.id() in seen:
                continue
            yield layer

    def _refresh_layer_combo(self, *_):
        """コンボの候補を再構築し、接続先を決定する。

        QGIS側でどのレイヤーがアクティブかには一切左右されない
        （レイヤーパネルでのクリックでプラグインの接続先が勝手に切り替わるのを防ぐため）。
        保存済みのレイヤーID→レイヤー名の順で前回接続先の復元のみ行い、
        どちらも見つからなければコンボは「空」のままにする。
        """
        settings = QSettings()
        current_id = (self.layer_combo.currentData()
                      or settings.value('fcloud_shizuoka/layer_id', ''))
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        # レイヤーパネルの並び順を優先しつつ、起動直後は mapLayers() も補助的に使う
        for layer in self._iter_selectable_layers():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.name().startswith('fcloud_'):
                continue
            src = layer.source().lower()
            if '.gpkg' not in src and '.shp' not in src:
                continue
            if layer.fields().indexOf('KEY1') < 0:
                continue
            self.layer_combo.addItem(layer.name(), layer.id())

        idx = -1
        if current_id:
            idx = self.layer_combo.findData(current_id)
            if idx < 0:
                saved_name = settings.value('fcloud_shizuoka/layer_name', '')
                if saved_name:
                    idx = self.layer_combo.findText(saved_name)
        # 見つからなければ明示的に「空」にする（Qtのデフォルト＝先頭候補への自動選択を防ぐ）
        self.layer_combo.setCurrentIndex(idx)
        self.layer_combo.blockSignals(False)
        new_id = self.layer_combo.currentData()
        if new_id != getattr(self, '_last_combo_id', None):
            self._last_combo_id = new_id
            self._on_layer_changed()

    def _connect_selection_signal(self):
        if self._connected_layer is not None and not sip.isdeleted(self._connected_layer):
            # 多重接続を避けるため、一旦切ってから繋ぎ直す（未接続でも例外は無視）
            try:
                self._connected_layer.selectionChanged.disconnect(self._on_selection_changed)
            except (TypeError, RuntimeError):
                pass  # 未接続時のTypeError/削除済みオブジェクトのRuntimeErrorは想定内
            try:
                self._connected_layer.selectionChanged.connect(self._on_selection_changed)
            except RuntimeError:
                pass  # チェックと接続の間でレイヤーが削除された場合のみ発生

    def _disconnect_selection_signal(self):
        if self._connected_layer is not None and not sip.isdeleted(self._connected_layer):
            try:
                self._connected_layer.selectionChanged.disconnect(self._on_selection_changed)
            except (TypeError, RuntimeError):
                pass  # 未接続時のTypeError/削除済みオブジェクトのRuntimeErrorは想定内

    def _on_layer_changed(self):
        self._last_combo_id = self.layer_combo.currentData()
        self._restore_selection_color()
        self._disconnect_selection_signal()
        self._connected_layer = None
        self._current_shinrinbo_key = ''
        self._shinrinbo_api_ids = []
        self._shinrinbo_col_map = []
        self.tbl_shinrinbo.setRowCount(0)
        self.tbl_shinrinbo.setColumnCount(0)

        layer_id = self.layer_combo.currentData()
        if not layer_id:
            self._apply_hoanrin_city_filter(None)
            self._apply_rinchi_city_filter(None)
        else:
            QSettings().setValue('fcloud_shizuoka/layer_id', layer_id)
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer:
                QSettings().setValue('fcloud_shizuoka/layer_name', layer.name())
                self._connected_layer = layer
                self.iface.setActiveLayer(layer)
                if self.isVisible():
                    self._connect_selection_signal()
                self._apply_selection_color(layer)
                flds = [f.name() for f in layer.fields()]
                if '市町村名称' in flds:
                    self._layer_type = 'gpkg'
                elif '市町村CD' in flds and 'ID' in flds:
                    self._layer_type = 'cd_gpkg'
                else:
                    self._layer_type = 'shp'
                self._init_shinrinbo_headers()
                self._apply_hoanrin_city_filter(layer)
                self._apply_rinchi_city_filter(layer)
        self._update_keikaku_load_btn()

    # ------------------------------------------------------------------
    # 連携レイヤーの選択ハイライト（塗り20%に変更・復元）
    # ------------------------------------------------------------------

    def _apply_selection_color(self, layer):
        # selectionProperties() は QGIS 3.30+ で追加されたAPI（本プラグインの
        # qgisMinimumVersion=3.14 環境では存在しない場合があるため未対応時はスキップ）
        if not hasattr(layer, 'selectionProperties'):
            return
        from qgis.core import Qgis
        sp = layer.selectionProperties()
        self._sel_color_layer_id = layer.id()
        self._sel_color_orig = QColor(sp.selectionColor())  # invalidな場合はコピーもinvalid
        self._sel_mode_orig = sp.selectionRenderingMode()

        base = sp.selectionColor()
        if not base.isValid():
            base = QgsProject.instance().selectionColor()
        faded = QColor(base)
        faded.setAlphaF(0.2)
        sp.setSelectionColor(faded)
        # selectionColor は selectionRenderingMode が CustomColor でないと反映されない
        sp.setSelectionRenderingMode(Qgis.SelectionRenderingMode.CustomColor)
        layer.triggerRepaint()
        self.iface.mapCanvas().refresh()

    def _restore_selection_color(self):
        if not self._sel_color_layer_id:
            return
        layer = QgsProject.instance().mapLayer(self._sel_color_layer_id)
        if layer and not sip.isdeleted(layer) and hasattr(layer, 'selectionProperties'):
            sp = layer.selectionProperties()
            sp.setSelectionColor(self._sel_color_orig or QColor())
            sp.setSelectionRenderingMode(self._sel_mode_orig)
            layer.triggerRepaint()
            self.iface.mapCanvas().refresh()
        self._sel_color_layer_id = None
        self._sel_color_orig = None
        self._sel_mode_orig = None

    # 市町村コンボは接続レイヤーの属性からは作らない:
    #   ・保安林: _apply_hoanrin_city_filter（現行市町村の固定リスト）
    #   ・林地開発: _apply_rinchi_city_filter（森林クラウドの固定リスト）
    # どちらも「計画図情報で絞込」ONのときだけレイヤーに出る市町村へ絞る。

    # ------------------------------------------------------------------
    # 選択小班 → 情報表示
    # ------------------------------------------------------------------

    def _on_selection_changed(self, selected_ids, deselected_ids, clear_and_select):
        if not self._connected_layer or sip.isdeleted(self._connected_layer):
            self._connected_layer = None
            return
        if not self._expanding_selection:
            seeds = list(selected_ids) if selected_ids else self._connected_layer.selectedFeatureIds()
            if selected_ids:
                # 展開すると getSelectedFeatures() は fid 順になり先頭が変わるため、
                # クリックした地物を左パネル表示用に覚えておく
                self._clicked_fid = seeds[0] if seeds else None
            if self._expand_selection_to_overlapping(seeds):
                # selectByIds が selectionChanged を同期再発火し、その中で最終状態を処理済み
                return
        features = list(self._connected_layer.getSelectedFeatures())
        cf = self._clicked_fid
        if cf is not None and len(features) > 1:
            features.sort(key=lambda f: 0 if f.id() == cf else 1)
        if not features:
            self.lbl_selected.setText('計画図レイヤーで選択してください')
            self.info_browser.clear()
            self._track_owner_info(None)
            self._refresh_shinrinbo_tab([])
            return
        feat = features[0]
        fnames = feat.fields().names()
        if self._layer_type == 'shp':
            key1 = feat['KEY1'] if 'KEY1' in fnames else ''
            self.lbl_selected.setText(f'小班: {key1}')
        elif self._layer_type == 'cd_gpkg':
            rinpan = feat['林班']      if '林班'      in fnames else ''
            junrin = feat['準林班CD']  if '準林班CD'  in fnames else ''
            kohan  = feat['小班_親番'] if '小班_親番' in fnames else ''
            self.lbl_selected.setText(f'小班: {rinpan}-{junrin}-{kohan}')
        else:
            rinpan = feat['林班_森林簿']     if '林班_森林簿'     in fnames else ''
            junrin = feat['準林班名称']       if '準林班名称'       in fnames else ''
            kohan  = feat['小班_親番_森林簿'] if '小班_親番_森林簿' in fnames else ''
            self.lbl_selected.setText(f'小班: {rinpan}-{junrin}-{kohan}')
        self._show_feature_info(feat)
        self.left_tab.setCurrentIndex(0)
        self._refresh_shinrinbo_tab(features)

    # ------------------------------------------------------------------
    # 重なり地物の自動追加選択（地点包含ベース）
    # ------------------------------------------------------------------

    def _expand_selection_to_overlapping(self, seed_ids):
        """選択された地物の「内部の一点」を覆う他地物をすべて選択に追加する。
        同じ位置に重なっている小班は QGIS 標準のクリック選択では最前面の1件しか
        取れず、下の小班の森林簿が参照できないため。形状の一致ではなく
        「その地点に重なっているか」で判定する（点包含なのでデジタイズの
        わずかな差では取りこぼさず、形が違う重なりも拾える）。"""
        layer = self._connected_layer
        if not seed_ids:
            return
        current = set(layer.selectedFeatureIds())
        want = set(current)
        for feat in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(seed_ids))):
            g = feat.geometry()
            if g is None or g.isEmpty():
                continue
            pt = g.pointOnSurface()
            if pt is None or pt.isEmpty():
                pt = g.centroid()
            if pt is None or pt.isEmpty():
                continue
            req = QgsFeatureRequest().setFilterRect(g.boundingBox())
            req.setNoAttributes()
            for cand in layer.getFeatures(req):
                cid = cand.id()
                if cid in want:
                    continue
                cg = cand.geometry()
                if cg is not None and not cg.isEmpty() and cg.contains(pt):
                    want.add(cid)
        if want != current:
            self._expanding_selection = True
            try:
                layer.selectByIds(list(want))
            finally:
                self._expanding_selection = False
            return True
        return False

    def _show_feature_info(self, feat):
        if self._layer_type in ('cd_gpkg', 'shp'):
            self._track_owner_info(None)
            self.info_browser.setHtml(
                '<p style="color:gray;padding:8px;">小班ID解決中...</p>'
            )
            self._info_gen += 1
            gen = self._info_gen

            def on_fid_resolved(fid_list):
                if self._info_gen != gen:
                    return
                fid = fid_list[0] if fid_list else None
                if fid is None:
                    self.info_browser.setHtml(
                        '<p style="color:red;padding:8px;">小班IDが特定できませんでした</p>'
                    )
                    return
                self._get_api(
                    f'{_API_BASE}/advanced-search/森林簿/{fid}',
                    lambda data: self._info_gen == gen and self._on_cd_api_result(data),
                )

            self._resolve_fids_via_mvt([feat], on_fid_resolved)
            return
        self._render_gpkg_info(feat)

    def _render_gpkg_info(self, feat):
        """gpkg 型の小班の基本情報・所有者情報・施業履歴を情報パネルへ描画する。"""
        fnames = feat.fields().names()
        parts = ['<table style="border-collapse:collapse;width:100%;">']

        parts.append(self._info_title_row('基本情報'))
        for src, label in _PRIMARY_FIELDS:
            if src not in fnames:
                continue
            val = feat[src]
            if val is None or str(val) in ('', 'NULL'):
                continue
            parts.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;">'
                f'{label}</td><td style="padding:1px 4px;">{val}</td></tr>')

        owner_rows = self._owner_rows(feat)
        parts.extend(owner_rows)
        self._track_owner_info(feat if owner_rows else None)

        hist_rows = []
        for y_f, m_f, e_f in _HISTORY_FIELDS:
            yr = feat[y_f] if y_f in fnames else None
            if not yr or str(yr) in ('0', '', 'NULL', 'None'):
                continue
            method = feat[m_f] if m_f in fnames else ''
            etype  = feat[e_f] if e_f in fnames else ''
            hist_rows.append(
                f'<tr><td colspan="2" style="padding:2px 4px;'
                f'border-bottom:1px solid #eee;">'
                f'{yr}年度: {method}（{etype}）</td></tr>')
        if hist_rows:
            parts.append(self._info_title_row('施業履歴'))
            parts.extend(hist_rows)

        parts.append('</table>')
        self.info_browser.setHtml(''.join(parts))

    # ------------------------------------------------------------------
    # 所有者情報の伏せ字（QGIS が一定時間無操作のとき）
    # ------------------------------------------------------------------

    def _owner_rows(self, feat):
        """feat の所有者情報を「項目名 | 値」の表の行にして返す（無ければ空）。
        項目名は先頭行だけで、住所など2行目以降は空にして続ける。
        伏せ字状態のときは値を伏せ字にする。"""
        fnames = feat.fields().names()
        vals = []
        for src in _OWNER_FIELDS:
            if src not in fnames:
                continue
            val = feat[src]
            if val is None or str(val) in ('', 'NULL'):
                continue
            vals.append(MASK_TEXT if self._privacy_masked else html.escape(str(val)))
        rows = []
        for i, val in enumerate(vals):
            label = _OWNER_LABEL if i == 0 else ''
            rows.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                f'{label}</td><td style="padding:1px 4px;">{val}</td></tr>')
        return rows

    def _track_owner_info(self, feat):
        """選択中の小班タブに所有者情報を表示中の地物を覚える（無ければ None）。"""
        self._owner_feat = feat
        self._update_privacy_guard()

    def _update_privacy_guard(self):
        """所有者情報をどちらかのタブに表示している間だけ、
        無操作の監視（アプリ全体のイベントフィルタ）を有効にする。"""
        if self._owner_feat is not None or self._cloud_owner_feat is not None:
            self._start_privacy_guard()
        else:
            self._stop_privacy_guard()

    def _start_privacy_guard(self):
        if self._idle_watcher is None:
            self._idle_watcher = IdleWatcher(IDLE_TIMEOUT_MS, self)
            self._idle_watcher.idled.connect(self._on_user_idle)
            # 入力イベントの処理中に再描画しないよう、解除は次のイベントループで行う
            self._idle_watcher.resumed.connect(
                self._on_user_resumed, Qt.ConnectionType.QueuedConnection)
        self._idle_watcher.start()

    def _stop_privacy_guard(self):
        if self._idle_watcher is not None:
            self._idle_watcher.stop()
        self._privacy_masked = False

    def _on_user_idle(self):
        self._set_privacy_masked(True)

    def _on_user_resumed(self):
        self._set_privacy_masked(False)

    def _set_privacy_masked(self, masked):
        if masked == self._privacy_masked:
            return
        self._privacy_masked = masked
        # 再描画でスクロール位置が先頭へ戻らないようにする
        if self._owner_feat is not None:
            bar = self.info_browser.verticalScrollBar()
            pos = bar.value()
            self._render_gpkg_info(self._owner_feat)
            bar.setValue(pos)
        if self._cloud_owner_feat is not None:
            bar = self.cloud_info_browser.verticalScrollBar()
            pos = bar.value()
            self._render_cloud_info()
            bar.setValue(pos)

    @staticmethod
    def _info_title_row(title):
        """情報パネルの見出し行（緑の帯）。基本情報・森林簿など、枠内の先頭に置くタイトルで共通に使う。"""
        return ('<tr><td colspan="2" style="background:#e8f4e8;font-weight:bold;'
                f'padding:3px;">{title}</td></tr>')

    def _show_cloud_table_row_info(self, title, table, row, owner_feat=None):
        """owner_feat を渡すと、クラウドの情報の後に区切りと所有者情報を続ける。"""
        if table is None or row < 0:
            self._clear_cloud_record_info()
            return
        parts = ['<table style="border-collapse:collapse;width:100%;">',
                 self._info_title_row(title)]
        for col in range(table.columnCount()):
            header_item = table.horizontalHeaderItem(col)
            item = table.item(row, col)
            if header_item is None or item is None:
                continue
            label = header_item.text().strip()
            val = item.text().strip()
            if not label or val == '':
                continue
            parts.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                f'{label}</td><td style="padding:1px 4px;">{val}</td></tr>')
        parts.append('</table>')
        self._set_cloud_info(parts, owner_feat)
        self.left_tab.setCurrentIndex(1)

    def _set_cloud_info(self, parts, owner_feat=None):
        """クラウド情報タブへ表を表示する。parts は '</table>' で終わる HTML 断片のリスト。
        owner_feat を渡すと、末尾に区切りと、計画図レイヤー情報として所有者情報を続ける。"""
        self._cloud_info_parts = parts
        self._cloud_owner_feat = owner_feat
        self._render_cloud_info()
        self._update_privacy_guard()

    def _render_cloud_info(self):
        parts = list(self._cloud_info_parts)
        owner_rows = (self._owner_rows(self._cloud_owner_feat)
                      if self._cloud_owner_feat is not None else [])
        if owner_rows:
            # クラウドの情報の後に区切りを入れ、出どころの違う見出し（緑の帯に対する灰色の帯）を付けて
            # 所有者情報を続ける
            parts[-1:-1] = [
                '<tr><td colspan="2"><hr></td></tr>',
                '<tr><td colspan="2" style="background:#eeeeee;color:#555555;font-weight:bold;padding:3px;">'
                f'{_OWNER_SOURCE_TITLE}</td></tr>',
            ] + owner_rows
        else:
            self._cloud_owner_feat = None
        self.cloud_info_browser.setHtml(''.join(parts))

    def _clear_cloud_record_info(self):
        self._cloud_info_parts = []
        self._cloud_owner_feat = None
        self.cloud_info_browser.clear()
        self._update_privacy_guard()

    # ------------------------------------------------------------------
    # 複数タブで共有する選択ハイライト
    # ------------------------------------------------------------------

    def _clear_selection_highlights(self):
        scene = self.iface.mapCanvas().scene()
        for rb in self._selection_highlights:
            if not sip.isdeleted(rb):
                scene.removeItem(rb)
        self._selection_highlights.clear()

    def _add_selection_highlight(self, geom, src_crs=None):
        from qgis.core import QgsGeometry as _G
        canvas = self.iface.mapCanvas()
        dst_crs = canvas.mapSettings().destinationCrs()
        g = _G(geom)
        if src_crs and src_crs != dst_crs:
            g.transform(QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance()))
        geom_type = g.type()
        rb_type = (QgsWkbTypes.GeometryType.PolygonGeometry if geom_type == 2
                   else QgsWkbTypes.GeometryType.LineGeometry if geom_type == 1
                   else QgsWkbTypes.GeometryType.PointGeometry)
        rb = QgsRubberBand(canvas, rb_type)
        rb.setColor(_HL_SEL_BORDER)
        rb.setFillColor(_HL_SEL_FILL)
        rb.setWidth(2)
        rb.setToGeometry(g)
        rb.show()
        self._selection_highlights.append(rb)
        return g.boundingBox()

    def _on_disable_zoom_toggled(self, checked):
        QSettings().setValue('fcloud_shizuoka/disable_zoom', checked)

    def _fit_canvas_to(self, extent):
        """表の行選択に伴って、地図を選択位置へ移す。「選択時のズームを無効化」がオンのときは
        縮尺を変えず、中心だけを extent の中心に合わせる。"""
        canvas = self.iface.mapCanvas()
        if self.chk_disable_zoom.isChecked():
            canvas.setCenter(extent.center())
        else:
            canvas.setExtent(extent)
        canvas.refresh()

    def _zoom_to_selection_highlights(self, margin_ratio=0.60, min_padding=5):
        bbox = None
        for rb in self._selection_highlights:
            if sip.isdeleted(rb):
                continue
            try:
                geom = rb.asGeometry()
            except Exception:
                geom = None
            if not geom or geom.isEmpty():
                continue
            b = geom.boundingBox()
            if bbox is None:
                bbox = b
            else:
                bbox.combineExtentWith(b)
        if bbox and not bbox.isEmpty():
            pad = max(bbox.width(), bbox.height()) * margin_ratio + min_padding
            bbox.grow(pad)
            self._fit_canvas_to(bbox)
            return True
        return False

    # ------------------------------------------------------------------
    # アンロード / クローズ
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if self._suspend_hide_cleanup:
            return  # コンテナ（ドック⇔別ウィンドウ）切り替え中の一時的な show は無視
        if self._connected_layer is not None and not sip.isdeleted(self._connected_layer):
            if self._sel_color_layer_id is None:
                self._apply_selection_color(self._connected_layer)
            self._connect_selection_signal()
            self._on_selection_changed(None, None, None)
        if self._owner_feat is not None or self._cloud_owner_feat is not None:
            self._start_privacy_guard()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._suspend_hide_cleanup:
            return  # コンテナ切り替え中の一時的な hide は無視
        self._disconnect_selection_signal()
        self._stop_privacy_guard()

    def _teardown_visible_state(self):
        """ドック純正 ✕ / 別ウィンドウのクローズ時に、選択色・ハイライト・
        プラグイン生成レイヤー・保留リクエストを片付ける。"""
        self._restore_selection_color()
        self._clear_hoanrin_highlights()
        self._clear_selection_highlights()
        self._clear_mori_markers()
        self._cleanup_plugin_layers()
        for reply in list(self._pending_replies):
            try:
                reply.abort()
            except RuntimeError:
                pass  # 終了処理中に既に削除済みのreplyへのabort()は無視してよい

    def closeEvent(self, event):
        self._teardown_visible_state()
        super().closeEvent(event)

    def cleanup_on_unload(self):
        self._stop_privacy_guard()
        try:
            QgsProject.instance().readProject.disconnect(self._on_project_read)
        except (TypeError, RuntimeError):
            pass  # 未接続時のTypeError/削除済みオブジェクトのRuntimeErrorは想定内
        # 終了処理中はプロジェクトのレイヤーが一斉に破棄されlayersAdded/layersRemoved
        # が連発しうる。ここで切っておけば、その後ウィジェットが破棄されていても
        # ハンドラ自体が呼ばれなくなる（_update_bg_cs3d_btn_state 等のガードとは
        # 別に、そもそも発火させない対策）。
        project = QgsProject.instance()
        for sig, slot in (
            (project.layersAdded, self._refresh_layer_combo),
            (project.layersRemoved, self._refresh_layer_combo),
            (project.layersAdded, self._update_bg_cs3d_btn_state),
            (project.layersRemoved, self._update_bg_cs3d_btn_state),
        ):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass  # 未接続時のTypeError/削除済みオブジェクトのRuntimeErrorは想定内
        self._teardown_visible_state()
