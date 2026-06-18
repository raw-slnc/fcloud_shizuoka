# -*- coding: utf-8 -*-
import os
import sip
from qgis.PyQt.QtWidgets import QAction, QApplication
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProject

PLUGIN_DIR = os.path.dirname(__file__)


class FcloudShizuoka:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
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
            except Exception:
                pass
        self._highlights.clear()
        canvas.refresh()

        import sys
        for key in list(sys.modules.keys()):
            if key.startswith('fcloud_shizuoka.'):
                del sys.modules[key]
        from .dock_widget import FcloudDockWidget
        self.dock = FcloudDockWidget(self.iface, highlights=self._highlights)
        self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
        self.dock.hide()

        icon = QIcon(os.path.join(PLUGIN_DIR, 'icon.png'))
        self.action = QAction(icon, '静岡県森林クラウド', self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle_dock)
        self.dock.visibilityChanged.connect(self.action.setChecked)

        self.iface.addVectorToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu('静岡県森林クラウド', self.action)

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
        to_remove = [
            lid for lid, layer in list(QgsProject.instance().mapLayers().items())
            if layer.name() in stale_names
        ]
        for lid in to_remove:
            QgsProject.instance().removeMapLayer(lid)
        root = QgsProject.instance().layerTreeRoot()
        while root.findGroup('林地開発'):
            root.removeChildNode(root.findGroup('林地開発'))

    def unload(self):
        try:
            QgsProject.instance().readProject.disconnect(self._remove_stale_layers)
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.disconnect(self._on_about_to_quit)
            except Exception:
                pass
        self.iface.removeVectorToolBarIcon(self.action)
        self.iface.removePluginVectorMenu('静岡県森林クラウド', self.action)
        if self.dock:
            self._shutdown_dock()
            self.iface.mapCanvas().refresh()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
        self.action = None
        self.dock = None

    def _shutdown_dock(self):
        if self._is_shutting_down or not self.dock:
            return
        self._is_shutting_down = True
        try:
            self.dock.cleanup_on_unload()
        finally:
            self._is_shutting_down = False

    def _on_about_to_quit(self):
        self._shutdown_dock()

    def _toggle_dock(self, checked):
        if checked and self.dock._first_show:
            self.dock.setVisible(True)
            self.dock._first_show = False
            self.dock._sync_keikaku_layer_visibility(ensure_loaded=True)
            layer = self.dock._connected_layer
            if layer and not sip.isdeleted(layer):
                QTimer.singleShot(200, lambda l=layer: self.iface.layerTreeView().setCurrentLayer(l))
        elif not checked:
            self.dock.setVisible(False)
            self.dock._first_show = True
        else:
            self.dock.setVisible(True)
            self.dock._sync_keikaku_layer_visibility(ensure_loaded=True)
