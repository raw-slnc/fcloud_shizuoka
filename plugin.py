# -*- coding: utf-8 -*-
import os
import sip
from qgis.PyQt.QtWidgets import QAction, QApplication
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProject
from .layer_cleanup import remove_project_layer

PLUGIN_DIR = os.path.dirname(__file__)
_TITLE = '静岡県森林クラウド'


class FcloudShizuoka:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None      # FcloudDockWidget: 薄い格納コンテナ
        self.window = None    # FcloudWindow: UI 実体
        self.action = None
        self._highlights = []  # プラグインインスタンスをまたいで存続するハイライトリスト
        self._is_shutting_down = False

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
        from .dock_widget import FcloudDockWidget, FcloudWindow
        self.window = FcloudWindow(self.iface, highlights=self._highlights)
        self.dock = FcloudDockWidget(_TITLE, self.iface.mainWindow())
        self.dock.setObjectName('FcloudShizuokaDock')
        self.dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        self.dock.setWidget(self.window)
        self.window.attach_dock_widget(self.dock)
        self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
        self.dock.hide()

        icon = QIcon(os.path.join(PLUGIN_DIR, 'icon.png'))
        self.action = QAction(icon, _TITLE, self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle_dock)
        self.dock.visibilityChanged.connect(self._on_dock_visibility_changed)
        self.dock.closed.connect(self._on_dock_closed)

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
        self.iface.removeVectorToolBarIcon(self.action)
        self.iface.removePluginVectorMenu(_TITLE, self.action)
        if self.dock:
            self._shutdown_window()
            self.iface.mapCanvas().refresh()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()  # setWidget 済みの window も一緒に破棄される
        self.action = None
        self.dock = None
        self.window = None

    def _shutdown_window(self):
        if self._is_shutting_down or not self.window:
            return
        self._is_shutting_down = True
        try:
            self.window.cleanup_on_unload()
        finally:
            self._is_shutting_down = False

    def _on_about_to_quit(self):
        self._shutdown_window()

    def _on_dock_visibility_changed(self, visible):
        """ツールバーのトグルだけでなく、ドック純正の✕ボタンでの非表示も含めて
        あらゆる経路の可視状態変化を拾う。非表示になったら次回表示時に再び
        「初回表示」扱いにする（✕ボタンは_toggle_dockを経由しないため、
        ここでリセットしないと接続レイヤーの自動アクティブ化が働かなくなる）。"""
        self.action.setChecked(visible)
        if not visible and self.window is not None:
            self.window._first_show = True

    def _on_dock_closed(self):
        """ドック純正の✕で閉じられたとき（＝明確な終了操作）だけ本格クリーンアップ。"""
        if self.window is not None:
            self.window._teardown_visible_state()
            self.window._first_show = True

    def _toggle_dock(self, checked):
        w = self.window
        if w is None:
            return
        if checked and w._first_show:
            self.dock.setVisible(True)
            w._first_show = False
            w._sync_keikaku_layer_visibility(ensure_loaded=True)
            layer = w._connected_layer
            if layer and not sip.isdeleted(layer):
                QTimer.singleShot(200, lambda ly=layer: self.iface.layerTreeView().setCurrentLayer(ly))
        elif not checked:
            self.dock.setVisible(False)
        else:
            self.dock.setVisible(True)
            w._sync_keikaku_layer_visibility(ensure_loaded=True)
