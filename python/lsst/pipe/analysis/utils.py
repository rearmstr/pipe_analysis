from __future__ import print_function

import os
import re

import numpy as np
import scipy.odr as scipyOdr
import scipy.optimize as scipyOptimize
import scipy.stats as scipyStats
try:
    import fastparquet
except ImportError:
    fastparquet = None
    import logging
    logging.warning('fastparquet package not available.  Parquet files will not be written.')

from contextlib import contextmanager

from lsst.daf.persistence.safeFileIo import safeMakeDir
from lsst.pipe.base import Struct, TaskError

import lsst.afw.cameraGeom as cameraGeom
import lsst.afw.geom as afwGeom
import lsst.afw.image as afwImage
import lsst.afw.table as afwTable
import lsst.pex.config as pexConfig

try:
    from lsst.meas.mosaic.updateExposure import applyMosaicResultsCatalog
except ImportError:
    applyMosaicResultsCatalog = None

__all__ = ["Filenamer", "Data", "Stats", "Enforcer", "MagDiff", "MagDiffMatches", "MagDiffCompare",
           "AstrometryDiff", "TraceSize", "PsfTraceSizeDiff", "TraceSizeCompare", "PercentDiff",
           "E1Resids", "E2Resids", "E1ResidsHsmRegauss", "E2ResidsHsmRegauss", "FootNpixDiffCompare",
           "MagDiffErr", "ApCorrDiffErr", "CentroidDiff", "CentroidDiffErr", "deconvMom",
           "deconvMomStarGal", "concatenateCatalogs", "joinMatches", "checkIdLists", "checkPatchOverlap",
           "joinCatalogs", "getFluxKeys", "addColumnsToSchema", "addApertureFluxesHSC", "addFpPoint",
           "addFootprintNPix", "addRotPoint", "makeBadArray", "addFlag", "addIntFloatOrStrColumn",
           "calibrateSourceCatalogMosaic", "calibrateSourceCatalog", "calibrateCoaddSourceCatalog",
           "backoutApCorr", "matchJanskyToDn", "checkHscStack", "fluxToPlotString", "andCatalog",
           "writeParquet", "getRepoInfo", "findCcdKey", "getCcdNameRefList", "getDataExistsRefList",
           "orthogonalRegression", "distanceSquaredToPoly", "p1CoeffsFromP2x0y0", "p2p1CoeffsFromLinearFit",
           "lineFromP2Coeffs", "linesFromP2P1Coeffs", "makeEqnStr", "catColors", "setAliasMaps"]


def writeParquet(table, path, badArray=None):
    """Write an afwTable into Parquet format

    Parameters
    ----------
    table : `lsst.afw.table.SourceCatalog`
       Table to be written to parquet.
    path : `str`
       Path to which to write.  Must end in ".parq".
    badArray : `numpy.ndarray`, optional
       Boolean array with same length as catalog whose values indicate whether the source was deemed
       inappropriate for qa analyses (`None` by default).

    Returns
    -------
    None

    Notes
    -----
    This function first converts the afwTable to an astropy table,
    then to a pandas DataFrame, which is then written to parquet
    format using the fastparquet library.  If fastparquet is not
    available, then it will do nothing.
    """
    if fastparquet is None:
        return

    if not path.endswith('.parq'):
        raise ValueError('Please provide a filename ending in .parq.')

    if badArray is not None:
        # Add flag indicating source "badness" for qa analyses for the benefit of the Parquet files
        # being written to disk for subsequent interactive QA analysis.
        table = addFlag(table, badArray, "qaBad_flag", "Set to True for any source deemed bad for qa")
    df = table.asAstropy().to_pandas()
    df = df.set_index('id', drop=True)
    fastparquet.write(path, df)


class Filenamer(object):
    """Callable that provides a filename given a style"""
    def __init__(self, butler, dataset, dataId={}):
        self.butler = butler
        self.dataset = dataset
        self.dataId = dataId

    def __call__(self, dataId, **kwargs):
        filename = self.butler.get(self.dataset + "_filename", self.dataId, **kwargs)[0]
        # When trying to write to a different rerun (or output), if the given dataset exists in the _parent
        # rerun (or input) directory, _parent is added to the filename, and thus the output files
        # will actually oversrite those in the _parent rerun (or input) directory (which is bad if
        # your intention is to write to a different output dir!).  So, here we check for the presence
        # of _parent in the filename and strip it out if present.
        if "_parent/" in filename:
            print("Note: stripping _parent from filename: ", filename)
            filename = filename.replace("_parent/", "")
        safeMakeDir(os.path.dirname(filename))
        return filename


class Data(Struct):
    def __init__(self, catalog, quantity, mag, selection, color, error=None, plot=True):
        Struct.__init__(self, catalog=catalog[selection].copy(deep=True), quantity=quantity[selection],
                        mag=mag[selection], selection=selection, color=color, plot=plot,
                        error=error[selection] if error is not None else None)


class Stats(Struct):
    def __init__(self, dataUsed, num, total, mean, stdev, forcedMean, median, clip):
        Struct.__init__(self, dataUsed=dataUsed, num=num, total=total, mean=mean, stdev=stdev,
                        forcedMean=forcedMean, median=median, clip=clip)

    def __repr__(self):
        return "Stats(mean={0.mean:.4f}; stdev={0.stdev:.4f}; num={0.num:d}; total={0.total:d}; " \
            "median={0.median:.4f}; clip={0.clip:.4f}; forcedMean={0.forcedMean:})".format(self)


class Enforcer(object):
    """Functor for enforcing limits on statistics"""
    def __init__(self, requireGreater={}, requireLess={}, doRaise=False):
        self.requireGreater = requireGreater
        self.requireLess = requireLess
        self.doRaise = doRaise

    def __call__(self, stats, dataId, log, description):
        for label in self.requireGreater:
            for ss in self.requireGreater[label]:
                value = getattr(stats[label], ss)
                if value <= self.requireGreater[label][ss]:
                    text = ("%s %s = %.2f exceeds minimum limit of %.2f: %s" %
                            (description, ss, value, self.requireGreater[label][ss], dataId))
                    log.warn(text)
                    if self.doRaise:
                        raise AssertionError(text)
        for label in self.requireLess:
            for ss in self.requireLess[label]:
                value = getattr(stats[label], ss)
                if value >= self.requireLess[label][ss]:
                    text = ("%s %s = %.2f exceeds maximum limit of %.2f: %s" %
                            (description, ss, value, self.requireLess[label][ss], dataId))
                    log.warn(text)
                    if self.doRaise:
                        raise AssertionError(text)


class MagDiff(object):
    """Functor to calculate magnitude difference"""
    def __init__(self, col1, col2, unitScale=1.0):
        self.col1 = col1
        self.col2 = col2
        self.unitScale = unitScale

    def __call__(self, catalog):
        return -2.5*np.log10(catalog[self.col1]/catalog[self.col2])*self.unitScale


class MagDiffMatches(object):
    """Functor to calculate magnitude difference for match catalog"""
    def __init__(self, column, colorterm, zp=27.0, unitScale=1.0):
        self.column = column
        self.colorterm = colorterm
        self.zp = zp
        self.unitScale = unitScale

    def __call__(self, catalog):
        ref1 = -2.5*np.log10(catalog["ref_" + self.colorterm.primary + "_flux"])
        ref2 = -2.5*np.log10(catalog["ref_" + self.colorterm.secondary + "_flux"])
        ref = self.colorterm.transformMags(ref1, ref2)
        src = self.zp - 2.5*np.log10(catalog["src_" + self.column])
        return (src - ref)*self.unitScale


class MagDiffCompare(object):
    """Functor to calculate magnitude difference between two entries in comparison catalogs

    Note that the column entries are in flux units and converted to mags here.
    """
    def __init__(self, column, unitScale=1.0):
        self.column = column
        self.unitScale = unitScale

    def __call__(self, catalog):
        src1 = -2.5*np.log10(catalog["first_" + self.column])
        src2 = -2.5*np.log10(catalog["second_" + self.column])
        return (src1 - src2)*self.unitScale


class AstrometryDiff(object):
    """Functor to calculate difference between astrometry"""
    def __init__(self, first, second, declination1=None, declination2=None, unitScale=1.0):
        self.first = first
        self.second = second
        self.declination1 = declination1
        self.declination2 = declination2
        self.unitScale = unitScale

    def __call__(self, catalog):
        first = catalog[self.first]
        second = catalog[self.second]
        cosDec1 = np.cos(catalog[self.declination1]) if self.declination1 is not None else 1.0
        cosDec2 = np.cos(catalog[self.declination2]) if self.declination2 is not None else 1.0
        return (first*cosDec1 - second*cosDec2)*(1.0*afwGeom.radians).asArcseconds()*self.unitScale


class TraceSize(object):
    """Functor to calculate trace radius size for sources"""
    def __init__(self, column):
        self.column = column

    def __call__(self, catalog):
        srcSize = np.sqrt(0.5*(catalog[self.column + "_xx"] + catalog[self.column + "_yy"]))
        return np.array(srcSize)


class PsfTraceSizeDiff(object):
    """Functor to calculate trace radius size difference (%) between object and psf model"""
    def __init__(self, column, psfColumn):
        self.column = column
        self.psfColumn = psfColumn

    def __call__(self, catalog):
        srcSize = np.sqrt(0.5*(catalog[self.column + "_xx"] + catalog[self.column + "_yy"]))
        psfSize = np.sqrt(0.5*(catalog[self.psfColumn + "_xx"] + catalog[self.psfColumn + "_yy"]))
        sizeDiff = 100*(srcSize - psfSize)/(0.5*(srcSize + psfSize))
        return np.array(sizeDiff)


class TraceSizeCompare(object):
    """Functor to calculate trace radius size difference (%) between objects in matched catalog"""
    def __init__(self, column):
        self.column = column

    def __call__(self, catalog):
        srcSize1 = np.sqrt(0.5*(catalog["first_" + self.column + "_xx"] +
                                catalog["first_" + self.column + "_yy"]))
        srcSize2 = np.sqrt(0.5*(catalog["second_" + self.column + "_xx"] +
                                catalog["second_" + self.column + "_yy"]))
        sizeDiff = 100.0*(srcSize1 - srcSize2)/(0.5*(srcSize1 + srcSize2))
        return np.array(sizeDiff)


class PercentDiff(object):
    """Functor to calculate the percent difference between a given column entry in matched catalog"""
    def __init__(self, column):
        self.column = column

    def __call__(self, catalog):
        value1 = catalog["first_" + self.column]
        value2 = catalog["second_" + self.column]
        percentDiff = 100.0*(value1 - value2)/(0.5*(value1 + value2))
        return np.array(percentDiff)


class E1Resids(object):
    """Functor to calculate e1 ellipticity residuals for a given object and psf model"""
    def __init__(self, column, psfColumn, unitScale=1.0):
        self.column = column
        self.psfColumn = psfColumn
        self.unitScale = unitScale

    def __call__(self, catalog):
        srcE1 = ((catalog[self.column + "_xx"] - catalog[self.column + "_yy"])/
                 (catalog[self.column + "_xx"] + catalog[self.column + "_yy"]))
        psfE1 = ((catalog[self.psfColumn + "_xx"] - catalog[self.psfColumn + "_yy"])/
                 (catalog[self.psfColumn + "_xx"] + catalog[self.psfColumn + "_yy"]))
        e1Resids = srcE1 - psfE1
        return np.array(e1Resids)*self.unitScale


class E2Resids(object):
    """Functor to calculate e2 ellipticity residuals for a given object and psf model"""
    def __init__(self, column, psfColumn, unitScale=1.0):
        self.column = column
        self.psfColumn = psfColumn
        self.unitScale = unitScale

    def __call__(self, catalog):
        srcE2 = (2.0*catalog[self.column + "_xy"]/
                 (catalog[self.column + "_xx"] + catalog[self.column + "_yy"]))
        psfE2 = (2.0*catalog[self.psfColumn + "_xy"]/
                 (catalog[self.psfColumn + "_xx"] + catalog[self.psfColumn + "_yy"]))
        e2Resids = srcE2 - psfE2
        return np.array(e2Resids)*self.unitScale


class E1ResidsHsmRegauss(object):
    """Functor to calculate HSM e1 ellipticity residuals for a given object and psf model"""
    def __init__(self, unitScale=1.0):
        self.unitScale = unitScale

    def __call__(self, catalog):
        srcE1 = catalog["ext_shapeHSM_HsmShapeRegauss_e1"]
        psfE1 = ((catalog["ext_shapeHSM_HsmPsfMoments_xx"] - catalog["ext_shapeHSM_HsmPsfMoments_yy"])/
                 (catalog["ext_shapeHSM_HsmPsfMoments_xx"] + catalog["ext_shapeHSM_HsmPsfMoments_yy"]))
        e1Resids = srcE1 - psfE1
        return np.array(e1Resids)*self.unitScale


class E2ResidsHsmRegauss(object):
    """Functor to calculate HSM e1 ellipticity residuals for a given object and psf model"""
    def __init__(self, unitScale=1.0):
        self.unitScale = unitScale

    def __call__(self, catalog):
        srcE2 = catalog["ext_shapeHSM_HsmShapeRegauss_e2"]
        psfE2 = (2.0*catalog["ext_shapeHSM_HsmPsfMoments_xy"]/
                 (catalog["ext_shapeHSM_HsmPsfMoments_xx"] + catalog["ext_shapeHSM_HsmPsfMoments_yy"]))
        e2Resids = srcE2 - psfE2
        return np.array(e2Resids)*self.unitScale


class FootNpixDiffCompare(object):
    """Functor to calculate footprint nPix difference between two entries in comparison catalogs
    """
    def __init__(self, column):
        self.column = column

    def __call__(self, catalog):
        nPix1 = catalog["first_" + self.column]
        nPix2 = catalog["second_" + self.column]
        return nPix1 - nPix2


class MagDiffErr(object):
    """Functor to calculate magnitude difference error"""
    def __init__(self, column, unitScale=1.0):
        zp = 27.0  # Exact value is not important, since we're differencing the magnitudes
        self.column = column
        self.calib = afwImage.Calib()
        self.calib.setFluxMag0(10.0**(0.4*zp))
        self.calib.setThrowOnNegativeFlux(False)
        self.unitScale = unitScale

    def __call__(self, catalog):
        mag1, err1 = self.calib.getMagnitude(catalog["first_" + self.column],
                                             catalog["first_" + self.column + "Err"])
        mag2, err2 = self.calib.getMagnitude(catalog["second_" + self.column],
                                             catalog["second_" + self.column + "Err"])
        return np.sqrt(err1**2 + err2**2)*self.unitScale


class ApCorrDiffErr(object):
    """Functor to calculate magnitude difference error"""
    def __init__(self, column, unitScale=1.0):
        self.column = column
        self.unitScale = unitScale

    def __call__(self, catalog):
        err1 = catalog["first_" + self.column + "Err"]
        err2 = catalog["second_" + self.column + "Err"]
        return np.sqrt(err1**2 + err2**2)*self.unitScale


class CentroidDiff(object):
    """Functor to calculate difference in astrometry"""
    def __init__(self, component, first="first_", second="second_", centroid1="base_SdssCentroid",
                 centroid2="base_SdssCentroid", unitScale=1.0):
        self.component = component
        self.first = first
        self.second = second
        self.centroid1 = centroid1
        self.centroid2 = centroid2
        self.unitScale = unitScale

    def __call__(self, catalog):
        first = self.first + self.centroid1 + "_" + self.component
        second = self.second + self.centroid2 + "_" + self.component
        return (catalog[first] - catalog[second])*self.unitScale


class CentroidDiffErr(CentroidDiff):
    """Functor to calculate difference error for astrometry"""
    def __call__(self, catalog):
        firstx = self.first + self.centroid + "_xErr"
        firsty = self.first + self.centroid + "_yErr"
        secondx = self.second + self.centroid + "_xErr"
        secondy = self.second + self.centroid + "_yErr"

        subkeys1 = [catalog.schema[firstx].asKey(), catalog.schema[firsty].asKey()]
        subkeys2 = [catalog.schema[secondx].asKey(), catalog.schema[secondy].asKey()]
        menu = {"x": 0, "y": 1}

        return np.hypot(catalog[subkeys1[menu[self.component]]],
                        catalog[subkeys2[menu[self.component]]])*self.unitScale


def deconvMom(catalog):
    """Calculate deconvolved moments"""
    if "ext_shapeHSM_HsmSourceMoments_xx" in catalog.schema:
        hsm = catalog["ext_shapeHSM_HsmSourceMoments_xx"] + catalog["ext_shapeHSM_HsmSourceMoments_yy"]
    else:
        hsm = np.ones(len(catalog))*np.nan
    sdss = catalog["base_SdssShape_xx"] + catalog["base_SdssShape_yy"]
    if "ext_shapeHSM_HsmPsfMoments_xx" in catalog.schema:
        psfXxName = "ext_shapeHSM_HsmPsfMoments_xx"
        psfYyName = "ext_shapeHSM_HsmPsfMoments_yy"
    elif "base_SdssShape_psf_xx" in catalog.schema:
        psfXxName = "base_SdssShape_psf_xx"
        psfYyName = "base_SdssShape_psf_yy"
    else:
        raise RuntimeError("No psf shape parameter found in catalog")
    psf = catalog[psfXxName] + catalog[psfYyName]
    return np.where(np.isfinite(hsm), hsm, sdss) - psf


def deconvMomStarGal(catalog):
    """Calculate P(star) from deconvolved moments"""
    rTrace = deconvMom(catalog)
    snr = catalog["base_PsfFlux_instFlux"]/catalog["base_PsfFlux_instFluxErr"]
    poly = (-4.2759879274 + 0.0713088756641*snr + 0.16352932561*rTrace - 4.54656639596e-05*snr*snr -
            0.0482134274008*snr*rTrace + 4.41366874902e-13*rTrace*rTrace + 7.58973714641e-09*snr*snr*snr +
            1.51008430135e-05*snr*snr*rTrace + 4.38493363998e-14*snr*rTrace*rTrace +
            1.83899834142e-20*rTrace*rTrace*rTrace)
    return 1.0/(1.0 + np.exp(-poly))


def concatenateCatalogs(catalogList):
    assert len(catalogList) > 0, "No catalogs to concatenate"
    template = catalogList[0]
    catalog = type(template)(template.schema)
    catalog.reserve(sum(len(cat) for cat in catalogList))
    for cat in catalogList:
        catalog.extend(cat, True)
    return catalog


def joinMatches(matches, first="first_", second="second_"):
    if not matches:
        return []

    mapperList = afwTable.SchemaMapper.join([matches[0].first.schema, matches[0].second.schema],
                                            [first, second])
    firstAliases = matches[0].first.schema.getAliasMap()
    secondAliases = matches[0].second.schema.getAliasMap()
    schema = mapperList[0].getOutputSchema()
    distanceKey = schema.addField("distance", type="Angle",
                                  doc="Distance between {0:s} and {1:s}".format(first, second))
    catalog = afwTable.BaseCatalog(schema)
    aliases = catalog.schema.getAliasMap()
    catalog.reserve(len(matches))
    for mm in matches:
        row = catalog.addNew()
        row.assign(mm.first, mapperList[0])
        row.assign(mm.second, mapperList[1])
        row.set(distanceKey, mm.distance*afwGeom.radians)
    # make sure aliases get persisted to match catalog
    for k, v in firstAliases.items():
        aliases.set(first + k, first + v)
    for k, v in secondAliases.items():
        aliases.set(second + k, second + v)
    return catalog


def checkIdLists(catalog1, catalog2, prefix=""):
    # Check to see if two catalogs have an identical list of objects by id
    idStrList = ["", ""]
    for i, cat in enumerate((catalog1, catalog2)):
        if "id" in cat.schema:
            idStrList[i] = "id"
        elif "objectId" in cat.schema:
            idStrList[i] = "objectId"
        elif prefix + "id" in cat.schema:
            idStrList[i] = prefix + "id"
        elif prefix + "objectId" in cat.schema:
            idStrList[i] = prefix + "objectId"
        else:
            raise RuntimeError("Cannot identify object id field (tried id, objectId, " + prefix + "id, and " +
                               prefix + "objectId)")

    return np.all(catalog1[idStrList[0]] == catalog2[idStrList[1]])


def checkPatchOverlap(patchList, tractInfo):
    # Given a list of patch dataIds along with the associated tractInfo, check if any of the patches overlap
    for i, patch0 in enumerate(patchList):
        overlappingPatches = False
        patchIndex = [int(val) for val in patch0.split(",")]
        patchInfo = tractInfo.getPatchInfo(patchIndex)
        patchBBox0 = patchInfo.getOuterBBox()
        for j, patch1 in enumerate(patchList):
            if patch1 != patch0 and j > i:
                patchIndex = [int(val) for val in patch1.split(",")]
                patchInfo = tractInfo.getPatchInfo(patchIndex)
                patchBBox1 = patchInfo.getOuterBBox()
                if patchBBox0.overlaps(patchBBox1):
                    overlappingPatches = True
                    break
        if overlappingPatches:
            break
    return overlappingPatches


def joinCatalogs(catalog1, catalog2, prefix1="cat1_", prefix2="cat2_"):
    # Make sure catalogs entries are all associated with the same object

    if not checkIdLists(catalog1, catalog2):
        raise RuntimeError("Catalogs with different sets of objects cannot be joined")

    mapperList = afwTable.SchemaMapper.join([catalog1[0].schema, catalog2[0].schema],
                                            [prefix1, prefix2])
    schema = mapperList[0].getOutputSchema()
    catalog = afwTable.BaseCatalog(schema)
    catalog.reserve(len(catalog1))
    for s1, s2 in zip(catalog1, catalog2):
        row = catalog.addNew()
        row.assign(s1, mapperList[0])
        row.assign(s2, mapperList[1])
    return catalog


def getFluxKeys(schema):
    """Retrieve the flux and flux error keys from a schema

    Both are returned as dicts indexed on the flux name (e.g. "base_PsfFlux_instFlux" or
    "modelfit_CModel_instFlux").
    """

    fluxTypeStr = "_instFlux"
    fluxSchemaItems = schema.extract("*" + fluxTypeStr)
    # Do not include any flag fields (as determined by their type).  Also exclude
    # slot fields, as these would effectively duplicate whatever they point to.
    fluxKeys = dict((name, schemaItem.key) for name, schemaItem in list(fluxSchemaItems.items()) if
                    schemaItem.field.getTypeString() != "Flag" and
                    not name.startswith("slot"))
    errSchemaItems = schema.extract("*" + fluxTypeStr + "Err")
    errKeys = dict((name, schemaItem.key) for name, schemaItem in list(errSchemaItems.items()) if
                   name[:-len("Err")] in fluxKeys)

    # Also check for any in HSC format
    schemaKeys = dict((s.field.getName(), s.key) for s in schema)
    fluxKeysHSC = dict((name, key) for name, key in schemaKeys.items() if
                       (re.search(r"^(flux\_\w+|\w+\_flux)$", name) or
                        re.search(r"^(\w+flux\_\w+|\w+\_flux)$", name)) and not
                       re.search(r"^(\w+\_apcorr)$", name) and name + "_err" in schemaKeys)
    errKeysHSC = dict((name + "_err", schemaKeys[name + "_err"]) for name in fluxKeysHSC.keys() if
                      name + "_err" in schemaKeys)
    if fluxKeysHSC:
        fluxKeys.update(fluxKeysHSC)
        errKeys.update(errKeysHSC)

    if not fluxKeys:
        raise RuntimeError("No flux keys found")

    return fluxKeys, errKeys


def addColumnsToSchema(fromCat, toCat, colNameList, prefix=""):
    """Copy columns from fromCat to new version of toCat"""
    fromMapper = afwTable.SchemaMapper(fromCat.schema)
    fromMapper.addMinimalSchema(toCat.schema, False)
    toMapper = afwTable.SchemaMapper(toCat.schema)
    toMapper.addMinimalSchema(toCat.schema)
    schema = fromMapper.editOutputSchema()
    for col in colNameList:
        colName = prefix + col
        fromKey = fromCat.schema.find(colName).getKey()
        fromField = fromCat.schema.find(colName).getField()
        schema.addField(fromField)
        toField = schema.find(colName).getField()
        fromMapper.addMapping(fromKey, toField, doReplace=True)

    newCatalog = afwTable.SourceCatalog(schema)
    newCatalog.reserve(len(toCat))

    newCatalog.extend(toCat, toMapper)
    for srcFrom, srcTo in zip(fromCat, newCatalog):
        srcTo.assign(srcFrom, fromMapper)

    aliases = newCatalog.schema.getAliasMap()
    for k, v in toCat.schema.getAliasMap().items():
        aliases.set(k, v)

    return newCatalog


def addApertureFluxesHSC(catalog, prefix=""):
    mapper = afwTable.SchemaMapper(catalog[0].schema)
    mapper.addMinimalSchema(catalog[0].schema)
    schema = mapper.getOutputSchema()
    apName = prefix + "base_CircularApertureFlux"
    apRadii = ["3_0", "4_5", "6_0", "9_0", "12_0", "17_0", "25_0", "35_0", "50_0", "70_0"]

    # for ia in range(len(apRadii)):
    # Just to 12 pixels for now...takes a long time...
    for ia in (4,):
        apFluxKey = schema.addField(apName + "_" + apRadii[ia] + "_instFlux", type="D",
                                    doc="flux within " + apRadii[ia].replace("_", ".") + "-pixel aperture",
                                    units="count")
        apFluxErrKey = schema.addField(apName + "_" + apRadii[ia] + "_instFluxErr", type="D",
                                       doc="1-sigma flux uncertainty")
    apFlagKey = schema.addField(apName + "_flag", type="Flag", doc="general failure flag")

    newCatalog = afwTable.SourceCatalog(schema)
    newCatalog.reserve(len(catalog))

    for source in catalog:
        row = newCatalog.addNew()
        row.assign(source, mapper)
        # for ia in range(len(apRadii)):
        for ia in (4,):
            row.set(apFluxKey, source[prefix+"flux_aperture"][ia])
            row.set(apFluxErrKey, source[prefix+"flux_aperture_err"][ia])
        row.set(apFlagKey, source[prefix + "flux_aperture_flag"])

    return newCatalog


def addFpPoint(det, catalog, prefix=""):
    # Compute Focal Plane coordinates for SdssCentroid of each source and add to schema
    mapper = afwTable.SchemaMapper(catalog[0].schema, shareAliasMap=True)
    mapper.addMinimalSchema(catalog[0].schema)
    schema = mapper.getOutputSchema()
    fpName = prefix + "base_FPPosition"
    fpxKey = schema.addField(fpName + "_x", type="D", doc="Position on the focal plane (in FP pixels)")
    fpyKey = schema.addField(fpName + "_y", type="D", doc="Position on the focal plane (in FP pixels)")
    fpFlag = schema.addField(fpName + "_flag", type="Flag", doc="Set to True for any fatal failure")

    newCatalog = afwTable.SourceCatalog(schema)
    newCatalog.reserve(len(catalog))
    xCentroidKey = catalog.schema[prefix + "base_SdssCentroid_x"].asKey()
    yCentroidKey = catalog.schema[prefix + "base_SdssCentroid_y"].asKey()
    for source in catalog:
        row = newCatalog.addNew()
        row.assign(source, mapper)
        try:
            center = afwGeom.Point2D(source[xCentroidKey], source[yCentroidKey])
            pixelsToFocalPlane = det.getTransform(cameraGeom.PIXELS, cameraGeom.FOCAL_PLANE)
            fpPoint = pixelsToFocalPlane.applyForward(center)
        except Exception:
            fpPoint = afwGeom.Point2D(np.nan, np.nan)
            row.set(fpFlag, True)
        row.set(fpxKey, fpPoint[0])
        row.set(fpyKey, fpPoint[1])
    return newCatalog


def addFootprintNPix(catalog, fromCat=None, prefix=""):
    # Retrieve the number of pixels in an sources footprint and add to schema
    mapper = afwTable.SchemaMapper(catalog[0].schema, shareAliasMap=True)
    mapper.addMinimalSchema(catalog[0].schema)
    schema = mapper.getOutputSchema()
    fpName = prefix + "base_Footprint_nPix"
    fpKey = schema.addField(fpName, type="I", doc="Number of pixels in Footprint")
    fpFlag = schema.addField(fpName + "_flag", type="Flag", doc="Set to True for any fatal failure")
    newCatalog = afwTable.SourceCatalog(schema)
    newCatalog.reserve(len(catalog))
    if fromCat:
        if len(fromCat) != len(catalog):
            raise TaskError("Lengths of fromCat and catalog for getting footprint Npixs do not agree")
    if fromCat is None:
        fromCat = catalog
    for srcFrom, srcTo in zip(fromCat, catalog):
        row = newCatalog.addNew()
        row.assign(srcTo, mapper)
        try:
            footNpix = srcFrom.getFootprint().getArea()
        except Exception:
            raise
            footNpix = 0  # used to be np.nan, but didn't work.
            row.set(fpFlag, True)
        row.set(fpKey, footNpix)
    return newCatalog


def rotatePixelCoord(s, width, height, nQuarter):
    """Rotate single (x, y) pixel coordinate such that LLC of detector in FP is (0, 0)
    """
    xKey = s.schema.find("slot_Centroid_x").key
    yKey = s.schema.find("slot_Centroid_y").key
    x0 = s[xKey]
    y0 = s[yKey]
    if nQuarter == 1:
        s.set(xKey, height - y0 - 1.0)
        s.set(yKey, x0)
    if nQuarter == 2:
        s.set(xKey, width - x0 - 1.0)
        s.set(yKey, height - y0 - 1.0)
    if nQuarter == 3:
        s.set(xKey, y0)
        s.set(yKey, width - x0 - 1.0)
    return s


def addRotPoint(catalog, width, height, nQuarter, prefix=""):
    # Compute rotated CCD pixel coords for comparing LSST vs HSC run centroids
    mapper = afwTable.SchemaMapper(catalog[0].schema, shareAliasMap=True)
    mapper.addMinimalSchema(catalog[0].schema)
    schema = mapper.getOutputSchema()
    rotName = prefix + "base_SdssCentroid_Rot"
    rotxKey = schema.addField(rotName + "_x", type="D", doc="Centroid x (in rotated pixels)")
    rotyKey = schema.addField(rotName + "_y", type="D", doc="Centroid y (in rotated pixels)")
    rotFlag = schema.addField(rotName + "_flag", type="Flag", doc="Set to True for any fatal failure")

    newCatalog = afwTable.SourceCatalog(schema)
    newCatalog.reserve(len(catalog))
    for source in catalog:
        row = newCatalog.addNew()
        row.assign(source, mapper)
        try:
            rotPoint = rotatePixelCoord(source, width, height, nQuarter).getCentroid()
        except Exception:
            rotPoint = afwGeom.Point2D(np.nan, np.nan)
            row.set(rotFlag, True)
        row.set(rotxKey, rotPoint[0])
        row.set(rotyKey, rotPoint[1])

    return newCatalog


def makeBadArray(catalog, flagList=[], onlyReadStars=False, patchInnerOnly=True, tractInnerOnly=False):
    """Create a boolean array indicating sources deemed unsuitable for qa analyses

    Sets value to True for unisolated objects (deblend_nChild > 0), "sky" objects (merge_peak_sky),
    and any of the flags listed in self.config.analysis.flags.  If onlyReadStars is True, sets boolean
    as True for all galaxies classified as extended (base_ClassificationExtendedness_value > 0.5).  If
    patchInnerOnly is True (the default), sets the bad boolean array value to True for any sources
    for which detect_isPatchInner is False (to avoid duplicates in overlapping patches).  If
    tractInnerOnly is True, sets the bad boolean value to True for any sources for which
    detect_isTractInner is False (to avoid duplicates in overlapping patches).  Note, however, that
    the default for tractInnerOnly is False as we are currently only running these scripts at the
    per-tract level, so there are no tract duplicates (and omitting the "outer" ones would just leave
    an empty band around the tract edges).

    Parameters
    ----------
    catalog : `lsst.afw.table.SourceCatalog`
       The source catalog under consideration.
    flagList : `list`
       The list of flags for which, if any is set for a given source, set bad entry to `True` for
       that source.
    onlyReadStars : `bool`, optional
       Boolean indicating if you want to select objects classified as stars only (based on
       base_ClassificationExtendedness_value > 0.5, `False` by default).
    patchInnerOnly : `bool`, optional
       Whether to select only sources for which detect_isPatchInner is `True` (`True` by default).
    tractInnerOnly : `bool`, optional
       Whether to select only sources for which detect_isTractInner is `True` (`False` by default).
       Note that these scripts currently only ever run at the per-tract level, so we do not need
       to filter out sources for which detect_isTractInner is `False` as, with only one tract, there
       are no duplicated tract inner/outer sources.

    Returns
    -------
    badArray : `numpy.ndarray`
       Boolean array with same length as catalog whose values indicate whether the source was deemed
       inappropriate for qa analyses.
    """
    bad = np.zeros(len(catalog), dtype=bool)
    if "detect_isPatchInner" in catalog.schema and patchInnerOnly:
        bad |= ~catalog["detect_isPatchInner"]
    if "detect_isTractInner" in catalog.schema and tractInnerOnly:
        bad |= ~catalog["detect_isTractInner"]
    bad |= catalog["deblend_nChild"] > 0  # Exclude non-deblended (i.e. parents)
    if "merge_peak_sky" in catalog.schema:
        bad |= catalog["merge_peak_sky"]  # Exclude "sky" objects (currently only inserted in coadds)
    for flag in flagList:
        bad |= catalog[flag]
    if onlyReadStars and "base_ClassificationExtendedness_value" in catalog.schema:
        bad |= catalog["base_ClassificationExtendedness_value"] > 0.5
    return bad


def addFlag(catalog, badArray, flagName, doc="General failure flag"):
    """Add a flag for any sources deemed not appropriate for qa analyses

    Parameters
    ----------
    catalog : `lsst.afw.table.SourceCatalog`
       Source catalog to which the flag will be added.
    badArray : `numpy.ndarray`
       Boolean array with same length as catalog whose values indicate whether the flag flagName
       should be set for a given oject.
    flagName : `str`
       Name of flag to be set
    doc : `str`, optional
       Docstring for ``flagName``

    Raises
    ------
    `RuntimeError`
       If lengths of ``catalog`` and ``badArray`` are not equal.

    Returns
    -------
    newCatalog : `lsst.afw.table.SourceCatalog`
       Source catalog with ``flagName`` column added.
    """
    if len(catalog) != len(badArray):
        raise RuntimeError('Lengths of catalog and bad objects array do not match.')

    mapper = afwTable.SchemaMapper(catalog[0].schema, shareAliasMap=True)
    mapper.addMinimalSchema(catalog[0].schema)
    schema = mapper.getOutputSchema()
    badFlag = schema.addField(flagName, type="Flag", doc=doc)
    newCatalog = afwTable.SourceCatalog(schema)
    newCatalog.reserve(len(catalog))
    newCatalog.extend(catalog, mapper)

    for i, row in enumerate(newCatalog):
        row.set(badFlag, bool(badArray[i]))
    return newCatalog


def addIntFloatOrStrColumn(catalog, values, fieldName, fieldDoc):
    """Add a column of values with name fieldName and doc fieldDoc to the catalog schema

    Parameters
    ----------
    catalog : `lsst.afw.table.SourceCatalog`
       Source catalog to which the column will be added.
    values : `list`, `numpy.ndarray`, or scalar of type `int`, `float`, or `str`
       The list of values to be added.  This list must have the same length as ``catalog`` or
       length 1 (to add a column with the same value for all objects).
    fieldName : `str`
       Name of the field to be added to the schema.
    fieldDoc : `str`
       Documentation string for the field to be added to the schema.

    Raises
    ------
    `RuntimeError`
       If type of all ``values`` is not one of `int`, `float`, or `str`.
    `RuntimeError`
       If length of ``values`` list is neither 1 nor equal to the ``catalog`` length.

    Returns
    -------
    newCatalog : `lsst.afw.table.SourceCatalog`
       Source catalog with ``fieldName`` column added.
    """
    if not isinstance(values, (list, np.ndarray)):
        if type(values) in (int, float, str):
            values = [values, ]
        else:
            raise RuntimeError(("Have only accommodated int, float, or str types.  Type provided was : "
                                "{}.  (Note, if you want to add a boolean flag column, use the addFlag "
                                "function.)").format(type(values)))
    if len(values) not in (len(catalog), 1):
        raise RuntimeError(("Length of values list must be either 1 or equal to the catalog length "
                            "({0:d}).  Length of values list provided was: {1:d}").
                           format(len(catalog), len(values)))

    size = None
    mapper = afwTable.SchemaMapper(catalog[0].schema, shareAliasMap=True)
    mapper.addMinimalSchema(catalog[0].schema)
    schema = mapper.getOutputSchema()

    if all(type(value) is int for value in values):
        fieldType = "I"
    elif all(isinstance(value, float) for value in values):
        fieldType = "D"
    elif all(type(value) is str for value in values):
        fieldType = str
        size = len(max(values, key=len))
    else:
        raise RuntimeError(("Have only accommodated int, float, or str types.  Type provided for the first "
                            "element was: {} (and note that all values in the list must have the same type.  "
                            "Also note, if you want to add a boolean flag column, use the addFlag "
                            "function.)").format(type(values[0])))

    fieldKey = schema.addField(fieldName, type=fieldType, size=size, doc=fieldDoc)

    newCatalog = afwTable.SourceCatalog(schema)
    newCatalog.reserve(len(catalog))

    newCatalog.extend(catalog, mapper)
    if len(values) == 1:
        for row in newCatalog:
            row.set(fieldKey, values[0])
    else:
        for i, row in enumerate(newCatalog):
            row.set(fieldKey, values[i])
    return newCatalog


def calibrateSourceCatalogMosaic(dataRef, catalog, fluxKeys=None, errKeys=None, zp=27.0):
    """Calibrate catalog with meas_mosaic results

    Requires a SourceCatalog input.
    """
    result = applyMosaicResultsCatalog(dataRef, catalog, True)
    catalog = result.catalog
    ffp = result.ffp
    # Convert to constant zero point, as for the coadds
    factor = ffp.calib.getFluxMag0()[0]/10.0**(0.4*zp)

    if fluxKeys is None:
        fluxKeys, errKeys = getFluxKeys(catalog.schema)
    for name, key in list(fluxKeys.items()) + list(errKeys.items()):
        if len(catalog[key].shape) > 1:
            continue
        catalog[key] /= factor
    return catalog


def calibrateSourceCatalog(catalog, zp):
    """Calibrate catalog in the case of no meas_mosaic results using FLUXMAG0 as zp

    Requires a SourceCatalog and zeropoint as input.
    """
    # Convert to constant zero point, as for the coadds
    fluxKeys, errKeys = getFluxKeys(catalog.schema)
    factor = 10.0**(0.4*zp)
    for name, key in list(fluxKeys.items()) + list(errKeys.items()):
        catalog[key] /= factor
    return catalog


def calibrateCoaddSourceCatalog(catalog, zp):
    """Calibrate coadd catalog

    Requires a SourceCatalog and zeropoint as input.
    """
    # Convert to constant zero point, as for the coadds
    fluxKeys, errKeys = getFluxKeys(catalog.schema)
    factor = 10.0**(0.4*zp)
    for name, key in list(fluxKeys.items()) + list(errKeys.items()):
        catalog[key] /= factor
    return catalog


def backoutApCorr(catalog):
    """Back out the aperture correction to all fluxes
    """
    ii = 0
    for k in catalog.schema.getNames():
        if "_instFlux" in k and k[:-5] + "_apCorr" in catalog.schema.getNames() and "_apCorr" not in k:
            if ii == 0:
                print("Backing out aperture corrections to fluxes")
                ii += 1
            catalog[k] /= catalog[k[:-5] + "_apCorr"]
    return catalog


def matchJanskyToDn(matches):
    # LSST reads in a_net catalogs with flux in "janskys", so must convert back to DN
    JANSKYS_PER_AB_FLUX = 3631.0
    schema = matches[0].first.schema
    keys = [schema[kk].asKey() for kk in schema.getNames() if "_flux" in kk]

    for m in matches:
        for k in keys:
            m.first[k] /= JANSKYS_PER_AB_FLUX
    return matches


def checkHscStack(metadata):
    """Check to see if data were processed with the HSC stack
    """
    try:
        hscPipe = metadata.getScalar("HSCPIPE_VERSION")
    except Exception:
        hscPipe = None
    return hscPipe


def fluxToPlotString(fluxToPlot):
    """Return a more succint string for fluxes for label plotting
    """
    fluxStrMap = {"base_PsfFlux_instFlux": "PSF",
                  "base_PsfFlux_flux": "PSF",
                  "base_PsfFlux": "PSF",
                  "base_GaussianFlux": "Gaussian",
                  "ext_photometryKron_KronFlux": "Kron",
                  "modelfit_CModel_instFlux": "CModel",
                  "modelfit_CModel_flux": "CModel",
                  "modelfit_CModel": "CModel",
                  "base_CircularApertureFlux_12_0": "CircAper 12pix"}
    if fluxToPlot in fluxStrMap:
        return fluxStrMap[fluxToPlot]
    else:
        print("WARNING: " + fluxToPlot + " not in fluxStrMap")
        return fluxToPlot


_eups = None


def getEups():
    """Return a EUPS handle

    We instantiate this once only, because instantiation is expensive.
    """
    global _eups
    from eups import Eups  # noqa Nothing else depends on eups, so prevent it from importing unless needed
    if not _eups:
        _eups = Eups()
    return _eups


@contextmanager
def andCatalog(version):
    eups = getEups()
    current = eups.findSetupVersion("astrometry_net_data")[0]
    eups.setup("astrometry_net_data", version, noRecursion=True)
    try:
        yield
    finally:
        eups.setup("astrometry_net_data", current, noRecursion=True)


def getRepoInfo(dataRef, coaddName=None, coaddDataset=None, doApplyUberCal=False):
    """Obtain the relevant repository information for the given dataRef

    Parameters
    ----------
    dataRef : `lsst.daf.persistence.butlerSubset.ButlerDataRef`
       The data reference for which the relevant repository information
       is to be retrieved.
    coaddName : `str`, optional
       The base name of the coadd (e.g. deep or goodSeeing) if
       ``dataRef`` is for coadd level processing (`None` by default).
    coaddDataset : `str`, optional
       The name of the coadd dataset (e.g. Coadd_forced_src or
       Coadd_meas) if ``dataRef`` is for coadd level processing
       (`None` by default).
    doApplyUberCal : `bool`, optional
       If `True`: Set the appropriate dataset type for the uber
       calibration from meas_mosaic.
       If `False` (the default): Set the dataset type to the source
       catalog from single frame processing.

    Raises
    ------
    `RuntimeError`
       If one of ``coaddName`` or ``coaddDataset`` is specified but
       the other is not.

    Returns
    -------
    result : `lsst.pipe.base.Struct`
       Result struct with components:

       - ``butler`` : the butler associated with ``dataRef``
         (`lsst.daf.persistence.Butler`).
       - ``camera`` : the camera associated with ``butler``
         (`lsst.afw.cameraGeom.Camera`).
       - ``dataId`` : the dataId associated with ``dataRef``
         (`lsst.daf.persistence.DataId`).
       - ``filterName`` : the name of the filter associated with ``dataRef``
         (`str`).
       - ``genericFilterName`` : a generic form for ``filterName`` (`str`).
       - ``ccdKey`` : the ccd key associated with ``dataId`` (`str` or `None`).
       - ``metadata`` : the metadata associated with ``butler`` and ``dataId``
         (`lsst.daf.base.propertyContainer.PropertyList`).
       - ``hscRun`` : string representing "HSCPIPE_VERSION" fits header if
         the data associated with ``dataRef``'s ``dataset`` were processed with
         the (now obsolete, but old reruns still exist) "HSC stack", None
         otherwise (`str` or `None`).
       - ``dataset`` : the dataset name ("src" if ``dataRef`` is visit level,
         coaddName + coaddDataset if ``dataRef`` is a coadd (`str`)
       - ``skyMap`` : the sky map associated with ``dataRef`` if it is a
         coadd (`lsst.skymap.SkyMap` or `None`).
       - ``wcs`` : the wcs of the coadd image associated with ``dataRef``
         -- only needed as a workaround for some old coadd catalogs that were
         persisted with all nan for ra dec (`lsst.afw.geom.SkyWcs` or `None`).
       - ``tractInfo`` : the tract information associated with ``dataRef`` if
         it is a coadd (`lsst.skymap.tractInfo.ExplicitTractInfo` or `None`).
    """
    if coaddName and not coaddDataset or not coaddName and coaddDataset:
        raise RuntimeError("If one of coaddName or coaddDataset is specified, the other must be as well.")

    butler = dataRef.getButler()
    camera = butler.get("camera")
    dataId = dataRef.dataId
    filterName = dataId["filter"]
    genericFilterName = afwImage.Filter(afwImage.Filter(filterName).getId()).getName()
    isCoadd = True if "patch" in dataId else False
    ccdKey = None if isCoadd else findCcdKey(dataId)
    # Check metadata to see if stack used was HSC
    metaStr = coaddName + coaddDataset + "_md" if coaddName is not None else "calexp_md"
    metadata = butler.get(metaStr, dataId)
    hscRun = checkHscStack(metadata)
    dataset = "src"
    skymap = butler.get(coaddName + "Coadd_skyMap") if coaddName is not None else None
    wcs = None
    tractInfo = None
    if isCoadd:
        coaddImageName = "Coadd_calexp_hsc" if hscRun else "Coadd_calexp"  # To get the coadd's WCS
        coadd = butler.get(coaddName + coaddImageName, dataId)
        wcs = coadd.getWcs()
        tractInfo = skymap[dataId["tract"]]
        dataset = coaddName + coaddDataset
    if doApplyUberCal:
        dataset = "wcs_hsc" if hscRun is not None else "jointcal_wcs"
    return Struct(
        butler=butler,
        camera=camera,
        dataId=dataId,
        filterName=filterName,
        genericFilterName=genericFilterName,
        ccdKey=ccdKey,
        metadata=metadata,
        hscRun=hscRun,
        dataset=dataset,
        skymap=skymap,
        wcs=wcs,
        tractInfo=tractInfo,
    )


def findCcdKey(dataId):
    """Determine the convention for identifying a "ccd" for the current camera

    Parameters
    ----------
    dataId : `instance` of `lsst.daf.persistence.DataId`

    Raises
    ------
    `RuntimeError`
       If "ccd" key could not be identified from the current hardwired list.

    Returns
    -------
    ccdKey : `str`
       The string associated with the "ccd" key.
    """
    ccdKey = None
    ccdKeyList = ["ccd", "sensor", "camcol", "detector"]
    for ss in ccdKeyList:
        if ss in dataId:
            ccdKey = ss
            break
    if ccdKey is None:
        raise RuntimeError("Could not identify ccd key for dataId: %s: \nNot in list of known keys: %s" %
                           (dataId, ccdKeyList))
    return ccdKey


def getCcdNameRefList(dataRefList):
    ccdNameRefList = None
    ccdKey = findCcdKey(dataRefList[0].dataId)
    if "raft" in dataRefList[0].dataId:
        ccdNameRefList = [re.sub("[,]", "", str(dataRef.dataId["raft"]) + str(dataRef.dataId[ccdKey])) for
                          dataRef in dataRefList]
    else:
        ccdNameRefList = [dataRef.dataId[ccdKey] for dataRef in dataRefList]
    # cull multiple entries
    ccdNameRefList = list(set(ccdNameRefList))

    if ccdNameRefList is None:
        raise RuntimeError("Failed to create ccdNameRefList")
    return ccdNameRefList


def getDataExistsRefList(dataRefList, dataset):
    dataExistsRefList = None
    ccdKey = findCcdKey(dataRefList[0].dataId)
    if "raft" in dataRefList[0].dataId:
        dataExistsRefList = [re.sub("[,]", "", str(dataRef.dataId["raft"]) + str(dataRef.dataId[ccdKey])) for
                             dataRef in dataRefList if dataRef.datasetExists(dataset)]
    else:
        dataExistsRefList = [dataRef.dataId[ccdKey] for dataRef in dataRefList if
                             dataRef.datasetExists(dataset)]
    # cull multiple entries
    dataExistsRefList = list(set(dataExistsRefList))

    if dataExistsRefList is None:
        raise RuntimeError("dataExistsRef list is empty")
    return dataExistsRefList


def fLinear(p, x):
    return p[0] + p[1]*x


def fQuadratic(p, x):
    return p[0] + p[1]*x + p[2]*x**2


def fCubic(p, x):
    return p[0] + p[1]*x + p[2]*x**2 + p[3]*x**3


def orthogonalRegression(x, y, order, initialGuess=None):
    """Perform an Orthogonal Distance Regression on the given data

    Parameters
    ----------
    x, y : `array`
       Arrays of x and y data to fit
    order : `int`, optional
       Order of the polynomial to fit
    initialGuess : `list` of `float`, optional
       List of the polynomial coefficients (highest power first) of an initial guess to feed to
       the ODR fit.  If no initialGuess is provided, a simple linear fit is performed and used
       as the guess (`None` by default).

    Returns
    -------
    result : `list` of `float`
       List of the fit coefficients (highest power first to mimic `numpy.polyfit` return).
    """
    if initialGuess is None:
        linReg = scipyStats.linregress(x, y)
        initialGuess = [linReg[0], linReg[1]]
        for i in range(order - 1):  # initialGuess here is linear, so need to pad array to match order
            initialGuess.insert(0, 0.0)
    if order == 1:
        odrModel = scipyOdr.Model(fLinear)
    elif order == 2:
        odrModel = scipyOdr.Model(fQuadratic)
    elif order == 3:
        odrModel = scipyOdr.Model(fCubic)
    else:
        raise RuntimeError("Order must be between 1 and 3 (value requested, {:}, not accommodated)".
                           format(order))
    odrData = scipyOdr.Data(x, y)
    orthDist = scipyOdr.ODR(odrData, odrModel, beta0=initialGuess)
    orthRegFit = orthDist.run()

    return list(reversed(orthRegFit.beta))


def distanceSquaredToPoly(x1, y1, x2, poly):
    """Calculate the square of the distance between point (x1, y1) and poly at x2

    Parameters
    ----------
    x1, y1 : `float`
       Point from which to calculate the square of the distance to the
       polynomial ``poly``.
    x2 : `float`
       Position on x axis from which to calculate the square of the distance
       between (``x1``, ``y1``) and ``poly`` (the position of the tangent of
       the polynomial curve closest to point (``x1``, ``y1``)).
    poly : `numpy.lib.polynomial.poly1d`
       Numpy polynomial fit from which to calculate the square of the distance
       to (``x1``, ``y1``) at ``x2``.

    Returns
    -------
    result : `float`
       Square of the distance between (``x1``, ``y1``) and ``poly`` at ``x2``
    """
    return (x2 - x1)**2 + (poly(x2) - y1)**2


def p1CoeffsFromP2x0y0(p2Coeffs, x0, y0):
    """Compute Ivezic P1 coefficients using the P2 coeffs and origin (x0, y0)

    Reference: Ivezic et al. 2004 (2004AN....325..583I)

    theta = arctan(mP1), where mP1 is the slope of the equivalent straight
                         line (the P1 line) from the P2 coeffs in the (x, y)
                         coordinate system and x = c1 - c2, y = c2 - c3
    P1 = cos(theta)*c1 + ((sin(theta) - cos(theta))*c2 - sin(theta)*c3 + deltaP1
    P1 = 0 at x0, y0 ==> deltaP1 = -cos(theta)*x0 - sin(theta)*y0

    Parameters
    ----------
    p2Coeffs : `list` of `float`
       List of the four P2 coefficients from which, along with the origin point
       (``x0``, ``y0``), to compute/derive the associated P1 coefficients.
    x0, y0 : `float`
       Coordinates at which to set P1 = 0 (i.e. the P1/P2 axis origin).

    Returns
    -------
    p1Coeffs: `list` of `float`
       The four P1 coefficients.
    """
    mP1 = p2Coeffs[0]/p2Coeffs[2]
    cosTheta = np.cos(np.arctan(mP1))
    sinTheta = np.sin(np.arctan(mP1))
    deltaP1 = -cosTheta*x0 - sinTheta*y0
    p1Coeffs = [cosTheta, sinTheta - cosTheta, -sinTheta, deltaP1]

    return p1Coeffs


def p2p1CoeffsFromLinearFit(m, b, x0, y0):
    """Derive the Ivezic et al. 2004 P2 and P1 equations based on linear fit

    Where the linear fit is to the given region in color-color space.
    Reference: Ivezic et al. 2004 (2004AN....325..583I)

    For y = m*x + b fit, where x = c1 - c2 and y = c2 - c3,
    P2 = (-m*c1 + (m + 1)*c2 - c3 - b)/sqrt(m**2 + 1)
    P2norm = P2/sqrt[(m**2 + (m + 1)**2 + 1**2)/(m**2 + 1)]

    P1 = cos(theta)*x + sin(theta)*y + deltaP1, theta = arctan(m)
    P1 = cos(theta)*(c1 - c2) + sin(theta)*(c2 - c3) + deltaP1
    P1 = cos(theta)*c1 + ((sin(theta) - cos(theta))*c2 - sin(theta)*c3 + deltaP1
    P1 = 0 at x0, y0 ==> deltaP1 = -cos(theta)*x0 - sin(theta)*y0

    Parameters
    ----------
    m : `float`
       Slope of line to convert.
    b : `float`
       Intercept of line to convert.
    x0, y0 : `float`
       Coordinates at which to set P1 = 0.

    Returns
    -------
    result : `lsst.pipe.base.Struct`
       Result struct with components:

       - ``p2Coeffs`` : four P2 equation coefficents (`list` of `float`).
       - ``p1Coeffs`` : four P1 equation coefficents (`list` of `float`).
    """
    # Compute Ivezic P2 coefficients using the linear fit slope and intercept
    scaleFact = np.sqrt(m**2 + 1.0)
    p2Coeffs = [-m/scaleFact, (m + 1.0)/scaleFact, -1.0/scaleFact, -b/scaleFact]
    p2Norm = 0.0
    for coeff in p2Coeffs[:-1]:  # Omit the constant normalization term
        p2Norm += coeff**2
    p2Norm = np.sqrt(p2Norm)
    p2Coeffs /= p2Norm

    # Compute Ivezic P1 coefficients equation using the linear fit slope and
    # point (x0, y0) as the origin
    p1Coeffs = p1CoeffsFromP2x0y0(p2Coeffs, x0, y0)

    return Struct(
        p2Coeffs=p2Coeffs,
        p1Coeffs=p1Coeffs,
    )


def lineFromP2Coeffs(p2Coeffs):
    """Compute P1 line in color-color space for given set P2 coefficients

    Reference: Ivezic et al. 2004 (2004AN....325..583I)

    Parameters
    ----------
    p2Coeffs : `list` of `float`
       List of the four P2 coefficients.

    Returns
    -------
    result : `lsst.pipe.base.Struct`
       Result struct with components:

       - ``mP1`` : associated slope for P1 in color-color coordinates (`float`).
       - ``bP1`` : associated intercept for P1 in color-color coordinates
                   (`float`).
    """
    mP1 = p2Coeffs[0]/p2Coeffs[2]
    bP1 = -p2Coeffs[3]*np.sqrt(mP1**2 + (mP1 + 1.0)**2 + 1.0)
    return Struct(
        mP1=mP1,
        bP1=bP1,
    )


def linesFromP2P1Coeffs(p2Coeffs, p1Coeffs):
    """Derive P1/P2 axes in color-color space based on the P2 and P1 coeffs

    Reference: Ivezic et al. 2004 (2004AN....325..583I)

    Parameters
    ----------
    p2Coeffs : `list` of `float`
       List of the four P2 coefficients.
    p1Coeffs : `list` of `float`
       List of the four P1 coefficients.

    Returns
    -------
    result : `lsst.pipe.base.Struct`
       Result struct with components:

       - ``mP2``, ``mP1`` : associated slopes for P2 and P1 in color-color
                            coordinates (`float`).
       - ``bP2``, ``bP1`` : associated intercepts for P2 and P1 in color-color
                            coordinates (`float`).
       - ``x0``, ``y0`` : x and y coordinates of the P2/P1 axes origin in
                          color-color coordinates (`float`).
    """
    p1Line = lineFromP2Coeffs(p2Coeffs)
    mP1 = p1Line.mP1
    bP1 = p1Line.bP1

    cosTheta = np.cos(np.arctan(mP1))
    sinTheta = np.sin(np.arctan(mP1))

    def func2(x):
        y = [cosTheta*x[0] + sinTheta*x[1] + p1Coeffs[3], mP1*x[0] - x[1] + bP1]
        return y

    x0y0 = scipyOptimize.fsolve(func2, [1, 1])
    mP2 = -1.0/mP1
    bP2 = x0y0[1] - mP2*x0y0[0]
    return Struct(
        mP2=mP2,
        bP2=bP2,
        mP1=mP1,
        bP1=bP1,
        x0=x0y0[0],
        y0=x0y0[1],
    )


def makeEqnStr(varName, coeffList, exponentList):
    """Make a string-formatted equation

    Parameters
    ----------
    varName : `str`
       Name of the equation to be stringified.
    coeffList : `list` of `float`
       List of equation coefficients (matched to exponenets in ``exponentList`` list).
    exponentList : `list` of `str`
       List of equation exponents (matched to coefficients in ``coeffList`` list).

    Raises
    ------
    `RuntimeError`
       If lengths of ``coeffList`` and ``exponentList`` are not equal.

    Returns
    -------
    eqnStr : `str`
       The stringified equation of the form:
       varName = coeffList[0]exponent[0] + ... + coeffList[n-1]exponent[n-1].
    """
    if len(coeffList) != len(exponentList):
        raise RuntimeError("Lengths of coeffList ({0:d}) and exponentList ({1:d}) are not equal".
                           format(len(coeffList), len(exponentList)))

    eqnStr = varName + " = "
    for i, (coeff, band) in enumerate(zip(coeffList, exponentList)):
        coeffStr = "{:.3f}".format(abs(coeff)) + band
        plusMinus = " $-$ " if coeff < 0.0 else " + "
        if i == 0:
            eqnStr += plusMinus.strip(" ") + coeffStr
        else:
            eqnStr += plusMinus + coeffStr

    return eqnStr


def catColors(c1, c2, magsCat, goodArray=None):
    """Compute color for a set of filters given a catalog of magnitudes by filter

    Parameters
    ----------
    c1, c2 : `str`
       String representation of the filters from which to compute the color.
    magsCat : `dict` of `numpy.ndarray`
       Dict of arrays of magnitude values.  Dict keys are the string representation of the filters.
    goodArray : `numpy.ndarray`, optional
       Boolean array with same length as the magsCat arrays whose values indicate whether the
       source was deemed "good" for intended use.  If `None`, all entries are considered "good"
       (`None` by default).

    Raises
    ------
    `RuntimeError`
       If lengths of ``goodArray`` and ``magsCat`` arrays are not equal.

    Returns
    -------
    `numpy.ndarray` of "good" colors (magnitude differeces).
    """
    if goodArray is None:
        goodArray = np.ones(len(magsCat[c1]), dtype=bool)

    if len(goodArray) != len(magsCat[c1]):
        raise RuntimeError("Lengths of goodArray ({0:d}) and magsCat ({1:d}) are not equal".
                           format(len(goodArray), len(magsCat[c1])))

    return (magsCat[c1] - magsCat[c2])[goodArray]


def setAliasMaps(catalog, aliasDictList, prefix=""):
    """Set an alias map for differing schema naming conventions

    Parameters
    ----------
    catalog : `lsst.afw.table.SourceCatalog`
       The source catalog to which the mapping will be added.
    aliasDictList : `dict` of `str` or `list` of `dict` of `str`
       A `list` of `dict` or single `dict` representing the alias mappings to
       be added to ``catalog``'s schema with the key representing the new
       name to be mapped to the value which represents the old name.  Note
       that the mapping will only be added if the old name exists in
       ``catalog``'s schema.

    prefix : `str`, optional
       This `str` will be prepended to the alias names (used, e.g., in matched
       catalogs for which "src_" and "ref_" prefixes have been added to all
       schema names).  Both the old and new names have ``prefix`` associated
       with them (default is an empty string).

    Raises
    ------
    `RuntimeError`
       If not all elements in ``aliasDictList`` are instances of type `dict` or
       `lsst.pex.config.dictField.Dict`.

    Returns
    -------
    catalog : `lsst.afw.table.SourceCatalog`
       The source catalog with the alias mappings added to the schema.
    """
    if isinstance(aliasDictList, dict):
        aliasDictList = [aliasDictList, ]
    if not all(isinstance(aliasDict, (dict, pexConfig.dictField.Dict)) for aliasDict in aliasDictList):
        raise RuntimeError("All elements in aliasDictList must be instances of type dict")
    aliasMap = catalog.schema.getAliasMap()
    for aliasDict in aliasDictList:
        for newName, oldName in aliasDict.items():
            if prefix + oldName in catalog.schema:
                aliasMap.set(prefix + newName, prefix + oldName)
    return catalog
