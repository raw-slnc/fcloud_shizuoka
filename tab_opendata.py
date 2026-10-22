# -*- coding: utf-8 -*-
"""森林資源情報タブ（G空間情報センター「静岡県森林資源解析データ」）。

森林クラウドには無いオープンデータを取得し、プロジェクトのフォルダの
fcapital_data_shizuoka/shared/ に GPKG として保存して表示する。
取得（ZIP のダウンロード）は QgsFileDownloader、GPKG への変換はワーカースレッドで行い、
QGIS の画面を止めない。表示するデータ（種類）は常に1つ。
"""
import datetime
import html
import json
import os
import re

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QMessageBox,
    QStackedWidget, QTextBrowser, QCheckBox, QSpinBox,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import (
    Qt, QUrl, QThread, pyqtSignal, QModelIndex, QItemSelectionModel, QTimer, QSettings,
)
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsNetworkAccessManager,
    QgsFileDownloader, QgsVectorLayerCache, QgsFeatureRequest, QgsGeometry,
)
from qgis.gui import (
    QgsAttributeTableView, QgsAttributeTableModel, QgsAttributeTableFilterModel,
)

from .constants import (
    _FCDS_API_PACKAGE_SHOW, _FCDS_DATASETS, _FCDS_KEIKU_CODE, _FCDS_SHARED_SUBDIR,
    _FCDS_SELECTION_RGBA, _FCDS_SPECIES_COLORS, _FCDS_SPECIES_NORM, _FCDS_SPECIES_RULES,
    _FCDS_TYPES,
)

# ZIP 名：市町（面積の大きい順）_R○計測(林相識別図以外)_番号 ／ 市町_R○計測_林相識別図_番号[-分割]
# 「林相識別図-1-4」のような表記揺れも許す
_ZIP_NAME_RE = re.compile(
    r'^(?P<city>.+?)_(?P<yr>R\d+)計測'
    r'(?P<kind>\(林相識別図以外\)|_林相識別図)[_-]?(?P<no>\d+)(?:-(?P<part>\d+))?\.zip$')

# 林相識別図以外 ZIP の中のレイヤー（フォルダ名, ファイル名に含むものを優先する語, 除外語, 出力レイヤー名）。
# フォルダの中身は ZIP によって違う（03 がメッシュでなく小班ごとの集計のもの、ファイル名に
# 「メッシュ」が無いものがある）。優先する語を含むファイルが無ければ、フォルダの中の残りを使う
_ZIP_LAYERS = {
    'species': ('01_樹種ポリゴン', '樹種ポリゴン', '林小班', '樹種ポリゴン'),
    'mesh':    ('03_森林資源量集計ポリゴン', 'メッシュ', '林小班', 'メッシュ'),
    'points':  ('02_単木ポイント', '単木ポイント', None, '単木ポイント'),
}

# 変換方式の版。2：文字コード（CP932）を明示する前に作った GPKG は文字化けしているので取得し直す。
# 3：樹種_統一 の作り方を判定ルール（_FCDS_SPECIES_RULES）に変えた
_CONV_VERSION = 3

_GROUP_NAME = '森林資源情報'
# 以前の版のグループ名。片付け・入れ替えのときに一緒に探す
_GROUP_NAME_OLD = '森林資源情報（オープンデータ）'
_TAB_INDEX = 6
_STYLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'styles')

# 終了待ちの上限を超えた変換スレッドを、完了まで生存させる。
# 実行中の QThread が Python の参照切れで破棄されると QGIS ごと落ちるため。
_OD_ORPHANED_WORKERS = set()


# データの形（メッシュかポリゴンか）を見分けるフォルダ。どちらに格子のデータが入っているかは解析範囲によって違う
_SHAPE_FOLDERS = ('mesh', 'species')


def _classify_areas(areas):
    """先頭の地物の面積（㎡）から、データの形を決める。
    ほとんど（9割以上）が 10m・20m 四方のセルの面積ならメッシュ、そうでなければポリゴン。"""
    sizes = [round(a) for a in areas]
    for cell, a in ((10, 100), (20, 400)):
        if sizes and sum(1 for s in sizes if s == a) >= len(sizes) * 0.9:
            return {'shape': 'grid', 'cell': cell}
    return {'shape': 'polygon', 'cell': None}


def _shape_label(info):
    """データの形の表示名（メッシュ（20m×20m）／ポリゴン）。"""
    if info and info.get('shape') == 'grid':
        cell = info.get('cell')
        return f'メッシュ（{cell}m×{cell}m）' if cell else 'メッシュ'
    return 'ポリゴン'


def _species_rule(names):
    """列の名前から樹種の列の形式を決める。（形式, 樹種_統一 の元にする列）。当てはまらなければ None。"""
    for label, required, sources in _FCDS_SPECIES_RULES:
        if all(c in names for c in required):
            return label, [c for c in sources if c in names]
    return None


def _is_od_group(name):
    return (name == _GROUP_NAME or name.startswith(_GROUP_NAME + '：')
            or name.startswith(_GROUP_NAME_OLD))


def _normalize_zip_name(name):
    return name.replace('（', '(').replace('）', ')')


class _ConvertWorker(QThread):
    """ダウンロード済み ZIP を GPKG（ベクタ）／TIFF（林相識別図）に変換する。"""
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, job):
        super().__init__()
        self.job = job

    def run(self):
        try:
            from osgeo import gdal
            gdal.UseExceptions()
            # ZIP 内の日本語ファイル名は Shift_JIS（このスレッドだけに効かせる）
            gdal.SetThreadLocalConfigOption('CPL_ZIP_ENCODING', 'CP932')
            job = self.job
            result = {'key': job['key'], 'layers': [], 'rasters': [], 'formats': {}, 'type': job['type']}
            if job.get('zip_vec'):
                result['layers'], result['formats'] = self._convert_vectors(gdal, job, result)
            for zpath in job.get('zip_rinso', []):
                result['rasters'] += self._extract_rinso(zpath, job['rinso_dir'])
            self.finished_ok.emit(result)
        except Exception as e:  # noqa: BLE001 - 変換失敗は理由を画面に出して続行させる
            self.failed.emit(str(e))

    @staticmethod
    def _find_shp(files, t):
        """ZIP の中の Shapefile のうち、フォルダ t（_ZIP_LAYERS のキー）のもの。無ければ None。"""
        folder, prefer, avoid, _out = _ZIP_LAYERS[t]
        cand = [f for f in files if folder in f
                and (avoid is None or avoid not in os.path.basename(f))]
        cand.sort(key=lambda f: prefer not in os.path.basename(f))
        return cand[0] if cand else None

    def _open_shp(self, gdal, vz, shp):
        # QGIS 内では Shapefile の文字コード自動判定が切られているため、DBF の文字コードを明示する
        return gdal.OpenEx(vz + '/' + shp, gdal.OF_VECTOR,
                           open_options=['ENCODING=' + self._shp_encoding(gdal, vz + '/' + shp)])

    def _classify_folders(self, gdal, vz, files):
        """ZIP の中の各フォルダのデータの形（メッシュかポリゴンか）。先頭の地物の面積だけを見る（変換しない）。"""
        shapes = {}
        for t in _SHAPE_FOLDERS:
            shp = self._find_shp(files, t)
            if shp is None:
                continue
            src = self._open_shp(gdal, vz, shp)
            lyr = src.GetLayer(0)
            areas = []
            for f in lyr:
                g = f.GetGeometryRef()
                if g is not None:
                    areas.append(g.GetArea())
                if len(areas) >= 300:
                    break
            src = None
            shapes[t] = _classify_areas(areas)
        return shapes

    def _convert_vectors(self, gdal, job, result):
        vz = '/vsizip/' + job['zip_vec']
        files = [f for f in gdal.ReadDirRecursive(vz) or [] if f.lower().endswith('.shp')]
        done = []
        formats = {}  # 出力レイヤー名 → 樹種の列の形式（判定ルールの名前）
        types = list(job['types'])
        if job.get('want'):
            # 「メッシュ」「ポリゴン」を選んだとき：ZIP の中のフォルダの形を見て、選んだ形のものを変換する
            self.progress.emit('データの形を確認中')
            shapes = self._classify_folders(gdal, vz, files)
            result['shapes'] = shapes
            folder = next((t for t in _SHAPE_FOLDERS
                           if shapes.get(t, {}).get('shape') == job['want']), None)
            if folder is None:
                result['missing'] = job['want']
                types = []
            else:
                types = [folder]
                result['type'] = folder
        for t in types:
            if t not in _ZIP_LAYERS:
                continue
            out_name = _ZIP_LAYERS[t][3]
            shp = self._find_shp(files, t)
            if shp is None:
                continue
            src = self._open_shp(gdal, vz, shp)
            lyr = src.GetLayer(0)
            defn = lyr.GetLayerDefn()
            names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
            rule = _species_rule(names)
            # SQL を挟むと GDAL が件数を数えられず進み具合が出ないので、変換はそのまま行い、
            # 樹種名をそろえた列はあとから足す
            self.progress.emit(f'変換中：{out_name}')
            # 座標系（.prj）の無い ZIP がある（掛川市・島田市・森町 R2 など）。そのときは
            # 静岡県の平面直角座標系 第VIII系（JGD2011）とみなす
            srs_opt = {} if lyr.GetSpatialRef() else {'srcSRS': 'EPSG:6676'}
            opts = gdal.VectorTranslateOptions(
                format='GPKG', layerName=out_name,
                dim='XY', explodeCollections=(t == 'points'),
                accessMode='overwrite' if os.path.exists(job['gpkg']) else None,
                layerCreationOptions=['SPATIAL_INDEX=YES'], dstSRS='EPSG:6676', **srs_opt,
                callback=self._pct_progress(out_name))
            gdal.VectorTranslate(job['gpkg'], src, options=opts)
            src = None
            if rule:
                self._add_species_norm(gdal, job['gpkg'], out_name, rule[1])
            done.append(out_name)
            formats[out_name] = rule[0] if rule else ''
        return done, formats

    @staticmethod
    def _shp_encoding(gdal, shp):
        """DBF の文字コード。.cpg があればそれ（UTF-8 のものがある）、無ければ CP932。"""
        cpg = shp[:-4] + '.cpg'
        if gdal.VSIStatL(cpg) is not None:
            f = gdal.VSIFOpenL(cpg, 'rb')
            try:
                text = gdal.VSIFReadL(1, 64, f).decode('ascii', 'ignore').strip().upper()
            finally:
                gdal.VSIFCloseL(f)
            if text.replace('-', '') == 'UTF8':
                return 'UTF-8'
        return 'CP932'

    def _add_species_norm(self, gdal, gpkg, lname, sources):
        """樹種名の表記をそろえた 樹種_統一 列を足す（色分けに使う）。
        元にする列は前のものから使い、空なら次の列を使う。"""
        from osgeo import ogr
        self.progress.emit(f'変換中：{lname}（樹種名の整理）')
        ds = gdal.OpenEx(gpkg, gdal.OF_VECTOR | gdal.OF_UPDATE)
        try:
            ds.GetLayerByName(lname).CreateField(ogr.FieldDefn('樹種_統一', ogr.OFTString))
            src = 'COALESCE(' + ', '.join(f"NULLIF(TRIM(\"{c}\"), '')" for c in sources) + ', NULL)'
            cases = ' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in _FCDS_SPECIES_NORM.items())
            # SQL に入るのはプラグインで決めたレイヤー名・対応表と、データの列名だけ（利用者の入力は入らない）
            ds.ExecuteSQL(f'UPDATE "{lname}" SET "樹種_統一" = CASE {src} {cases} ELSE {src} END')  # nosec B608
        finally:
            ds = None

    def _extract_rinso(self, zpath, out_dir):
        """林相識別図を ZIP から取り出し、見た目を変えずに軽い形式へ変換する。
        元は値 0〜255 の16ビット無圧縮（範囲外は 256）なので、8ビット・可逆圧縮（DEFLATE）・
        ピラミッド付きにし、範囲外は透明（アルファ）にする。画素の値は変わらない。"""
        import zipfile
        from osgeo import gdal
        os.makedirs(out_dir, exist_ok=True)
        out = []
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                name = info.filename
                if not (info.flag_bits & 0x800):
                    name = name.encode('cp437').decode('cp932')
                base = os.path.basename(name)
                if not base.lower().endswith('.tif'):
                    continue  # 位置情報（.tfw）は変換後の GeoTIFF に含まれるので使わない
                raw = os.path.join(out_dir, '_raw_' + base)
                dst = os.path.join(out_dir, base)
                self.progress.emit(f'展開中：{base}')
                with z.open(info) as fsrc, open(raw, 'wb') as fdst:
                    while True:
                        buf = fsrc.read(1 << 20)
                        if not buf:
                            break
                        fdst.write(buf)
                tfw = info.filename[:-4] + '.tfw'
                try:
                    data = z.read(tfw)  # GeoTIFF に位置情報が無い場合に備えて、変換元の横に置く
                    with open(raw[:-4] + '.tfw', 'wb') as f:
                        f.write(data)
                except KeyError:
                    pass
                self.progress.emit(f'変換中：{base}')
                self._compress_rinso(gdal, raw, dst, self._pct_progress(base))
                for p in (raw, raw[:-4] + '.tfw'):
                    if os.path.exists(p):
                        os.remove(p)
                out.append(dst)
        return out

    def _pct_progress(self, base):
        """GDAL の進み具合（0〜1）を、1% 変わるごとにステータスへ送る。"""
        last = [-1]

        def cb(complete, _message, _data):
            pct = int(complete * 100)
            if pct != last[0]:
                last[0] = pct
                self.progress.emit(f'変換中：{base}  {pct}%')
            return 1
        return cb

    @staticmethod
    def _compress_rinso(gdal, src, dst, callback=None):
        vrt = '/vsimem/_fcds_rinso.vrt'
        gdal.Warp(vrt, src, format='VRT', outputType=gdal.GDT_Byte, srcNodata=256, dstAlpha=True)
        # 細かい模様の多い画像で、強く圧縮しても容量はほとんど変わらないので弱め（LEVEL=1）にして時間を減らす
        common = ['COMPRESS=DEFLATE', 'LEVEL=1', 'PREDICTOR=YES', 'NUM_THREADS=ALL_CPUS', 'BIGTIFF=IF_SAFER']
        try:
            if gdal.GetDriverByName('COG') is not None:
                gdal.Translate(dst, vrt, format='COG',
                               colorInterpretation=['red', 'green', 'blue', 'alpha'],
                               creationOptions=common + ['OVERVIEWS=AUTO'], callback=callback)
            else:
                # COG に対応していない GDAL：同じ圧縮のタイル化 GeoTIFF にして、ピラミッドを別に作る
                common = [o.replace('LEVEL=', 'ZLEVEL=') for o in common]
                gdal.Translate(dst, vrt, format='GTiff',
                               colorInterpretation=['red', 'green', 'blue', 'alpha'],
                               creationOptions=common + ['TILED=YES'], callback=callback)
                ds = gdal.Open(dst, gdal.GA_Update)
                ds.BuildOverviews('AVERAGE', [2, 4, 8, 16, 32])
                ds = None
        finally:
            gdal.Unlink(vrt)


class _StatsWorker(QThread):
    """取得したデータ（GPKG の1レイヤー）の全体の統計を求める。形状は読まず、SQL で件数・合計・平均を出す。
    数百万件の点でも画面を止めないよう別スレッドで行う。"""
    finished_ok = pyqtSignal(str, str, dict)  # (データのキー, レイヤー名, 統計)
    failed = pyqtSignal(str, str, str)

    # レイヤーごとの集計（合計する項目, 平均する項目）。樹種は 樹種_統一 → 解析樹種 → 樹種 の順に探す。
    # 項目ごとに、ZIP によって違う列名を並べ、上から順にある列で求める。
    # 「名前#」は末尾 1〜3 の列の和（小班ごとの集計の第1〜第3樹種）、「a*b」は列の積
    _SPEC = {
        'メッシュ': (
            {'合計材積': ('合計材積', '材積合計', '合計材積#'),
             '立木本数': ('立木本数', '立木本数#', '立木密度*面積_ha'),
             '面積_ha': ('面積_ha', '面積ha', '小班面積')},
            {'平均樹高': ('平均樹高', '樹高'), '平均直径': ('平均直径', '胸高直径')}),
        '単木ポイント': (
            {'単木材積': ('単木材積', '材積')},
            {'樹高': ('樹高',), '胸高直径': ('胸高直径', '胸高直')}),
    }
    # 統計の求め方の版。変えたら、取得済みの記録に残した統計を求め直す
    # 4：統計を出すかどうか・集計の決まりを、レイヤー名でなくデータの形（メッシュ）で決める
    _VERSION = 4

    @staticmethod
    def _forest_area_expr(sums, cols):
        """対象の森林（材積の入っているところ）の面積の式。小班ごとの集計は第1〜第3樹種ごとに見る。"""
        parts = [f'CASE WHEN IFNULL("合計材積{i}", 0) > 0 THEN IFNULL("面積_ha{i}", 0) ELSE 0 END'
                 for i in (1, 2, 3) if f'合計材積{i}' in cols and f'面積_ha{i}' in cols]
        if parts:
            return ' + '.join(parts)
        vol, area = sums.get('合計材積'), sums.get('面積_ha')
        if vol and area:
            return f'CASE WHEN ({vol}) > 0 THEN {area} ELSE 0 END'
        return None

    @staticmethod
    def _source_expr(alts, cols):
        """項目を求める SQL の式。どの列も無ければ None。"""
        for a in alts:
            if a.endswith('#'):
                parts = [f'IFNULL("{a[:-1]}{i}", 0)' for i in (1, 2, 3) if f'{a[:-1]}{i}' in cols]
                if parts:
                    return ' + '.join(parts)
            elif '*' in a:
                x, y = a.split('*')
                if x in cols and y in cols:
                    return f'"{x}" * "{y}"'
            elif a in cols:
                return f'"{a}"'
        return None

    @classmethod
    def _aggregate(cls, con, lname, where='', spec=None, cell_ha=None):
        """件数・合計・平均・樹種ごとの件数を SQL で求める。where は絞り込みの条件（' WHERE …'）。
        spec：集計の決まり（_SPEC のキー。データの形がメッシュなら 'メッシュ'）。None ならレイヤー名。
        cell_ha：メッシュの1セルの面積（ha）。面積の列が無いメッシュでは、セルの数 × この面積を面積にする。"""
        spec = spec or lname
        cols = {r[1] for r in con.execute(f'PRAGMA table_info("{lname}")')}
        sum_spec, avg_spec = cls._SPEC.get(spec, ({}, {}))
        sums = [(k, e) for k, alts in sum_spec.items()
                if (e := cls._source_expr(alts, cols))]
        avgs = [(k, e) for k, alts in avg_spec.items()
                if (e := cls._source_expr(alts, cols))]
        if spec == 'メッシュ' and cell_ha and '面積_ha' not in dict(sums):
            sums.append(('面積_ha', repr(float(cell_ha))))
        if spec == 'メッシュ':
            # haあたり・平均の分母は対象の森林（材積の入っているセル・樹種）にする。
            # 広葉樹などのセルは材積・本数・樹高が 0 で、土地の面積で割ると値が薄まる
            forest = cls._forest_area_expr(dict(sums), cols)
            if forest:
                sums.append(('森林面積', forest))
            vol = dict(sums).get('合計材積')
            if vol:
                avgs = [(k, f'CASE WHEN ({vol}) > 0 THEN {e} END') for k, e in avgs]
        sel = (['COUNT(*)'] + [f'SUM({e})' for _, e in sums]
               + [f'AVG({e})' for _, e in avgs])
        # SQL に入るのはプラグインで決めたレイヤー名・項目・条件と、データの列名だけ（利用者の入力は入らない）
        row = con.execute(f'SELECT {", ".join(sel)} FROM "{lname}"{where}').fetchone()  # nosec B608
        out = {'v': cls._VERSION, 'count': row[0], 'sum': {}, 'avg': {}, 'species': []}
        # 小班ごとの集計（メッシュの代わりに入っている ZIP がある）は、数える単位が小班
        if 'KEY1' in cols and ('解析樹種1' in cols or '第1_林相名' in cols):
            out['unit'] = '小班'
        for i, (k, _) in enumerate(sums):
            out['sum'][k] = row[1 + i]
        for i, (k, _) in enumerate(avgs):
            out['avg'][k] = row[1 + len(sums) + i]
        sp = next((c for c in ('樹種_統一', '解析樹種', '樹種') if c in cols), None)
        if sp:
            out['species'] = [
                [name if name not in (None, '') else '不明', n]
                for name, n in con.execute(
                    f'SELECT "{sp}", COUNT(*) FROM "{lname}"{where} GROUP BY 1 ORDER BY 2 DESC')]  # nosec B608
        return out

    def __init__(self, key, gpkg, lname, spec=None, cell_ha=None, fp_src=None):
        super().__init__()
        self.key, self.gpkg, self.lname = key, gpkg, lname
        self.spec, self.cell_ha = spec, cell_ha
        # 計画図との重なりに使う範囲を作るレイヤー（メッシュはそれ自身、単木ポイントは同じ GPKG の面のデータ）
        self.fp_src = fp_src

    def run(self):
        import sqlite3
        try:
            from pathlib import Path
            uri = Path(self.gpkg).resolve().as_uri() + '?mode=ro'  # 読むだけ（書き込まない）
            con = sqlite3.connect(uri, uri=True)
            try:
                out = self._aggregate(con, self.lname, spec=self.spec, cell_ha=self.cell_ha)
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                fp_src = self.fp_src if self.fp_src in tables else None
            finally:
                con.close()
            if fp_src:
                out['footprint'] = self._grid_footprint(fp_src)
                out['footprint_src'] = fp_src
            self.finished_ok.emit(self.key, self.lname, out)
        except Exception as e:  # noqa: BLE001 - 集計できなくても表示は続けられるので理由だけ伝える
            self.failed.emit(self.key, self.lname, str(e))

    def _grid_footprint(self, lname, cell=20.0):
        """面のレイヤー（メッシュ・樹種ポリゴン）のある範囲の形（WKT、座標系はレイヤーのもの）。
        地物を1つずつ形としてまとめると重いので、20m の画像に焼き付けてから輪郭を取る。"""
        from osgeo import gdal, ogr
        src = gdal.OpenEx(self.gpkg, gdal.OF_VECTOR)
        lyr = src.GetLayerByName(lname)
        xmin, xmax, ymin, ymax = lyr.GetExtent()
        w = max(1, int(round((xmax - xmin) / cell)))
        h = max(1, int(round((ymax - ymin) / cell)))
        mem = gdal.GetDriverByName('MEM').Create('', w, h, 1, gdal.GDT_Byte)
        mem.SetGeoTransform((xmin, cell, 0, ymax, 0, -cell))
        srs = lyr.GetSpatialRef()
        if srs is not None:
            mem.SetProjection(srs.ExportToWkt())
        gdal.Rasterize(mem, src, layers=[lname], burnValues=[1])
        band = mem.GetRasterBand(1)
        # メモリ上のベクタは、GDAL 3.11 より前は Memory、以後は MEM という名前
        drv = ogr.GetDriverByName('Memory') or ogr.GetDriverByName('MEM')
        vds = drv.CreateDataSource('fp')
        out = vds.CreateLayer('fp')
        out.CreateField(ogr.FieldDefn('v', ogr.OFTInteger))
        gdal.Polygonize(band, band, out, 0)
        multi = ogr.Geometry(ogr.wkbMultiPolygon)
        for f in out:
            g = f.GetGeometryRef()
            if g is not None:
                multi.AddGeometry(g)
        fp = multi.UnionCascaded() if multi.GetGeometryCount() else None
        wkt = fp.Simplify(cell / 2).ExportToWkt() if fp is not None else ''
        vds = mem = src = None
        return wkt


class _PlanStatsWorker(QThread):
    """計画図で選んだ小班の範囲にあるメッシュ（単木ポイント）の統計を求める。
    小班をセルの大きさの画像に焼き付け、セル（点）の中心がその画素に入るかで選んでから SQL で集計する。
    形状どうしの判定をしないので、数万件の小班・数十万件のセルでも画面を止めずに求められる。"""
    finished_ok = pyqtSignal(int, dict)  # (世代, 統計)
    failed = pyqtSignal(int, str)
    progress = pyqtSignal(int, int, str)  # (世代, 進み具合 0〜100, いまの段階)

    # 焼き付ける画像の画素数の上限。超えるときは画素を粗くする（境界の振り分けがおおよそになる）
    _MAX_PIXELS = 25_000_000

    def __init__(self, gen, gpkg, lname, wkbs, extent, fp_wkb=None, spec=None, cell_ha=None):
        super().__init__()
        self.gen, self.gpkg, self.lname = gen, gpkg, lname
        # 集計の決まり（_StatsWorker._SPEC のキー）。None なら統計を求めず、選んだ地物を返す（ポリゴン）
        self.spec, self.cell_ha = spec, cell_ha
        self.wkbs = wkbs        # 小班の形（データの座標系の WKB）
        self.extent = extent    # 小班の外接範囲 (xmin, ymin, xmax, ymax)
        self.fp_wkb = fp_wkb    # データのある範囲の形（無ければ None。面積を範囲の中に限るのに使う）
        self.cancelled = False  # 新しい選択で要らなくなったら立てる（区切りごとに見て止める）

    def run(self):
        import math
        import sqlite3
        try:
            from pathlib import Path
            from osgeo import gdal, ogr
            uri = Path(self.gpkg).resolve().as_uri() + '?mode=ro'  # 読むだけ（書き込まない）
            con = sqlite3.connect(uri, uri=True)
            try:
                geom_col = con.execute(
                    'SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?',
                    (self.lname,)).fetchone()[0]
                xmin, ymin, xmax, ymax = self.extent
                # 進み具合の目安：範囲の読み込み 0〜20、小班の焼き付け 20〜60、振り分け 60〜85、集計 85〜100
                self.progress.emit(self.gen, 0, 'データの範囲を読み込み中')
                # 地物の範囲は空間索引（R木）から読む。形状そのものは読まない
                # SQL に入るのはデータの表・列の名前だけ（利用者の入力は入らない）
                rows = con.execute(
                    f'SELECT id, minx, maxx, miny, maxy FROM "rtree_{self.lname}_{geom_col}" '  # nosec B608
                    'WHERE maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ?',
                    (xmin, xmax, ymin, ymax)).fetchall()
                if self.cancelled:
                    return
                # メッシュ（10m・20m の正方形）は、画素をセルに合わせる（画素の中心＝セルの中心）。
                # それ以外（点・小班ごとの集計）は、範囲から決めた細かさの画素で中心の位置を見る
                widths = sorted(round(r[2] - r[1], 1) for r in rows[:200])
                cell = widths[len(widths) // 2] if widths else 0
                grid = cell in (10.0, 20.0)
                area = max(xmax - xmin, 1.0) * max(ymax - ymin, 1.0)
                res = max(cell if grid else 1.0, math.sqrt(area / self._MAX_PIXELS))
                approx = not grid or res > cell
                if grid and not approx and rows:
                    # セルの1つの角を基準に、範囲を覆うように格子の原点を決める
                    x0, y0 = rows[0][1], rows[0][4]
                    gx = x0 - math.ceil((x0 - xmin) / res) * res
                    gy = y0 + math.ceil((ymax - y0) / res) * res
                else:
                    gx, gy = xmin, ymax
                w = max(1, int(math.ceil((xmax - gx) / res)))
                h = max(1, int(math.ceil((gy - ymin) / res)))
                self.progress.emit(self.gen, 20, '小班を焼き付け中')

                mem = gdal.GetDriverByName('MEM').Create('', w, h, 1, gdal.GDT_Byte)
                mem.SetGeoTransform((gx, res, 0, gy, 0, -res))
                # メモリ上のベクタは、GDAL 3.11 より前は Memory、以後は MEM という名前
                drv = ogr.GetDriverByName('Memory') or ogr.GetDriverByName('MEM')
                vds = drv.CreateDataSource('plan')
                vl = vds.CreateLayer('plan', geom_type=ogr.wkbMultiPolygon)
                defn = vl.GetLayerDefn()
                for wkb in self.wkbs:
                    f = ogr.Feature(defn)
                    f.SetGeometry(ogr.CreateGeometryFromWkb(wkb))
                    vl.CreateFeature(f)
                if self.cancelled:
                    return
                last = [-1]

                def on_rasterize(complete, _message, _data):
                    pct = 20 + int(complete * 40)
                    if pct != last[0]:
                        last[0] = pct
                        self.progress.emit(self.gen, pct, '小班を焼き付け中')
                    return 0 if self.cancelled else 1  # 0 を返すと GDAL が止める

                # 既定（ALL_TOUCHED なし）では、中心が小班の中にある画素だけが焼き付けられる。
                # 小班は 2、データのある範囲は 1 を足して焼き付ける（両方＝3、小班だけ＝2、範囲だけ＝1）
                gdal.RasterizeLayer(mem, [1], vl, burn_values=[2], callback=on_rasterize)
                if self.cancelled:
                    return
                if self.fp_wkb:
                    fl = vds.CreateLayer('fp', geom_type=ogr.wkbMultiPolygon)
                    f = ogr.Feature(fl.GetLayerDefn())
                    f.SetGeometry(ogr.CreateGeometryFromWkb(self.fp_wkb))
                    fl.CreateFeature(f)
                    gdal.RasterizeLayer(mem, [1], fl, burn_values=[1], options=['MERGE_ALG=ADD'])
                mask = mem.GetRasterBand(1).ReadRaster()
                vds = mem = None
                # 選んだ範囲のうちデータにかかる部分の面積（重なった小班も場所として1回だけ数える）
                inside = mask.count(3) if self.fp_wkb else mask.count(2)
                area_ha = inside * res * res / 10000

                con.execute('CREATE TEMP TABLE _fcds_sel (fid INTEGER PRIMARY KEY)')
                keep = []
                n = len(rows)
                step = max(1, n // 25)
                for i, (fid, x1, x2, y1, y2) in enumerate(rows):
                    if i % step == 0:
                        if self.cancelled:
                            return
                        self.progress.emit(self.gen, 60 + int(25 * i / n), '小班の中にあるものを振り分け中')
                    col = int(((x1 + x2) / 2 - gx) // res)
                    row = int((gy - (y1 + y2) / 2) // res)
                    if 0 <= col < w and 0 <= row < h and mask[row * w + col] >= 2:
                        keep.append((fid,))
                self.progress.emit(self.gen, 85, '集計中')
                if self.spec is None:
                    # 統計を求めないデータ（ポリゴン）は、選んだ地物を表に出す
                    out = {'fids': [k[0] for k in keep]}
                else:
                    con.executemany('INSERT INTO _fcds_sel VALUES (?)', keep)
                    if self.cancelled:
                        return
                    # GPKG の主キー（整数）は SQLite の rowid と同じなので、列名を調べて埋め込まず rowid で絞る
                    out = _StatsWorker._aggregate(
                        con, self.lname, ' WHERE rowid IN (SELECT fid FROM temp._fcds_sel)',
                        spec=self.spec, cell_ha=self.cell_ha)
                out['approx'] = approx
                out['res'] = res
                out['area_ha'] = area_ha
            finally:
                con.close()
            if not self.cancelled:
                self.finished_ok.emit(self.gen, out)
        except Exception as e:  # noqa: BLE001 - 集計できなくても表示は続けられるので理由だけ伝える
            if not self.cancelled:
                self.failed.emit(self.gen, str(e))


class OpendataMixin:

    # ------------------------------------------------------------------
    # タブ構築
    # ------------------------------------------------------------------

    def _build_tab_opendata(self):
        self._od_catalog = []          # [{dataset, keiku, city, yr, no, vec, rinso:[...], key}]
        self._od_catalog_pending = 0
        self._od_catalog_parts = []
        self._od_current_job = None
        self._od_downloader = None
        self._od_download_label = ''
        self._od_pending_failure = None
        self._od_worker = None
        self._od_shutting_down = False
        self._od_shown = None          # 表示中 (key, type)。常に1つ
        self._od_keiku_applied_layer = None  # 計画区の初期値を合わせた計画図レイヤー
        self._od_attr = None           # 属性テーブルの (cache, model, filter)
        self._od_layer = None          # 表示中のベクタレイヤー
        self._od_left_saved = None     # 左パネル「クラウド情報」の退避

        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.addWidget(QLabel('計画区:'))
        self.combo_od_keiku = QComboBox()
        self.combo_od_keiku.setToolTip('計画図レイヤーの計画区を初期値にする')
        row.addWidget(self.combo_od_keiku)
        row.addWidget(QLabel('種類:'))
        self.combo_od_type = QComboBox()  # 選択肢は番号（解析範囲）ごとに作る（_od_fill_types）
        self.combo_od_type.setToolTip(
            'メッシュは格子（10m・20m 四方）のデータ、ポリゴンはそれ以外の形のデータ。\n'
            '林相識別図は 0.5m の画像で、1件あたり数百MB〜2GB程度あります')
        row.addWidget(self.combo_od_type)
        row.addWidget(QLabel('番号:'))
        self.combo_od_no = QComboBox()
        self.combo_od_no.setToolTip('計測年度－番号（解析範囲にかかる市町）')
        row.addWidget(self.combo_od_no, 1)
        self.btn_od_catalog = QPushButton('一覧を更新')
        self.btn_od_catalog.setToolTip('G空間情報センターからデータの一覧を取得し直す（データ本体は取得しない）')
        self.btn_od_fetch_show = QPushButton('取得し表示')
        self.btn_od_fetch_show.setToolTip('選んだデータを取得（取得済みならそのまま）して表示する。表示するのは常に1つ')
        row.addWidget(self.btn_od_catalog)
        row.addWidget(self.btn_od_fetch_show)
        v.addLayout(row)

        self.tbl_od = QgsAttributeTableView()
        # 標準の属性テーブルは行番号のクリックでしか地物を選ばないので、
        # 行単位の選択にして、クリック・十字キーで現在の行が変わったらその地物を選ぶ
        from qgis.PyQt.QtWidgets import QAbstractItemView
        self.tbl_od.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_od.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._od_table_fid = None      # 表で選んだ地物（QGIS の選択状態とは別に持つ）
        self.tbl_od.setToolTip('地図で選んだ、表示中のレイヤーの地物'
                               '（統計を求めないデータでは、計画図で選んだ小班の中にある地物）')
        # 表の場所は、ベクタのデータなら属性の表、林相識別図（画像）ならその画像の情報を出す
        self.txt_od_rinso = QTextBrowser()
        self.txt_od_rinso.setOpenLinks(False)
        self.stack_od = QStackedWidget()
        self.stack_od.addWidget(self.tbl_od)        # 0：属性の表
        self.stack_od.addWidget(self.txt_od_rinso)  # 1：林相識別図の情報
        v.addWidget(self.stack_od, 1)

        bottom = QHBoxLayout()
        self.lbl_od_status = QLabel('「一覧を更新」を押してください')
        self.lbl_od_status.setStyleSheet('font-size: 10px;')  # 通常の文字色（ダークテーマでも読める）
        bottom.addWidget(self.lbl_od_status, 1)
        # 選んだ小班の範囲の統計を求めている間だけ出す（終わったら隠す）
        from qgis.PyQt.QtWidgets import QProgressBar
        self.bar_od_progress = QProgressBar()
        self.bar_od_progress.setRange(0, 100)
        self.bar_od_progress.setMaximumWidth(220)
        self.bar_od_progress.setMaximumHeight(14)
        self.bar_od_progress.setTextVisible(True)
        self.bar_od_progress.setVisible(False)
        bottom.addWidget(self.bar_od_progress)
        # すぐ終わる集計で一瞬だけ出てちらつかないよう、少し待っても終わらないときに出す
        self._od_progress_timer = QTimer(w)
        self._od_progress_timer.setSingleShot(True)
        self._od_progress_timer.setInterval(300)
        self._od_progress_timer.timeout.connect(lambda: self.bar_od_progress.setVisible(True))
        # 拡大したときにメッシュ・樹種ポリゴンの外周線が小班の線と紛らわしいので、消せるようにする。
        # どの縮尺から紛らわしいかは使い方によるので、利用者が決めて QGIS の設定に覚えさせる
        st = QSettings()
        self.chk_od_outline = QCheckBox('縮尺')
        self.chk_od_outline.setChecked(st.value('fcloud_shizuoka/od_outline_hide', False, type=bool))
        self.spin_od_outline = QSpinBox()
        self.spin_od_outline.setRange(100, 1000000)
        self.spin_od_outline.setSingleStep(500)
        self.spin_od_outline.setPrefix('1:')
        self.spin_od_outline.setGroupSeparatorShown(True)
        self.spin_od_outline.setValue(st.value('fcloud_shizuoka/od_outline_scale', 12000, type=int))
        tip = 'この縮尺より拡大したとき（分母がこの値以下）、メッシュ・樹種ポリゴンの外周線を消す'
        for wdg in (self.chk_od_outline, self.spin_od_outline):
            wdg.setToolTip(tip)
            bottom.addWidget(wdg)
        bottom.addWidget(QLabel('より拡大したら外周線を消す'))
        src = QLabel('オープンデータ（G空間情報センター）')
        src.setStyleSheet('color: gray; font-size: 10px;')
        bottom.addWidget(src, 0, Qt.AlignmentFlag.AlignRight)
        v.addLayout(bottom)

        self.btn_od_catalog.clicked.connect(self._od_fetch_catalog)
        self.btn_od_fetch_show.clicked.connect(self._od_fetch_and_show)
        self.combo_od_keiku.activated.connect(lambda _: self._od_fill_no(focus_left=True))
        self.combo_od_keiku.currentIndexChanged.connect(lambda _: self._od_fill_no())
        self.combo_od_no.activated.connect(lambda _: self._od_render_left(focus_left=True))
        self.combo_od_no.currentIndexChanged.connect(lambda _: (self._od_fill_types(), self._od_render_left()))
        self.combo_od_type.activated.connect(lambda _: self._od_render_left(focus_left=True))
        self.chk_od_outline.toggled.connect(lambda _: self._od_on_outline_setting())
        self.spin_od_outline.editingFinished.connect(self._od_on_outline_setting)
        self._od_fill_keiku()
        return w

    # ------------------------------------------------------------------
    # 保存場所
    # ------------------------------------------------------------------

    def _od_shared_dir(self):
        """{プロジェクトのフォルダ}/fcapital_data_shizuoka/shared/。未保存なら None。"""
        home = QgsProject.instance().absolutePath()
        if not home:
            return None
        return os.path.join(home, _FCDS_SHARED_SUBDIR)

    def _od_index_path(self):
        d = self._od_shared_dir()
        return os.path.join(d, 'index.json') if d else None

    def _od_load_index(self):
        """取得済みの記録。古い変換方式（文字化け）のものは除く。
        選択のたびに呼ばれるので、ファイルの更新時刻が変わったときだけ読み直す。"""
        p = self._od_index_path()
        if not p or not os.path.exists(p):
            return {}
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return {}
        cached = getattr(self, '_od_index_cache', None)
        if cached and cached[0] == p and cached[1] == mtime:
            return dict(cached[2])
        try:
            with open(p, encoding='utf-8') as f:
                idx = json.load(f)
        except (OSError, ValueError):
            return {}
        idx = {k: v for k, v in idx.items() if v.get('conv') == _CONV_VERSION}
        self._od_index_cache = (p, mtime, idx)
        return dict(idx)

    def _od_save_index(self, idx):
        p = self._od_index_path()
        if not p:
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)
        self._od_index_cache = None

    def _od_catalog_cache_path(self):
        d = self._od_shared_dir()
        return os.path.join(d, 'catalog.json') if d else None

    # ------------------------------------------------------------------
    # タブの出入り（左パネルの「クラウド情報」→「資源情報」）
    # ------------------------------------------------------------------

    def _od_on_tab_shown(self):
        if not self._od_catalog:
            p = self._od_catalog_cache_path()
            if p and os.path.exists(p):
                try:
                    with open(p, encoding='utf-8') as f:
                        self._od_catalog = json.load(f)
                except (OSError, ValueError):
                    self._od_catalog = []
                if self._od_catalog:
                    self._od_fill_keiku()
                    self._od_set_status(f'保存済みの一覧を読み込みました（{len(self._od_catalog)} 件）')
        self._od_apply_layer_keiku()
        if self._od_left_saved is None:
            self._od_left_saved = (list(self._cloud_info_parts), self._cloud_owner_feat)
        self.left_tab.setTabText(1, '資源情報')
        self._od_refresh_table()
        self._od_render_left()
        self._od_update_buttons()
        self._od_update_bottom_ts()

    def _od_on_tab_hidden(self):
        self.left_tab.setTabText(1, 'クラウド情報')
        if self._od_left_saved is not None:
            self._cloud_info_parts, self._cloud_owner_feat = self._od_left_saved
            self._od_left_saved = None
            self._render_cloud_info()

    def _od_render_left(self, focus_left=False):
        """選んだデータの内容（と、属性テーブルで選んだ地物）を左パネル（資源情報）に出す。
        focus_left: このタブでの操作によるときは左パネルを「資源情報」に切り替える。"""
        if self.cloud_tab.currentIndex() != _TAB_INDEX:
            self._od_update_buttons()
            return
        if focus_left:
            self.left_tab.setCurrentIndex(1)
        e = self._od_current_entry()
        if e is None:
            self.cloud_info_browser.setHtml(
                '<p style="color:gray;padding:8px;">「一覧を更新」でデータの一覧を取得してください</p>'
                if not self._od_catalog else '')
            self._od_update_buttons()
            return
        got = self._od_load_index().get(e['key'])
        parts = len(e['rinso'])
        if not parts:
            rinso = 'なし（この解析範囲には作られていません）'
        else:
            size = self._od_size_text(e['rinso'])
            detail = '、'.join(x for x in ((f'{parts}分割' if parts > 1 else ''), size) if x)
            rinso = f'あり（{detail}）' if detail else 'あり'
        rows = [
            ('計画区', e['keiku']),
            ('範囲にかかる市町', e['city']),
            ('計測年度', e['yr']),
            ('番号', e['no']),
            ('林相識別図', rinso),
            ('データセット', f"静岡県森林資源解析データ（{e['keiku']}計画区）"),
            ('データセット更新日', e.get('modified', '')),
            ('取得状況', f"取得済み {got['fetched_at']}" if got else '未取得'),
        ]
        if got:
            kinds = ([self._od_lname_label(e['key'], n) for n in got.get('layers', [])]
                     + (['林相識別図'] if got.get('rasters') else []))
            rows.append(('取得済みの種類', '、'.join(kinds) or '—'))
        if self._od_shown and self._od_shown[0] == e['key']:
            rows.append(('表示中', self._od_shown_label()))
        body = ['<table style="border-collapse:collapse;width:100%;">',
                self._info_title_row('森林資源情報（オープンデータ）')]
        for k, val in rows:
            body.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                f'{html.escape(k)}</td><td style="padding:1px 4px;">{html.escape(str(val))}</td></tr>')
        body.append('</table>')
        body += self._od_selected_feature_html()
        self.cloud_info_browser.setHtml(''.join(body))
        self._od_update_buttons()

    @staticmethod
    def _od_size_text(items):
        """一覧に載っている容量（バイト）の合計を「合計 約2.3GB」の形にする。不明なら空。"""
        total = 0
        for it in items:
            try:
                total += int(it.get('size') or 0)
            except (TypeError, ValueError):
                return ''
        if total <= 0:
            return ''
        if total >= 1e9:
            return f'合計 約{total / 1e9:.1f}GB'
        return f'合計 約{total / 1e6:.0f}MB'

    def _od_selected_feature_html(self):
        layer = self._od_shown_layer()
        if layer is None:
            return []
        if self._od_table_fid is not None:
            f = next(layer.getFeatures(QgsFeatureRequest().setFilterFid(self._od_table_fid)), None)
            n = 1
        elif layer.selectedFeatureCount():
            f = next(layer.getSelectedFeatures(QgsFeatureRequest().setLimit(1)), None)
            n = layer.selectedFeatureCount()
        else:
            return []
        if f is None:
            return []
        title = f'選択中の地物（{layer.name()}）' + (f'　{n} 件中の1件' if n > 1 else '')
        out = ['<table style="border-collapse:collapse;width:100%;margin-top:6px;">',
               self._info_title_row(html.escape(title))]
        for fld in layer.fields():
            name = fld.name()
            if name == 'fid':
                continue
            val = f[name]
            if val is None or str(val) in ('', 'NULL'):
                continue
            out.append(
                f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                f'{html.escape(name)}</td><td style="padding:1px 4px;">{html.escape(str(val))}</td></tr>')
        out.append('</table>')
        return out

    # ------------------------------------------------------------------
    # 一覧（CKAN API）
    # ------------------------------------------------------------------

    def _od_keiku_of_layer(self, layer):
        """計画図レイヤーの計画区（データセットID）。計画区名称 → KEY1 の先頭3桁の順に見る。"""
        names = layer.fields().names()
        f = next(layer.getFeatures(QgsFeatureRequest().setLimit(1)), None)
        if f is None:
            return None
        if '計画区名称' in names and f['計画区名称']:
            name = str(f['計画区名称'])
            for keiku, ds_id in _FCDS_DATASETS:
                if name.startswith(keiku):
                    return ds_id
        if 'KEY1' in names and f['KEY1']:
            return _FCDS_KEIKU_CODE.get(str(f['KEY1'])[:3])
        return None

    def _od_apply_layer_keiku(self):
        """計画図レイヤーを切り替えた後、最初に開いたときだけ計画区の初期値を合わせる。"""
        layer = self._connected_layer
        if layer is None or not self._od_catalog:
            return
        try:
            lid = layer.id()
        except RuntimeError:
            return  # レイヤーが削除済み
        if lid == self._od_keiku_applied_layer:
            return
        ds_id = self._od_keiku_of_layer(layer)
        self._od_keiku_applied_layer = lid
        i = self.combo_od_keiku.findData(ds_id) if ds_id else -1
        if i >= 0:
            self.combo_od_keiku.setCurrentIndex(i)

    def _od_fetch_catalog(self):
        if self._od_catalog_pending:
            return
        self._od_catalog_parts = []
        self._od_catalog_pending = len(_FCDS_DATASETS)
        self._od_update_buttons()
        self._od_set_status('一覧を取得中…')
        for keiku, ds_id in _FCDS_DATASETS:
            req = QNetworkRequest(QUrl(_FCDS_API_PACKAGE_SHOW + ds_id))
            reply = QgsNetworkAccessManager.instance().get(req)
            self._pending_replies.append(reply)
            reply.finished.connect(lambda r=reply, k=keiku, d=ds_id: self._od_on_catalog_reply(r, k, d))

    def _od_on_catalog_reply(self, reply, keiku, ds_id):
        if reply in self._pending_replies:
            self._pending_replies.remove(reply)
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = json.loads(bytes(reply.readAll()).decode('utf-8'))
                self._od_catalog_parts += self._od_parse_package(data.get('result', {}), keiku, ds_id)
            else:
                self._od_catalog_parts.append({'error': f'{keiku}: {reply.errorString()}'})
        except ValueError:
            self._od_catalog_parts.append({'error': f'{keiku}: 一覧の形式を読み取れません'})
        finally:
            reply.deleteLater()
        self._od_catalog_pending -= 1
        if self._od_catalog_pending > 0:
            return
        errors = [p['error'] for p in self._od_catalog_parts if 'error' in p]
        entries = [p for p in self._od_catalog_parts if 'error' not in p]
        if entries:
            self._od_catalog = entries
            p = self._od_catalog_cache_path()
            if p:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(entries, f, ensure_ascii=False)
            self._od_fill_keiku()
            self._od_apply_layer_keiku()
        self._od_render_left()
        if errors:
            self._od_set_status('一覧の取得に一部失敗：' + ' / '.join(errors), warn=True)
        else:
            self._od_set_status(f'一覧を更新しました（{len(entries)} 件）')

    @staticmethod
    def _od_parse_package(pkg, keiku, ds_id):
        groups = {}
        for r in pkg.get('resources', []):
            name = _normalize_zip_name(r.get('name') or '')
            m = _ZIP_NAME_RE.match(name)
            if not m:
                continue
            key = (m['city'], m['yr'], m['no'])
            g = groups.setdefault(key, {
                'dataset': ds_id, 'keiku': keiku, 'city': m['city'], 'yr': m['yr'],
                'no': m['no'], 'vec': None, 'rinso': [],
                'modified': pkg.get('metadata_modified', '')[:10]})
            item = {'name': r.get('name'), 'url': r.get('url'), 'id': r.get('id'),
                    'size': r.get('size')}
            if '以外' in m['kind']:
                g['vec'] = item
            else:
                item['part'] = m['part'] or ''
                g['rinso'].append(item)
        for g in groups.values():
            g['rinso'].sort(key=lambda it: it.get('part') or '')
            g['key'] = f"{g['dataset']}__{g['city']}_{g['yr']}_{g['no']}"
        return list(groups.values())

    # ------------------------------------------------------------------
    # コンボ（計画区 → 番号）
    # ------------------------------------------------------------------

    @staticmethod
    def _od_refill(combo, items):
        """items: [(表示, data)]。直前の選択（data）が残っていれば保つ。"""
        prev = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for text, data in items:
            combo.addItem(text, data)
        i = combo.findData(prev) if prev is not None else -1
        combo.setCurrentIndex(i if i >= 0 else (0 if items else -1))
        combo.blockSignals(False)

    def _od_fill_keiku(self):
        have = {e['dataset'] for e in self._od_catalog}
        items = [(k, d) for k, d in _FCDS_DATASETS if not self._od_catalog or d in have]
        self._od_refill(self.combo_od_keiku, items)
        self._od_fill_no()

    def _od_fill_no(self, focus_left=False):
        ds = self.combo_od_keiku.currentData()
        es = [e for e in self._od_catalog if e['dataset'] == ds]
        es.sort(key=lambda e: (e['city'], int(e['yr'][1:]), int(e['no'])))
        self._od_refill(self.combo_od_no, [(f"{e['yr']}-{e['no']}（{e['city']}）", e['key']) for e in es])
        self._od_fill_types()
        self._od_render_left(focus_left=focus_left)

    def _od_type_items(self, key):
        """「種類」の欄の選択肢 [(表示, data)]。data は次のどれか。
        'grid@mesh' のような「データの形@フォルダ」：取得済みなどで、どのフォルダがその形かわかっているもの。
        'grid'・'polygon'：まだわからないもの（ZIP を取得したときに形を見てフォルダを決める）。
        'points'・'rinso'：単木ポイント・林相識別図。"""
        rec = self._od_load_index().get(key) if key else None
        shapes = {}
        complete = False  # ZIP の中のフォルダの形をすべて見たか
        if rec:
            complete = bool(rec.get('shapes_complete'))
            for f in _SHAPE_FOLDERS:
                info = (rec.get('shapes') or {}).get(f) or self._od_layer_shape(key, f)
                if info:
                    shapes[f] = info
        items = []
        for kind, label in _FCDS_TYPES:
            if kind not in ('grid', 'polygon'):
                items.append((label, kind))
                continue
            known = [f for f in _SHAPE_FOLDERS if shapes.get(f, {}).get('shape') == kind]
            items += [(label, f'{kind}@{f}') for f in known]
            if not known and not complete:
                items.append((label, kind))
        return items

    def _od_fill_types(self):
        """「種類」の欄を、選んでいる番号（解析範囲）のデータに合わせて作り直す。直前に選んでいた種類は保つ。"""
        prev = self.combo_od_type.currentData() or ''
        items = self._od_type_items(self.combo_od_no.currentData())
        combo = self.combo_od_type
        combo.blockSignals(True)
        combo.clear()
        for text, data in items:
            combo.addItem(text, data)
        i = combo.findData(prev)
        if i < 0:
            kind = prev.split('@')[0]
            i = next((n for n, (_t, d) in enumerate(items) if d.split('@')[0] == kind), 0)
        combo.setCurrentIndex(i if items else -1)
        combo.blockSignals(False)

    def _od_current_entry(self):
        key = self.combo_od_no.currentData()
        return next((e for e in self._od_catalog if e['key'] == key), None)

    def _od_update_buttons(self):
        busy = bool(self._od_current_job)
        self.btn_od_catalog.setEnabled(not self._od_catalog_pending and not busy)
        self.btn_od_fetch_show.setEnabled(self._od_current_entry() is not None and not busy)
        for c in (self.combo_od_keiku, self.combo_od_type, self.combo_od_no):
            c.setEnabled(bool(self._od_catalog) and not busy)

    def _od_set_status(self, text, warn=False):
        if getattr(self, '_od_shutting_down', False):
            return
        label = getattr(self, 'lbl_od_status', None)
        if label is None or sip.isdeleted(label):
            return
        label.setStyleSheet(
            'color: #b00020; font-size: 10px;' if warn else 'font-size: 10px;')
        label.setText(text)

    def _od_update_bottom_ts(self):
        if self.cloud_tab.currentIndex() != _TAB_INDEX:
            return
        idx = self._od_load_index()
        if not self._od_shared_dir():
            self.lbl_cache_ts.setText('保存先：プロジェクト未保存')
        elif idx:
            newest = max(v.get('fetched_at', '') for v in idx.values())
            self.lbl_cache_ts.setText(f'取得済み {len(idx)} 件（最新 {newest}）')
        else:
            self.lbl_cache_ts.setText('取得済み：なし')

    # ------------------------------------------------------------------
    # 取得し表示
    # ------------------------------------------------------------------

    def _od_fetch_and_show(self):
        e = self._od_current_entry()
        if e is None:
            return
        self.left_tab.setCurrentIndex(1)
        shared = self._od_shared_dir()
        if not shared:
            QMessageBox.information(
                self, '森林資源情報',
                'データはプロジェクトのフォルダに保存します。\n先にプロジェクトを保存してください。')
            return
        kind, _sep, folder = (self.combo_od_type.currentData() or '').partition('@')
        got = self._od_load_index().get(e['key'], {})
        want = None  # 「メッシュ」「ポリゴン」で、どのフォルダかまだわからないとき：取得した ZIP の中で形を見て決める
        if kind in ('grid', 'polygon'):
            if folder:
                t = folder
            else:
                t, want = None, kind
        else:
            t = kind
        if t == 'rinso':
            if not e['rinso']:
                self._od_set_status('この解析範囲には林相識別図がありません', warn=True)
                return
            need = not got.get('rasters')
            if need and QMessageBox.question(
                    self, '森林資源情報',
                    f'林相識別図（{len(e["rinso"])} ファイル、{self._od_size_text(e["rinso"]) or "容量不明"}）を'
                    '取得します。展開後はさらに容量が必要です。取得しますか？'
            ) != QMessageBox.StandardButton.Yes:
                return
        elif want:
            if not e['vec']:
                self._od_set_status('この解析範囲にはベクタのデータ（メッシュ・ポリゴン）がありません', warn=True)
                return
            need = True
        else:
            need = _ZIP_LAYERS[t][3] not in got.get('layers', [])
        if not need:
            self._od_show(e, t)   # 取得済み
            return
        tmp = os.path.join(shared, '_download')
        job = {'key': e['key'], 'entry': e, 'type': t,
               'types': [t] if t in _ZIP_LAYERS else [], 'want': want,
               'gpkg': os.path.join(shared, e['key'] + '.gpkg'),
               'rinso_dir': os.path.join(shared, 'rinso', e['key']),
               'downloads': [], 'zip_rinso': []}
        if (t in _ZIP_LAYERS or want) and e['vec']:
            job['zip_vec'] = os.path.join(tmp, e['vec']['id'] + '.zip')
            job['downloads'].append((e['vec']['url'], job['zip_vec']))
        if t == 'rinso':
            for it in e['rinso']:
                p = os.path.join(tmp, it['id'] + '.zip')
                job['zip_rinso'].append(p)
                job['downloads'].append((it['url'], p))
        self._od_remove_display()
        self._od_current_job = job
        self._od_update_buttons()
        self._od_next_download()

    def _od_next_download(self):
        if self._od_shutting_down:
            return
        job = self._od_current_job
        if not job['downloads']:
            self._od_start_convert(job)
            return
        url, path = job['downloads'].pop(0)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        label = f"{job['entry']['city']}_{job['entry']['yr']}_{job['entry']['no']}"
        self._od_download_label = label
        self._od_set_status(f'取得中：{label}')
        dl = QgsFileDownloader(QUrl(url), path, delayStart=True)
        # QgsFileDownloader は終わると自分で消える（deleteLater）。Python 側が持ち主のままだと、
        # 次のダウンロードへ差し替えた時点で合図の最中に破棄され、二重破棄にもなって QGIS ごと落ちる。
        # 持ち主を C++ 側に渡して、破棄は QGIS に任せる
        sip.transferto(dl, None)
        dl.downloadProgress.connect(self._od_download_progress)
        # 次の処理は、終わった合図の処理を抜けてから始める
        dl.downloadCompleted.connect(self._od_download_completed)
        dl.downloadError.connect(self._od_download_error)
        dl.downloadCanceled.connect(self._od_download_canceled)
        self._od_downloader = dl
        dl.startDownload()

    def _od_download_progress(self, received, total):
        if self._od_shutting_down:
            return
        text = f'取得中：{self._od_download_label}  {received / 1e6:.0f}'
        self._od_set_status(
            text + (f' / {total / 1e6:.0f} MB' if total > 0 else ' MB'))

    def _od_download_completed(self, *_args):
        if not self._od_shutting_down:
            QTimer.singleShot(0, self._od_next_download)

    def _od_download_error(self, errors):
        self._od_queue_download_failure(
            f'{self._od_download_label}：' + '; '.join(errors))

    def _od_download_canceled(self):
        self._od_queue_download_failure(
            f'{self._od_download_label}：中止しました')

    def _od_queue_download_failure(self, message):
        if self._od_shutting_down:
            return
        self._od_pending_failure = message
        QTimer.singleShot(0, self._od_deliver_download_failure)

    def _od_deliver_download_failure(self):
        if self._od_shutting_down or self._od_pending_failure is None:
            return
        message = self._od_pending_failure
        self._od_pending_failure = None
        self._od_job_failed(message)

    def _od_start_convert(self, job):
        if self._od_shutting_down:
            return
        self._od_set_status('変換中…')
        old = self._od_worker
        if old is not None and old.isRunning():
            old.wait()
        # スレッドの参照は次の変換まで持ち続ける（終わりきる前に破棄すると落ちるため）
        self._od_worker = _ConvertWorker(job)
        # bound method として接続し、画面破棄時に Qt が自動切断できるようにする。
        self._od_worker.progress.connect(self._od_set_status)
        self._od_worker.finished_ok.connect(self._od_job_done)
        self._od_worker.failed.connect(self._od_job_failed)
        self._od_worker.start()

    def _od_job_done(self, result):
        if self._od_shutting_down:
            return
        job = self._od_current_job
        self._od_cleanup_downloads(job)
        idx = self._od_load_index()
        old = idx.get(job['key'], {})
        e = job['entry']
        layers = sorted(set(old.get('layers', [])) | set(result['layers']))
        idx[job['key']] = {
            'keiku': e['keiku'], 'city': e['city'], 'yr': e['yr'], 'no': e['no'],
            'dataset': e['dataset'], 'conv': _CONV_VERSION,
            'gpkg': os.path.basename(job['gpkg']) if layers else None,
            'layers': layers,
            'formats': {**old.get('formats', {}), **result.get('formats', {})},
            # ZIP の中のフォルダごとのデータの形（メッシュかポリゴンか）。ZIP の中を見たときはすべてのフォルダがわかる
            'shapes': {**old.get('shapes', {}), **result.get('shapes', {})},
            'shapes_complete': bool(old.get('shapes_complete') or 'shapes' in result),
            'rasters': sorted(set(old.get('rasters', [])) | {
                os.path.relpath(p, self._od_shared_dir()) for p in result['rasters']}),
            'fetched_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
            'source_modified': e.get('modified', ''),
        }
        self._od_save_index(idx)
        self._od_current_job = self._od_downloader = None
        self._od_fill_types()  # 形がわかったので、「種類」の欄をこのデータに合わせる
        if result.get('missing'):
            label = dict(_FCDS_TYPES).get(result['missing'], '')
            self._od_set_status(f'このデータには{label}がありません', warn=True)
            self._od_render_left()
            self._od_update_buttons()
            return
        self._od_show(e, result.get('type') or job['type'])

    def _od_job_failed(self, msg):
        if self._od_shutting_down:
            return
        job = self._od_current_job
        if job:
            self._od_cleanup_downloads(job)
        self._od_current_job = self._od_downloader = None
        self._od_set_status('失敗：' + msg, warn=True)
        self._od_render_left()

    def _od_cleanup_downloads(self, job):
        for p in [job.get('zip_vec')] + list(job.get('zip_rinso', [])):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass  # 次回の取得で上書きされるので残っても支障はない

    def _od_cancel_all(self):
        """プラグイン終了時：進行中の取得を止め、属性テーブルを外す。"""
        self._od_shutting_down = True
        self._od_cancel_plan_stats()
        self._od_set_attr_layer(None)
        downloader = self._od_downloader
        if downloader is not None:
            try:
                for signal in (
                        downloader.downloadProgress, downloader.downloadCompleted,
                        downloader.downloadError, downloader.downloadCanceled):
                    try:
                        signal.disconnect()
                    except TypeError:
                        pass
                downloader.cancelDownload()
            except RuntimeError:
                pass  # 既に破棄済み
            self._od_downloader = None
        worker = self._od_worker
        if worker is not None:
            # 破棄済み画面へ、待機中・待機後のシグナルを送らない。
            for signal in (worker.progress, worker.finished_ok, worker.failed):
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass
            _OD_ORPHANED_WORKERS.add(worker)
            worker.finished.connect(
                lambda w=worker: _OD_ORPHANED_WORKERS.discard(w))
            if worker.isRunning():
                worker.wait(3000)
            if not worker.isRunning():
                _OD_ORPHANED_WORKERS.discard(worker)
            self._od_worker = None

    # ------------------------------------------------------------------
    # 表示（常に1つ）
    # ------------------------------------------------------------------

    def _od_clear_group(self, grp):
        ids = [n.layerId() for n in grp.findLayers()]
        if ids:
            QgsProject.instance().removeMapLayers(ids)
        for child in list(grp.children()):
            grp.removeChildNode(child)

    def _od_show(self, e, t):
        shared = self._od_shared_dir()
        rec = self._od_load_index().get(e['key'])
        if not shared or not rec:
            self._od_render_left()
            return
        self._od_set_attr_layer(None)  # レイヤーを外す前に属性テーブルを切り離す
        self._clear_selection_highlights()
        root = QgsProject.instance().layerTreeRoot()
        # グループ名は表示中のデータ名を後ろに付けて変えるので、先頭の一致で探す
        groups = [g for g in root.findGroups(True) if _is_od_group(g.name())]
        visible = groups[0].itemVisibilityChecked() if groups else True
        for old in groups:             # 前に表示していたデータは外す
            self._od_clear_group(old)
            old.parent().removeChildNode(old)
        # グループは選択中の計画図レイヤーの直下に作る（見つからなければ最上段）
        plan_id = self.layer_combo.currentData() or ''
        plan_node = root.findLayer(plan_id)
        if plan_node and plan_node.parent():
            parent = plan_node.parent()
            pos = next(i for i, c in enumerate(parent.children())
                       if getattr(c, 'layerId', lambda: None)() == plan_id)
            grp = parent.insertGroup(pos + 1, _GROUP_NAME)
        else:
            grp = root.insertGroup(0, _GROUP_NAME)
        grp.setItemVisibilityChecked(visible)
        shown = None
        if t in _ZIP_LAYERS:
            lname = _ZIP_LAYERS[t][3]
            gpkg = os.path.join(shared, rec['gpkg']) if rec.get('gpkg') else None
            if gpkg and lname in rec.get('layers', []):
                # 凡例の名前・スタイルはデータの形（メッシュ／ポリゴン）で決める（GPKG のレイヤー名は県のフォルダの区分）
                lyr = QgsVectorLayer(f'{gpkg}|layername={lname}', self._od_lname_label(e['key'], lname), 'ogr')
                if lyr.isValid():
                    style = t
                    if t in _SHAPE_FOLDERS:
                        info = self._od_layer_shape(e['key'], t)
                        style = 'mesh' if info and info.get('shape') == 'grid' else 'species'
                    self._od_style_layer(lyr, style)
                    QgsProject.instance().addMapLayer(lyr, False)
                    grp.addLayer(lyr).setExpanded(False)  # 凡例（樹種の一覧）は閉じておく
                    shown = lyr
        else:
            for rel in rec.get('rasters', []):
                path = os.path.join(shared, rel)
                if not os.path.exists(path):
                    continue
                r = QgsRasterLayer(path, '林相識別図_' + os.path.splitext(os.path.basename(path))[0][-6:])
                if r.isValid():
                    r.setOpacity(1.0)
                    QgsProject.instance().addMapLayer(r, False)
                    grp.addLayer(r).setExpanded(False)
                    shown = shown or r
        grp.setName(f"{_GROUP_NAME}：{e['city']}_{e['yr']}計測_{e['no']}")
        self._od_shown = (e['key'], t) if shown else None
        # 先に表のページにしておく。レイヤーを表に結び付けると、何も選んでいなければ
        # 統計のページに切り替わるので、そのあとで戻さない
        self.stack_od.setCurrentIndex(0)
        if isinstance(shown, QgsVectorLayer):
            self._od_set_attr_layer(shown)
        if shown is not None and t not in _ZIP_LAYERS:
            self._od_show_rinso_info(e['key'], rec, shared)
        label = self._od_shown_label() if shown else self._od_lname_label(e['key'], _ZIP_LAYERS.get(t, ('',) * 4)[3])
        # 色分けがふつうと違うとき（小班ごとの集計・樹種の列が無い）は、そのことを添える
        note = ''
        if t in _ZIP_LAYERS:
            fmt = (rec.get('formats') or {}).get(_ZIP_LAYERS[t][3])
            if fmt == '':
                note = '・樹種の列が無いため色分けなし'
            elif fmt and fmt.startswith('小班集計'):
                note = f'・{fmt}で色分け'
        self._od_set_status(
            f"表示しました：{e['city']}_{e['yr']}_{e['no']}（{label}{note}）" if shown
            else f'{label}を表示できませんでした', warn=not shown)
        self._od_render_left()
        self._od_update_bottom_ts()
        self._refresh_map_canvas()

    def _od_style_layer(self, lyr, t):
        """解析元（下田市・南伊豆町・松崎町）のレイヤーから書き出したスタイルを当て、
        色分けの列を樹種名をそろえた 樹種_統一 に差し替える。
        t：スタイルの種類（mesh＝メッシュ、species＝ポリゴン、points＝単木ポイント）。"""
        qml = os.path.join(_STYLE_DIR, f'{t}.qml')
        if os.path.exists(qml):
            lyr.loadNamedStyle(qml)
        r = lyr.renderer()
        if hasattr(r, 'setClassAttribute') and lyr.fields().indexOf('樹種_統一') >= 0:
            r.setClassAttribute('樹種_統一')
            if t != 'points':  # 単木ポイントはスギ・ヒノキ類だけなので足さない
                self._od_add_missing_categories(r)
        if t != 'points':
            self._od_apply_outline_rule(lyr)
        # 選択の色：「選択された地物の色を上書き」（半透明の黄色）。プロジェクトの選択の色に左右されない
        if hasattr(lyr, 'selectionProperties'):
            from qgis.core import Qgis
            from qgis.PyQt.QtGui import QColor
            sp = lyr.selectionProperties()
            sp.setSelectionColor(QColor(*_FCDS_SELECTION_RGBA))
            sp.setSelectionRenderingMode(Qgis.SelectionRenderingMode.CustomColor)

    def _od_apply_outline_rule(self, lyr):
        """「この縮尺より拡大したら外周線を消す」を、区分ごとの記号の線のスタイルに当てる（データ定義の上書き）。
        オフなら上書きを外す。凡例は変わらない。"""
        from qgis.core import QgsProperty, QgsSymbolLayer
        r = lyr.renderer()
        if not hasattr(r, 'categories'):
            return
        key = QgsSymbolLayer.Property.StrokeStyle
        if self.chk_od_outline.isChecked():
            prop = QgsProperty.fromExpression(
                f"CASE WHEN @map_scale <= {self.spin_od_outline.value()} THEN 'no' ELSE 'solid' END")
        else:
            prop = QgsProperty()
        for i, cat in enumerate(r.categories()):
            sym = cat.symbol().clone()
            for k in range(sym.symbolLayerCount()):
                sl = sym.symbolLayer(k)
                if hasattr(sl, 'strokeStyle'):  # 塗り（外周線のある記号）だけ
                    sl.setDataDefinedProperty(key, prop)
            r.updateCategorySymbol(i, sym)

    def _od_on_outline_setting(self):
        """外周線の設定を変えた。QGIS の設定に覚えさせ、表示中のメッシュ・樹種ポリゴンに当て直す。"""
        st = QSettings()
        st.setValue('fcloud_shizuoka/od_outline_hide', self.chk_od_outline.isChecked())
        st.setValue('fcloud_shizuoka/od_outline_scale', self.spin_od_outline.value())
        layer = self._od_shown_layer()
        if layer is None or not self._od_shown or self._od_shown[1] not in ('mesh', 'species'):
            return
        self._od_apply_outline_rule(layer)
        layer.triggerRepaint()

    @staticmethod
    def _od_add_missing_categories(r):
        """スタイルに無い樹種の区分（森林外など）を、空の値の区分の前に足す。
        記号は先頭の区分のものを写して色だけ変える（塗りの不透明度は先頭の区分に合わせる）。"""
        from qgis.core import QgsRendererCategory
        from qgis.PyQt.QtGui import QColor
        cats = r.categories()
        if not cats:
            return

        def is_null(v):
            return v is None or (hasattr(v, 'isNull') and v.isNull())
        have = {c.value() for c in cats if not is_null(c.value())}
        added = []
        for name, color in _FCDS_SPECIES_COLORS:
            if name in have:
                continue
            sym = cats[0].symbol().clone()
            c = QColor(color)
            c.setAlpha(cats[0].symbol().color().alpha())
            sym.setColor(c)
            added.append(QgsRendererCategory(name, sym, name))
        if not added:
            return
        nulls = [c for c in cats if is_null(c.value())]
        keep = [c for c in cats if not is_null(c.value())]
        r.deleteAllCategories()
        for c in keep + added + nulls:
            r.addCategory(c)

    # ------------------------------------------------------------------
    # 林相識別図の情報（表の場所に出す）
    # ------------------------------------------------------------------

    def _od_show_rinso_info(self, key, rec, shared):
        """林相識別図の画像からわかる情報と、計画図との重なりを表の場所に出す。"""
        self.stack_od.setCurrentIndex(1)
        image = self._od_ensure_rinso_image(key, rec, shared)
        overlap, has_plan, src, missing = self._od_number_overlap(key, rec, shared)
        self.txt_od_rinso.setHtml(self._od_rinso_html(image, overlap, has_plan, src, missing))

    def _od_ensure_rinso_image(self, key, rec, shared):
        """林相識別図の画像の情報。一度求めたら取得済みの記録（index.json）に残し、次からは読むだけにする。"""
        image = (rec.get('rinso_info') or {}).get('image')
        if image:
            return image
        paths = [os.path.join(shared, rel) for rel in rec.get('rasters', [])
                 if os.path.exists(os.path.join(shared, rel))]
        self._od_set_status('林相識別図の情報を調べています…')
        self._od_repaint()
        image = self._od_rinso_image_info(paths)
        idx = self._od_load_index()
        if key in idx:
            idx[key]['rinso_info'] = {'image': image}
            self._od_save_index(idx)
        return image

    def _od_number_overlap(self, key, rec, shared):
        """同じ番号のデータと計画図の重なり（市町・林班の範囲・小班数）。（結果, 計画図があるか, 範囲の出所, 無い理由）。
        メッシュ・単木ポイント・林相識別図で共通に1つだけ持つ。範囲は、統計と一緒に求めたメッシュ（または
        樹種ポリゴン）の範囲、無ければ林相識別図のデータのある範囲を使う。
        計画図レイヤーが変わったときだけ求め直し、取得済みの記録に残す。"""
        plan = self._connected_layer
        try:
            if plan is None or sip.isdeleted(plan):
                return None, False, '', None
            plan_key = f'{plan.source()}|{plan.subsetString()}'
        except RuntimeError:
            return None, False, '', None  # 計画図レイヤーが削除済み
        cached = rec.get('overlap') or {}
        if cached.get('key') == plan_key:
            return cached, True, cached.get('src', ''), None
        footprint = fp_crs = None
        src = ''
        for lname, st in (rec.get('stats') or {}).items():
            if st.get('footprint'):
                footprint, fp_crs = QgsGeometry.fromWkt(st['footprint']), 'EPSG:6676'
                src = f"{self._od_lname_label(key, st.get('footprint_src', lname))}のある範囲"
                break
        if footprint is None and rec.get('rasters'):
            paths = [os.path.join(shared, rel) for rel in rec['rasters']
                     if os.path.exists(os.path.join(shared, rel))]
            self._od_set_status('林相識別図のデータのある範囲を調べています…')
            self._od_repaint()
            footprint, fp_crs = self._od_rinso_footprint(paths)
            src = '林相識別図のデータのある範囲'
        if footprint is None:
            return None, True, '', 'メッシュ・単木ポイントの統計を求めるか、林相識別図を表示すると表示します'
        self._od_set_status('計画図との重なりを調べています…')
        self._od_repaint()
        result = self._od_rinso_plan_overlap(footprint, fp_crs, plan)
        result['key'], result['src'] = plan_key, src
        idx = self._od_load_index()
        if key in idx:
            idx[key]['overlap'] = result
            self._od_save_index(idx)
        return result, True, src, None

    def _od_repaint(self):
        """重い処理の前に、状態表示を描き直しておく（操作は受け付けない）。"""
        from qgis.PyQt.QtCore import QEventLoop
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    @staticmethod
    def _od_rinso_alpha_overview(ds):
        """透明度のバンドの縮小版のうち、長い辺が 1,500 画素以上で最も小さいもの。
        （縮小版, 縮小の倍率）。透明度のバンドか縮小版が無ければ (None, None)。"""
        from osgeo import gdal
        alpha = None
        for i in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(i)
            if band.GetColorInterpretation() == gdal.GCI_AlphaBand:
                alpha = band
                break
        if alpha is None or alpha.GetOverviewCount() == 0:
            return None, None
        best = None
        for i in range(alpha.GetOverviewCount()):
            ov = alpha.GetOverview(i)
            if max(ov.XSize, ov.YSize) < 1500:
                continue
            if best is None or ov.XSize < best.XSize:
                best = ov
        if best is None:  # どれも小さいときは一番大きい縮小版
            best = max((alpha.GetOverview(i) for i in range(alpha.GetOverviewCount())),
                       key=lambda o: o.XSize)
        return best, ds.RasterXSize / best.XSize

    def _od_rinso_image_info(self, paths):
        """分割ごとの大きさ・解像度・バンド数・座標系・容量、外接範囲、データのある面積。"""
        from osgeo import gdal
        parts = []
        xmin = ymin = xmax = ymax = None
        data_m2 = 0.0
        approx_px = None
        crs = ''
        bands = 0
        size = 0
        for path in paths:
            ds = gdal.Open(path)
            if ds is None:
                continue
            gt = ds.GetGeoTransform()
            px = abs(gt[1])
            w, h = ds.RasterXSize, ds.RasterYSize
            x0, y1 = gt[0], gt[3]
            x1, y0 = x0 + w * gt[1], y1 + h * gt[5]
            xmin = x0 if xmin is None else min(xmin, x0)
            xmax = x1 if xmax is None else max(xmax, x1)
            ymin = min(y0, y1) if ymin is None else min(ymin, y0, y1)
            ymax = max(y0, y1) if ymax is None else max(ymax, y0, y1)
            bands = sum(1 for i in range(1, ds.RasterCount + 1)
                        if ds.GetRasterBand(i).GetColorInterpretation() != gdal.GCI_AlphaBand)
            srs = ds.GetSpatialRef()
            if srs is not None and not crs:
                code = srs.GetAuthorityCode(None)
                crs = f'EPSG:{code}（{srs.GetName()}）' if code else srs.GetName()
            ov, scale = self._od_rinso_alpha_overview(ds)
            if ov is not None:
                arr = ov.ReadAsArray()
                data_m2 += float((arr > 0).sum()) * (px * scale) ** 2
                approx_px = px * scale
            else:
                data_m2 += w * h * px * px
            try:
                size += os.path.getsize(path)
            except OSError:
                pass
            name = os.path.splitext(os.path.basename(path))[0][-6:]
            parts.append({'name': name, 'w': w, 'h': h, 'px': px,
                          'km': [round(w * px / 1000, 2), round(h * px / 1000, 2)]})
            ds = None
        return {
            'parts': parts, 'bands': bands, 'crs': crs, 'size': size,
            'extent': [xmin, ymin, xmax, ymax] if xmin is not None else None,
            'data_ha': round(data_m2 / 10000, 1), 'approx_px': approx_px,
        }

    def _od_rinso_footprint(self, paths):
        """データのある範囲（透明でない画素）の形と、その座標系（WKT）。縮小版の透明度から作る。
        縮小版が無い分割は、画像の四角形で代える。"""
        from osgeo import gdal, ogr
        geoms = []
        wkt = ''
        for path in paths:
            ds = gdal.Open(path)
            if ds is None:
                continue
            wkt = wkt or ds.GetProjection()
            gt = ds.GetGeoTransform()
            ov, scale = self._od_rinso_alpha_overview(ds)
            if ov is None:
                x0, y1 = gt[0], gt[3]
                x1, y0 = x0 + ds.RasterXSize * gt[1], y1 + ds.RasterYSize * gt[5]
                from qgis.core import QgsRectangle
                geoms.append(QgsGeometry.fromRect(QgsRectangle(x0, y0, x1, y1)))
                ds = None
                continue
            # 縮小版はそれだけでは位置を持たないので、位置付きの一時画像に写してから形にする
            mask = (ov.ReadAsArray() > 0).astype('uint8')
            mem = gdal.GetDriverByName('MEM').Create('', ov.XSize, ov.YSize, 1, gdal.GDT_Byte)
            mem.SetGeoTransform((gt[0], gt[1] * scale, 0, gt[3], 0, gt[5] * scale))
            band = mem.GetRasterBand(1)
            band.WriteArray(mask)
            # メモリ上のベクタは、GDAL 3.11 より前は Memory、以後は MEM という名前
            drv = ogr.GetDriverByName('Memory') or ogr.GetDriverByName('MEM')
            vds = drv.CreateDataSource('fp')
            lyr = vds.CreateLayer('fp')
            lyr.CreateField(ogr.FieldDefn('v', ogr.OFTInteger))
            gdal.Polygonize(band, band, lyr, 0)
            for f in lyr:
                g = f.GetGeometryRef()
                if g is not None:
                    geoms.append(QgsGeometry.fromWkt(g.ExportToWkt()))
            vds = mem = ds = None
        if not geoms:
            return None, wkt
        return QgsGeometry.unaryUnion(geoms), wkt

    def _od_rinso_plan_overlap(self, footprint, fp_wkt, plan):
        """データのある範囲に重なる計画図の小班から、市町・林班の範囲・小班数を集める。"""
        if footprint is None or footprint.isEmpty():
            return {'cities': [], 'count': 0}
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform
        fp = QgsGeometry(footprint)
        # 'EPSG:6676' のような書き方と WKT のどちらでも受け取る
        img_crs = QgsCoordinateReferenceSystem(fp_wkt) if fp_wkt else plan.crs()
        if img_crs.isValid() and plan.crs() != img_crs:
            fp.transform(QgsCoordinateTransform(img_crs, plan.crs(), QgsProject.instance()))
        names = plan.fields().names()
        city_f = next((n for n in ('市町村名称', '市町村名', '市町村CD') if n in names), None)
        rin_f = next((n for n in ('林班_森林簿', '林班', 'RIN') if n in names), None)
        key_f = 'KEY1' if 'KEY1' in names else None
        engine = QgsGeometry.createGeometryEngine(fp.constGet())
        engine.prepareGeometry()
        req = QgsFeatureRequest().setFilterRect(fp.boundingBox())
        attrs = [n for n in (city_f, rin_f, key_f) if n]
        req.setSubsetOfAttributes(attrs, plan.fields())
        cities = {}
        count = 0
        for f in plan.getFeatures(req):
            g = f.geometry()
            if g is None or g.isEmpty() or not engine.intersects(g.constGet()):
                continue
            count += 1
            city = f[city_f] if city_f else None
            rin = f[rin_f] if rin_f else None
            if (city in (None, '') or rin in (None, '')) and key_f and f[key_f]:
                k = str(f[key_f])  # 計画区・市町村・林班…を3桁ずつ並べた番号
                city = city if city not in (None, '') else f'市町村CD {int(k[3:6])}'
                rin = rin if rin not in (None, '') else k[6:9]
            city = str(city) if city not in (None, '') else '不明'
            rins = cities.setdefault(city, set())
            try:
                rins.add(int(str(rin)))
            except (TypeError, ValueError):
                pass
        out = []
        for city, rins in sorted(cities.items(), key=lambda kv: len(kv[1]), reverse=True):
            out.append({'name': city, 'min': min(rins) if rins else None,
                        'max': max(rins) if rins else None, 'n': len(rins)})
        return {'cities': out, 'count': count}

    def _od_rinso_html(self, img, overlap, has_plan, src, missing):
        def row(k, val):
            return (f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                    f'{html.escape(k)}</td><td style="padding:1px 4px;">{html.escape(str(val))}</td></tr>')

        img = img or {}
        parts = img.get('parts') or []
        out = ['<table style="border-collapse:collapse;width:100%;">', self._info_title_row('林相識別図')]
        if parts:
            px = parts[0]['px']
            out.append(row('画像', f"{len(parts)}分割　{px:g}m／画素　{img.get('bands', 0)}バンド"))
            for p in parts:
                out.append(row(f"分割 {p['name']}",
                               f"{p['w']:,} × {p['h']:,} 画素（{p['km'][0]:g} × {p['km'][1]:g} km）"))
        ext = img.get('extent')
        if ext:
            out.append(row('範囲', f'東西 {(ext[2] - ext[0]) / 1000:.1f} km × '
                                 f'南北 {(ext[3] - ext[1]) / 1000:.1f} km（外接範囲）'))
        if img.get('data_ha'):
            approx = img.get('approx_px')
            note = f'（約 {approx:.0f}m の縮小版から求めたおおよその値）' if approx else ''
            out.append(row('データのある面積', f"約 {img['data_ha']:,.0f} ha{note}"))
        if img.get('crs'):
            out.append(row('座標系', img['crs']))
        if img.get('size'):
            out.append(row('容量', f"{img['size'] / 1e9:.2f} GB（取得・変換後）"))
        out.append('</table>')

        title = f'計画図との重なり（{src}）' if src else '計画図との重なり'
        out += self._od_plan_overlap_html(title, overlap, has_plan, missing)
        return ''.join(out)

    @staticmethod
    def _od_info_row(k, val):
        return (f'<tr><td style="color:gray;padding:1px 4px;white-space:nowrap;vertical-align:top;">'
                f'{html.escape(k)}</td><td style="padding:1px 4px;">{html.escape(str(val))}</td></tr>')

    def _od_plan_overlap_html(self, title, plan, has_plan, missing=None):
        """計画図との重なり（カバーする市町・林班の範囲・小班数）の表。
        missing：重なりを求める材料が無いときに出す文（あれば優先）。"""
        row = self._od_info_row
        out = ['<table style="border-collapse:collapse;width:100%;margin-top:6px;">',
               self._info_title_row(html.escape(title))]
        if missing:
            out.append(row('—', missing))
        elif not has_plan:
            out.append(row('—', '計画図レイヤーを選ぶと表示します'))
        elif not plan or not plan.get('count'):
            out.append(row('—', '重なる小班がありません'))
        else:
            cities = plan.get('cities', [])
            out.append(row('カバーする市町', '・'.join(c['name'] for c in cities)))
            for c in cities:
                if c['min'] is None:
                    continue
                span = f"{c['min']}" if c['min'] == c['max'] else f"{c['min']}〜{c['max']}"
                out.append(row(f"林班（{c['name']}）", f"{span}（{c['n']} 林班）"))
            out.append(row('かかる小班数', f"{plan['count']:,} 小班"))
        out.append('</table>')
        return out

    # ------------------------------------------------------------------
    # データの形（メッシュ・ポリゴン）
    # ------------------------------------------------------------------

    def _od_layer_shape(self, key, folder):
        """取得済みのフォルダ folder（_ZIP_LAYERS のキー）のデータの形 {'shape', 'cell'}。
        記録に無ければ（以前の版で取得したデータ）、GPKG の先頭の地物の面積から求めて記録に残す。"""
        if folder not in _SHAPE_FOLDERS:
            return None
        idx = self._od_load_index()
        rec = idx.get(key)
        if not rec:
            return None
        info = (rec.get('shapes') or {}).get(folder)
        if info:
            return info
        lname = _ZIP_LAYERS[folder][3]
        if lname not in rec.get('layers', []) or not rec.get('gpkg'):
            return None
        lyr = QgsVectorLayer(f"{os.path.join(self._od_shared_dir(), rec['gpkg'])}|layername={lname}", lname, 'ogr')
        if not lyr.isValid():
            return None
        areas = [f.geometry().area() for f in lyr.getFeatures(
            QgsFeatureRequest().setLimit(300).setNoAttributes()) if f.hasGeometry()]
        info = _classify_areas(areas)
        rec.setdefault('shapes', {})[folder] = info
        self._od_save_index(idx)
        return info

    def _od_stats_spec(self):
        """表示中のデータの統計の決まり (集計の決まり, 1セルの面積 ha)。
        統計を出すのはデータの形がメッシュのものと単木ポイント。ポリゴンは (None, None)。"""
        if not self._od_shown:
            return None, None
        key, t = self._od_shown
        if t == 'points':
            return '単木ポイント', None
        info = self._od_layer_shape(key, t)
        if info and info.get('shape') == 'grid':
            cell = info.get('cell')
            return 'メッシュ', (cell * cell / 10000 if cell else None)
        return None, None

    def _od_footprint_src(self, key, t, rec):
        """計画図との重なりに使う範囲を作るレイヤー。メッシュはそれ自身、単木ポイントは同じ GPKG の面のデータ
        （メッシュを優先。点からは範囲の形を作りにくいので、面のデータで代える）。"""
        if t != 'points':
            return _ZIP_LAYERS[t][3]
        folders = [f for f in _SHAPE_FOLDERS if _ZIP_LAYERS[f][3] in rec.get('layers', [])]
        folders.sort(key=lambda f: (self._od_layer_shape(key, f) or {}).get('shape') != 'grid')
        return _ZIP_LAYERS[folders[0]][3] if folders else None

    # ------------------------------------------------------------------
    # データ全体の統計（何も選んでいないとき、表の場所に出す）
    # ------------------------------------------------------------------

    def _od_show_data_stats(self):
        """表示中のデータ（メッシュ・単木ポイント）全体の統計を表の場所に出す。
        統計は初回だけ別スレッドで求め、取得済みの記録に残して次からは読むだけにする。"""
        if not self._od_shown:
            return
        key, t = self._od_shown
        if t not in _ZIP_LAYERS:
            return
        lname = _ZIP_LAYERS[t][3]
        spec, cell_ha = self._od_stats_spec()
        if spec is None:
            return  # ポリゴンは統計を出さない
        shared = self._od_shared_dir()
        rec = self._od_load_index().get(key) if shared else None
        if not rec or not rec.get('gpkg'):
            return
        self.stack_od.setCurrentIndex(1)
        stats = (rec.get('stats') or {}).get(lname)
        if stats is not None and stats.get('v') != _StatsWorker._VERSION:
            stats = None  # 求め方が古い統計は求め直す
        # 計画図との重なりは同じ番号のデータで共通（どれか1つで求めてあれば使う）
        plan, has_plan, src, missing = None, False, '', None
        if stats is not None:
            plan, has_plan, src, missing = self._od_number_overlap(key, rec, shared)
        self.txt_od_rinso.setHtml(self._od_stats_html(spec, stats, plan, has_plan, src, missing))
        if stats is None:
            self._od_start_stats(key, os.path.join(shared, rec['gpkg']), lname,
                                 spec, cell_ha, self._od_footprint_src(key, t, rec))

    def _od_start_stats(self, key, gpkg, lname, spec, cell_ha, fp_src):
        running = getattr(self, '_od_stats_running', None)
        if running is None:
            running = self._od_stats_running = set()
        if (key, lname) in running:
            return
        running.add((key, lname))
        worker = _StatsWorker(key, gpkg, lname, spec, cell_ha, fp_src)
        # 終わるまで参照を持っておく（実行中の QThread が捨てられると QGIS ごと落ちる）
        _OD_ORPHANED_WORKERS.add(worker)
        worker.finished.connect(lambda w=worker: _OD_ORPHANED_WORKERS.discard(w))
        worker.finished_ok.connect(self._od_stats_done)
        worker.failed.connect(self._od_stats_failed)
        self._od_set_status(f'{self._od_shown_label()}の全体の統計を求めています…')
        worker.start()

    def _od_lname_label(self, key, lname):
        """GPKG のレイヤー名（県のフォルダの区分）を、画面に出す名前（メッシュ／ポリゴン／単木ポイント）にする。"""
        folder = next((t for t, v in _ZIP_LAYERS.items() if v[3] == lname), None)
        if folder in _SHAPE_FOLDERS:
            return _shape_label(self._od_layer_shape(key, folder))
        return dict((k, label) for k, label in _FCDS_TYPES).get(folder, lname)

    def _od_shown_label(self):
        """表示中のデータの表示名（メッシュ（20m×20m）／ポリゴン／単木ポイント／林相識別図）。"""
        if not self._od_shown:
            return ''
        key, t = self._od_shown
        if t in _SHAPE_FOLDERS:
            return _shape_label(self._od_layer_shape(key, t))
        return dict((k, label) for k, label in _FCDS_TYPES).get(t, '')

    def _od_stats_done(self, key, lname, stats):
        getattr(self, '_od_stats_running', set()).discard((key, lname))
        if getattr(self, '_od_shutting_down', False) or sip.isdeleted(self.txt_od_rinso):
            return
        idx = self._od_load_index()
        if key in idx:
            idx[key].setdefault('stats', {})[lname] = stats
            self._od_save_index(idx)
        # まだ同じデータを表示していて、表が空（何も選んでいない）なら出し直す
        # （選んだ小班の範囲の統計を出しているときは、そのままにする）
        if (self._od_shown and self._od_shown[0] == key and self._od_attr is None
                and getattr(self, '_od_table_mode', None) != 'plan_stats'
                and _ZIP_LAYERS.get(self._od_shown[1], (None,) * 4)[3] == lname):
            self._od_show_data_stats()
            self._od_set_status('計画図レイヤーで小班を選択すると、その小班にかかる地物を表に出します')

    def _od_stats_failed(self, key, lname, message):
        getattr(self, '_od_stats_running', set()).discard((key, lname))
        if getattr(self, '_od_shutting_down', False):
            return
        self._od_set_status(f'{self._od_shown_label()}の全体の統計を求められませんでした：{message}', warn=True)

    def _od_stats_html(self, spec, stats, plan, has_plan, src, missing):
        """spec：集計の決まり（'メッシュ'／'単木ポイント'）。"""
        out = ['<table style="border-collapse:collapse;width:100%;">',
               self._info_title_row(html.escape(f'データ全体の統計（{self._od_shown_label()}）'))]
        out += self._od_stats_rows(spec, stats, '求めています…（初回だけ時間がかかります）')
        out.append('</table>')
        title = f'計画図との重なり（{src}）' if src else '計画図との重なり'
        out += self._od_plan_overlap_html(title, plan, has_plan,
                                          '求めています…' if stats is None else missing)
        out.append('<p style="color:gray;margin-top:8px;">計画図レイヤーで小班を選ぶと、その範囲の統計を出します。'
                   '地図で地物を選ぶと、その地物を表に出します。</p>')
        return ''.join(out)

    def _od_stats_rows(self, lname, stats, pending, show_area=True, per_ha=None):
        """統計の表の行（件数・面積・材積・本数・平均・樹種）。stats が None なら pending の文を出す。
        lname：集計の決まり（'メッシュ'／'単木ポイント'）。
        show_area=False：「メッシュデータの面積」を出さない（同じ広さを別の行で出しているとき）。
        per_ha：haあたり（ha材積・立木密度）の分母にする面積。None なら材積のある森林の面積で割る。"""
        row = self._od_info_row
        out = []
        if stats is None:
            out.append(row('—', pending))
        else:
            n = stats.get('count') or 0
            sums, avgs = stats.get('sum', {}), stats.get('avg', {})
            unit = stats.get('unit') or ('セル' if lname == 'メッシュ' else '本')
            out.append(row(f'{unit}数', f'{n:,} {unit}'))
            area = sums.get('面積_ha')
            # 面積の列の合計。メッシュの代わりに小班ごとの集計が入っているデータでは小班の面積の合計
            area_name = '小班集計データの面積' if unit == '小班' else f'{lname}データの面積'
            if area and show_area:
                out.append(row(area_name, f'{area:,.1f} ha'))
            # データ全体の統計では、haあたりの分母は対象の森林（材積の入っているところ）。土地全体の面積ではない
            forest = sums.get('森林面積')
            if forest:
                out.append(row('材積のある森林の面積', f'{forest:,.1f} ha'))
            vol = sums.get('合計材積', sums.get('単木材積'))
            if vol is not None:
                out.append(row('材積合計', f'{vol:,.0f} m³'))
            # 選んだ範囲の統計は、範囲の面積で割る（小班で見るときは小班の面積あたりが知りたい値。
            # 材積のある森林の面積で割ると、広葉樹ばかりの小班でも材積のある数セルの値になる）
            # データ全体の統計では、メッシュデータの面積（メッシュ全体）で割った値も並べる。厳密ではないが、
            # 選んだ範囲の統計（範囲の面積で割る）と同じ見方で比べられる
            if per_ha is not None:
                denoms = [('範囲の面積あたり', per_ha)]
            else:
                denoms = [('材積のある森林あたり', forest), (f'{area_name}あたり', area)]
            denoms = [(label, d) for label, d in denoms if d]
            if vol is not None:
                for label, d in denoms:
                    out.append(row(f'ha材積（{label}）', f'{vol / d:,.0f} m³/ha'))
            trees = sums.get('立木本数')
            if trees is not None:
                out.append(row('立木本数', f'{trees:,.0f} 本'))
                for label, d in denoms:
                    out.append(row(f'立木密度（{label}）', f'{trees / d:,.0f} 本/ha'))
            cell = f'材積のある{unit}の平均'
            labels = {'平均樹高': ('平均樹高', 'm', cell), '平均直径': ('平均直径', 'cm', cell),
                      '樹高': ('樹高', 'm', '平均'), '胸高直径': ('胸高直径', 'cm', '平均')}
            for col, val in avgs.items():
                if val is not None and col in labels:
                    label, u, note = labels[col]
                    out.append(row(label, f'{val:.1f} {u}（{note}）'))
            species = stats.get('species') or []
            if species and n:
                text = '、'.join(f'{name} {cnt:,}（{cnt / n:.0%}）' for name, cnt in species)
                out.append(row(f'樹種（{unit}数）', text))
        return out

    # ------------------------------------------------------------------
    # 計画図で選んだ小班の範囲の統計（表の場所に出す）
    # ------------------------------------------------------------------

    def _od_plan_selected_count(self):
        plan = self._connected_layer
        try:
            return plan.selectedFeatureCount() if plan is not None and not sip.isdeleted(plan) else 0
        except RuntimeError:
            return 0  # 計画図レイヤーが削除済み

    def _od_plan_mask_geoms(self, layer, rec):
        """選択中の小班のうち、このデータの範囲にかかるものの形（表示中のレイヤーの座標系）。
        データの範囲（統計と一緒に求めた形、無ければレイヤーの範囲）から先に小班を絞るので、
        計画図の全件を選んでいても、読むのは範囲の中の小班だけになる。"""
        from qgis.core import QgsCoordinateTransform
        plan = self._connected_layer
        selected = set(plan.selectedFeatureIds())
        fp = None
        for st in (rec.get('stats') or {}).values():
            if st.get('footprint'):
                fp = QgsGeometry.fromWkt(st['footprint'])  # 表示中のレイヤーと同じ座標系
                break
        to_plan = to_layer = None
        if plan.crs() != layer.crs():
            to_plan = QgsCoordinateTransform(layer.crs(), plan.crs(), QgsProject.instance())
            to_layer = QgsCoordinateTransform(plan.crs(), layer.crs(), QgsProject.instance())
        engine = None
        fp_layer = QgsGeometry(fp) if fp is not None and not fp.isEmpty() else None
        if fp_layer is not None:
            if to_plan is not None:
                fp.transform(to_plan)
            bbox = fp.boundingBox()
            engine = QgsGeometry.createGeometryEngine(fp.constGet())
            engine.prepareGeometry()
        else:
            bbox = layer.extent()
            if to_plan is not None:
                bbox = to_plan.transformBoundingBox(bbox)
        req = QgsFeatureRequest().setNoAttributes()
        # 選んだ小班が少なければ選んだものだけ、多ければ範囲の中の小班から選んだものを拾う
        if len(selected) <= 2000:
            req.setFilterFids(list(selected))
            feats = (f for f in plan.getFeatures(req) if bbox.intersects(f.geometry().boundingBox()))
        else:
            req.setFilterRect(bbox)
            feats = (f for f in plan.getFeatures(req) if f.id() in selected)
        out = []
        for f in feats:
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            if engine is not None and not engine.intersects(g.constGet()):
                continue
            g = QgsGeometry(g)
            if to_layer is not None:
                g.transform(to_layer)
            out.append(g)
        # 全件を選んでいるか（件数ではなく「計画図の全体」と書くため）
        whole = len(selected) >= plan.featureCount()
        return out, whole, fp_layer

    def _od_show_plan_stats(self, layer):
        """計画図で選んだ小班の範囲にある、表示中のデータの統計を表の場所に出す（集計は別スレッド）。"""
        self._od_table_mode = 'plan_stats'
        key, t = self._od_shown
        lname = _ZIP_LAYERS[t][3]
        shared = self._od_shared_dir()
        rec = self._od_load_index().get(key) if shared else None
        self.stack_od.setCurrentIndex(1)
        if not rec or not rec.get('gpkg'):
            self.txt_od_rinso.setHtml('')
            return
        try:
            geoms, whole, fp = self._od_plan_mask_geoms(layer, rec)
        except RuntimeError:
            return  # 計画図レイヤーが削除済み
        gen = self._od_plan_gen
        spec, cell_ha = self._od_stats_spec()  # ポリゴンは統計を求めず、小班の中の地物を表に出す
        self._od_plan_ctx = (gen, spec, whole, bool(geoms))
        # ポリゴンは統計を出さず、見つけた地物を表に出す（見つけるまで表の場所は空にしておく）
        self.txt_od_rinso.setHtml(self._od_plan_stats_html(None) if spec else '')
        if not geoms:
            self._od_set_status('選択中の範囲は、このデータの範囲にかかりません')
            return
        self._od_set_status('選択中の範囲の統計を求めています…' if spec
                            else '選択中の小班の中にある地物を探しています…')
        box = self._od_union_bbox(geoms)
        worker = _PlanStatsWorker(
            gen, os.path.join(shared, rec['gpkg']), lname, [bytes(g.asWkb()) for g in geoms],
            (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()),
            bytes(fp.asWkb()) if fp is not None else None, spec, cell_ha)
        # 終わるまで参照を持っておく（実行中の QThread が捨てられると QGIS ごと落ちる）
        _OD_ORPHANED_WORKERS.add(worker)
        worker.finished.connect(lambda w=worker: _OD_ORPHANED_WORKERS.discard(w))
        worker.finished_ok.connect(self._od_plan_stats_done)
        worker.failed.connect(self._od_plan_stats_failed)
        worker.progress.connect(self._od_plan_stats_progress)
        self._od_plan_worker = worker
        self.bar_od_progress.setValue(0)
        self.bar_od_progress.setFormat('%p%')
        self._od_progress_timer.start()
        worker.start()

    def _od_plan_stats_progress(self, gen, pct, text):
        ctx = getattr(self, '_od_plan_ctx', None)
        if (getattr(self, '_od_shutting_down', False) or ctx is None or ctx[0] != gen
                or sip.isdeleted(self.bar_od_progress)):
            return
        self.bar_od_progress.setValue(pct)
        self.bar_od_progress.setToolTip(text)
        what = '選択中の範囲の統計を求めています' if ctx[1] else '選択中の小班の中にある地物を探しています'
        self._od_set_status(f'{what}…（{text}）')

    def _od_hide_progress(self):
        bar = getattr(self, 'bar_od_progress', None)
        if bar is None or sip.isdeleted(bar):
            return
        timer = getattr(self, '_od_progress_timer', None)
        if timer is not None and not sip.isdeleted(timer):
            timer.stop()
        bar.setVisible(False)

    @staticmethod
    def _od_union_bbox(geoms):
        box = QgsGeometry(geoms[0]).boundingBox()
        for g in geoms[1:]:
            box.combineExtentWith(g.boundingBox())
        return box

    def _od_cancel_plan_stats(self):
        worker = getattr(self, '_od_plan_worker', None)
        if worker is not None:
            worker.cancelled = True
            self._od_plan_worker = None
        self._od_hide_progress()

    def _od_plan_stats_done(self, gen, stats):
        ctx = getattr(self, '_od_plan_ctx', None)
        if (getattr(self, '_od_shutting_down', False) or ctx is None or ctx[0] != gen
                or sip.isdeleted(self.txt_od_rinso)):
            return
        self._od_plan_worker = None
        self._od_hide_progress()
        if 'fids' in stats:
            layer = self._od_shown_layer()
            if layer is not None:
                self._od_build_table(layer, stats['fids'], 'plan')
            return
        self.txt_od_rinso.setHtml(self._od_plan_stats_html(stats))
        self._od_set_status('計画図の全体の統計' if ctx[2] else '選択中の範囲の統計')

    def _od_plan_stats_failed(self, gen, message):
        ctx = getattr(self, '_od_plan_ctx', None)
        if getattr(self, '_od_shutting_down', False) or ctx is None or ctx[0] != gen:
            return
        self._od_plan_worker = None
        self._od_hide_progress()
        self._od_set_status(f'選択中の範囲の統計を求められませんでした：{message}', warn=True)

    def _od_plan_stats_html(self, stats):
        _gen, lname, whole, n_in = self._od_plan_ctx  # lname：集計の決まり（'メッシュ'／'単木ポイント'／None）
        row = self._od_info_row
        scope = '計画図の全体' if whole else '選択中の範囲'
        out = ['<table style="border-collapse:collapse;width:100%;">',
               self._info_title_row(html.escape(f'{scope}の統計（{self._od_shown_label()}）'))]
        # 小班の数は出さない（同じ場所に枝番・整理番号違いの小班が重なっていることがあり、
        # 数えると場所としての広さと合わない）。広さは面積で出す
        if not n_in:
            out.append(row('このデータにかかる部分', 'ありません'))
        elif stats is None:
            out.append(row('このデータにかかる部分', '求めています…'))
        else:
            out.append(row('このデータにかかる部分', f"約 {stats.get('area_ha', 0):,.1f} ha"))
        if n_in:
            out += self._od_stats_rows(lname, stats, '求めています…', show_area=False,
                                       per_ha=(stats or {}).get('area_ha'))
        out.append('</table>')
        if n_in:
            what = 'セル' if lname == 'メッシュ' else '地物'
            note = (f'{what}の中心が小班の中にあるものを集計しています（境界の{what}は中心の位置で振り分け）。')
            if stats is not None and stats.get('approx'):
                note += f'中心の位置は約 {stats.get("res", 0):.1f}m の細かさで見たおおよそのものです。'
            out.append(f'<p style="color:gray;margin-top:8px;">{html.escape(note)}</p>')
        out.append('<p style="color:gray;margin-top:8px;">地図で地物を選ぶと、その地物を表に出します。</p>')
        return ''.join(out)

    # ------------------------------------------------------------------
    # 属性テーブル（表示中のレイヤー）
    # ------------------------------------------------------------------

    def _od_drop_attr(self):
        """属性テーブルの部品（選択モデル・フィルタ・モデル・キャッシュ）を表から外して破棄する。
        外すだけだと、地図の合図につながったまま残り、レイヤーの削除後に落ちる原因になる。"""
        self.tbl_od.setModel(None)
        parts = self._od_attr
        self._od_attr = None
        if not parts:
            return
        cache, model, filt, sel = parts
        for obj in (sel, filt, model, cache):
            if obj is not None and not sip.isdeleted(obj):
                sip.delete(obj)

    def _od_shown_layer(self):
        layer = getattr(self, '_od_layer', None)
        if layer is None:
            return None
        try:
            return layer if layer.isValid() else None
        except RuntimeError:
            return None  # 削除済み

    def _od_set_attr_layer(self, layer):
        """表示中のレイヤーを切り替える（None で切り離す）。表の中身は計画図の選択で決まる。"""
        old = self._od_shown_layer()
        if old is not None:
            try:
                old.selectionChanged.disconnect(self._od_on_layer_selection)
            except (TypeError, RuntimeError):
                pass  # 未接続・削除済みは想定内
        self._od_drop_attr()
        self._od_layer = layer
        if layer is None:
            return
        self._od_layer_is_grid = self._od_is_grid_layer(layer)
        layer.willBeDeleted.connect(lambda: self._od_set_attr_layer(None))
        # 地図での選択が変わったら表を出し直す（表の行選択とは連動させない）
        layer.selectionChanged.connect(self._od_on_layer_selection)
        self._od_refresh_table()

    @staticmethod
    def _od_is_grid_layer(layer):
        """計画図の選択で絞るレイヤーか。20m（または10m）のセル・点・小さいポリゴン（小班ごとの集計、
        樹冠）は絞る。樹種ごとにまとめた大きなポリゴンは対象範囲が広く、絞る意味がない。"""
        from qgis.core import QgsWkbTypes
        if layer.geometryType() != QgsWkbTypes.GeometryType.PolygonGeometry:
            return True
        areas = sorted(f.geometry().area() for f in layer.getFeatures(
            QgsFeatureRequest().setLimit(200).setNoAttributes()))
        if not areas:
            return False
        cells = sum(1 for a in areas if round(a) in (100, 400))
        # 小班の面積はふつう数 ha まで。半分以上が 5 ha 未満なら小さいポリゴンとみなす
        return cells >= len(areas) * 0.9 or areas[len(areas) // 2] < 50000

    def _od_refresh_table(self):
        """選んだものを表の場所に出す。表示中のレイヤーで地図から直接選んだ地物は表に、
        計画図で選んだ小班は、その範囲の統計にする（後から選んだほうを出す）。
        どちらも無ければデータ全体の統計を出す。"""
        layer = self._od_shown_layer()
        self._od_table_fid = None
        self._od_table_mode = None     # 表の場所：'direct'＝直接選んだ地物の表、'plan_stats'＝小班の範囲の統計
        # 選び直したら、前の選択で求めている統計は要らない
        self._od_plan_gen = getattr(self, '_od_plan_gen', 0) + 1
        self._od_cancel_plan_stats()
        self._od_drop_attr()
        if layer is None:
            return
        grid = getattr(self, '_od_layer_is_grid', True)
        if grid:
            plan_sel = self._od_plan_selected_count()
            direct = list(layer.selectedFeatureIds())
            # 両方あれば後から選んだほう（_od_table_source）を出す
            if direct and (not plan_sel or getattr(self, '_od_table_source', None) == 'direct'):
                fids = direct
                self._od_table_mode = 'direct'
            elif plan_sel:
                self._od_show_plan_stats(layer)
                return
            else:
                # 選択が無くなったら、前の選択の範囲の統計を消してから全体の表示に戻す
                # （全体の統計を出せないデータでは、表の場所は空になる）
                self.txt_od_rinso.clear()
                self.stack_od.setCurrentIndex(0)
                self._od_set_status('計画図レイヤーで小班を選択すると、その小班の範囲の統計を出します')
                self._od_show_data_stats()  # 何も選んでいないときは、データ全体の統計を出す
                return
        self._od_build_table(layer, fids if grid else None, self._od_table_mode)

    def _od_build_table(self, layer, fids, mode):
        """表示中のレイヤーの属性の表を作る。fids が None なら全件。
        mode：'direct'＝地図で直接選んだ地物、'plan'＝選んだ小班の中にある地物（統計を出さないデータ）。"""
        self._od_table_mode = mode
        canvas = self.iface.mapCanvas()
        grid = fids is not None
        # 読み込み中は画面が止まるので、先に件数を出して描き直しておく（固まったように見せない）。
        # 描き直しだけにし、操作は受け付けない（読み込みの途中で表を作り直されないように）
        total = len(fids) if grid else layer.featureCount()
        self._od_set_status(f'{layer.name()}を読み込み中…（{total:,} 件）')
        from qgis.PyQt.QtCore import QEventLoop
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        # 表に要るのは属性だけなので形状は持たない（大量の地物でも軽くする）。
        # 行を選んだときのハイライトは、その地物の形状だけを別に読む
        cache = QgsVectorLayerCache(layer, 50000, self)
        cache.setCacheGeometry(False)
        model = QgsAttributeTableModel(cache, self)
        if grid:
            model.setRequest(QgsFeatureRequest().setFilterFids(fids))
        model.loadLayer()
        filt = QgsAttributeTableFilterModel(canvas, model, self)
        filt.setFilterMode(QgsAttributeTableFilterModel.FilterMode.ShowAll)
        self.tbl_od.setModel(filt)
        self.stack_od.setCurrentIndex(0)
        # 属性テーブル標準の描画部品は、レイヤーで選択中の地物の行を選択色で塗る。森林簿タブと
        # 同じく地図の選択を表の行に出さないよう、選択状態を見ない標準の描画部品にする
        if getattr(self, '_od_plain_delegate', None) is None:
            from qgis.PyQt.QtWidgets import QStyledItemDelegate
            self._od_plain_delegate = QStyledItemDelegate(self.tbl_od)
        self.tbl_od.setItemDelegate(self._od_plain_delegate)
        # 標準の選択モデルは行の色を QGIS の選択状態で塗る。QGIS の選択を変えると大きなレイヤーの
        # 描き直しが起きて重いので、表だけの選択モデルにして行の色を出す
        sel = QItemSelectionModel(filt, self.tbl_od)
        self.tbl_od.setSelectionModel(sel)
        # 行番号のクリックは標準の処理で QGIS の選択状態を変えてしまうので、押せないようにする
        # （行番号は何行目かを見るためだけに表示する）
        self.tbl_od.verticalHeader().setSectionsClickable(False)
        from qgis.PyQt.QtWidgets import QAbstractItemView
        self.tbl_od.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_od.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # GPKG の fid は行番号で足りるので出さない
        for col in range(filt.columnCount(QModelIndex())):
            if str(filt.headerData(col, Qt.Orientation.Horizontal,
                                   Qt.ItemDataRole.DisplayRole)).lower() == 'fid':
                self.tbl_od.setColumnHidden(col, True)
        self._od_attr = (cache, model, filt, sel)
        sel.currentRowChanged.connect(self._od_on_table_current_row)
        if mode == 'direct':
            self._od_set_status(f'地図で選択中の{layer.name()}：{len(fids)} 件')
        elif mode == 'plan':
            self._od_set_status(f'選択中の小班の中にある{layer.name()}：{len(fids)} 件')
        else:
            self._od_set_status(f'{layer.name()}（樹種ごとにまとめた形のため全件）：{layer.featureCount()} 件')

    def _od_on_plan_selection(self):
        """計画図レイヤーの選択が変わった（dock_widget から呼ばれる）。"""
        if getattr(self, '_od_clearing_plan_selection', False):
            return  # 地図で直接選んだときの小班の選択解除（表は呼び出し元で出し直す）
        if self.cloud_tab.currentIndex() != _TAB_INDEX or self._od_shown_layer() is None:
            return
        if not getattr(self, '_od_layer_is_grid', True):
            return  # まとめた形のポリゴンは計画図の選択で絞らない
        layer = self._od_shown_layer()
        plan = self._connected_layer
        try:
            plan_selected = plan is not None and plan.selectedFeatureCount() > 0
        except RuntimeError:
            plan_selected = False  # 計画図レイヤーが削除済み
        if plan_selected:
            self._od_table_source = 'plan'
            if layer.selectedFeatureCount():
                # 小班を選んだら、表示中のレイヤーで直接選んでいた地物の選択は外す。
                # 選択の解除で表が出し直されないよう、その間はレイヤーの選択の合図を受けない
                self._od_clearing_layer_selection = True
                try:
                    layer.removeSelection()
                finally:
                    self._od_clearing_layer_selection = False
        self._clear_selection_highlights()
        self._od_refresh_table()
        self._od_render_left()

    def _od_on_layer_selection(self, selected, deselected, clear_and_select):
        """地図で表示中のレイヤーの選択が変わった。森林簿タブと同じく、地図の選択は表の行選択と
        連動させない（赤いハイライト・位置合わせは、表で行を選んだときだけ行う）。
        地図で直接選んだら、小班を選んでいてもその地物を表に出し直す（後から選んだほうを出す）。"""
        if getattr(self, '_od_clearing_layer_selection', False):
            return  # 小班を選んだときの選択解除（表は呼び出し元で出し直す）
        layer = self._od_shown_layer()
        self._clear_selection_highlights()
        if layer is None or self.cloud_tab.currentIndex() != _TAB_INDEX:
            return
        self._od_table_fid = None  # 地図で選んだときは表で選んだ地物を解く
        if getattr(self, '_od_layer_is_grid', True):
            if layer.selectedFeatureCount():
                self._od_table_source = 'direct'
                # 小班を選んだときにメッシュの選択を外すのと対に、地図で直接選んだら小班の選択を外す。
                # 選択の解除で表が出し直されないよう、その間は計画図の選択の合図を受けない
                plan = self._connected_layer
                try:
                    if plan is not None and plan.selectedFeatureCount():
                        self._od_clearing_plan_selection = True
                        try:
                            plan.removeSelection()
                        finally:
                            self._od_clearing_plan_selection = False
                except RuntimeError:
                    pass  # 計画図レイヤーが削除済み
            else:
                self._od_table_source = None
            self._od_refresh_table()
        else:
            # まとめた形のポリゴンは表が全件なので、地図で選んだ地物の行を選んで、そこまで表を送る
            ids = list(selected or []) or list(layer.selectedFeatureIds())
            self._od_select_table_row(ids[-1] if ids else None)
        self._od_render_left(focus_left=True)

    def _od_select_table_row(self, fid):
        """表の中で地物 fid の行を選び、そこまで表を送る（地図で選んだとき）。fid が None なら行の選択を外す。
        地図で選んだ地物なので、地図のハイライト・移動はしない（左パネルの表示だけ変える）。"""
        attr = self._od_attr
        if attr is None:
            return
        _cache, _model, filt, sel = attr
        if fid is None:
            sel.clear()
            return
        index = filt.fidToIndex(fid)
        if not index.isValid():
            return
        self._od_row_from_map = True
        try:
            sel.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                | QItemSelectionModel.SelectionFlag.Rows)
        finally:
            self._od_row_from_map = False
        self.tbl_od.scrollTo(index)
        self._od_table_fid = fid

    def _od_on_table_current_row(self, current, previous):
        """クリック・十字キーで現在の行が変わった。QGIS の選択状態は変えず、
        赤い枠でハイライトして位置を合わせる。"""
        if self._od_attr is None or not current.isValid():
            return
        self._od_table_fid = self._od_attr[2].rowToId(current)
        self._od_apply_table_row()

    def _od_apply_table_row(self):
        layer = self._od_shown_layer()
        if layer is None or self._od_table_fid is None or self.cloud_tab.currentIndex() != _TAB_INDEX:
            return
        self._clear_selection_highlights()
        # 地図で選んだ地物の行を選んだときは、地図はそのまま（左パネルに出すだけ）。
        # 表で選んだときは赤くハイライトして位置を合わせる。まとめた形のポリゴンも同じで、
        # 「画面ロック」「選択時のズームを無効化」がオンならその設定に従う
        if not getattr(self, '_od_row_from_map', False):
            f = next(layer.getFeatures(QgsFeatureRequest().setFilterFid(self._od_table_fid)), None)
            if f is not None and f.hasGeometry():
                self._add_selection_highlight(f.geometry(), layer.crs())
                self._zoom_to_selection_highlights()
        self._od_render_left(focus_left=True)

    def _od_remove_display(self):
        """表示中のデータのレイヤーを片付ける（パネルを閉じたとき・プラグイン終了時）。"""
        if not hasattr(self, '_od_attr'):
            return
        self._od_set_attr_layer(None)
        root = QgsProject.instance().layerTreeRoot()
        groups = [g for g in root.findGroups(True) if _is_od_group(g.name())]
        for g in groups:
            self._od_clear_group(g)
            parent = g.parent()
            if parent is not None:
                parent.removeChildNode(g)
        self._od_shown = None
        self.txt_od_rinso.clear()
        self.stack_od.setCurrentIndex(0)
        if groups:
            # 取得中は次の表示まで時間がかかるので、外したレイヤーの絵を地図に残さない
            self._refresh_map_canvas()
