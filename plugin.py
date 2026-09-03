# -*- coding: utf-8 -*-
import os
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import (
    QAction, QApplication, QDialog, QDockWidget, QVBoxLayout,
)
from qgis.PyQt.QtCore import Qt, QTimer, QSettings
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProject
from .layer_cleanup import remove_project_layer

PLUGIN_DIR = os.path.dirname(__file__)
_TITLE = '静岡県森林クラウド'


class FcloudShizuoka:
    def __init__(self, iface):
        self.iface = iface
        self.window = None   # FcloudWindow: 実体の UI。コンテナ間で使い回す
        self.dock = None     # FcloudDock: QGIS 格納用の薄いドック
        self.dialog = None   # QDialog: 「別ウィンドウ」表示用のトップレベル
        self.action = None
        self._highlights = []  # プラグインインスタンスをまたいで存続するハイライトリスト
        self._is_shutting_down = False
        self._switching_container = False

    # ------------------------------------------------------------------
    # initGui / unload
    # ------------------------------------------------------------------

    def initGui(self):
        # 前セッションの残留ハイライトをクリア
        canvas = self.iface.mapCanvas()
        scene = canvas.scene()
        for hl in self._highlights:
            try:
                if not sip.isdeleted(hl):
                    scene.removeItem(hl)
            except RuntimeError:
                pass  # チェック後にシーン側で既に削除された場合のみ発生
        self._highlights.clear()
        canvas.refresh()

        import sys
        for key in list(sys.modules.keys()):
            if key.startswith('fcloud_shizuoka.'):
                del sys.modules[key]

        self._ensure_window()
        self._create_dock()
        self.dock.hide()

        icon = QIcon(os.path.join(PLUGIN_DIR, 'icon.png'))
        self.action = QAction(icon, _TITLE, self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle)

        self.iface.addVectorToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu(_TITLE, self.action)

        self._remove_stale_layers()
        QgsProject.instance().readProject.connect(self._remove_stale_layers)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_about_to_quit)

    def _remove_stale_layers(self, *args):
        stale_names = {
            'fcloud_林道', 'fcloud_森の力実施箇所',
            'fcloud_経営計画作成箇所', '林地開発_許可', '林地開発_連絡調整',
        }
        project = QgsProject.instance()
        to_remove = [
            lid for lid, layer in list(project.mapLayers().items())
            if layer.name() in stale_names
        ]
        for lid in to_remove:
            remove_project_layer(project, lid)
        root = project.layerTreeRoot()
        while root.findGroup('林地開発'):
            root.removeChildNode(root.findGroup('林地開発'))

    def unload(self):
        try:
            QgsProject.instance().readProject.disconnect(self._remove_stale_layers)
        except (TypeError, RuntimeError):
            pass  # 未接続時のTypeError/削除済みオブジェクトのRuntimeErrorは想定内
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.disconnect(self._on_about_to_quit)
            except (TypeError, RuntimeError):
                pass  # 未接続時のTypeError/削除済みオブジェクトのRuntimeErrorは想定内
        if self.action is not None:
            self.iface.removeVectorToolBarIcon(self.action)
            self.iface.removePluginVectorMenu(_TITLE, self.action)
        if self.window is not None:
            self._shutdown_window()
        if self.dialog is not None:
            self._save_dialog_geometry()
            self.dialog.hide()
            self.dialog.deleteLater()
            self.dialog = None
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()  # setWidget 済みの window も一緒に破棄される
            self.dock = None
        self.iface.mapCanvas().refresh()
        self.action = None
        self.window = None

    def _shutdown_window(self):
        if self._is_shutting_down or self.window is None:
            return
        self._is_shutting_down = True
        try:
            self.window.cleanup_on_unload()
        finally:
            self._is_shutting_down = False

    def _on_about_to_quit(self):
        self._shutdown_window()

    # ------------------------------------------------------------------
    # コンテナ生成 / 切り替え
    # ------------------------------------------------------------------

    def _ensure_window(self):
        if self.window is None:
            from .dock_widget import FcloudWindow
            self.window = FcloudWindow(self.iface, highlights=self._highlights)
            self.window.set_window_mode_callback(self._set_window_mode)

    def _create_dock(self):
        if self.dock is not None:
            return
        from .dock_widget import FcloudDock
        self._ensure_window()
        self.dock = FcloudDock(_TITLE, self.iface.mainWindow())
        self.dock.setObjectName('FcloudShizuokaDock')
        self.dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        # ネイティブ floating は無効（フロート⇔格納の遷移で Windows/Qt6 に透過残像バグ）
        self.dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        self.dock.setWidget(self.window)
        self.iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock)
        self.dock.visibilityChanged.connect(self._on_visibility_changed)
        self.dock.closed.connect(self._on_container_closed)

    def _create_dialog(self):
        if self.dialog is not None:
            return
        self._ensure_window()
        self.dialog = QDialog(None, Qt.WindowType.Window)
        self.dialog.setObjectName('FcloudShizuokaWindow')
        self.dialog.setWindowTitle(_TITLE)
        self.dialog.setWindowIcon(QIcon(os.path.join(PLUGIN_DIR, 'icon.png')))
        layout = QVBoxLayout(self.dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.window)
        # dock.setWidget(None) は実体ウィジェットを明示的に hide() するため、
        # 別ウィンドウ側へ移したあとは show() し直さないと中身が真っ白になる。
        self.window.show()
        geometry = QSettings().value('fcloud_shizuoka/window_geometry', None)
        if geometry:
            self.dialog.restoreGeometry(geometry)
        else:
            self.dialog.resize(1180, 640)
        self.dialog.finished.connect(self._on_dialog_finished)

    def _save_dialog_geometry(self):
        if self.dialog is not None:
            QSettings().setValue(
                'fcloud_shizuoka/window_geometry', self.dialog.saveGeometry()
            )

    def _set_window_mode(self, enabled):
        """「別ウィンドウ」チェックボックスのトグルで呼ばれる。実体（self.window）を
        ドックと QDialog の間で付け替え、使わない側のコンテナは破棄する。"""
        self._ensure_window()
        self._switching_container = True
        self.window.set_hide_cleanup_suspended(True)
        try:
            if enabled:
                if self.dock is not None:
                    try:
                        self.dock.visibilityChanged.disconnect(
                            self._on_visibility_changed)
                        self.dock.closed.disconnect(self._on_container_closed)
                    except TypeError:
                        pass
                    self.dock.setWidget(None)
                    self.iface.removeDockWidget(self.dock)
                    self.dock.deleteLater()
                    self.dock = None
                self._create_dialog()
                self.dialog.show()
                self.dialog.raise_()
                self.dialog.activateWindow()
            else:
                if self.dialog is not None:
                    self._save_dialog_geometry()
                    try:
                        self.dialog.finished.disconnect(self._on_dialog_finished)
                    except TypeError:
                        pass
                    layout = self.dialog.layout()
                    if layout is not None:
                        layout.removeWidget(self.window)
                    self.window.setParent(None)
                    self.dialog.hide()
                    self.dialog.deleteLater()
                    self.dialog = None
                self._create_dock()
                self.window.show()
                self.dock.show()
                self.dock.raise_()
        finally:
            self._switching_container = False
            w = self.window
            QTimer.singleShot(
                0,
                lambda: (w.set_hide_cleanup_suspended(False)
                         if w is not None else None),
            )
        if self.action is not None:
            self.action.setChecked(True)

    # ------------------------------------------------------------------
    # 可視状態 / トグル
    # ------------------------------------------------------------------

    def _on_visibility_changed(self, visible):
        """ドックのあらゆる可視状態変化（ツールバートグル / ✕ / タブ移動）を拾う。"""
        if self._switching_container:
            return
        if self.action is not None:
            self.action.setChecked(visible)
        if not visible and self.window is not None:
            self.window._first_show = True

    def _on_container_closed(self):
        """ドック純正の ✕ で閉じられたとき（＝明確な終了操作）だけ本格クリーンアップ。"""
        if self._switching_container or self.window is None:
            return
        self.window._teardown_visible_state()
        self.window._first_show = True

    def _on_dialog_finished(self, *args):
        """別ウィンドウをウィンドウ ✕ で閉じたとき。ツールバートグルでの hide() は
        finished を出さないため、ここは通らない（＝軽い hide とは区別される）。"""
        if self._switching_container or self.window is None:
            return
        self._save_dialog_geometry()
        self.window._teardown_visible_state()
        self.window._first_show = True
        if self.action is not None:
            self.action.setChecked(False)

    def _show_container(self, show):
        if self.dialog is not None:
            if show:
                self.dialog.show()
                self.dialog.raise_()
                self.dialog.activateWindow()
            else:
                self.dialog.hide()
        elif self.dock is not None:
            self.dock.setVisible(show)

    def _toggle(self, checked):
        w = self.window
        if w is None:
            return
        if checked and w._first_show:
            self._show_container(True)
            w._first_show = False
            w._sync_keikaku_layer_visibility(ensure_loaded=True)
            layer = w._connected_layer
            if layer and not sip.isdeleted(layer):
                QTimer.singleShot(
                    200,
                    lambda ly=layer: self.iface.layerTreeView().setCurrentLayer(ly),
                )
        elif not checked:
            self._show_container(False)
        else:
            self._show_container(True)
            w._sync_keikaku_layer_visibility(ensure_loaded=True)
