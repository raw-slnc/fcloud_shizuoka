# -*- coding: utf-8 -*-
import os
import sip
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon

PLUGIN_DIR = os.path.dirname(__file__)


class FcloudShizuoka:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self._highlights = []  # プラグインインスタンスをまたいで存続するハイライトリスト

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

    def unload(self):
        self.iface.removeVectorToolBarIcon(self.action)
        self.iface.removePluginVectorMenu('静岡県森林クラウド', self.action)
        if self.dock:
            self.dock.cleanup_on_unload()
            self.iface.mapCanvas().refresh()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
        self.action = None
        self.dock = None

    def _toggle_dock(self, checked):
        if checked and self.dock._first_show:
            self.dock.setVisible(True)
            self.dock._first_show = False
            layer = self.dock._connected_layer
            if layer and not sip.isdeleted(layer):
                QTimer.singleShot(200, lambda l=layer: self.iface.layerTreeView().setCurrentLayer(l))
        elif not checked:
            self.dock.cleanup_on_unload()
            self.dock.setVisible(False)
            self.dock._first_show = True
        else:
            self.dock.setVisible(True)
