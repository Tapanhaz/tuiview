
"""
Module that contains the ViewerLUT class
amongst other things
"""

import sys
import numpy
import json
from PyQt4.QtGui import QImage
from PyQt4.QtCore import QObject, SIGNAL
from osgeo import gdal
from . import viewererrors
from . import viewerstretch

gdal.UseExceptions()

# are we big endian or not?
BIG_ENDIAN = sys.byteorder == 'big'

# scale, offset, size, min, max
INTEGER_SCALEOFFSETS = {gdal.GDT_Byte : (1, 0, 256, 0, 255),
                        gdal.GDT_UInt16 : (1, 0, 65536, 0, 65535),
                        gdal.GDT_Int16 : (1, -32768, 65536, -32768, 32767) }
DEFAULT_LUTSIZE = 1024 # if not one of the types above

# Qt expects the colours in BGRA order packed into
# a 32bit int. We do this by inserting stuff into
# a 8 bit numpy array, but this is endian specific
if BIG_ENDIAN:
    CODE_TO_LUTINDEX = {'b' : 3, 'g' : 2, 'r' : 1, 'a' : 0}
else:
    CODE_TO_LUTINDEX = {'b' : 0, 'g' : 1, 'r' : 2, 'a' : 3}

# to save creating this tuple all the time
RGB_CODES = ('r', 'g', 'b')

def GDALProgressFunc(value, string, lutobject):
    """
    Callback function called by GDAL when calculating
    stats or histogram.
    """
    percent = int(value * 100)
    lutobject.emit(SIGNAL("newPercent(int)"), percent)

class BandLUTInfo(object):
    """
    Class that holds information about a band's LUT
    """
    def __init__(self, scale, offset, lutsize, min, max):
        self.scale = scale
        self.offset = offset
        self.lutsize = lutsize
        self.min = min
        self.max = max

    def toString(self):
        rep = {'scale' : self.scale, 'offset' : self.offset, 
                    'lutsize' : self.lutsize, 'min' : self.min, 
                    'max' : self.max}
        return json.dumps(rep)

    @staticmethod
    def fromString(string):
        rep = json.loads(string)
        bi = BandLUTInfo(rep['scale'], rep['offset'], 
                rep['lutsize'], rep['min'], rep['max'])
        return bi


class ViewerLUT(QObject):
    """
    Class that handles the Lookup Table
    used for transformation between raw
    data and stretched data
    """
    def __init__(self):
        QObject.__init__(self) # so we can emit signal
        # array shape [lutsize,4] for color table and greyscale
        # shape [4, lutsize] for RGB
        self.lut = None
        # a single BandLUTInfo instance for single band
        # dictionary keyed on code for RGB
        self.bandinfo = None

    def saveToFile(self, fname):
        """
        Save current stretch to a text file
        so it can be stored and manipulated
        """
        fileobj = open(fname, 'w')

        if self.lut.shape[1] == 4:
            rep = {'nbands' : 1}
            fileobj.write('%s\n' % json.dumps(rep))

            # color table - just one bandinfo - write it out
            bi = self.bandinfo
            fileobj.write('%s\n' % bi.toString())
            for code in RGB_CODES:
                lutindex = CODE_TO_LUTINDEX[code]
                lut = self.lut[...,lutindex]
                rep = {'code' : code, 'data' : lut.tolist()}
                fileobj.write('%s\n' % json.dumps(rep))
        else:
            # rgb
            rep = {'nbands' : 3}
            fileobj.write('%s\n' % json.dumps(rep))
            for code in RGB_CODES:
                lutindex = CODE_TO_LUTINDEX[code]
                bi = self.bandinfo[code]

                fileobj.write('%s\n' % bo.toString())

                lut = self.lut[lutindex]
                rep = {'data' : lut.tolist()}
                fileobj.write('%s\n' % json.dumps(rep))

        fileobj.close()

    @staticmethod
    def createFromFile(fname):

        lutobj = ViewerLUT()
        fileobj = open(fname)
        s = fileobj.readline()
        rep = json.loads(s)
        nbands = rep['nbands']
        if nbands == 1:
            # color table
            s = fileobj.readline()
            bi = BandLUTInfo.fromString(s)
            lutobj.bandinfo = bi
            lutobj.lut = numpy.empty((bi.lutsize, 4), numpy.uint8, 'C')
            for n in range(len(RGB_CODES)):
                s = fileobj.readline()
                rep = json.loads(s)
                code = rep['code']
                lut = numpy.fromiter(rep['data'], numpy.uint8)
                lutindex = CODE_TO_LUTINDEX[code]
                lutobj.lut[...,lutindex] = lut
        else:
            # rgb
            lutobj.bandinfo = {}
            for n in range(len(RGB_CODES)):
                s = fileobj.readline()
                bi = BandLUTInfo.fromString(s)
                code = rep['code']
                lutobj.bandinfo[code] = bi
                lutindex = CODE_TO_LUTINDEX[code]

                if lutobj.lut is None:
                    lutobj.lut = numpy.empty((4, bi.lutsize), numpy.uint8, 'C')
        
                s = fileobj.readline()
                rep = json.loads(s)
                lut = numpy.fromiter(rep['data'], numpy.uint8)
                lutobj.lut[lutindex] = lut

        fileobj.close()
        return lutobj

    def loadColorTable(self, gdalband):
        """
        Creates a LUT for a single band using 
        the color table
        """
        ct = gdalband.GetColorTable()
        if ct is not None:

            if ct.GetPaletteInterpretation() != gdal.GPI_RGB:
                msg = 'only handle RGB color tables'
                raise viewererrors.InvalidColorTable(msg)

            codes = ('r', 'g', 'b', 'a')
            # read in the colour table as lut
            ctcount = ct.GetCount()
            for i in range(ctcount):
                entry = ct.GetColorEntry(i)
                # entry is RGBA, need to store as BGRA - always ignore alpha for now
                for (value, code) in zip(entry, codes):
                    lutindex = CODE_TO_LUTINDEX[code]
                    self.lut[i,lutindex] = value
        else:
            msg = 'No color table present'
            raise viewererrors.InvalidColorTable(msg)


    def createStretchLUT(self, gdalband, stretch, bandinfo):
        """
        Creates a LUT for a single band using the stretch
        method specified and returns it
        """

        if stretch.stretchmode == viewerstretch.VIEWER_STRETCHMODE_NONE:
            # just a linear stretch between 0 and 255
            # for the range of possible values
            lut = numpy.linspace(0, 255, num=bandinfo.lutsize).astype(numpy.uint8)
            return lut

        # other methods below require statistics
        minVal, maxVal, mean, stdDev = self.getStatisticsWithProgress(gdalband)

        # code below sets stretchMin and stretchMax

        if stretch.stretchmode == viewerstretch.VIEWER_STRETCHMODE_LINEAR:
            # stretch between reported min and max
            stretchMin = minVal
            stretchMax = maxVal

        elif stretch.stretchmode == viewerstretch.VIEWER_STRETCHMODE_STDDEV:
            # linear stretch n std deviations from the mean
            nstddev = stretch.stretchparam[0]

            stretchMin = mean - (nstddev * stdDev)
            if stretchMin < minVal:
                stretchMin = minVal
            stretchMax = mean + (nstddev * stdDev)
            if stretchMax > maxVal:
                stretchMax = maxVal

        elif stretch.stretchmode == viewerstretch.VIEWER_STRETCHMODE_HIST:
            self.emit(SIGNAL("newProgress(QString)"), "Calculating Histogram...")

            numBins = int(numpy.ceil(maxVal - minVal))
            histo = gdalband.GetHistogram(min=minVal, max=maxVal, buckets=numBins, 
                    include_out_of_range=0, approx_ok=0, callback=GDALProgressFunc, 
                    callback_data=self)
            sumPxl = sum(histo)

            self.emit(SIGNAL("endProgress()"))

            histmin, histmax = stretch.stretchparam

            # Pete: what is this based on?
            bandLower = sumPxl * histmin
            bandUpper = sumPxl * histmax

            # calc min and max from histo
            # find bin number that bandLower/Upper fall into 
            # maybe we can do better with numpy?
            stretchMin = minVal
            stretchMax = maxVal
            sumVals = 0
            for i in range(numBins):
                sumVals = sumVals + histo[i]
                if sumVals > bandLower:
                    stretchMin = minVal + ((maxVal - minVal) * (i / numBins))
                    break
            sumVals = 0
            for i in range(numBins):
                sumVals = sumVals + histo[-i]
                if sumVals > bandUpper:
                    stretchMax = maxVal + ((maxVal - minVal) * ((numBins - i - 1) / numBins))
                    break

        else:
            msg = 'unsupported stretch mode'
            raise viewererrors.InvalidParameters(msg)

        # the location of the min/max values in the LUT
        minLoc = int((stretchMin + bandinfo.offset) * bandinfo.scale)
        maxLoc = int((stretchMax + bandinfo.offset) * bandinfo.scale)

        # create the LUT
        lut = numpy.empty(bandinfo.lutsize, numpy.uint8, 'C')

        # set values outside to 0/255
        lut[0:minLoc] = 0
        lut[maxLoc:bandinfo.lutsize] = 255

        # create the stretch between stretchMin/Max ind insert into LUT 
        # at right place
        linstretch = numpy.linspace(0, 255, num=(maxLoc - minLoc)).astype(numpy.uint8)
        lut[minLoc:maxLoc] = linstretch

        return lut

    def getStatisticsWithProgress(self, gdalband):
        """
        Helper method. Just quickly returns the stats if
        they are easily available. Calculates them using
        the supplied progress if not.
        """
        gdal.ErrorReset()
        stats = gdalband.GetStatistics(0, 0)
        if stats == [0, 0, 0, -1] or gdal.GetLastErrorNo() != gdal.CE_None:
            # need to actually calculate them
            gdal.ErrorReset()
            self.emit(SIGNAL("newProgress(QString)"), "Calculating Statistics...")
            stats = gdalband.ComputeStatistics(0, GDALProgressFunc, self)
            self.emit(SIGNAL("endProgress()"))

            if stats == [0, 0, 0, -1] or gdal.GetLastErrorNo() != gdal.CE_None:
                msg = 'unable to calculate statistics'
                raise viewererrors.StatisticsError(msg)
            else:
                minVal, maxVal, mean, stdDev = stats
        else:
            minVal, maxVal, mean, stdDev = stats

        return minVal, maxVal, mean, stdDev

    def getInfoForBand(self, gdalband):
        """
        Using either standard numbers, or based
        upon the range of numbers in the image, 
        return the BandLUTInfo instance
        to be used in the LUT for this image
        """
        dtype = gdalband.DataType
        if dtype in INTEGER_SCALEOFFSETS:
            (scale, offset, lutsize, min, max) = INTEGER_SCALEOFFSETS[dtype]
        else:
            # must be float or 32bit int
            # scale to the range of the data
            minVal, maxVal, mean, stdDev = self.getStatisticsWithProgress(gdalband)
            offset = -minVal
            scale = (maxVal - minVal) / DEFAULT_LUTSIZE
            lutsize = DEFAULT_LUTSIZE
            min = minVal
            max = maxVal

        info = BandLUTInfo(scale, offset, lutsize, min, max)
        return info

    def createLUT(self, dataset, stretch):
        """
        Main function
        """
        if stretch.mode == viewerstretch.VIEWER_MODE_DEFAULT or \
                stretch.stretchmode == viewerstretch.VIEWER_STRETCHMODE_DEFAULT:
            msg = 'must set mode and stretchmode'
            raise viewererrors.InvalidStretch(msg)

        # decide what to do based on the ode
        if stretch.mode == viewerstretch.VIEWER_MODE_COLORTABLE:

            if len(stretch.bands) > 1:
                msg = 'specify one band when opening a color table image'
                raise viewererrors.InvalidParameters(msg)

            if stretch.stretchmode != viewerstretch.VIEWER_STRETCHMODE_NONE:
                msg = 'stretchmode should be set to none for color tables'
                raise viewererrors.InvalidParameters(msg)

            band = stretch.bands[0]
            gdalband = dataset.GetRasterBand(band)

            self.bandinfo = self.getInfoForBand(gdalband)

            # LUT is shape [lutsize,4] so we can index from a single 
            # band and get the brga (native order)
            self.lut = numpy.empty((self.bandinfo.lutsize, 4), numpy.uint8, 'C')

            if self.bandinfo.scale != 1:
                msg = 'Can only apply colour table to images with 1:1 scale on LUT'
                raise viewererrors.InvalidColorTable(msg)

            # load the color table
            self.loadColorTable(gdalband)

        elif stretch.mode == viewerstretch.VIEWER_MODE_GREYSCALE:
            if len(stretch.bands) > 1:
                msg = 'specify one band when opening a greyscale image'
                raise viewererrors.InvalidParameters(msg)

            band = stretch.bands[0]
            gdalband = dataset.GetRasterBand(band)

            self.bandinfo = self.getInfoForBand(gdalband)

            # LUT is shape [lutsize,4] so we can index from a single 
            # band and get the brga (native order)
            self.lut = numpy.empty((self.bandinfo.lutsize, 4), numpy.uint8, 'C')

            lut = self.createStretchLUT( gdalband, stretch, self.bandinfo )

            # copy to all bands
            for code in RGB_CODES:
                lutindex = CODE_TO_LUTINDEX[code]
                self.lut[...,lutindex] = lut

        elif stretch.mode == viewerstretch.VIEWER_MODE_RGB:
            if len(stretch.bands) != 3:
                msg = 'must specify 3 bands when opening rgb'
                raise viewererrors.InvalidParameters(msg)


            self.bandinfo = {}
            self.lut = None

            # user supplies RGB
            for (band, code) in zip(stretch.bands, RGB_CODES):
                gdalband = dataset.GetRasterBand(band)

                bandinfo = self.getInfoForBand(gdalband)

                if self.lut == None:
                    # LUT is shape [4,lutsize]. We apply the stretch seperately
                    # to each band. Order is RGBA (native order to make things easier)
                    self.lut = numpy.empty((4, bandinfo.lutsize), numpy.uint8, 'C')
                elif self.lut.shape[1] != bandinfo.lutsize:
                    msg = 'cannot handle bands with different LUT sizes'
                    raise viewererrors.InvalidStretch(msg)

                self.bandinfo[code] = bandinfo

                lutindex = CODE_TO_LUTINDEX[code]
                # create stretch for each band
                lut = self.createStretchLUT( gdalband, stretch, bandinfo )

                self.lut[lutindex] = lut
            
        else:
            msg = 'unsupported display mode'
            raise viewererrors.InvalidParameters(msg)

    def applyLUTSingle(self, data):
        """
        Apply the LUT to a single band (color table
        or greyscale image) and return the result as
        a QImage
        """
        # apply scaling
        data = (data + self.bandinfo.offset) * self.bandinfo.scale

        # do the lookup
        bgra = self.lut[data]
        winysize, winxsize = data.shape
        
        # create QImage from numpy array
        # see http://www.mail-archive.com/pyqt@riverbankcomputing.com/msg17961.html
        image = QImage(bgra.data, winxsize, winysize, QImage.Format_RGB32)
        image.ndarray = data # hold on to the data in case we
                            # want to change the lut and quickly re-apply it
        return image

    def applyLUTRGB(self, datalist):
        """
        Apply LUT to 3 bands of imagery
        passed as a lit of arrays.
        Return a QImage
        """
        winysize, winxsize = datalist[0].shape

        # create blank array to stretch into
        bgra = numpy.empty((winysize, winxsize, 4), numpy.uint8, 'C')
        for (data, code) in zip(datalist, RGB_CODES):
            lutindex = CODE_TO_LUTINDEX[code]
            bandinfo = self.bandinfo[code]

            # apply scaling
            data = (data + bandinfo.offset) * bandinfo.scale
            # can only do lookups with integer data
            if numpy.issubdtype(data.dtype, numpy.floating):
                data = data.astype(numpy.integer)

            # do the lookup
            bgra[...,lutindex] = self.lut[lutindex][data]
        
        # turn into QImage
        image = QImage(bgra.data, winxsize, winysize, QImage.Format_RGB32)
        return image


