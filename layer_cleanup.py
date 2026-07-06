# -*- coding: utf-8 -*-
import sip


def _detach_layers_from_snapping(project, layers):
    valid_layers = [
        layer for layer in layers
        if layer is not None and not sip.isdeleted(layer)
    ]
    if not valid_layers:
        return False

    snapping_config = project.snappingConfig()
    if not snapping_config.removeLayers(valid_layers):
        return False

    project.setSnappingConfig(snapping_config)
    return True


def remove_project_layer(project, layer_id):
    if not layer_id:
        return False

    layer = project.mapLayer(layer_id)
    if layer is None or sip.isdeleted(layer):
        return False

    _detach_layers_from_snapping(project, [layer])
    project.removeMapLayer(layer_id)
    return True
