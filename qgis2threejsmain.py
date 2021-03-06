# -*- coding: utf-8 -*-
"""
/***************************************************************************
 Qgis2threejs
                                 A QGIS plugin
 export terrain data, map canvas image and vector data to web browser
                              -------------------
        begin                : 2014-01-16
        copyright            : (C) 2014 Minoru Akagi
        email                : akaginch@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
import os
import codecs
import datetime
import re

from PyQt4.QtCore import QDir, QSettings, Qt, qDebug, QT_VERSION_STR
from PyQt4.QtGui import QColor, QImage, QImageReader, QPainter, QMessageBox
from qgis.core import *

try:
  from osgeo import ogr
except ImportError:
  import ogr

import gdal2threejs
import qgis2threejstools as tools
from propertyreader import DEMPropertyReader, VectorPropertyReader
from quadtree import QuadTree, DEMQuadList

debug_mode = 1
apiChanged23 = QGis.QGIS_VERSION_INT >= 20300

# used for tree widget and properties
class ObjectTreeItem:
  topItemNames = ["World", "Controls", "DEM", "Additional DEM", "Point", "Line", "Polygon"]
  ITEM_WORLD = 0
  ITEM_CONTROLS = 1
  ITEM_DEM = 2
  ITEM_OPTDEM = 3
  ITEM_POINT = 4
  ITEM_LINE = 5
  ITEM_POLYGON = 6

class Point:
  def __init__(self, x, y, z=0):
    self.x = x
    self.y = y
    self.z = z

  def __eq__(self, other):
    return self.x == other.x and self.y == other.y and self.z == other.z

  def __ne__(self, other):
    return self.x != other.x or self.y != other.y or self.z != other.z


class MapTo3D:
  def __init__(self, mapCanvas, planeWidth=100, verticalExaggeration=1, verticalShift=0):
    # map canvas
    self.mapExtent = mapCanvas.extent()

    # 3d
    self.planeWidth = planeWidth
    self.planeHeight = planeWidth * mapCanvas.extent().height() / mapCanvas.extent().width()

    self.verticalExaggeration = verticalExaggeration
    self.verticalShift = verticalShift

    self.multiplier = planeWidth / mapCanvas.extent().width()
    self.multiplierZ = self.multiplier * verticalExaggeration

  def transform(self, x, y, z=0):
    extent = self.mapExtent
    return Point((x - extent.xMinimum()) * self.multiplier - self.planeWidth / 2,
                 (y - extent.yMinimum()) * self.multiplier - self.planeHeight / 2,
                 (z + self.verticalShift) * self.multiplierZ)

  def transformPoint(self, pt):
    return self.transform(pt.x, pt.y, pt.z)


class OutputContext:

  def __init__(self, templateName, templateType, mapTo3d, canvas, properties, dialog, objectTypeManager, localBrowsingMode=True):
    self.templateName = templateName
    self.templateType = templateType
    self.mapTo3d = mapTo3d
    self.canvas = canvas
    self.baseExtent = canvas.extent()
    self.properties = properties
    self.dialog = dialog
    self.objectTypeManager = objectTypeManager
    self.localBrowsingMode = localBrowsingMode

    self.mapSettings = canvas.mapSettings() if apiChanged23 else canvas.mapRenderer()
    self.crs = self.mapSettings.destinationCrs()

    wgs84 = QgsCoordinateReferenceSystem(4326)
    transform = QgsCoordinateTransform(self.crs, wgs84)
    self.wgs84Center = transform.transform(self.baseExtent.center())

    self.image_basesize = 256
    self.timestamp = datetime.datetime.today().strftime("%Y%m%d%H%M%S")

    world = properties[ObjectTreeItem.ITEM_WORLD] or {}
    self.coordsInWGS84 = world.get("radioButton_WGS84", False)

    p = properties[ObjectTreeItem.ITEM_CONTROLS]
    if p is None:
      self.controls = QSettings().value("/Qgis2threejs/lastControls", "OrbitControls.js", type=unicode)
    else:
      self.controls = p["comboBox_Controls"]

    self.demLayerId = None
    if templateType == "sphere":
      return

    self.demLayerId = demLayerId = properties[ObjectTreeItem.ITEM_DEM]["comboBox_DEMLayer"]
    if demLayerId:
      layer = QgsMapLayerRegistry.instance().mapLayer(demLayerId)
      self.warp_dem = tools.MemoryWarpRaster(layer.source())
    else:
      self.warp_dem = tools.FlatRaster()

    self.triMesh = None

  def triangleMesh(self):
    if self.triMesh is None:
      self.triMesh = TriangleMesh.createFromContext(self)
    return self.triMesh

class DataManager:
  """ manages a list of unique items """

  def __init__(self):
    self._list = []

  def _index(self, image):
    if image in self._list:
      return self._list.index(image)

    index = len(self._list)
    self._list.append(image)
    return index

class ImageManager(DataManager):

  IMAGE_FILE = 1
  CANVAS_IMAGE = 2
  MAP_IMAGE = 3
  LAYER_IMAGE = 4

  def __init__(self, context):
    DataManager.__init__(self)
    self.context = context
    self.renderer = None

  def imageIndex(self, path):
    img = (self.IMAGE_FILE, path)
    return self._index(img)

  def canvasImageIndex(self, transp_background):
    img = (self.CANVAS_IMAGE, transp_background)
    return self._index(img)

  def mapImageIndex(self, width, height, extent, transp_background):
    img = (self.MAP_IMAGE, (width, height, extent, transp_background))
    return self._index(img)

  def layerImageIndex(self, layerid, width, height, extent):
    img = (self.LAYER_IMAGE, (layerid, width, height, extent))
    return self._index(img)

  def mapCanvasImage(self, transp_background=False):
    """ returns base64 encoded map canvas image """
    canvas = self.context.canvas
    if transp_background:
      size = self.context.mapSettings.outputSize()
      return self.renderedImage(size.width(), size.height(), canvas.extent(), transp_background)

    if QGis.QGIS_VERSION_INT >= 20400:
     return tools.base64image(canvas.map().contentImage())
    temp_dir = QDir.tempPath()
    texfilename = os.path.join(temp_dir, "tex%s.png" % (self.context.timestamp))
    canvas.saveAsImage(texfilename)
    texData = gdal2threejs.base64image(texfilename)
    tools.removeTemporaryFiles([texfilename, texfilename + "w"])
    return texData

  def saveMapCanvasImage(self):
    texfilename = os.path.splitext(self.context.htmlfilename)[0] + ".png"
    self.context.canvas.saveAsImage(texfilename)
    texSrc = os.path.split(texfilename)[1]
    tools.removeTemporaryFiles([texfilename + "w"])

  def _initRenderer(self):
    canvas = self.context.canvas

    # set up a renderer
    labeling = QgsPalLabeling()
    renderer = QgsMapRenderer()
    renderer.setDestinationCrs(self.context.crs)
    renderer.setProjectionsEnabled(True)
    renderer.setLabelingEngine(labeling)

    # save renderer
    self._labeling = labeling
    self.renderer = renderer

    # layer list
    self._layerids = [mapLayer.id() for mapLayer in canvas.layers()]

    # canvas color
    self.canvasColor = canvas.canvasColor()

  def renderedImage(self, width, height, extent, transp_background=False, layerids=None):
    antialias = True

    if self.renderer is None:
      self._initRenderer()

    renderer = self.renderer
    if layerids is None:
      renderer.setLayerSet(self._layerids)
    else:
      renderer.setLayerSet(layerids)

    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)

    # QImage::fill ( const QColor & color ) was introduced in Qt 4.8.
    # http://qt-project.org/doc/qt-4.8/qimage.html#fill-3
    if transp_background:
      image.fill(QColor(Qt.transparent).rgba())   # though image.fill(Qt.transparent) seems to work in Qt 4.7.1
    else:
      image.fill(self.canvasColor.rgba())

    renderer.setOutputSize(image.size(), image.logicalDpiX())
    renderer.setExtent(extent)

    painter = QPainter()
    painter.begin(image)
    if antialias:
      painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()

    return tools.base64image(image)

    #if context.localBrowsingMode:
    #else:
    #  texfilename = os.path.splitext(htmlfilename)[0] + "_%d.png" % plane_index    #TODO: index
    #  image.save(texfilename)
    #  texSrc = os.path.split(texfilename)[1]
    #  tex["src"] = texSrc

  def write(self, f):   #TODO: separated image files (not in localBrowsingMode)
    if len(self._list) == 0:
      return

    f.write(u'\n// Base64 encoded images\n')
    for index, image in enumerate(self._list):
      imageType = image[0]
      if imageType == self.IMAGE_FILE:
        image_path = image[1]
        if os.path.exists(image_path):
          size = QImageReader(image_path).size()
          args = (index, size.width(), size.height(), gdal2threejs.base64image(image_path))
        else:
          f.write(u"project.images[%d] = {data:null};\n" % index)
          QgsMessageLog.logMessage(u'Image file not found: {0}'.format(image_path), "Qgis2threejs")
          continue

      elif imageType == self.MAP_IMAGE:
        width, height, extent, transp_background = image[1]
        args = (index, width, height, self.renderedImage(width, height, extent, transp_background))

      elif imageType == self.LAYER_IMAGE:
        layerid, width, height, extent = image[1]
        args = (index, width, height, self.renderedImage(width, height, extent, True, [layerid]))

      else:   #imageType == self.CANVAS_IMAGE:
        transp_background = image[1]
        size = self.context.mapSettings.outputSize()
        args = (index, size.width(), size.height(), self.mapCanvasImage(transp_background))

      f.write(u'project.images[%d] = {width:%d,height:%d,data:"%s"};\n' % args)


class MaterialManager(DataManager):

  MESH_LAMBERT = 0
  MESH_PHONG = 1
  LINE_BASIC = 2
  SPRITE = 3

  WIREFRAME = 10
  MESH_LAMBERT_SMOOTH = 0
  MESH_LAMBERT_FLAT = 11

  CANVAS_IMAGE = 20
  MAP_IMAGE = 21
  LAYER_IMAGE = 22
  IMAGE_FILE = 23

  ERROR_COLOR = "0"

  def __init__(self):
    DataManager.__init__(self)

  def _indexCol(self, type, color, transparency=0, doubleSide=False):
    if color[0:2] != "0x":
      color = self.ERROR_COLOR
    mat = (type, color, transparency, doubleSide)
    return self._index(mat)

  def getMeshLambertIndex(self, color, transparency=0, doubleSide=False):
    return self._indexCol(self.MESH_LAMBERT, color, transparency, doubleSide)

  def getSmoothMeshLambertIndex(self, color, transparency=0, doubleSide=False):
    return self._indexCol(self.MESH_LAMBERT_SMOOTH, color, transparency, doubleSide)

  def getFlatMeshLambertIndex(self, color, transparency=0, doubleSide=False):
    return self._indexCol(self.MESH_LAMBERT_FLAT, color, transparency, doubleSide)

  def getLineBasicIndex(self, color, transparency=0):
    return self._indexCol(self.LINE_BASIC, color, transparency)

  def getWireframeIndex(self, color, transparency=0):
    return self._indexCol(self.WIREFRAME, color, transparency)

  def getCanvasImageIndex(self, transparency=0, transp_background=False):
    mat = (self.CANVAS_IMAGE, transp_background, transparency, True)
    return self._index(mat)

  def getMapImageIndex(self, width, height, extent, transparency=0, transp_background=False):
    mat = (self.MAP_IMAGE, (width, height, extent, transp_background), transparency, True)
    return self._index(mat)

  def getLayerImageIndex(self, layerid, width, height, extent, transparency=0):
    mat = (self.LAYER_IMAGE, (layerid, width, height, extent), transparency, True)
    return self._index(mat)

  def getImageFileIndex(self, path, transparency=0, doubleSide=False):
    mat = (self.IMAGE_FILE, path, transparency, doubleSide)
    return self._index(mat)

  def getSpriteIndex(self, path, transparency=0):
    mat = (self.SPRITE, path, transparency, False)
    return self._index(mat)

  def write(self, f, imageManager):
    if not len(self._list):
      return

    toMaterialType = {self.WIREFRAME: self.MESH_LAMBERT,
                      self.MESH_LAMBERT_FLAT: self.MESH_LAMBERT,
                      self.CANVAS_IMAGE: self.MESH_PHONG,
                      self.MAP_IMAGE: self.MESH_PHONG,
                      self.LAYER_IMAGE: self.MESH_PHONG,
                      self.IMAGE_FILE: self.MESH_PHONG}

    for index, mat in enumerate(self._list):
      m = {"type": toMaterialType.get(mat[0], mat[0])}

      if mat[0] == self.CANVAS_IMAGE:
        transp_background = mat[1]
        m["i"] = imageManager.canvasImageIndex(transp_background)
      elif mat[0] == self.MAP_IMAGE:
        width, height, extent, transp_background = mat[1]
        m["i"] = imageManager.mapImageIndex(width, height, extent, transp_background)
      elif mat[0] == self.LAYER_IMAGE:
        layerid, width, height, extent = mat[1]
        m["i"] = imageManager.layerImageIndex(layerid, width, height, extent)
      elif mat[0] in [self.IMAGE_FILE, self.SPRITE]:
        filepath = mat[1]
        m["i"] = imageManager.imageIndex(filepath)
      else:
        m["c"] = mat[1]

      if mat[0] == self.WIREFRAME:
        m["w"] = 1

      if mat[0] == self.MESH_LAMBERT_FLAT:
        m["flat"] = 1

      transparency = mat[2]
      if transparency > 0:
        opacity = 1.0 - float(transparency) / 100
        m["o"] = opacity

      # double sides
      if mat[3]:
        m["ds"] = 1

      f.write(u"lyr.m[{0}] = {1};\n".format(index, pyobj2js(m, quoteHex=False)))

class JSONManager(DataManager):

  def __init__(self):
    DataManager.__init__(self)

  def jsonIndex(self, path):
    return self._index(path)

  def write(self, f):
    if len(self._list) == 0:
      return

    f.write(u'\n// JSON data\n')
    for index, path in enumerate(self._list):
      if os.path.exists(path):
        with open(path) as json:
          data = json.read().replace("\\", "\\\\").replace("'", "\\'").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
        f.write(u"project.jsons[%d] = {data:'%s'};\n" % (index, data))
        continue
      f.write(u"project.jsons[%d] = {data:null};\n" % index)
      QgsMessageLog.logMessage(u'JSON file not found: {0}'.format(path), "Qgis2threejs")


class JSWriter:
  def __init__(self, htmlfilename, context):
    self.htmlfilename = htmlfilename
    self.context = context
    self.jsfile = None
    self.jsindex = -1
    self.jsfile_count = 0
    self.layerCount = 0
    self.currentLayerIndex = 0
    self.currentFeatureIndex = -1
    self.attrs = []
    self.imageManager = ImageManager(context)
    self.jsonManager = JSONManager()
    #TODO: integrate OutputContext and JSWriter => ThreeJSExporter
    #TODO: written flag

  def setContext(self, context):
    self.context = context

  def openFile(self, newfile=False):
    if newfile:
      self.prepareNext()
    if self.jsindex == -1:
      jsfilename = os.path.splitext(self.htmlfilename)[0] + ".js"
    else:
      jsfilename = os.path.splitext(self.htmlfilename)[0] + "_%d.js" % self.jsindex
    self.jsfile = codecs.open(jsfilename, "w", "UTF-8")
    self.jsfile_count += 1

  def closeFile(self):
    if self.jsfile:
      self.jsfile.close()
      self.jsfile = None

  def write(self, data):
    if self.jsfile is None:
      self.openFile()
    self.jsfile.write(data)

  def writeProject(self):
    # write project information
    self.write(u"// Qgis2threejs Project\n")
    title = os.path.splitext(os.path.split(self.htmlfilename)[1])[0]
    extent = self.context.baseExtent
    mapTo3d = self.context.mapTo3d
    wgs84Center = self.context.wgs84Center

    opt = {"title": title,
           "crs": unicode(self.context.crs.authid()),
           "proj": self.context.crs.toProj4(),
           "baseExtent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
           "width": mapTo3d.planeWidth,
           "zExaggeration": mapTo3d.verticalExaggeration,
           "zShift": mapTo3d.verticalShift,
           "wgs84Center": {"lat": wgs84Center.y(), "lon": wgs84Center.x()}}

    self.write(u"project = new Q3D.Project({0});\n".format(pyobj2js(opt)))

  def writeLayer(self, obj, fieldNames=None):
    self.currentLayerIndex = self.layerCount
    type2classprefix = {"dem": "DEM", "point": "Point", "line": "Line", "polygon": "Polygon"}
    self.write(u"\n// Layer {0}\n".format(self.currentLayerIndex))
    self.write(u"lyr = project.addLayer(new Q3D.{0}Layer({1}));\n".format(type2classprefix[obj["type"]], pyobj2js(obj)))
    # del obj["type"]

    if fieldNames is not None:
      self.write(u"lyr.a = {0};\n".format(pyobj2js(fieldNames)))
    self.layerCount += 1
    self.currentFeatureIndex = -1
    self.attrs = []
    return self.currentLayerIndex

  def writeFeature(self, f):
    self.currentFeatureIndex += 1
    self.write(u"lyr.f[{0}] = {1};\n".format(self.currentFeatureIndex, pyobj2js(f)))

  def addAttributes(self, attrs):
    self.attrs.append(attrs)

  def writeAttributes(self):
    for index, attrs in enumerate(self.attrs):
      self.write(u"lyr.f[{0}].a = {1};\n".format(index, pyobj2js(attrs, True)))

  def writeMaterials(self, materialManager):
    materialManager.write(self, self.imageManager)

  def writeImages(self):
    self.imageManager.write(self)

  def writeJSONData(self):
    self.jsonManager.write(self)

  def prepareNext(self):
    self.closeFile()
    self.jsindex += 1

  def options(self):
    options = []
    properties = self.context.properties
    world = properties[ObjectTreeItem.ITEM_WORLD] or {}
    if world.get("radioButton_Color", False):
      options.append("option.bgcolor = {0};".format(world.get("lineEdit_Color", 0)))

    return "\n".join(options)

  def scripts(self):
    lines = []

    if self.context.coordsInWGS84:
      # display coordinates in latitude and longitude
      lines.append('<script src="./proj4js/proj4.js"></script>')

    filetitle = os.path.splitext(os.path.split(self.htmlfilename)[1])[0]
    if self.jsindex == -1:
      lines.append('<script src="./%s.js"></script>' % filetitle)
    else:
      lines += map(lambda x: '<script src="./%s_%s.js"></script>' % (filetitle, x), range(self.jsfile_count))
    return "\n".join(lines)

  def log(self, message):
    QgsMessageLog.logMessage(message, "Qgis2threejs")

def exportToThreeJS(htmlfilename, context, progress=None):
  if progress is None:
    progress = dummyProgress

  if htmlfilename == "":
    htmlfilename = tools.temporaryOutputDir() + "/%s.html" % context.timestamp
  out_dir, filename = os.path.split(htmlfilename)
  if not QDir(out_dir).exists():
    QDir().mkpath(out_dir)

  context.htmlfilename = htmlfilename

  # create JavaScript writer object
  writer = JSWriter(htmlfilename, context)

  # read configuration of the template
  templatePath = os.path.join(tools.templateDir(), context.templateName)
  templateConfig = tools.getTemplateConfig(templatePath)
  templateType = templateConfig.get("type", "plain")
  if templateType == "sphere":
    writer.openFile(False)
    # render texture for sphere and write it
    progress(5, "Rendering texture")
    writeSphereTexture(writer)
  else:
    # plain type
    demProperties = context.properties[ObjectTreeItem.ITEM_DEM]
    isSimpleMode = demProperties.get("radioButton_Simple", False)
    writer.openFile(not isSimpleMode)
    writer.writeProject()
    progress(5, "Writing DEM")

    # write primary DEM
    if isSimpleMode:
      writeSimpleDEM(writer, demProperties, progress)
    else:
      writeMultiResDEM(writer, demProperties, progress)
      writer.prepareNext()

    # write additional DEM(s)
    primaryDEMLayerId = demProperties["comboBox_DEMLayer"]
    for layerId, properties in context.properties[ObjectTreeItem.ITEM_OPTDEM].iteritems():
      if layerId != primaryDEMLayerId and properties.get("visible", False):
        writeSimpleDEM(writer, properties)

    progress(30, "Writing vector data")

    # write vector data
    writeVectors(writer, progress)

  # write images and JSON data
  progress(60, "Writing texture images")
  writer.writeImages()
  writer.writeJSONData()

  progress(90, "Copying library files")

  # copy three.js files
  tools.copyThreejsFiles(out_dir, context.controls)

  # copy proj4js files
  if context.coordsInWGS84:
    tools.copyProj4js(out_dir)

  # copy additional library files
  tools.copyLibraries(out_dir, templateConfig)

  # generate html file
  with codecs.open(templatePath, "r", "UTF-8") as f:
    html = f.read()

  filetitle = os.path.splitext(filename)[0]
  with codecs.open(htmlfilename, "w", "UTF-8") as f:
    f.write(html.replace("${title}", filetitle).replace("${controls}", '<script src="./threejs/%s"></script>' % context.controls).replace("${options}", writer.options()).replace("${scripts}", writer.scripts()))

  return htmlfilename

def writeSimpleDEM(writer, properties, progress=None):
  context = writer.context
  mapTo3d = context.mapTo3d
  extent = context.baseExtent
  htmlfilename = writer.htmlfilename
  if progress is None:
    progress = dummyProgress

  prop = DEMPropertyReader(properties)
  dem_width = prop.width()
  dem_height = prop.height()

  # warp dem
  # calculate extent. output dem should be handled as points.
  xres = extent.width() / (dem_width - 1)
  yres = extent.height() / (dem_height - 1)
  geotransform = [extent.xMinimum() - xres / 2, xres, 0, extent.yMaximum() + yres / 2, 0, -yres]
  wkt = str(context.crs.toWkt())

  demLayerId = properties["comboBox_DEMLayer"]
  if demLayerId:
    mapLayer = QgsMapLayerRegistry.instance().mapLayer(demLayerId)
    layerName = mapLayer.name()
    warp_dem = tools.MemoryWarpRaster(mapLayer.source())
  else:
    mapLayer = None
    layerName = "Flat plane"
    warp_dem = tools.FlatRaster()

  # warp dem
  dem_values = warp_dem.read(dem_width, dem_height, wkt, geotransform)

  # calculate statistics
  stats = {"max": max(dem_values), "min": min(dem_values)}

  # shift and scale
  if mapTo3d.verticalShift != 0:
    dem_values = map(lambda x: x + mapTo3d.verticalShift, dem_values)
  if mapTo3d.multiplierZ != 1:
    dem_values = map(lambda x: x * mapTo3d.multiplierZ, dem_values)
  if debug_mode:
    qDebug("Warped DEM: %d x %d, extent %s" % (dem_width, dem_height, str(geotransform)))

  surroundings = properties.get("checkBox_Surroundings", False)
  if surroundings:
    roughenEdges(dem_width, dem_height, dem_values, properties["spinBox_Roughening"])

  # layer
  layer = DEMLayer(context, mapLayer, prop)

  # dem block
  #TODO: rename this to block
  dem = {"width": dem_width, "height": dem_height}
  dem["plane"] = {"width": mapTo3d.planeWidth, "height": mapTo3d.planeHeight, "offsetX": 0, "offsetY": 0}

  # material option
  transparency = prop.properties["spinBox_demtransp"]

  # display type
  if properties.get("radioButton_MapCanvas", False):
    transp_background = properties.get("checkBox_TransparentBackground", False)
    dem["m"] = layer.materialManager.getCanvasImageIndex(transparency, transp_background)

  elif properties.get("radioButton_LayerImage", False):
    layerid = properties.get("comboBox_ImageLayer")
    size = context.mapSettings.outputSize()
    dem["m"] = layer.materialManager.getLayerImageIndex(layerid, size.width(), size.height(), extent, transparency)

  elif properties.get("radioButton_ImageFile", False):
    filepath = properties.get("lineEdit_ImageFile", "")
    dem["m"] = layer.materialManager.getImageFileIndex(filepath, transparency, True)

  elif properties.get("radioButton_SolidColor", False):
    dem["m"] = layer.materialManager.getMeshLambertIndex(properties["lineEdit_Color"], transparency, True)

  elif properties.get("radioButton_Wireframe", False):
    dem["m"] = layer.materialManager.getWireframeIndex(properties["lineEdit_Color"], transparency)

  # shading (whether compute normals)
  if properties.get("checkBox_Shading", True):
    dem["shading"] = True

  if not surroundings and properties.get("checkBox_Sides", False):
    dem["s"] = True

  if not surroundings and properties.get("checkBox_Frame", False):
    dem["frame"] = True

  # layer
  lyr = {"type": "dem", "name": layerName, "stats": stats}
  lyr["q"] = 1    #queryable

  # write layer
  lyrIdx = writer.writeLayer(lyr)

  # write central block
  writer.write("bl = lyr.addBlock({0});\n".format(pyobj2js(dem)))
  writer.write("bl.data = [{0}];\n".format(",".join(map(gdal2threejs.formatValue, dem_values))))

  # write surrounding dems
  if surroundings:
    writeSurroundingDEM(writer, layer, stats, properties, progress)
    # overwrite stats
    writer.write("lyr.stats = {0};\n".format(pyobj2js(stats)))

  writer.writeMaterials(layer.materialManager)


def roughenEdges(width, height, values, interval):
  if interval == 1:
    return

  for y in [0, height - 1]:
    for x1 in range(interval, width, interval):
      x0 = x1 - interval
      z0 = values[x0 + width * y]
      z1 = values[x1 + width * y]
      for xx in range(1, interval):
        z = (z0 * (interval - xx) + z1 * xx) / interval
        values[x0 + xx + width * y] = z

  for x in [0, width - 1]:
    for y1 in range(interval, height, interval):
      y0 = y1 - interval
      z0 = values[x + width * y0]
      z1 = values[x + width * y1]
      for yy in range(1, interval):
        z = (z0 * (interval - yy) + z1 * yy) / interval
        values[x + width * (y0 + yy)] = z

def writeSurroundingDEM(writer, layer, stats, properties, progress=None):
  context = writer.context
  mapTo3d = context.mapTo3d
  baseExtent = context.baseExtent
  if progress is None:
    progress = dummyProgress
  demlayer = QgsMapLayerRegistry.instance().mapLayer(properties["comboBox_DEMLayer"])
  htmlfilename = writer.htmlfilename

  # options
  size = properties["spinBox_Size"]
  roughening = properties["spinBox_Roughening"]
  transparency = properties["spinBox_demtransp"]

  prop = DEMPropertyReader(properties)
  dem_width = (prop.width() - 1) / roughening + 1
  dem_height = (prop.height() - 1) / roughening + 1

  warp_dem = tools.MemoryWarpRaster(demlayer.source())
  wkt = str(context.crs.toWkt())

  # texture image size
  hpw = baseExtent.height() / baseExtent.width()
  if hpw < 1:
    image_width = context.image_basesize
    image_height = round(image_width * hpw)
    #image_height = context.image_basesize * max(1, int(round(1 / hpw)))    # not rendered expectedly
  else:
    image_height = context.image_basesize
    image_width = round(image_height / hpw)

  scripts = []
  plane_index = 1
  size2 = size * size
  for i in range(size2):
    progress(20 * i / size2 + 10)
    if i == (size2 - 1) / 2:    # center (map canvas)
      continue
    sx = i % size - (size - 1) / 2
    sy = i / size - (size - 1) / 2

    # calculate extent
    extent = QgsRectangle(baseExtent.xMinimum() + sx * baseExtent.width(), baseExtent.yMinimum() + sy * baseExtent.height(),
                          baseExtent.xMaximum() + sx * baseExtent.width(), baseExtent.yMaximum() + sy * baseExtent.height())

    # calculate extent. output dem should be handled as points.
    xres = extent.width() / (dem_width - 1)
    yres = extent.height() / (dem_height - 1)
    geotransform = [extent.xMinimum() - xres / 2, xres, 0, extent.yMaximum() + yres / 2, 0, -yres]

    # warp dem
    dem_values = warp_dem.read(dem_width, dem_height, wkt, geotransform)
    if stats is None:
      stats = {"max": max(dem_values), "min": min(dem_values)}
    else:
      stats["max"] = max(max(dem_values), stats["max"])
      stats["min"] = min(min(dem_values), stats["min"])

    # shift and scale
    if mapTo3d.verticalShift != 0:
      dem_values = map(lambda x: x + mapTo3d.verticalShift, dem_values)
    if mapTo3d.multiplierZ != 1:
      dem_values = map(lambda x: x * mapTo3d.multiplierZ, dem_values)
    if debug_mode:
      qDebug("Warped DEM: %d x %d, extent %s" % (dem_width, dem_height, str(geotransform)))

    # generate javascript data file
    planeWidth = mapTo3d.planeWidth * extent.width() / baseExtent.width()
    planeHeight = mapTo3d.planeHeight * extent.height() / baseExtent.height()
    offsetX = mapTo3d.planeWidth * (extent.xMinimum() - baseExtent.xMinimum()) / baseExtent.width() + planeWidth / 2 - mapTo3d.planeWidth / 2
    offsetY = mapTo3d.planeHeight * (extent.yMinimum() - baseExtent.yMinimum()) / baseExtent.height() + planeHeight / 2 - mapTo3d.planeHeight / 2

    # dem block
    #TODO: rename this to block
    dem = {"width": dem_width, "height": dem_height}
    dem["plane"] = {"width": planeWidth, "height": planeHeight, "offsetX": offsetX, "offsetY": offsetY}

    # display type
    if properties.get("radioButton_MapCanvas", False):
      transp_background = properties.get("checkBox_TransparentBackground", False)
      dem["m"] = layer.materialManager.getMapImageIndex(image_width, image_height, extent, transparency, transp_background)

    elif properties.get("radioButton_LayerImage", False):
      layerid = properties.get("comboBox_ImageLayer")
      dem["m"] = layer.materialManager.getLayerImageIndex(layerid, image_width, image_height, extent, transparency)

    elif properties.get("radioButton_SolidColor", False):
      dem["m"] = layer.materialManager.getMeshLambertIndex(properties["lineEdit_Color"], transparency, True)

    elif properties.get("radioButton_Wireframe", False):
      dem["m"] = layer.materialManager.getWireframeIndex(properties["lineEdit_Color"], transparency)

    # shading (whether compute normals)
    if properties.get("checkBox_Shading", True):
      dem["shading"] = True

    # write block
    writer.write("bl = lyr.addBlock({0});\n".format(pyobj2js(dem)))
    writer.write("bl.data = [{0}];\n".format(",".join(map(gdal2threejs.formatValue, dem_values))))
    plane_index += 1

def writeMultiResDEM(writer, properties, progress=None):
  context = writer.context
  mapTo3d = context.mapTo3d
  baseExtent = context.baseExtent
  if progress is None:
    progress = dummyProgress
  prop = DEMPropertyReader(properties)
  demlayer = QgsMapLayerRegistry.instance().mapLayer(properties["comboBox_DEMLayer"])
  if demlayer is None:
    return
  htmlfilename = writer.htmlfilename

  out_dir, filename = os.path.split(htmlfilename)
  filetitle = os.path.splitext(filename)[0]

  # material options
  transparency = properties["spinBox_demtransp"]
  transp_background = properties.get("checkBox_TransparentBackground", False)
  imageLayerId = properties.get("comboBox_ImageLayer")

  # layer
  layer = DEMLayer(context, demlayer, prop)
  lyr = {"type": "dem", "name": demlayer.name()}
  lyr["q"] = 1    #queryable
  lyrIdx = writer.writeLayer(lyr)

  # create quad tree
  quadtree = createQuadTree(baseExtent, properties)
  if quadtree is None:
    QMessageBox.warning(None, "Qgis2threejs", "Focus point/area is not selected.")
    return
  quads = quadtree.quads()

  # create quads and a point on map canvas with rubber bands
  context.dialog.createRubberBands(quads, quadtree.focusRect.center())

  # image size
  hpw = baseExtent.height() / baseExtent.width()
  if hpw < 1:
    image_width = context.image_basesize
    image_height = round(image_width * hpw)
  else:
    image_height = context.image_basesize
    image_width = round(image_height / hpw)

  # (currently) dem size should be 2 ^ quadtree.height * a + 1, where a is larger integer than 0
  # with smooth resolution change, this is not necessary
  dem_width = dem_height = max(64, 2 ** quadtree.height) + 1

  warp_dem = tools.MemoryWarpRaster(demlayer.source())
  wkt = str(context.crs.toWkt())

  unites_center = True
  centerQuads = DEMQuadList(dem_width, dem_height)
  scripts = []
  stats = None
  plane_index = 0
  for i, quad in enumerate(quads):
    progress(30 * i / len(quads) + 5)
    extent = quad.extent

    # calculate extent. output dem should be handled as points.
    xres = extent.width() / (dem_width - 1)
    yres = extent.height() / (dem_height - 1)
    geotransform = [extent.xMinimum() - xres / 2, xres, 0, extent.yMaximum() + yres / 2, 0, -yres]

    # warp dem
    dem_values = warp_dem.read(dem_width, dem_height, wkt, geotransform)
    if stats is None:
      stats = {"max": max(dem_values), "min": min(dem_values)}
    else:
      stats["max"] = max(max(dem_values), stats["max"])
      stats["min"] = min(min(dem_values), stats["min"])

    # shift and scale
    if mapTo3d.verticalShift != 0:
      dem_values = map(lambda x: x + mapTo3d.verticalShift, dem_values)
    if mapTo3d.multiplierZ != 1:
      dem_values = map(lambda x: x * mapTo3d.multiplierZ, dem_values)
    if debug_mode:
      qDebug("Warped DEM: %d x %d, extent %s" % (dem_width, dem_height, str(geotransform)))

    # generate javascript data file
    planeWidth = mapTo3d.planeWidth * extent.width() / baseExtent.width()
    planeHeight = mapTo3d.planeHeight * extent.height() / baseExtent.height()
    offsetX = mapTo3d.planeWidth * (extent.xMinimum() - baseExtent.xMinimum()) / baseExtent.width() + planeWidth / 2 - mapTo3d.planeWidth / 2
    offsetY = mapTo3d.planeHeight * (extent.yMinimum() - baseExtent.yMinimum()) / baseExtent.height() + planeHeight / 2 - mapTo3d.planeHeight / 2

    # value resampling on edges for combination with different resolution DEM
    neighbors = quadtree.neighbors(quad)
    #qDebug("Output quad (%d %s): height=%d" % (i, str(quad), quad.height))
    for direction, neighbor in enumerate(neighbors):
      if neighbor is None:
        continue
      #qDebug(" neighbor %d %s: height=%d" % (direction, str(neighbor), neighbor.height))
      interval = 2 ** (quad.height - neighbor.height)
      if interval > 1:
        if direction == QuadTree.UP or direction == QuadTree.DOWN:
          y = 0 if direction == QuadTree.UP else dem_height - 1
          for x1 in range(interval, dem_width, interval):
            x0 = x1 - interval
            z0 = dem_values[x0 + dem_width * y]
            z1 = dem_values[x1 + dem_width * y]
            for xx in range(1, interval):
              z = (z0 * (interval - xx) + z1 * xx) / interval
              dem_values[x0 + xx + dem_width * y] = z
        else:   # LEFT or RIGHT
          x = 0 if direction == QuadTree.LEFT else dem_width - 1
          for y1 in range(interval, dem_height, interval):
            y0 = y1 - interval
            z0 = dem_values[x + dem_width * y0]
            z1 = dem_values[x + dem_width * y1]
            for yy in range(1, interval):
              z = (z0 * (interval - yy) + z1 * yy) / interval
              dem_values[x + dem_width * (y0 + yy)] = z

    if quad.height < quadtree.height or unites_center == False:
      dem = {"width": dem_width, "height": dem_height}
      dem["plane"] = {"width": planeWidth, "height": planeHeight, "offsetX": offsetX, "offsetY": offsetY}

      # display type
      if properties.get("radioButton_MapCanvas", False):
        dem["m"] = layer.materialManager.getMapImageIndex(image_width, image_height, extent, transparency, transp_background)

      elif properties.get("radioButton_LayerImage", False):
        dem["m"] = layer.materialManager.getLayerImageIndex(imageLayerId, image_width, image_height, extent, transparency)

      elif properties.get("radioButton_SolidColor", False):
        dem["m"] = layer.materialManager.getMeshLambertIndex(properties["lineEdit_Color"], transparency, True)

      elif properties.get("radioButton_Wireframe", False):
        dem["m"] = layer.materialManager.getWireframeIndex(properties["lineEdit_Color"], transparency)

      # shading (whether compute normals)
      if properties.get("checkBox_Shading", True):
        dem["shading"] = True

      # write block
      writer.openFile(True)
      writer.write("bl = lyr.addBlock({0});\n".format(pyobj2js(dem)))
      writer.write("bl.data = [{0}];\n".format(",".join(map(gdal2threejs.formatValue, dem_values))))
      plane_index += 1
    else:
      centerQuads.addQuad(quad, dem_values)

  if unites_center:
    extent = centerQuads.extent()
    dem_width = (dem_width - 1) * centerQuads.width() + 1
    dem_height = (dem_height - 1) * centerQuads.height() + 1
    dem_values = centerQuads.unitedDEM()
    planeWidth = mapTo3d.planeWidth * extent.width() / baseExtent.width()
    planeHeight = mapTo3d.planeHeight * extent.height() / baseExtent.height()
    offsetX = mapTo3d.planeWidth * (extent.xMinimum() - baseExtent.xMinimum()) / baseExtent.width() + planeWidth / 2 - mapTo3d.planeWidth / 2
    offsetY = mapTo3d.planeHeight * (extent.yMinimum() - baseExtent.yMinimum()) / baseExtent.height() + planeHeight / 2 - mapTo3d.planeHeight / 2
    dem = {"width": dem_width, "height": dem_height}
    dem["plane"] = {"width": planeWidth, "height": planeHeight, "offsetX": offsetX, "offsetY": offsetY}

    if hpw < 1:
      image_width = context.image_basesize * centerQuads.width()
      image_height = round(image_width * hpw)
    else:
      image_height = context.image_basesize * centerQuads.height()
      image_width = round(image_height / hpw)

    # display type
    if properties.get("radioButton_MapCanvas", False):
      dem["m"] = layer.materialManager.getMapImageIndex(image_width, image_height, extent, transparency, transp_background)

    elif properties.get("radioButton_LayerImage", False):
      dem["m"] = layer.materialManager.getLayerImageIndex(imageLayerId, image_width, image_height, extent, transparency)

    elif properties.get("radioButton_SolidColor", False):
      dem["m"] = layer.materialManager.getMeshLambertIndex(properties["lineEdit_Color"], transparency, True)

    elif properties.get("radioButton_Wireframe", False):
      dem["m"] = layer.materialManager.getWireframeIndex(properties["lineEdit_Color"], transparency)

    # write block
    writer.openFile(True)
    writer.write("bl = lyr.addBlock({0});\n".format(pyobj2js(dem)))
    writer.write("bl.data = [{0}];\n".format(",".join(map(gdal2threejs.formatValue, dem_values))))
    plane_index += 1

  writer.write("lyr.stats = {0};\n".format(pyobj2js(stats)))
  writer.writeMaterials(layer.materialManager)

class TriangleMesh:

  # 0 - 3
  # | / |
  # 1 - 2

  def __init__(self, xmin, ymin, xmax, ymax, x_segments, y_segments):
    self.flen = 0
    self.quadrangles = []
    self.spatial_index = QgsSpatialIndex()

    xres = (xmax - xmin) / x_segments
    yres = (ymax - ymin) / y_segments
    for y in range(y_segments):
      for x in range(x_segments):
        pt0 = QgsPoint(xmin + x * xres, ymax - y * yres)
        pt1 = QgsPoint(xmin + x * xres, ymax - (y + 1) * yres)
        pt2 = QgsPoint(xmin + (x + 1) * xres, ymax - (y + 1) * yres)
        pt3 = QgsPoint(xmin + (x + 1) * xres, ymax - y * yres)
        self._addQuadrangle(pt0, pt1, pt2, pt3)

  def _addQuadrangle(self, pt0, pt1, pt2, pt3):
    f = QgsFeature(self.flen)
    f.setGeometry(QgsGeometry.fromPolygon([[pt0, pt1, pt2, pt3, pt0]]))
    self.quadrangles.append(f)
    self.spatial_index.insertFeature(f)
    self.flen += 1

  def intersects(self, geom):
    for fid in self.spatial_index.intersects(geom.boundingBox()):
      quad = self.quadrangles[fid].geometry()
      if quad.intersects(geom):
        yield quad

  def splitPolygon(self, geom):
    polygons = []
    for quad in self.intersects(geom):
      pts = quad.asPolygon()[0]
      tris = [[[pts[0], pts[1], pts[3], pts[0]]], [[pts[3], pts[1], pts[2], pts[3]]]]
      if geom.contains(quad):
        polygons += tris
      else:
        for i, tri in enumerate(map(QgsGeometry.fromPolygon, tris)):
          if geom.contains(tri):
            polygons.append(tris[i])
          elif geom.intersects(tri):
            poly = geom.intersection(tri)
            if poly.isMultipart():
              polygons += poly.asMultiPolygon()
            else:
              polygons.append(poly.asPolygon())
    return polygons

  @classmethod
  def createFromContext(cls, context):
    prop = DEMPropertyReader(context.properties[ObjectTreeItem.ITEM_DEM])
    dem_width = prop.width()
    dem_height = prop.height()
    extent = context.baseExtent
    triMesh = TriangleMesh(extent.xMinimum(), extent.yMinimum(),
                           extent.xMaximum(), extent.yMaximum(),
                           dem_width - 1, dem_height - 1)
    return triMesh


# Geometry classes

class PointGeometry:
  def __init__(self):
    self.pts = []

  def asList(self):
    return map(lambda pt: [pt.x, pt.y, pt.z], self.pts)

  @staticmethod
  def fromQgsGeometry(geometry, z_func, transform_func):
    geom = PointGeometry()
    pts = geometry.asMultiPoint() if geometry.isMultipart() else [geometry.asPoint()]
    geom.pts = [transform_func(pt.x(), pt.y(), z_func(pt.x(), pt.y())) for pt in pts]
    return geom

  @staticmethod
  def fromWkb25D(wkb, transform_func):
    geom = ogr.CreateGeometryFromWkb(wkb)
    geomType = geom.GetGeometryType()

    if geomType == ogr.wkbPoint25D:
      geoms = [geom]
    elif geomType == ogr.wkbMultiPoint25D:
      geoms = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
    else:
      geoms = []

    pts = []
    for geom25d in geoms:
      if hasattr(geom25d, "GetPoints"):
        pts += geom25d.GetPoints()
      else:
        pts += [geom25d.GetPoint(i) for i in range(geom25d.GetPointCount())]

    point_geom = PointGeometry()
    point_geom.pts = [transform_func(pt[0], pt[1], pt[2]) for pt in pts]
    return point_geom


class LineGeometry:
  def __init__(self):
    self.lines = []

  def asList(self):
    return [map(lambda pt: [pt.x, pt.y, pt.z], line) for line in self.lines]

  @staticmethod
  def fromQgsGeometry(geometry, z_func, transform_func):
    geom = LineGeometry()
    lines = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
    geom.lines = [[transform_func(pt.x(), pt.y(), z_func(pt.x(), pt.y())) for pt in line] for line in lines]
    return geom

  @staticmethod
  def fromWkb25D(wkb, transform_func):
    geom = ogr.CreateGeometryFromWkb(wkb)
    geomType = geom.GetGeometryType()

    if geomType == ogr.wkbLineString25D:
      geoms = [geom]
    elif geomType == ogr.wkbMultiLineString25D:
      geoms = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
    else:
      geoms = []

    line_geom = LineGeometry()
    for geom25d in geoms:
      if hasattr(geom25d, "GetPoints"):
        pts = geom25d.GetPoints()
      else:
        pts = [geom25d.GetPoint(i) for i in range(geom25d.GetPointCount())]

      points = [transform_func(pt[0], pt[1], pt[2]) for pt in pts]
      line_geom.lines.append(points)

    return line_geom


class PolygonGeometry:
  def __init__(self):
    self.polygons = []
    self.centroids = []
    self.split_polygons = []

  def asList(self):
    p = []
    for boundaries in self.polygons:
      # outer boundary
      pts = map(lambda pt: [pt.x, pt.y, pt.z], boundaries[0])
      if not GeometryUtils.isClockwise(boundary):
        pts.reverse()   # to clockwise
      b = [pts]

      # inner boundaries
      for boundary in boundaries[1:]:
        pts = map(lambda pt: [pt.x, pt.y, pt.z], boundary)
        if GeometryUtils.isClockwise(boundary):
          pts.reverse()   # to counter-clockwise
        b.append(pts)
      p.append(b)
    return p

  @staticmethod
  def fromQgsGeometry(geometry, z_func, transform_func, calcCentroid=False, triMesh=None):

    useCentroidHeight = True
    centroidPerPolygon = True

    polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
    geom = PolygonGeometry()
    if calcCentroid and not centroidPerPolygon:
      pt = geometry.centroid().asPoint()
      centroidHeight = z_func(pt.x(), pt.y())
      geom.centroids.append(transform_func(pt.x(), pt.y(), centroidHeight))

    for polygon in polygons:
      if useCentroidHeight or calcCentroid:
        pt = QgsGeometry.fromPolygon(polygon).centroid().asPoint()
        centroidHeight = z_func(pt.x(), pt.y())
        if calcCentroid and centroidPerPolygon:
          geom.centroids.append(transform_func(pt.x(), pt.y(), centroidHeight))

      if useCentroidHeight:
        z_func = lambda x, y: centroidHeight

      boundaries = []
      # outer boundary
      points = []
      for pt in polygon[0]:
        points.append(transform_func(pt.x(), pt.y(), z_func(pt.x(), pt.y())))

      if not GeometryUtils.isClockwise(points):
        points.reverse()    # to clockwise
      boundaries.append(points)

      # inner boundaries
      for boundary in polygon[1:]:
        points = [transform_func(pt.x(), pt.y(), z_func(pt.x(), pt.y())) for pt in boundary]
        if GeometryUtils.isClockwise(points):
          points.reverse()    # to counter-clockwise
        boundaries.append(points)

      geom.polygons.append(boundaries)

    if triMesh is None:
      return geom

    # split polygon for overlay
    for polygon in triMesh.splitPolygon(geometry):
      boundaries = []
      # outer boundary
      points = [transform_func(pt.x(), pt.y(), 0) for pt in polygon[0]]
      if not GeometryUtils.isClockwise(points):
        points.reverse()    # to clockwise
      boundaries.append(points)

      # inner boundaries
      for boundary in polygon[1:]:
        points = [transform_func(pt.x(), pt.y(), 0) for pt in boundary]
        if GeometryUtils.isClockwise(points):
          points.reverse()    # to counter-clockwise
        boundaries.append(points)

      geom.split_polygons.append(boundaries)

    return geom

#  @staticmethod
#  def fromWkb25D(wkb):
#    pass


class Feature:

  def __init__(self, layer):
    self.layer = layer

    self.context = layer.context
    self.prop = layer.prop
    self.wkt = layer.wkt
    self.transform = layer.transform
    self.geomType = layer.geomType
    self.geomClass = layer.geomClass
    self.hasLabel = layer.hasLabel

    self.feat = None
    self.geom = None

  def setQgsFeature(self, feat, clipGeom=None):
    self.feat = feat
    self.geom = None

    geom = feat.geometry()
    if geom is None:
      qDebug("null geometry skipped")
      return

    # coordinate transformation - layer crs to project crs
    geom.transform(self.transform)

    # clip geometry
    if clipGeom and self.geomType in [QGis.Line, QGis.Polygon]:
      geom = geom.intersection(clipGeom)

    # check if geometry is empty
    if geom.isGeosEmpty():
      qDebug("empty geometry skipped")
      return

    # z_func: function to get z coordinate at given point (x, y)
    if self.prop.isHeightRelativeToDEM():
      # calculate elevation with dem
      z_func = lambda x, y: self.context.warp_dem.readValue(self.wkt, x, y)
    else:
      z_func = lambda x, y: 0

    # transform_func: function to transform the map coordinates to 3d coordinates
    relativeHeight = self.prop.relativeHeight(feat)
    def transform_func(x, y, z):
      return self.context.mapTo3d.transform(x, y, z + relativeHeight)

    if self.geomType == QGis.Polygon:
      triMesh = None
      if self.prop.type_index == 1 and self.prop.isHeightRelativeToDEM():   # Overlay
        z_func = lambda x, y: 0
        triMesh = self.context.triangleMesh()
      self.geom = self.geomClass.fromQgsGeometry(geom, z_func, transform_func, self.hasLabel, triMesh)
    elif self.prop.useZ():
      self.geom = self.geomClass.fromWkb25D(geom.asWkb(), transform_func)
    else:
      self.geom = self.geomClass.fromQgsGeometry(geom, z_func, transform_func)

  def relativeHeight(self):
    return self.prop.relativeHeight(self.feat)

  def color(self):
    return self.prop.color(self.feat)

  def transparency(self):
    return self.prop.transparency(self.feat)

  def propValues(self):
    return self.prop.values(self.feat)


class Layer:

  def __init__(self, context, layer, prop):
    self.context = context
    self.layer = layer
    self.prop = prop

    self.materialManager = MaterialManager()


class DEMLayer(Layer):
  pass


class VectorLayer(Layer):

  geomType2Class = {QGis.Point: PointGeometry, QGis.Line: LineGeometry, QGis.Polygon: PolygonGeometry}

  def __init__(self, context, layer, prop):
    Layer.__init__(self, context, layer, prop)

    self.wkt = str(context.crs.toWkt())
    self.transform = QgsCoordinateTransform(layer.crs(), context.crs)
    self.geomType = layer.geometryType()
    self.geomClass = self.geomType2Class.get(self.geomType)
    self.hasLabel = prop.properties.get("checkBox_ExportAttrs", False) and prop.properties.get("comboBox_Label") is not None


def writeVectors(writer, progress=None):
  context = writer.context
  baseExtent = context.baseExtent
  mapTo3d = context.mapTo3d
  renderer = QgsMapRenderer()
  if progress is None:
    progress = dummyProgress

  layerProperties = {}
  for itemType in [ObjectTreeItem.ITEM_POINT, ObjectTreeItem.ITEM_LINE, ObjectTreeItem.ITEM_POLYGON]:
    for layerId, properties in context.properties[itemType].iteritems():
      if properties.get("visible", False):
        layerProperties[layerId] = properties

  finishedLayers = 0
  for layerId, properties in layerProperties.iteritems():
    mapLayer = QgsMapLayerRegistry.instance().mapLayer(layerId)
    if mapLayer is None:
      continue

    prop = VectorPropertyReader(context.objectTypeManager, mapLayer, properties)
    obj_mod = context.objectTypeManager.module(prop.mod_index)
    if obj_mod is None:
      qDebug("Module not found")
      continue

    # prepare triangle mesh
    geom_type = mapLayer.geometryType()
    if geom_type == QGis.Polygon and prop.type_index == 1 and prop.isHeightRelativeToDEM():   # Overlay
      progress(None, "Initializing triangle mesh for overlay polygons")
      context.triangleMesh()

    progress(30 + 30 * finishedLayers / len(layerProperties), u"Writing vector layer ({0} of {1}): {2}".format(finishedLayers + 1, len(layerProperties), mapLayer.name()))

    # layer object
    layer = VectorLayer(context, mapLayer, prop)

    # TODO: do these in VectorLayer.__init__()
    lyr = {"name": mapLayer.name()}
    lyr["type"] = {QGis.Point: "point", QGis.Line: "line", QGis.Polygon: "polygon"}.get(geom_type, "")
    lyr["q"] = 1    #queryable
    lyr["objType"] = prop.type_name

    if geom_type == QGis.Polygon and prop.type_index == 1:   # Overlay
      lyr["am"] = "relative" if prop.isHeightRelativeToDEM() else "absolute"    # altitude mode

    # make list of field names
    writeAttrs = properties.get("checkBox_ExportAttrs", False)
    fieldNames = None
    if writeAttrs:
      fieldNames = [field.name() for field in mapLayer.pendingFields()]

    hasLabel = False
    if writeAttrs:
      attIdx = properties.get("comboBox_Label", None)
      if attIdx is not None:
        widgetValues = properties.get("labelHeightWidget", {})
        lyr["l"] = {"i": attIdx, "ht": int(widgetValues.get("comboData", 0)), "v": float(widgetValues.get("editText", 0)) * mapTo3d.multiplierZ}
        hasLabel = True

    # write layer object
    writer.writeLayer(lyr, fieldNames)    #TODO: writer.writeLayer(layer) or writer.write(layer.obj)

    feat = Feature(layer)

    # initialize symbol rendering
    mapLayer.rendererV2().startRender(renderer.rendererContext(), mapLayer.pendingFields() if apiChanged23 else mapLayer)

    request = QgsFeatureRequest()
    # features to export
    clipGeom = None
    if properties.get("radioButton_IntersectingFeatures", False):
      request.setFilterRect(layer.transform.transformBoundingBox(baseExtent, QgsCoordinateTransform.ReverseTransform))
      if properties.get("checkBox_Clip"):
        rect = QgsRectangle(baseExtent)
        rect.scale(0.999999)    # clip with slightly smaller extent than map canvas extent
        clipGeom = QgsGeometry.fromRect(rect)
        #clipGeom = QgsGeometry.fromRect(canvas.extent())

    for f in mapLayer.getFeatures(request):
      feat.setQgsFeature(f, clipGeom)
      if feat.geom is None:
        continue

      # write geometry
      obj_mod.write(writer, layer, feat)   #TODO: writer.writeFeature(layer, feat, obj_mod)
                                           #      obj_mod.feature(writer, layer, feat)
      # stack attributes in writer
      if writeAttrs:
        writer.addAttributes(f.attributes())

    # write attributes
    if writeAttrs:
      writer.writeAttributes()

    # write materials
    writer.writeMaterials(layer.materialManager)

    mapLayer.rendererV2().stopRender(renderer.rendererContext())
    finishedLayers += 1


def writeSphereTexture(writer):
  #context = writer.context
  canvas = writer.context.canvas
  antialias = True

  image_height = 1024
  image_width = 2 * image_height
  image = QImage(image_width, image_height, QImage.Format_ARGB32_Premultiplied)

  # fill image with canvas color
  image.fill(canvas.canvasColor().rgba())

  # set up a renderer
  renderer = QgsMapRenderer()
  renderer.setOutputSize(image.size(), image.logicalDpiX())

  crs = QgsCoordinateReferenceSystem(4326)
  renderer.setDestinationCrs(crs)
  renderer.setProjectionsEnabled(True)

  layerids = [layer.id() for layer in canvas.layers()]
  renderer.setLayerSet(layerids)

  extent = QgsRectangle(-180, -90, 180, 90)
  renderer.setExtent(extent)

  # render map image
  painter = QPainter()
  painter.begin(image)
  if antialias:
    painter.setRenderHint(QPainter.Antialiasing)
  renderer.render(painter)
  painter.end()

  #if context.localBrowsingMode:
  texData = tools.base64image(image)
  writer.write('var tex = "{0}";\n'.format(texData))


class GeometryUtils:

  @classmethod
  def _signedArea(cls, p):
    """Calculates signed area of polygon."""
    area = 0
    for i in range(len(p) - 1):
      area += (p[i].x - p[i + 1].x) * (p[i].y + p[i + 1].y)
    return area / 2

  @classmethod
  def isClockwise(cls, linearRing):
    """Returns whether given linear ring is clockwise."""
    return cls._signedArea(linearRing) < 0


def pyobj2js(obj, escape=False, quoteHex=True):
  if isinstance(obj, dict):
    items = [u"{0}:{1}".format(k, pyobj2js(v, escape, quoteHex)) for k, v in obj.iteritems()]
    return "{" + ",".join(items) + "}"
  elif isinstance(obj, list):
    items = [unicode(pyobj2js(v, escape, quoteHex)) for v in obj]
    return "[" + ",".join(items) + "]"
  elif isinstance(obj, bool):
    return "true" if obj else "false"
  elif isinstance(obj, (str, unicode)):
    if escape:
      return '"' + obj.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if not quoteHex and re.match("0x[0-9A-Fa-f]+$", obj):
      return obj
    return '"' + obj + '"'
  elif isinstance(obj, (int, float)):
    return obj
  elif obj == NULL:   # qgis.core.NULL
    return "null"
  return '"' + str(obj) + '"'

# createQuadTree(extent, demProperties)
def createQuadTree(extent, p):
  try:
    c = map(float, [p["lineEdit_xmin"], p["lineEdit_ymin"], p["lineEdit_xmax"], p["lineEdit_ymax"]])
  except:
    return None
  quadtree = QuadTree(extent)
  quadtree.buildTreeByRect(QgsRectangle(c[0], c[1], c[2], c[3]), p["spinBox_Height"])
  return quadtree

def dummyProgress(progress=None, statusMsg=None):
  pass
