# -*- coding: utf-8 -*-
"""地位指数マップの書き出し（森林資源情報タブ）。

林野庁「航空機 LiDAR データを使った地位指数分布図の作成の手引き」（2022年3月）の作業8と同じ式で、
取得したメッシュ（20m）の樹高・樹種と、計画図などの小班の林齢から、地位指数（林齢40年時の樹高）を求め、
GeoTIFF に書き出す。Morizon Next の「取込」（手引 4.2 の地位指数マップ）でそのまま使える。

厳密さは追わない。代わりに、使った条件（森林簿の基準年・森林計測年・樹高の列・式と係数）を
GeoTIFF のメタデータに残し、どういう前提で作ったマップかを後から見られるようにする。
"""
import datetime
import math
import os

from qgis.PyQt.QtCore import QSettings, QThread, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGridLayout, QLabel, QSpinBox, QVBoxLayout,
)
from qgis.core import Qgis, QgsIconUtils, QgsProject, QgsVectorLayer
from qgis.gui import QgsFieldComboBox, QgsFileWidget

# 樹種_統一 の値 → (コード, 表示名)。手引きの対象はスギ・ヒノキの人工林
SPECIES = {'スギ': (1, 'スギ'), 'ヒノキ類': (2, 'ヒノキ')}
# 手引き p.66〜67 の例（ミッチャーリッヒ式）。地域の地位指数曲線があれば書き換える
# 地位指数曲線の係数の選択肢（キー: (表示名, {樹種コード: (b, k)})）。精度の詰めはこれから。
# 徳島県：手引き p.64〜67 の例（徳島県スギ林分収穫表 2012・ヒノキ林分収穫表 2013）。
# 静岡県：「あたらしい林業技術・システム収穫表」図1・図2（樹高成長曲線、長伐期対応の実線）の地位Ⅲの曲線を
# 図から読み取って当てはめた値。公表された係数ではない（地位Ⅱ・Ⅳでもほぼ同じ b・k になることは確かめた）
COEF_PRESETS = {
    'tokushima': ('徳島県の林分収穫表（手引きの例）', {1: (0.9607, 0.026), 2: (0.9677, 0.0269)}),
    'shizuoka': ('静岡県の樹高成長曲線（図から読み取り・公表値ではない）', {1: (0.899, 0.0116), 2: (0.918, 0.0101)}),
}
DEFAULT_PRESET = 'tokushima'
DEFAULT_COEF = COEF_PRESETS[DEFAULT_PRESET][1]
CUSTOM_LABEL = '手入力'
BASE_AGE = 40               # 地位指数の基準林齢（スギ・ヒノキは40年）
MIN_AGE = 5                 # これより若い林は、樹高から地位を求めにくいので値なしにする
SI_RANGE = (5.0, 45.0)      # 樹高と林齢が合わない（林齢の誤りなど）とみなして値なしにする範囲の外
OLD_MEASURE_YEARS = 10      # 森林計測年がこれより前なら注意を出す
NODATA = -9999.0
HEIGHT_COLUMNS = ('平均樹高', '樹高')
AGE_COLUMNS = ('林齢', '樹立林齢１', '樹立林齢1')
REGISTER_SPECIES_COLUMNS = ('樹種名称', '樹種１', '樹種1', '樹種')
# 森林簿（小班のレイヤー）の樹種のうち、スギ・ヒノキとみなす値
REGISTER_CONIFERS = ('杉', 'スギ', 'すぎ', '桧', '檜', 'ヒノキ', 'ひのき')
_SETTINGS = 'fcloud_shizuoka/si_export/'


def height_column(field_names):
    return next((name for name in HEIGHT_COLUMNS if name in field_names), None)


def site_index(height, age, b, k):
    """ミッチャーリッヒ式：a = H /(1 - b·exp(-k·林齢))、SI = a·(1 - b·exp(-k·40))"""
    a = height / (1.0 - b * math.exp(-k * age))
    return a * (1.0 - b * math.exp(-k * BASE_AGE))


def reiwa_year(yr_label):
    """'R1' → 2019。読めなければ None"""
    if yr_label and yr_label[:1] == 'R' and yr_label[1:].isdigit():
        return 2018 + int(yr_label[1:])
    return None


class SiteIndexWorker(QThread):
    """メッシュと小班の林齢をマス目にして、地位指数を求めて GeoTIFF に書き出す（画面を止めない）"""
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(str, dict)   # (出力のパス, 集計)
    failed = pyqtSignal(str)

    def __init__(self, job):
        super().__init__()
        self.job = job
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            path, summary = self._run()
            if path:
                self.finished_ok.emit(path, summary)
        except Exception as e:  # noqa: BLE001  画面に理由を出すため、まとめて受ける
            self.failed.emit(str(e))

    def _run(self):
        import numpy as np
        from osgeo import gdal, ogr
        gdal.UseExceptions()
        job = self.job
        mesh = ogr.Open(job['mesh_gpkg'])
        layer = mesh.GetLayerByName(job['mesh_layer'])
        srs = layer.GetSpatialRef()
        cell = float(job['cell'])
        # メッシュの格子にそろえる（先頭の地物の左下を格子の基準にする）
        first = layer.GetNextFeature()
        fx, _, fy, _ = first.GetGeometryRef().GetEnvelope()
        layer.ResetReading()
        xmin, xmax, ymin, ymax = layer.GetExtent()
        xmin = fx + math.floor((xmin - fx) / cell) * cell
        ymin = fy + math.floor((ymin - fy) / cell) * cell
        cols = int(math.ceil((xmax - xmin) / cell))
        rows = int(math.ceil((ymax - ymin) / cell))
        transform = (xmin, cell, 0.0, ymin + rows * cell, 0.0, -cell)

        def grid():
            ds = gdal.GetDriverByName('MEM').Create('', cols, rows, 1, gdal.GDT_Float32)
            ds.SetGeoTransform(transform)
            ds.SetProjection(srs.ExportToWkt())
            ds.GetRasterBand(1).Fill(NODATA)
            ds.GetRasterBand(1).SetNoDataValue(NODATA)
            return ds

        def burn(layer_name, source=None, attribute=None, value=None, where=None, into=None):
            """レイヤーの地物を格子に焼き込む（列の値か、決まった値）。SQL の文を組み立てず、
            地物の絞り込み（where）だけを使う。into を渡すと、その格子に重ねて焼き込む"""
            ds = into or grid()
            options = {'layers': [layer_name], 'where': where}
            if attribute:
                options['attribute'] = attribute
            else:
                options['burnValues'] = [value]
            # 既にある格子に焼き込む（値なしで埋めてある。initValues を渡すと新しく作る扱いになる）
            gdal.Rasterize(ds, source or mesh, **options)
            return ds

        def values(ds):
            return ds.GetRasterBand(1).ReadAsArray().astype('float64')

        table = job['mesh_layer']
        self.progress.emit(10)
        height = values(burn(table, attribute=job['height_column']))
        self.progress.emit(25)
        species_ds = grid()
        for name, (code, _) in SPECIES.items():
            burn(table, value=code, where=f"\"樹種_統一\" = '{name}'", into=species_ds)
        species = values(species_ds)
        self.progress.emit(40)
        # 森林計測年は「2020-01-01」のような文字なので、出てくる年ごとに、その年の値を焼き込む
        layer.SetIgnoredFields([layer.GetLayerDefn().GetFieldDefn(i).GetName()
                                for i in range(layer.GetLayerDefn().GetFieldCount())
                                if layer.GetLayerDefn().GetFieldDefn(i).GetName() != '森林計測年'] + ['OGR_GEOMETRY'])
        years = {str(f.GetField('森林計測年'))[:4] for f in layer if f.GetField('森林計測年')}
        layer.SetIgnoredFields([])
        layer.ResetReading()
        measure_ds = grid()
        for year in sorted(y for y in years if y.isdigit()):
            burn(table, value=int(year), where=f"\"森林計測年\" LIKE '{year}%'", into=measure_ds)
        measure = values(measure_ds)
        fallback_year = job.get('fallback_measure_year')
        measure_missing = measure == NODATA
        if fallback_year:
            measure[measure_missing] = fallback_year
        if self._cancel:
            return None, {}

        # 小班の林齢：メッシュの範囲だけを、メッシュの座標系に変換してからマス目にする
        bounds = (xmin, ymin, xmin + cols * cell, ymin + rows * cell)
        gdal.VectorTranslate(
            '/vsimem/si_age.gpkg', job['age_source'], format='GPKG', dstSRS=srs.ExportToWkt(),
            layers=[job['age_layer']] if job.get('age_layer') else None,
            spatFilter=bounds, spatSRS=srs.ExportToWkt(),
            selectFields=[job['age_field']] + ([job['species_field']] if job.get('species_field') else []),
            layerName='age', geometryType='PROMOTE_TO_MULTI')
        age_src = ogr.Open('/vsimem/si_age.gpkg', 1)
        self.progress.emit(55)
        age = values(burn('age', source=age_src, attribute=job['age_field']))
        # 森林簿の樹種（1：スギ・ヒノキ、2：それ以外、値なし：列が無い・空）。計算には使わず、
        # 航空レーザの樹種（メッシュ）と食い違うマスの数を知らせるだけにする（樹種はメッシュを優先する）
        register = None
        if job.get('species_field'):
            # 小班が重なる所でも林齢と同じ小班の樹種になるよう、地物ごとに区分の列を足して、林齢と同じ順で焼き込む
            age_layer = age_src.GetLayerByName('age')
            age_layer.CreateField(ogr.FieldDefn('si_register', ogr.OFTInteger))
            for feature in age_layer:
                name = feature.GetField(job['species_field'])
                if name:
                    feature.SetField('si_register', 1 if str(name).strip() in REGISTER_CONIFERS else 2)
                    age_layer.SetFeature(feature)
            register = values(burn('age', source=age_src, attribute='si_register'))
        age_src = None
        gdal.Unlink('/vsimem/si_age.gpkg')
        self.progress.emit(70)

        # 計測時の林齢 ＝ 森林簿の林齢 −（森林簿の基準年 − 森林計測年）
        age_at_measure = age - (job['base_year'] - measure)
        out = np.full(height.shape, NODATA, dtype='float64')
        counts = {'target': 0, 'written': 0, 'no_age': 0, 'young': 0, 'mismatch': 0}
        for code, (b, k) in job['coef'].items():
            target = (species == code) & (height > 0)
            counts['target'] += int(target.sum())
            has_age = target & (age != NODATA) & (measure != NODATA)
            counts['no_age'] += int((target & ~has_age).sum())
            old_enough = has_age & (age_at_measure >= MIN_AGE)
            counts['young'] += int((has_age & ~old_enough).sum())
            h = height[old_enough]
            t = age_at_measure[old_enough]
            values = h / (1.0 - b * np.exp(-k * t)) * (1.0 - b * math.exp(-k * BASE_AGE))
            plausible = (values >= SI_RANGE[0]) & (values <= SI_RANGE[1])
            counts['mismatch'] += int((~plausible).sum())
            values[~plausible] = NODATA
            out[old_enough] = values
        counts['written'] = int((out != NODATA).sum())
        if register is not None:
            counts['register_other'] = int(((out != NODATA) & (register == 2)).sum())
        valid_measure = measure[(measure != NODATA) & (species > 0)]
        counts['measure_min'] = int(valid_measure.min()) if valid_measure.size else None
        counts['measure_max'] = int(valid_measure.max()) if valid_measure.size else None
        counts['measure_fallback'] = bool(fallback_year) and bool((measure_missing & (species > 0)).any())
        if self._cancel:
            return None, {}
        self.progress.emit(85)

        os.makedirs(os.path.dirname(job['output']), exist_ok=True)
        dst = gdal.GetDriverByName('GTiff').Create(
            job['output'], cols, rows, 1, gdal.GDT_Float32, options=['COMPRESS=DEFLATE', 'TILED=YES'])
        dst.SetGeoTransform(transform)
        dst.SetProjection(srs.ExportToWkt())
        band = dst.GetRasterBand(1)
        band.SetNoDataValue(NODATA)
        band.WriteArray(out.astype('float32'))
        band.SetDescription('地位指数（林齢40年時の上層木平均樹高の推定 m）')
        dst.SetMetadata(self._metadata(counts))
        dst.FlushCache()
        dst = None
        self.progress.emit(100)
        return job['output'], counts

    def _metadata(self, counts):
        job = self.job
        coef = '、'.join(f"{SPECIES_LABEL[code]} b={b:g} k={k:g}" for code, (b, k) in job['coef'].items())
        if counts.get('measure_min') is None:
            measure = '不明'
        elif counts['measure_min'] == counts['measure_max']:
            measure = str(counts['measure_min'])
        else:
            measure = f"{counts['measure_min']}〜{counts['measure_max']}"
        if counts.get('measure_fallback'):
            measure += '（記録の無い所はデータの計測年度）'
        return {
            'SI_METHOD': '航空機 LiDAR データを使った地位指数分布図の作成の手引き（林野庁 2022）の作業8と同じ式',
            'SI_CURVE': (f'ミッチャーリッヒ式 SI = H/(1-b·exp(-k·林齢))·(1-b·exp(-k·{BASE_AGE}))。{coef}'
                         f"（係数：{job.get('coef_source', CUSTOM_LABEL)}）"),
            'SI_HEIGHT': f"メッシュの「{job['height_column']}」（{job['mesh_name']}）",
            'SI_AGE': f"{job['age_layer_name']} の「{job['age_field']}」。森林簿の基準年 {job['base_year']}",
            'SI_MEASURE_YEAR': measure,
            'SI_AGE_CORRECTION': '計測時の林齢 = 林齢 -（基準年 - 森林計測年）',
            'SI_EXCLUDED': (f'スギ・ヒノキ以外、林齢なし、計測時の林齢 {MIN_AGE} 年未満、'
                            f'地位指数 {SI_RANGE[0]:g}〜{SI_RANGE[1]:g} m の外（樹高と林齢が合わない）'),
            **({'SI_REGISTER_SPECIES': (
                f"地位指数を求めたマス {counts['written']:,} のうち、森林簿（{job['species_field']}）の樹種が"
                f"スギ・ヒノキでない小班のマス {counts['register_other']:,}。樹種はメッシュ（航空レーザ）を優先し、"
                "計算からは外していない。林齢は参考値として使っているので、樹種の調査が必要な場所の目安になる"
            )} if 'register_other' in counts else {}),
            'SI_CREATED': datetime.date.today().isoformat(),
            'SI_CREATOR': 'fcloud_shizuoka',
        }


SPECIES_LABEL = {code: label for code, label in SPECIES.values()}


class SiteIndexDialog(QDialog):
    """地位指数マップを書き出す設定の窓。OK で job（SiteIndexWorker に渡す辞書の一部）を作る"""

    def __init__(self, parent, mesh_name, default_output):
        super().__init__(parent)
        self.setWindowTitle('地位指数マップを書き出す')
        self.setMinimumWidth(520)
        st = QSettings()
        v = QVBoxLayout(self)
        about = QLabel(
            '表示中のメッシュの樹高と樹種（スギ・ヒノキ）に、小班の林齢を合わせて、地位指数（林齢40年時の樹高）を'
            '求め、GeoTIFF に書き出します。式は林野庁「航空機 LiDAR データを使った地位指数分布図の作成の手引き」の'
            '作業8と同じです。使った条件はファイルに記録されます。<br>'
            f'<span style="color:gray;">メッシュ：{mesh_name}</span>')
        about.setWordWrap(True)
        v.addWidget(about)

        form = QFormLayout()
        # 候補はレイヤーパネルの並び（QgsMapLayerComboBox は名前の順に並べ、並べ方を変えられない）。
        # 窓を開いている間はレイヤーパネルを触れないので、開いたときに一度だけ作る
        self.age_layer = QComboBox()
        self.age_layer.setToolTip('林齢の列がある小班のレイヤー（森林計画図など）。ファイル（GPKG・SHP）のレイヤーに限る')
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            layer = node.layer()
            if isinstance(layer, QgsVectorLayer) and layer.geometryType() == Qgis.GeometryType.Polygon:
                self.age_layer.addItem(QgsIconUtils.iconForLayer(layer), layer.name(), layer.id())
        self.age_field = QgsFieldComboBox()
        self.age_layer.currentIndexChanged.connect(lambda _index: self._on_age_layer(self._age_layer()))
        form.addRow('林齢のレイヤー', self.age_layer)
        form.addRow('林齢の列', self.age_field)
        self.species_field = QgsFieldComboBox()
        self.species_field.setAllowEmptyFieldName(True)
        self.species_field.setToolTip('森林簿の樹種の列。計算には使わず、メッシュ（航空レーザ）の樹種と食い違うマスの数を知らせる')
        form.addRow('森林簿の樹種の列', self.species_field)
        self.base_year = QSpinBox()
        self.base_year.setRange(1950, 2100)
        self.base_year.setValue(st.value(_SETTINGS + 'base_year', datetime.date.today().year, type=int))
        self.base_year.setToolTip('林齢が何年時点の値か（森林簿の基準年）。森林計測年との差だけ林齢を補正する')
        form.addRow('森林簿の基準年', self.base_year)
        v.addLayout(form)

        coef_box = QGridLayout()
        coef_box.addWidget(QLabel('地位指数曲線（ミッチャーリッヒ式）の係数'), 0, 0)
        # 係数の出典を選ぶと b・k を入れ直す。数値を書き換えたら「手入力」になる
        self.coef_preset = QComboBox()
        for key, (label, _) in COEF_PRESETS.items():
            self.coef_preset.addItem(label, key)
        self.coef_preset.addItem(CUSTOM_LABEL, None)
        coef_box.addWidget(self.coef_preset, 0, 1, 1, 2)
        coef_box.addWidget(QLabel('b'), 1, 1)
        coef_box.addWidget(QLabel('k'), 1, 2)
        self.coef_spins = {}
        for row, (code, (b, k)) in enumerate(DEFAULT_COEF.items(), start=2):
            coef_box.addWidget(QLabel(SPECIES_LABEL[code]), row, 0)
            spins = []
            for col, (value, key) in enumerate(((b, 'b'), (k, 'k')), start=1):
                spin = QDoubleSpinBox()
                spin.setDecimals(4)
                spin.setRange(0.0001, 5.0)
                spin.setSingleStep(0.001)
                spin.setValue(st.value(f'{_SETTINGS}{code}_{key}', value, type=float))
                spin.setMinimumHeight(spin.sizeHint().height())  # 窓が小さくても数字が潰れないように
                coef_box.addWidget(spin, row, col)
                spins.append(spin)
            self.coef_spins[code] = spins
        note = QLabel('徳島県の値は手引き p.64〜67 の例、静岡県の値は県の「システム収穫表」の図から読み取った参考値です。'
                      '地域の地位指数曲線があれば書き換えてください。')
        note.setStyleSheet('color: gray;')
        note.setWordWrap(True)
        coef_box.addWidget(note, len(DEFAULT_COEF) + 2, 0, 1, 3)
        v.addLayout(coef_box)
        saved = st.value(_SETTINGS + 'coef_preset', DEFAULT_PRESET, type=str)
        index = self.coef_preset.findData(saved if saved in COEF_PRESETS else None)
        self.coef_preset.setCurrentIndex(index if index >= 0 else 0)
        self._sync_preset_label()
        self.coef_preset.currentIndexChanged.connect(self._on_coef_preset)
        for spins in self.coef_spins.values():
            for spin in spins:
                spin.valueChanged.connect(lambda _value: self._sync_preset_label())

        self.output = QgsFileWidget()
        self.output.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.output.setFilter('GeoTIFF (*.tif)')
        self.output.setFilePath(default_output)
        out_form = QFormLayout()
        out_form.addRow('書き出し先', self.output)
        v.addLayout(out_form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('書き出す')
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self.error = QLabel('')
        self.error.setStyleSheet('color: #b00020;')
        v.addWidget(self.error)

        self._pick_default_age_layer()
        # 既定の大きさで欄や文字が潰れないよう、中身が収まる大きさで開く（折り返しの説明文の高さも含める）
        width = max(self.minimumWidth(), self.sizeHint().width())
        self.resize(width, self.layout().totalHeightForWidth(width))

    def showEvent(self, event):
        super().showEvent(event)
        # 基準年の欄の高さを、上の選択欄が実際に描かれた高さにそろえる。
        # 高さはテーマや文字の大きさで変わり、見積もり（sizeHint）とずれることがあるので、並べ終わってから測る
        QTimer.singleShot(0, self._match_base_year_height)

    def _match_base_year_height(self):
        # 入力欄は枠の描き方の違いで、同じ高さだと選択欄より低く見えるので +1 する
        height = self.age_layer.height() + 1
        if height > 1 and self.base_year.height() != height:
            self.base_year.setFixedHeight(height)

    def _on_coef_preset(self, _index):
        key = self.coef_preset.currentData()
        if key not in COEF_PRESETS:
            return
        for code, (b, k) in COEF_PRESETS[key][1].items():
            b_spin, k_spin = self.coef_spins[code]
            for spin, value in ((b_spin, b), (k_spin, k)):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

    def _sync_preset_label(self):
        """今の b・k がどの出典の値と同じかを選択欄に出す（どれとも違えば「手入力」）"""
        current = {code: (round(b.value(), 4), round(k.value(), 4)) for code, (b, k) in self.coef_spins.items()}
        match = next((key for key, (_, coef) in COEF_PRESETS.items()
                      if all(current[c] == (round(b, 4), round(k, 4)) for c, (b, k) in coef.items())), None)
        index = self.coef_preset.findData(match)
        if index >= 0 and index != self.coef_preset.currentIndex():
            self.coef_preset.blockSignals(True)
            self.coef_preset.setCurrentIndex(index)
            self.coef_preset.blockSignals(False)

    def _age_layer(self):
        layer_id = self.age_layer.currentData()
        return QgsProject.instance().mapLayer(layer_id) if layer_id else None

    def _pick_default_age_layer(self):
        """林齢の列を持つ、ファイルの小班レイヤーのうち、レイヤーパネルで一番上のものを選んでおく（計画図を優先）"""
        for index in range(self.age_layer.count()):
            layer = QgsProject.instance().mapLayer(self.age_layer.itemData(index))
            if layer is None or layer.providerType() != 'ogr':
                continue
            names = layer.fields().names()
            if any(name in names for name in AGE_COLUMNS) and '計画図' in layer.name():
                self.age_layer.setCurrentIndex(index)
                break
        self._on_age_layer(self._age_layer())

    def _on_age_layer(self, layer):
        self.age_field.setLayer(layer)
        self.species_field.setLayer(layer)
        if layer is not None:
            names = layer.fields().names()
            self.species_field.setField(next((name for name in REGISTER_SPECIES_COLUMNS if name in names), ''))
        if layer is not None:
            names = layer.fields().names()
            for name in AGE_COLUMNS:
                if name in names:
                    self.age_field.setField(name)
                    break

    def _accept(self):
        layer = self._age_layer()
        field = self.age_field.currentField()
        output = self.output.filePath()
        if layer is None or not field:
            self.error.setText('林齢のレイヤーと列を選んでください。')
            return
        if layer.providerType() != 'ogr':
            self.error.setText('林齢のレイヤーは、ファイル（GPKG・SHP）のレイヤーを選んでください。')
            return
        if not output:
            self.error.setText('書き出し先を指定してください。')
            return
        if not output.lower().endswith('.tif'):
            output += '.tif'
        source, _, options = layer.source().partition('|')
        layer_name = None
        for part in options.split('|'):
            if part.startswith('layername='):
                layer_name = part[len('layername='):]
        st = QSettings()
        st.setValue(_SETTINGS + 'base_year', self.base_year.value())
        coef = {}
        for code, (b_spin, k_spin) in self.coef_spins.items():
            coef[code] = (b_spin.value(), k_spin.value())
            st.setValue(f'{_SETTINGS}{code}_b', b_spin.value())
            st.setValue(f'{_SETTINGS}{code}_k', k_spin.value())
        preset = self.coef_preset.currentData()
        st.setValue(_SETTINGS + 'coef_preset', preset or '')
        self.job = {
            'age_source': source, 'age_layer': layer_name, 'age_field': field,
            'species_field': self.species_field.currentField(),
            'age_layer_name': layer.name(), 'base_year': self.base_year.value(),
            'coef': coef, 'output': output,
            'coef_source': COEF_PRESETS[preset][0] if preset in COEF_PRESETS else CUSTOM_LABEL,
        }
        self.accept()
