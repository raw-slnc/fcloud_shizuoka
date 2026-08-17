# -*- coding: utf-8 -*-


def classFactory(iface):
    from .plugin import FcloudShizuoka
    return FcloudShizuoka(iface)
