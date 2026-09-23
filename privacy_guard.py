# -*- coding: utf-8 -*-
"""無操作の検知。QGIS 内で一定時間入力がなければ idled、その後の最初の入力で resumed を出す。
所有者情報のような人目に触れさせたくない表示を、離席中に伏せ字にするために使う。"""
import time

from qgis.PyQt.QtCore import QCoreApplication, QEvent, QObject, QTimer, pyqtSignal

# この時間、QGIS 内で入力がなければ無操作とみなす
IDLE_TIMEOUT_MS = 3 * 60 * 1000

# 伏せ字。文字数から内容を推測されないよう、実際の長さによらず固定長にする
MASK_TEXT = '＊＊＊＊＊＊＊'

# 無操作の判定間隔。分単位の判定なので粗くてよい（入力のたびにタイマーを再始動するより軽い）
_CHECK_INTERVAL_MS = 5000

# 「操作」とみなす入力。アプリ全体のフィルタは全イベントで呼ばれるため、
# ここに無いイベントは即戻る
_INPUT_EVENTS = frozenset({
    QEvent.MouseMove,
    QEvent.MouseButtonPress,
    QEvent.MouseButtonDblClick,
    QEvent.Wheel,
    QEvent.KeyPress,
    QEvent.TouchBegin,
})


class IdleWatcher(QObject):
    """アプリ全体の入力を見て、無操作の開始（idled）と操作の再開（resumed）を通知する。
    start() から stop() の間だけアプリにイベントフィルタを入れる。"""

    idled   = pyqtSignal()
    resumed = pyqtSignal()

    def __init__(self, timeout_ms, parent=None):
        super().__init__(parent)
        self._timeout    = timeout_ms / 1000.0
        self._last_input = time.monotonic()
        self._idle       = False
        self._running    = False
        self._timer      = QTimer(self)
        self._timer.setInterval(_CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check)

    def start(self):
        if self._running:
            return
        app = QCoreApplication.instance()
        if app is None:
            return
        self._running    = True
        self._idle       = False
        self._last_input = time.monotonic()
        app.installEventFilter(self)
        self._timer.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._idle    = False
        self._timer.stop()
        app = QCoreApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def _check(self):
        if not self._idle and time.monotonic() - self._last_input >= self._timeout:
            self._idle = True
            self.idled.emit()

    def eventFilter(self, obj, event):
        # spontaneous() は OS 由来の実際の入力だけ。Qt が内部で合成するイベントは数えない
        if event.type() in _INPUT_EVENTS and event.spontaneous():
            self._last_input = time.monotonic()
            if self._idle:
                self._idle = False
                self.resumed.emit()
        return False
