import math
import os
import time
import numpy as N
from numpy import random
import pyfits

import cosutil
import burst
import ccos
import concurrent
import phot
import wavecal
from calcosparam import *       # parameter definitions

# This variable gives the data quality flags that will result in events
# not being included when writeImages writes the flt and counts images.
# It will be modified if brstcorr, badtcorr or phacorr is set to perform.
serious_dq_flags = 0

# These are column names in the corrtag table.  The default values are
# appropriate for FUV data.  These can be reset in setCorrColNames().

xcorr = "xcorr"
ycorr = "ycorr"
xdopp = "xdopp"
ydopp = "ycorr"
xfull = "xfull"
yfull = "yfull"

# This will be a Boolean array, true for events that are within the
# active area.  This is only needed for FUV, but it will also be defined
# for NUV (all True).
active_area = None

def timetagBasicCalibration (input, inpha, outtag,
                  output, outcounts, outflash, outcsum,
                  info, switches, reffiles,
                  wavecal_info,
                  stimfile=None, livetimefile=None, burstfile=None):
    """Do the basic processing for time-tag data.

    The function value will be zero if there was no problem,
    and it will be one if there was no input data.

    arguments:
    input         name of the input file
    inpha         name of the input file containing the pulse height
                  histogram (FUV ACCUM only)
    outtag        name of the output file for corrected time-tag data
    output        name of the output file for flat-fielded count-rate image
    outcounts     name of the output file for count-rate image
    outflash      name of the output file for tagflash wavecal spectra (or None)
    outcsum       name of the output image for OPUS to add to cumulative
                      image (or None)
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    wavecal_info  when wavecal exposures were processed, the results
                    were stored in this dictionary
    stimfile      name of output text file for stim positions (or None)
    livetimefile  name of output text file for livetime factors (or None)
    burstfile     name of output text file for burst info (or None)
    """

    if info["obsmode"] == "TIME-TAG":
        cosutil.printIntro ("TIME-TAG calibration")
        names = [("Input", input), ("OutTag", outtag),
                 ("OutFlt", output), ("OutCounts", outcounts)]
        if outflash is not None:
            names.append (("OutFlash", outflash))
        if outcsum is not None:
            names.append (("OutCsum", outcsum))
        cosutil.printFilenames (names, stimfile=stimfile,
                                livetimefile=livetimefile)
        cosutil.printMode (info)

    # Copy data from the input file to the output.  Then open the output
    # file read/write.
    if info["obsmode"] == "TIME-TAG":
        nrows = cosutil.writeOutputEvents (input, outtag)
    ofd = pyfits.open (outtag, mode="update")
    if ofd["EVENTS"].data is None:
        nrows = 0
    else:
        nrows = len (ofd["EVENTS"].data)

    # Get a copy of the primary header.  This copy will be modified and
    # written to the output image files.
    phdr = ofd[0].header

    # Update the switches and reference file names, so the output header
    # will reflect what was actually used.
    cosutil.overrideKeywords (phdr, ofd[1].header, info, switches, reffiles)

    # events_hdu is a complete fits HDU object (i.e., header plus data),
    # while events (assigned below) is just the data, a recarray object.
    events_hdu = ofd["EVENTS"]

    if nrows == 0:
        writeNull (input, output, outcounts, outcsum, info, phdr, events_hdu)
        ofd.close()
        return 1

    setCorrColNames (info["detector"])

    events = events_hdu.data

    setActiveArea (events, info, reffiles["brftab"])

    doPhotcorr (info, switches, reffiles["imphttab"], phdr, ofd[1].header)

    updateGlobrate (info, events_hdu.header)

    bursts = doBurstcorr (events, info, switches, reffiles, phdr, burstfile)

    badt = doBadtcorr (events, info, switches, reffiles, phdr)

    if info["obsmode"] == "TIME-TAG":
        gti = recomputeExptime (input, bursts, badt, events,
                                events_hdu.header, info)
        saveNewGTI (ofd, gti)

    doPhacorr (inpha, events, info, switches, reffiles, phdr, events_hdu.header)

    doRandcorr (events, info, switches, reffiles, phdr)

    (stim_param, stim_countrate, stim_livetime) = initTempcorr (events,
            input, info, switches, reffiles, events_hdu.header, stimfile)

    doTempcorr (stim_param, events, info, switches, reffiles, phdr)

    doGeocorr (events, info, switches, reffiles, phdr)

    # Set this array of flags again, after geometric correction.
    setActiveArea (events, info, reffiles["brftab"])

    # Copy columns to xdopp, xfull, yfull so we'll have default values.
    copyColumns (events)

    doDoppcorr (events, info, switches, reffiles, phdr)
    initHelcorr (events, info, switches, events_hdu.header)

    doDeadcorr (events, input, info, switches, reffiles, phdr,
                stim_countrate, stim_livetime, livetimefile)

    # Write the calcos sum image.
    if outcsum is not None:
        if info["detector"] == "FUV" and info["obsmode"] == "TIME-TAG":
            pha = events.field ("pha")
        else:
            pha = None
        writeCsum (events.field (xcorr), events.field (ycorr),
                   events.field ("epsilon"), pha,
                   info["detector"], info["subarray"],
                   phdr, events_hdu.header,
                   outcsum)

    doFlatcorr (events, info, switches, reffiles, phdr)

    if info["obstype"] == "SPECTROSCOPIC" and \
       switches["wavecorr"] == "PERFORM":
        if info["tagflash"]:
            (avg_dx, avg_dy, shift1_vs_time) = \
                    concurrent.processConcurrentWavecal (events, outflash,
                        info, switches, reffiles, phdr, events_hdu.header)
            phdr.update ("wavecals", os.path.basename (input))
        else:
            (avg_dx, avg_dy, shift1_vs_time) = \
                    updateFromWavecal (events, wavecal_info,
                        info, switches, reffiles, phdr, events_hdu.header)
    else:
        (avg_dx, avg_dy, shift1_vs_time) = (0., 0., None)

    minmax_shifts = getWavecalOffsets (events)

    dq_array = doDqicorr (events, input, info, switches, reffiles,
                          stim_param,
                          phdr, events_hdu.header, minmax_shifts)

    writeImages (events.field (xfull), events.field (yfull),
                 events.field ("epsilon"), events.field ("dq"),
                 phdr, events_hdu.header,
                 dq_array, info["npix"], info["x_offset"], info["exptime"],
                 outcounts, output)

    doStatflag (switches, output, outcounts)

    ofd.close()

    # Comment this out for the time being.
    # appendShift1 (outtag, output, outcounts, shift1_vs_time)

    return 0            # 0 is OK


def setCorrColNames (detector):
    """Assign column names to global variables.

    argument:
    detector      FUV or NUV
    """

    global xcorr, ycorr, xdopp, ydopp, xfull, yfull

    xcorr = "XCORR"
    ycorr = "YCORR"
    xdopp = "XDOPP"
    ydopp = "YCORR"

    xfull = "XFULL"
    yfull = "YFULL"

def setActiveArea (events, info, brftab):
    """Assign a value to active_area.

    @param events: the data unit containing the events table
    @type events: record array
    @param info: header keywords and values
    @type info: dictionary
    @param brftab: name of the baseline reference table
    @type brftab: string

    This function updates the global variable active_area, which is a
    Boolean array with the same number of elements as there are rows in
    the events table.  An element will be True if the corresponding
    event (row in the table) is within the FUV active area.  For NUV
    all elements will be set to True.
    """

    global active_area

    xi  = events.field (xcorr)
    eta = events.field (ycorr)
    active_area = N.ones (len (xi), dtype=N.bool8)

    # A value of 1 (True) in active_area means the corresponding event
    # is within the active area.
    if info["detector"] == "FUV":
        (b_low, b_high, b_left, b_right) = \
                cosutil.activeArea (info["segment"], brftab)
        (b_low, b_high, b_left, b_right) = \
                       (b_low+2, b_high-2, b_left+2, b_right-2)
        active_area = N.where (xi > b_right, False, active_area)
        active_area = N.where (xi < b_left,  False, active_area)
        active_area = N.where (eta > b_high, False, active_area)
        active_area = N.where (eta < b_low,  False, active_area)
        # Make sure the data type is still boolean.
        active_area = active_area.astype (N.bool8)

def doPhotcorr (info, switches, imphttab, phdr, hdr):
    """Update photometry parameter keywords for imaging data.

    @param info: header keywords and values
    @type info: dictionary
    @param switches: calibration switches
    @type switches: dictionary
    @param imphttab: the name of the imaging photometric parameters table
    @type imphttab: string
    @param phdr: the primary header, photcorr keyword updated in-place
    @type phdr: pyfits Header object
    @param hdr: the first extension header, updated in-place
    @type hdr: pyfits Header object
    """

    if info["obstype"] == "IMAGING" and info["detector"] == "NUV":
        cosutil.printSwitch ("PHOTCORR", switches)
        if switches["photcorr"] == "PERFORM":
            obsmode = "cos,nuv," + info["opt_elem"] + "," + info["aperture"]
            obsmode = obsmode.lower()
            phot.doPhot (imphttab, obsmode, hdr)
            phdr.update ("photcorr", "COMPLETE")

def updateGlobrate (info, hdr):
    """Update the GLOBRATE keyword in the extension header.

    arguments:
    info          dictionary of header keywords and values
    hdr           the input events extension header
    """

    globrate = globrate_tt (info["exptime"], info["detector"])
    hdr.update ("globrate", globrate)

def globrate_tt (exptime, detector):
    """Return the global count rate for time-tag data.

    arguments:
    exptime       the exposure time
    detector      FUV or NUV

    The function value is the global count rate, counts per second.
    """

    global active_area

    if exptime <= 0.:
        return 0.

    if detector == "NUV":
        return float (len (active_area)) / exptime

    return N.sum (active_area.astype (N.float32)) / exptime

def doBurstcorr (events, info, switches, reffiles, phdr, burstfile):
    """Find bursts, and flag them in the data quality column.

    @param events: the data unit containing the events table
    @type events: pyfits record array
    @param info: header keywords and values
    @type info: dictionary
    @param switches: calibration switches
    @type switches: dictionary
    @param reffiles: reference file names
    @type reffiles: dictionary
    @param phdr: the input primary header
    @type phdr: pyfits Header object
    @param burstfile: name of output text file for burst info (or None)
    @type burstfile: string

    @return: list of [bad_start, bad_stop] intervals during which a burst
        was detected (seconds since expstart)
    @rtype: list of two-element lists, or None
    """

    global serious_dq_flags

    bursts = None
    if info["segment"][:3] == "FUV":
        # Find and flag regions where the count rate is unreasonably high.
        cosutil.printSwitch ("BRSTCORR", switches)
        if switches["brstcorr"] == "PERFORM":
            serious_dq_flags |= DQ_BURST
            cosutil.printRef ("brsttab", reffiles)
            cosutil.printRef ("xtractab", reffiles)
            bursts = burst.burstFilter (events.field ("time"),
                         events.field (ycorr), events.field ("dq"),
                         reffiles, info, burstfile)
            phdr.update ("brstcorr", "COMPLETE")

    return bursts

def doBadtcorr (events, info, switches, reffiles, phdr):
    """Flag bad time intervals in the data quality column.

    @param events: the data unit containing the events table
    @type events: pyfits record array
    @param info: header keywords and values
    @type info: dictionary
    @param switches: calibration switches
    @type switches: dictionary
    @param reffiles: reference file names
    @type reffiles: dictionary
    @param phdr: the input primary header
    @type phdr: pyfits Header object

    @return: list of [bad_start, bad_stop] intervals from the badttab
        (converted to seconds since expstart)
    @rtype: list of two-element lists
    """

    global serious_dq_flags

    badt = []

    cosutil.printSwitch ("BADTCORR", switches)
    if switches["badtcorr"] == "PERFORM":
        serious_dq_flags |= DQ_BAD_TIME
        cosutil.printRef ("BADTTAB", reffiles)
        badt = filterByTime (events.field ("time"), events.field ("dq"),
                    reffiles["badttab"], info["expstart"], info["segment"])
        phdr["badtcorr"] = "COMPLETE"

    return badt

def filterByTime (time, dq, badttab, expstart, segment):
    """Flag bad time intervals in dq.

    For each bad time interval in the badttab, a flag will be set in the
    data quality column for each event within that time interval.

    @param time: the time column in the events table
    @type time: numpy array
    @param dq: the data quality column in the events table (updated in-place)
    @type dq: numpy array
    @param badttab: the name of the bad-time-intervals table
    @type badttab: string
    @param expstart: the exposure start time (MJD)
    @type expstart: float
    @param segment: FUVA or FUVB
    @type segment: string

    @return: list of [bad_start, bad_stop] intervals from the badttab
        (converted to seconds since expstart)
    @rtype: list of two-element lists
    """

    # Flag regions listed in the badt table.
    badt_info = cosutil.getTable (badttab, filter={"segment": segment})

    badt = []
    if badt_info is not None:
        nrows = badt_info.shape[0]

        start = badt_info.field ("start")
        stop  = badt_info.field ("stop")

        # Convert from MJD to seconds after expstart.
        for i in range (nrows):
            start[i] = (start[i] - expstart) * SEC_PER_DAY
            stop[i] = (stop[i] - expstart) * SEC_PER_DAY
            badt.append ([start[i], stop[i]])

        # For each time interval in the badttab, flag every event for which
        # the time falls within that interval.
        for i in range (nrows):
            dq |= N.where (N.logical_and \
                      (time >= start[i], time <= stop[i]), DQ_BAD_TIME, 0)

    return badt

def recomputeExptime (input, bursts, badt, events, hdr, info):
    """Recompute the exposure time and update the keyword.

    @param input: name of the input file (for getting GTI table)
    @type input: string
    @param bursts: list of [bad_start, bad_stop] intervals during which
        a burst was detected
    @type bursts: list of two-element lists
    @param badt: list of [bad_start, bad_stop] intervals from the badttab
        (converted to seconds since expstart)
    @type badt: list of two-element lists
    @param events: the data unit containing the events table
    @type events: pyfits record array
    @param hdr: the events extension header (exptime keyword can be updated)
    @type hdr: pyfits Header object
    @param info: keywords and values (exptime can be updated)
    @type info: dictionary

    @return: list of [start, stop] good time intervals (seconds since
        expstart), updated from the GTI table in the raw file by excluding
        bursts and intervals flagged as bad by the badttab
    @rtype: list of two-element lists
    """

    gti = cosutil.returnGTI (input)
    if len (gti) <= 0:
        cosutil.printWarning ("No GTI table found in raw file.", VERBOSE)
        time = events.field ("time")
        gti = [[time[0], time[-1]]]

    gti = recomputeGTI (gti, bursts)
    gti = recomputeGTI (gti, badt)

    exptime = 0.
    for (start, stop) in gti:
        exptime += (stop - start)

    old_exptime = hdr.get ("exptime", 0.)
    if exptime != old_exptime:
        hdr.update ("exptime", exptime)
        info["exptime"] = exptime
        if abs (exptime - old_exptime) > 1.:
            cosutil.printWarning ("exposure time in header was %.3f" % \
                    old_exptime, VERBOSE)
            cosutil.printContinuation ("exptime has been corrected to %.3f" % \
                    exptime, VERBOSE)

    return gti

def recomputeGTI (gti, badt):
    """Recompute the list of good [start, stop] intervals.

    @param gti: list of [start, stop] good time intervals (times are in
        seconds since EXPSTART)
    @type gti: list of two-element lists
    @param badt: list of [bad_start, bad_stop] intervals, e.g. during which
        there was a burst or a bad time interval from the BADTTAB (seconds
        since EXPSTART)
    @type badt: list of two-element lists

    @return: an updated list of [start, stop] good time intervals
    @rtype: list of two-element lists
    """

    if not badt:
        return gti

    for (bad_start, bad_stop) in badt:
        new_gti = []
        for (start, stop) in gti:
            if bad_start >= stop or bad_stop <= start:
                new_gti.append ([start, stop])
            else:
                if bad_start > start:
                    new_gti.append ([start, bad_start])
                if bad_stop < stop:
                    new_gti.append ([bad_stop, stop])
        gti = new_gti

    return gti

def saveNewGTI (ofd, gti):
    """xxx not implemented yet"""
    pass

def doPhacorr (inpha, events, info, switches, reffiles, phdr, hdr):
    """Filter by pulse height.

    arguments:
    inpha         name of the input file containing the pulse height histogram
    events        the data unit containing the events table
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    phdr          the input primary header
    hdr           the input events extension header
    """

    global serious_dq_flags

    if info["detector"] == "FUV":
        cosutil.printSwitch ("PHACORR", switches)
        if switches["phacorr"] == "PERFORM":
            serious_dq_flags |= DQ_PH_LOW
            serious_dq_flags |= DQ_PH_HIGH
            if info["obsmode"] == "TIME-TAG":
                cosutil.printRef ("PHATAB", reffiles)
                filterByPulseHeight (events.field ("pha"), events.field ("dq"),
                        reffiles["phatab"], info["segment"], hdr)
            else:
                checkPulseHeight (inpha, reffiles["phatab"], info, hdr)
            phdr["phacorr"] = "COMPLETE"

def filterByPulseHeight (pha, dq, phatab, segment, hdr):
    """Flag events that have a pulse height outside an allowed range.

    This is only called for TIME-TAG mode data.

    arguments:
    pha        pulse-height column in events table
    dq         data-quality column in events table (modified in-place)
    phatab     name of PHA thresholds table
    segment    segment name (FUVA or FUVB)
    hdr        header for events table extension (keywords for screening
                 limits and number of rejected events will be assigned)
    """

    global active_area

    pha_info = cosutil.getTable (phatab, filter={"segment": segment},
                   exactly_one=True)

    low = pha_info.field ("llt")[0]
    high = pha_info.field ("ult")[0]

    # Flag an event if the pulse height is below the minimum value or
    # above the maximum value that is likely to be encountered from a
    # real photon event.
    # Restrict this test to the active area.
    test_low = N.logical_and (active_area, pha < low)
    test_high = N.logical_and (active_area, pha > high)
    dq |= N.where (test_low, DQ_PH_LOW, 0)
    dq |= N.where (test_high, DQ_PH_HIGH, 0)

    # Count the number of rejected events.
    rejected = N.nonzero (dq & DQ_PH_LOW)[0]
    nbad_low = len (rejected)
    rejected = N.nonzero (dq & DQ_PH_HIGH)[0]
    nbad_high = len (rejected)
    nbad = nbad_low + nbad_high
    if cosutil.checkVerbosity (VERY_VERBOSE):
        cosutil.printMsg ("Filter by pulse height:", VERY_VERBOSE)
        if nbad_low == 0:
            msg = "  no event was"
        elif nbad_low == 1:
            msg = "  one event was"
        else:
            msg = "  %d events were" % nbad_low
        msg += " rejected because PHA was less than %d" % low
        cosutil.printMsg (msg, VERY_VERBOSE)
        if nbad_high == 0:
            msg = "  no event was"
        elif nbad_high == 1:
            msg = "  one event was"
        else:
            msg = "  %d events were" % nbad_high
        msg += " rejected because PHA was greater than %d" % high
        cosutil.printMsg (msg, VERY_VERBOSE)

    keyword = "PHA_BAD" + segment[-1]
    hdr.update (keyword, nbad)

    # Update the values for the screening limit keywords
    # (low and high are the default values).
    cosutil.updatePulseHeightKeywords (hdr, segment, low, high)

def checkPulseHeight (inpha, phatab, info, hdr):
    """Check that the pulse-height distribution is reasonable.

    This is only called for ACCUM mode data.

    arguments:
    inpha         name of file containing pulse-height distribution
    phatab        name of table of pulse-height parameters
    info          dictionary of keywords and values
    hdr           extension header
    """

    pha_info = cosutil.getTable (phatab, filter={"segment": info["segment"]},
                   exactly_one=True)

    low = pha_info.field ("llt")[0]
    high = pha_info.field ("ult")[0]

    # Update the values for the screening limit keywords
    cosutil.updatePulseHeightKeywords (hdr, info["segment"], low, high)

    # The peak in the pulse-height distribution should be within low and high.
    # Apply a factor to low and high to account for the fact that the
    # histogram is from seven-bit values but the values in the table are
    # for five-bit values (the PHA column in an EVENTS table).
    # The mean should be within the factors min_peak and max_peak of the peak.
    low *= TWO_BITS
    high *= TWO_BITS
    min_peak = pha_info.field ("min_peak")[0]
    max_peak = pha_info.field ("max_peak")[0]

    # Read the pulse-height histogram.
    fd = pyfits.open (inpha, mode="readonly")
    pha_data = fd[1].data

    npts = len (pha_data)

    sum = N.sum (N.arange (npts, dtype=N.float32) * pha_data.astype (N.float32))
    sumwgt = N.sum (pha_data.astype (N.float32))
    pha_index = N.argsort (pha_data)
    peak = pha_index[-1]

    if sumwgt == 0.:
        cosutil.printWarning ("Histogram is empty.")
        fd.close()
        return

    meanval = sum / sumwgt

    warn = (cosutil.checkVerbosity (VERY_VERBOSE))      # initial value
    if peak <= low:
        cosutil.printWarning ("Peak in pulse-height distribution is too low.")
        warn = 1
    if peak >= high:
        cosutil.printWarning ("Peak in pulse-height distribution is too high.")
        warn = 1

    if meanval < peak * min_peak:
        cosutil.printWarning ("Mean of pulse-height distribution is too low.")
        warn = 1
    if meanval > peak * max_peak:
        cosutil.printWarning ("Mean of pulse-height distribution is too high.")
        warn = 1

    if warn:
        cosutil.printMsg (
                "Pulse-height distribution peak = %d, mean = %.6g;" % \
                (peak, meanval))
        cosutil.printMsg (
                "the peak should be between %.6g and %.6g," % (low, high))
        cosutil.printMsg (
                "and the mean should be between %.6g and %.6g." % \
                (peak * min_peak, peak * max_peak))

    fd.close()

def doRandcorr (events, info, switches, reffiles, phdr):
    """Add pseudo-random numbers to x and y coordinates within the active area.

    arguments:
    events        the data unit containing the events table
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    phdr          the input primary header
    """

    global active_area

    if info["detector"] == "FUV":
        cosutil.printSwitch ("RANDCORR", switches)
        if switches["randcorr"] == "PERFORM":
            xi  = events.field (xcorr)
            eta = events.field (ycorr)
            nelem = len (xi)
            if info["randseed"] == -1:
                seed = int (time.time())
                phdr["randseed"] = seed
            else:
                seed = info["randseed"]
            random.seed (seed)
            rn = random.uniform (-0.5, +0.5, nelem)
            xi[:] = N.where (active_area, xi - rn, xi)
            rn = random.uniform (-0.5, +0.5, nelem)
            eta[:] = N.where (active_area, eta - rn, eta)
            phdr["randcorr"] = "COMPLETE"

def initTempcorr (events, input, info, switches, reffiles, hdr, stimfile):
    """Compute parameters for thermal distortion.

    arguments:
    events        the data unit containing the events table
    input         name of raw file (for writing to stimfile)
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    hdr           the input events extension header
    stimfile      name of output text file for stim positions (or None)
    """

    if info["detector"] == "FUV" and \
       (switches["tempcorr"] == "PERFORM" or switches["deadcorr"] == "PERFORM"):
        # Compute the parameters (to be used later).
        time = cosutil.getColCopy (data=events, column="time")
        (stim_param, avg_s1, avg_s2, rms_s1, rms_s2, s1_ref, s2_ref,
         stim_countrate, stim_livetime) = \
         computeThermalParam (time,
            events.field (xcorr), events.field (ycorr), events.field ("dq"),
            reffiles["brftab"], info["obsmode"],
            info["segment"], info["exptime"], info["stimrate"],
            input, stimfile)
        # Update stim location keywords in extension header.
        stimKeywords (hdr, info["segment"], avg_s1, avg_s2, rms_s1, rms_s2,
                      s1_ref, s2_ref)
    else:
        stim_countrate = 0.
        stim_livetime = 1.
        stim_param = {}

    return (stim_param, stim_countrate, stim_livetime)

def computeThermalParam (time, x, y, dq,
           brftab, obsmode,
           segment, exptime, stimrate, input, stimfile):
    """Compute thermal distortion parameters from stim positions.

    This function loops over intervals of time, and within each interval
    calls routines to find the stim locations and compute the thermal
    distortion parameters.

    If a stimfile was specified, it will be opened (append mode), and the
    stim positions for each time interval will be written to the file.
    (The 'input' argument is included in the calling sequence only for the
    purpose of writing its name to the stimfile.)

    arguments:
    time          array of event times
    x, y          arrays of detector X and Y coordinates
    dq            array of data quality flags   (NOTE:  not currently used)
    brftab        name of baseline reference data table
    obsmode       TIME-TAG or ACCUM
    segment       segment name (for FUV)
    exptime       exposure time (for computing livetime)
    stimrate      input count rate for a stim (for computing livetime)
    input         name of raw file (for writing to stimfile)
    stimfile      name of text file to which stim locations will be appended

    The function value is a tuple:
      (stim_param, avg_s1, avg_s2, rms_s1, rms_s2, s1_ref, s2_ref,
       stim_countrate, stim_livetime)

    stim_param is a dictionary of lists:  (i0, i1, x0, xslope, y0, yslope)

    avg_s1[0] is the average Y location of the first stim.
    avg_s1[1] is the average X location of the first stim.
    avg_s2[0] is the average Y location of the second stim.
    avg_s2[1] is the average X location of the second stim.

    rms_s1[0] is the RMS in Y for the first stim.
    rms_s1[1] is the RMS in X for the first stim.
    rms_s2[0] is the RMS in Y for the second stim.
    rms_s2[1] is the RMS in X for the second stim.

    stim_countrate is the observed count rate for a stim, or None if
      neither stim could be found
    stim_livetime is the live time computed from the input and observed
      stim rate

    For each i:
      i0[i], i1[i] is the slice of indices in 'events' corresponding to the
        ith time interval.  Each such interval is of length dt_thermal in
        duration (except possibly the last, which could be shorter).
      x0[i] and xslope[i] are the intercept and slope respectively of the
        linear correction to the X positions (the more rapidly varying
        direction).
      y0[i] and yslope[i] are the intercept and slope for the linear
        correction to the Y positions.
    """

    if stimfile is None:
        fd = None
    else:
        fd = open (stimfile, "a")
        fd.write ("# %s\n" % input)

    nevents = len (time)

    brf_info = cosutil.getTable (brftab, filter={"segment": segment},
                   exactly_one=True)

    # Find stims and compute parameters every dt_thermal seconds.
    if obsmode == "TIME-TAG":
        fd_brf = pyfits.open (brftab, mode="readonly")
        dt_thermal = fd_brf[1].header["timestep"]
        fd_brf.close()
        cosutil.printMsg (
"Compute thermal corrections from stim positions; timestep is %.6g s:" \
            % dt_thermal, VERY_VERBOSE)
    else:
        # For ACCUM data we want just one time interval.
        dt_thermal = time[-1] - time[0] + 1.

    sx1 = brf_info.field ("sx1")[0]
    sy1 = brf_info.field ("sy1")[0]
    sx2 = brf_info.field ("sx2")[0]
    sy2 = brf_info.field ("sy2")[0]
    xwidth = brf_info.field ("xwidth")[0]
    ywidth = brf_info.field ("ywidth")[0]

    # These are the reference locations of the stims.
    s1_ref = (sy1, sx1)
    s2_ref = (sy2, sx2)

    counts1 = 0.
    counts2 = 0.
    i0 = []
    i1 = []
    x0 = []
    xslope = []
    y0 = []
    yslope = []
    if fd is not None:
        fd.write ("# t0 t1 stim_locations\n")

    t0 = time[0]
    t1 = t0 + dt_thermal
    sumstim = (0, 0., 0., 0., 0., 0, 0., 0., 0., 0.)
    last_s1 = s1_ref            # initial default values
    last_s2 = s2_ref
    while t0 <= time[nevents-1]:

        # time[i:j] matches t0 to t1.
        try:
            (i, j) = ccos.range (time, t0, t1)
        except:
            t0 = t1
            t1 = t0 + dt_thermal
            continue
        if i >= j:              # i and j can be equal due to roundoff
            t0 = t1
            t1 = t0 + dt_thermal
            continue

        (s1, sumsq1, counts1, found_s1) = \
                findStim (x[i:j], y[i:j], s1_ref, xwidth, ywidth)

        (s2, sumsq2, counts2, found_s2) = \
                findStim (x[i:j], y[i:j], s2_ref, xwidth, ywidth)

        # Increment sums for averaging the stim positions.
        sumstim = updateStimSum (sumstim, counts1, s1, sumsq1, found_s1,
                                          counts2, s2, sumsq2, found_s2)

        if fd is not None:
            fd.write ("%.0f %.0f" % (t0, min (time[nevents-1], t1)))
            if found_s1:
                fd.write ("  %.1f %.1f" % (s1[1], s1[0]))
            else:
                fd.write ("  INDEF INDEF")
            if found_s2:
                fd.write ("  %.1f %.1f\n" % (s2[1], s2[0]))
            else:
                fd.write ("  INDEF INDEF\n")
        if found_s1:
            last_s1 = s1        # save current value
        else:
            s1 = last_s1        # use last stim position that was found
        if found_s2:
            last_s2 = s2
        else:
            s2 = last_s2
        if cosutil.checkVerbosity (VERY_VERBOSE) or \
           not (found_s1 and found_s2):
            msg = "  %7d ... %7d" % (i, j-1)
            msg += "  %.1f %.1f" % (s1[1], s1[0])
            if not found_s1:
                msg += " (stim1 not found)"
            msg += "  %.1f %.1f" % (s2[1], s2[0])
            if not found_s2:
                msg += " (stim2 not found)"
            if not (found_s1 and found_s2):
                cosutil.printWarning (msg)
                if time[j-1] - time[i] < dt_thermal and obsmode == "TIME-TAG":
                    cosutil.printContinuation (\
                "Note that the time interval is %g s" % (time[j-1] - time[i]))
            else:
                cosutil.printMsg (msg)

        (x0_n, xslope_n, y0_n, yslope_n) = thermalParam (s1, s2, s1_ref, s2_ref)
        i0.append (i)
        i1.append (j)
        x0.append (x0_n)
        xslope.append (xslope_n)
        y0.append (y0_n)
        yslope.append (yslope_n)
        t0 = t1
        t1 = t0 + dt_thermal

    # Compute the average of the stim positions.
    avg_s1 = [-1., -1.]
    avg_s2 = [-1., -1.]
    rms_s1 = [-1., -1.]
    rms_s2 = [-1., -1.]
    if sumstim[0] > 0:
        avg_s1[0] = sumstim[1] / sumstim[0]             # y
        avg_s1[1] = sumstim[2] / sumstim[0]             # x
        if sumstim[0] > 1:
            rms_s1[0] = math.sqrt (sumstim[3] / (sumstim[0] - 1.))
            rms_s1[1] = math.sqrt (sumstim[4] / (sumstim[0] - 1.))
        else:
            rms_s1[0] = math.sqrt (sumstim[3])
            rms_s1[1] = math.sqrt (sumstim[4])
    if sumstim[5] > 0:
        avg_s2[0] = sumstim[6] / sumstim[5]
        avg_s2[1] = sumstim[7] / sumstim[5]
        if sumstim[5] > 1:
            rms_s2[0] = math.sqrt (sumstim[8] / (sumstim[5] - 1.))
            rms_s2[1] = math.sqrt (sumstim[9] / (sumstim[5] - 1.))
        else:
            rms_s2[0] = math.sqrt (sumstim[8])
            rms_s2[1] = math.sqrt (sumstim[9])

    if counts1 > 0 and counts2 > 0:
        stim_countrate = (counts1 + counts2) / (2. * exptime)
    elif counts1 > 0:
        stim_countrate = counts1 / exptime
    elif counts2 > 0:
        stim_countrate = counts2 / exptime
    else:
        stim_countrate = None
    if stim_countrate is not None and stimrate > 0.:
        stim_livetime = stim_countrate / stimrate
    else:
        stim_livetime = 1.

    if fd is not None:
        fd.close()

    stim_param = {"i0": i0, "i1": i1,
                  "x0": x0, "xslope": xslope,
                  "y0": y0, "yslope": yslope}

    return (stim_param, avg_s1, avg_s2, rms_s1, rms_s2, s1_ref, s2_ref,
            stim_countrate, stim_livetime)

def findStim (x, y, stim_ref, xwidth, ywidth):
    """Find one stim in time-tag data.

    @param x: array of detector X coordinates
    @type x: array
    @param y: array of detector Y coordinates
    @type y: array
    @param stim_ref: reference position (y, x) for the stim
    @type stim_ref: tuple
    @param xwidth: half width of the search region in X
    @type xwidth: int
    @param ywidth: half width of the search region in Y
    @type ywidth: int

    @return: ((sy, sx), (sumysq, sumxsq), n, found_stim), where (sy, sx) is
        the stim location (if found), (sumysq, sumxsq) is the sum of squared
        deviations from the mean location, n is the number of events for this
        stim within the current time interval, and found_stim is True if the
        stim was actually found (i.e. if n > 0).
    @rtype: tuple
    """

    # This is the search region for finding the stim.
    sxlow  = stim_ref[1] - xwidth
    sxhigh = stim_ref[1] + xwidth
    sylow  = stim_ref[0] - ywidth
    syhigh = stim_ref[0] + ywidth

    # Truncate at the lower and upper borders, excluding the first
    # and last lines.
    sylow = max (sylow, 1)
    syhigh = min (syhigh, 1022)

    # Initial value of mask is 1. (which in this case means "good").
    mask = N.ones (len (x), dtype=N.float32)

    # Now set mask to 0. ("bad") outside the search region.
    mask = N.where (x > sxhigh, 0., mask)
    mask = N.where (x < sxlow,  0., mask)
    mask = N.where (y > syhigh, 0., mask)
    mask = N.where (y < sylow,  0., mask)
    n = N.sum (mask)
    if n > 0.:
        # The stim reference position is subtracted before taking the sum
        # and then added back to the average in order to reduce the
        # possibility of numerical roundoff errors.
        sumx = N.sum ((x-stim_ref[1]) * mask)
        sumy = N.sum ((y-stim_ref[0]) * mask)
        sx = sumx / n + stim_ref[1]
        sy = sumy / n + stim_ref[0]
        # sum of squared deviations, for computing RMS
        sumxsq = N.sum ((x-sx)**2 * mask)
        sumysq = N.sum ((y-sy)**2 * mask)
        found_stim = True
    else:
        sx = None
        sy = None
        sumxsq = None
        sumysq = None
        found_stim = False

    return ((sy, sx), (sumysq, sumxsq), n, found_stim)

def updateStimSum (sumstim, nevents1, s1, sumsq1, found_s1,
                            nevents2, s2, sumsq2, found_s2):
    """Update sums for averages of stim positions.

    arguments:
    sumstim    tuple with current sums:
                 n1      number of events for first stim
                 sum1y   sum for first stim, Y coordinate
                 sum1x   sum for first stim, X coordinate
                 sumsq1y sum of squares for first stim, Y coordinate
                 sumsq1x sum of squares for first stim, X coordinate
                 n2      number of events for second stim
                 sum2y   sum for second stim, Y coordinate
                 sum2x   sum for second stim, X coordinate
                 sumsq2y sum of squares for second stim, Y coordinate
                 sumsq2x sum of squares for second stim, X coordinate
    nevents1   number of events for first stim in current time interval
    s1         tuple of (y,x) coordinates of the first stim in current
                 interval
    found_s1   True if the first stim was actually found
    nevents2   number of events for second stim in current time interval
    s2         same as s1, but for the second stim
    found_s2   True if the second stim was actually found

    The function value is an updated sumstim tuple.

    nevents1 and nevents2 are used as weights when incrementing the sums.
    n1 and n2 are the total number of events for the first and second stims
    respectively.
    """

    (n1, sum1y, sum1x, sumsq1y, sumsq1x,
     n2, sum2y, sum2x, sumsq2y, sumsq2x) = sumstim

    if found_s1:
        n1 = n1 + nevents1
        sum1y = sum1y + s1[0] * nevents1
        sum1x = sum1x + s1[1] * nevents1
        sumsq1y = sumsq1y + sumsq1[0]
        sumsq1x = sumsq1x + sumsq1[1]

    if found_s2:
        n2 = n2 + nevents2
        sum2y = sum2y + s2[0] * nevents2
        sum2x = sum2x + s2[1] * nevents2
        sumsq2y = sumsq2y + sumsq2[0]
        sumsq2x = sumsq2x + sumsq2[1]

    return (n1, sum1y, sum1x, sumsq1y, sumsq1x,
            n2, sum2y, sum2x, sumsq2y, sumsq2x)

def stimKeywords (hdr, segment, avg_s1, avg_s2, rms_s1, rms_s2,
                  s1_ref, s2_ref):
    """Update keywords for the locations of the stims.

    arguments:
    hdr             the input events extension header (updated)
    segment         FUVA or FUVB

    avg_s1[0] is the average Y location of the first stim.
    avg_s1[1] is the average X location of the first stim.
    avg_s2[0] is the average Y location of the second stim.
    avg_s2[1] is the average X location of the second stim.

    rms_s1[0] is the RMS in Y for the first stim.
    rms_s1[1] is the RMS in X for the first stim.
    rms_s2[0] is the RMS in Y for the second stim.
    rms_s2[1] is the RMS in X for the second stim.

    s1_ref[0] is the Y position of the first stim, from the BRFTAB.
    s1_ref[1] is the X position of the first stim, from the BRFTAB.
    s2_ref[0] is the Y position of the second stim, from the BRFTAB.
    s2_ref[1] is the X position of the second stim, from the BRFTAB.
    """

    seg = segment[-1]           # "A" or "B"

    hdr.update ("STIM"+seg+"0LX", s1_ref[1])
    hdr.update ("STIM"+seg+"0LY", s1_ref[0])
    hdr.update ("STIM"+seg+"0RX", s2_ref[1])
    hdr.update ("STIM"+seg+"0RY", s2_ref[0])

    if avg_s1[0] is None or avg_s1[1] is None:
        hdr.update ("STIM"+seg+"_LX", -1.)
        hdr.update ("STIM"+seg+"_LY", -1.)
    else:
        hdr.update ("STIM"+seg+"_LX", round (avg_s1[1], 3))
        hdr.update ("STIM"+seg+"_LY", round (avg_s1[0], 3))
        hdr.update ("STIM"+seg+"SLX", round (rms_s1[1], 3))
        hdr.update ("STIM"+seg+"SLY", round (rms_s1[0], 3))

    if avg_s2[0] is None or avg_s2[1] is None:
        hdr.update ("STIM"+seg+"_RX", -1.)
        hdr.update ("STIM"+seg+"_RY", -1.)
    else:
        hdr.update ("STIM"+seg+"_RX", round (avg_s2[1], 3))
        hdr.update ("STIM"+seg+"_RY", round (avg_s2[0], 3))
        hdr.update ("STIM"+seg+"SRX", round (rms_s2[1], 3))
        hdr.update ("STIM"+seg+"SRY", round (rms_s2[0], 3))

def thermalParam (s1, s2, s1_ref, s2_ref):
    """Compute linear thermal distortion correction from stim positions.

    arguments:
    s1          measured location in raw data of first stim (y, x)
    s2          measured location in raw data of second stim (y, x)
    s1_ref      reference location of first stim (y, x)
    s2_ref      reference location of second stim (y, x)
    """

    if s1[0] is None or s2[0] is None:

        xslope = 1.
        xintercept = 0.
        yslope = 1.
        yintercept = 0.

    else:

        (sy1, sx1) = s1_ref
        (sy2, sx2) = s2_ref

        xslope = (sx2 - sx1) / (s2[1] - s1[1])
        xintercept = sx1 - s1[1] * xslope

        yslope = (sy2 - sy1) / (s2[0] - s1[0])
        yintercept = sy1 - s1[0] * yslope

    return (xintercept, xslope, yintercept, yslope)

def doTempcorr (stim_param, events, info, switches, reffiles, phdr):
    """Apply thermal distortion correction.

    arguments:
    stim_param    a dictionary of lists, with keys
                    i0, i1, x0, xslope, y0, yslope
    events        the data unit containing the events table
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    phdr          the input primary header
    """

    if info["detector"] == "FUV":
        cosutil.printSwitch ("TEMPCORR", switches)
        if switches["tempcorr"] == "PERFORM":
            cosutil.printRef ("BRFTAB", reffiles)
            # The function value is true if a correction was actually applied.
            if thermalDistortion (events.field (xcorr),
                                  events.field (ycorr), stim_param):
                phdr["tempcorr"] = "COMPLETE"
            else:
                phdr["tempcorr"] = "SKIPPED"
                cosutil.printWarning ("TEMPCORR was skipped")

def thermalDistortion (x, y, stim_param):
    """Apply thermal distortion correction to positions in events list.

    arguments:
    x, y          arrays of pixel coordinates of events
    stim_param    a dictionary of lists, with keys
                    i0, i1, x0, xslope, y0, yslope

    The function value will be true if a correction was actually applied.
    No correction is necessary and none will be applied if the slopes are
    all 0 and the intercepts are all 1.
    """

    # These are the parameters found by computeThermalParam.
    x0 = stim_param["x0"]
    xslope = stim_param["xslope"]
    y0 = stim_param["y0"]
    yslope = stim_param["yslope"]

    actually_done = 0

    if stim_param.has_key ("i0"):
        i0 = stim_param["i0"]
        i1 = stim_param["i1"]
    else:
        i0 = [0]
        i1 = [len (x)]

    for n in range (len (i0)):
        i = i0[n]
        j = i1[n]
        if x0[n] != 0. or xslope[n] != 1. or \
           y0[n] != 0. or yslope[n] != 1.:
            x[i:j] = x0[n] + x[i:j] * xslope[n]
            y[i:j] = y0[n] + y[i:j] * yslope[n]
            actually_done = 1

    return actually_done

def doGeocorr (events, info, switches, reffiles, phdr):
    """Apply geometric correction.

    arguments:
    events        the data unit containing the events table
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    phdr          the input primary header
    """

    if info["detector"] == "FUV":
        cosutil.printSwitch ("GEOCORR", switches)
        if switches["geocorr"] == "PERFORM":
            cosutil.printRef ("GEOFILE", reffiles)
            cosutil.printSwitch ("IGEOCORR", switches)
            cosutil.geometricDistortion (events.field (xcorr),
                    events.field (ycorr), reffiles["geofile"],
                    info["segment"], switches["igeocorr"])
            phdr["geocorr"] = "COMPLETE"
            if switches["igeocorr"] == "PERFORM":
                phdr["igeocorr"] = "COMPLETE"

def doDqicorr (events, input, info, switches, reffiles,
               stim_param,
               phdr, hdr, minmax_shifts):
    """Create a data quality array, initialized from the DQI table.

    arguments:
    events         the data unit containing the events table
    input          name of raw file, used for getting DQ array for ACCUM data
    info           dictionary of header keywords and values
    switches       dictionary of calibration switches
    reffiles       dictionary of reference file names
    stim_param     a dictionary of lists, with keys
                     i0, i1, x0, xslope, y0, yslope
    phdr           the input primary header
    hdr            the input events extension header
    minmax_shifts  (min_shift1, max_shift1, min_shift2, max_shift2)

    This function applies the data quality initialization table (bpixtab) to
    two arrays, the 2-D DQ image extension and the 1-D DQ events table column.

    The 2-D DQ image array dq_array is created and initialized to zero.
    The 1-D DQ events table column, on the other hand, is not initialized
    because it may already contain meaningful flags from pulse-height or time
    filtering.  Note that flags for pulse-height or time filtering that are
    set in the 1-D DQ table column are _not_ included in the 2-D image array,
    since they would be associated with either specific events or time
    intervals, rather than spatial regions on the detector.

    The function value is the 2-D data quality image array, or None if
    dqicorr is not PERFORM.
    """

    cosutil.printSwitch ("DQICORR", switches)

    if info["obsmode"] == "TIME-TAG":
        # Create an initially zero 2-D data quality extension array.
        dq_array = N.zeros (info["npix"], dtype=N.int16)
    else:
        # Read the data quality array from the rawaccum file.
        dq_array = cosutil.getInputDQ (input)

    if switches["dqicorr"] == "PERFORM":

        cosutil.printRef ("BPIXTAB", reffiles)

        # Update the dq column in the events list with the bpixtab regions.
        dq_info = cosutil.getTable (reffiles["bpixtab"],
                            filter={"segment": info["segment"]})
        if dq_info is not None:
            ccos.applydq (dq_info.field ("lx"), dq_info.field ("ly"),
                          dq_info.field ("dx"), dq_info.field ("dy"),
                          dq_info.field ("dq"),
                          events.field (xcorr), events.field (ycorr),
                          events.field ("dq"))
            del dq_info

        # Copy values from the bpixtab to the dq_array, applying offsets
        # depending on the wavecal shift and the Doppler shift.
        (doppmag, doppzero, orbitper) = dopplerParam (info,
                                reffiles["disptab"], switches["doppcorr"])
        minmax_doppler = cosutil.minmaxDoppler (info, switches["doppcorr"],
                               doppmag, doppzero, orbitper)
        cosutil.updateDQArray (reffiles["bpixtab"], info, dq_array,
                               minmax_shifts, minmax_doppler)

        # Flag regions that are outside any subarray as out of bounds.
        cosutil.flagOutOfBounds (phdr, hdr, dq_array, stim_param,
                                 info, switches,
                                 reffiles["brftab"], reffiles["geofile"],
                                 minmax_shifts, minmax_doppler)

        # Flag the region that is outside the active area.
        if info["detector"] == "FUV":
            cosutil.flagOutsideActiveArea (dq_array,
                        info["segment"], reffiles["brftab"], info["x_offset"],
                        minmax_shifts, minmax_doppler)

        phdr["dqicorr"] = "COMPLETE"

    return dq_array

def dopplerParam (info, disptab, doppcorr):
    """Return the appropriate set of Doppler keyword values.

    @param info: keywords and values
    @type info: dictionary
    @param disptab: name of dispersion relation table
    @type disptab: string
    @param doppcorr: if Doppler correction is OMIT, return dummy values
    @type doppcorr: string

    @return: Doppler magnitude in pixels, time (MJD) when the Doppler shift
        is zero and increasing, period (seconds) of HST; different keywords
        will be used depending on whether the data are TIME-TAG or ACCUM
    @rtype: tuple
    """

    if doppcorr != "PERFORM":
        doppmag  = 0.
        doppzero = info["expstart"]
        orbitper = 5760.
    elif info["obsmode"] == "TIME-TAG":
        if info["tc2_2"] == 1.:                 # default value
            # tc2_2 is the dispersion.  If its value is the default,
            # compute the dispersion from the central wavelength and
            # the dispersion coefficients.
            # xxx this section should only be needed temporarily xxx
            cosutil.printWarning ("TC2_2 keyword has the default value.")
            filter = {"opt_elem": info["opt_elem"],
                      "cenwave": info["cenwave"],
                      "aperture": info["aperture"]}
            if info["detector"] == "FUV":
                filter["segment"] = info["segment"]
                middle = float (FUV_X) / 2.
            else:
                filter["segment"] = "NUVB"
                middle = float (NUV_X) / 2.
            # Use at_least_one instead of exactly_one in case there is an
            # fpoffset column in the disptab.
            disp_info = cosutil.getTable (disptab, filter, at_least_one=True)
            ncoeff = disp_info.field ("nelem")[0]
            coeff = disp_info.field ("coeff")[0][0:ncoeff]
            # get the dispersion (disp) at the middle of the detector
            disp = cosutil.evalDerivDisp (middle, coeff, 0.)
        else:
            disp = info["tc2_2"]
        # Compute the Doppler shift in pixels from the shift in km/s.
        doppmag = (info["doppmagv"] / SPEED_OF_LIGHT) * (info["cenwave"] / disp)
        doppzero = info["doppzero"]
        orbitper = info["orbitper"]

    else:               # ACCUM
        doppmag  = info["dopmagt"]
        doppzero = info["dopzerot"]
        orbitper = info["orbtpert"]

    return (doppmag, doppzero, orbitper)

def doDoppcorr (events, info, switches, reffiles, phdr):
    """Apply Doppler correction to the x and y pixel coordinates.

    arguments:
    events        the data unit containing the events table
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    phdr          the input primary header
    """

    if info["obsmode"] == "ACCUM":              # done on-board
        return

    if info["obstype"] == "SPECTROSCOPIC":
        cosutil.printSwitch ("DOPPCORR", switches)

    if switches["doppcorr"] == "PERFORM":

        # xi and eta are the columns of pixel coordinates for the
        # dispersion and cross-dispersion directions respectively.
        # (explicit column names are used here for clarity)
        xi = events.field ("xcorr")
        eta = events.field ("ycorr")
        dopp = events.field ("xdopp")
        xi_full  = events.field ("xfull")

        cosutil.printRef ("XTRACTAB", reffiles)
        cosutil.printRef ("DISPTAB", reffiles)
        if info["detector"] == "FUV":
            cosutil.printRef ("BRFTAB", reffiles)

        xtractab = reffiles["xtractab"]
        disptab = reffiles["disptab"]
        wcptab = reffiles["wcptab"]
        if info["detector"] == "FUV":
            # This array of flags indicates which events should be corrected.
            region_flags = fuvDopplerRegions (eta, info, xtractab)
            # Apply the orbital Doppler correction to the flagged events.
            dopp[:] = N.where (region_flags, \
                               dopplerCorrection (events.field ("time"),
                                           xi, info, reffiles),
                               xi)
        else:
            region_flags_dict = nuvPsaRegions (eta, info, xtractab)
            dopp[:] = xi
            for stripe in ["NUVA", "NUVB", "NUVC"]:
                dopp[:] = N.where (region_flags_dict[stripe], \
                                   dopplerCorrection (events.field ("time"),
                                           xi, info, reffiles, stripe=stripe),
                                   dopp)

        # Copy to xfull in case wavecal processing will not be done.
        xi_full[:] = dopp.copy()

        phdr["doppcorr"] = "COMPLETE"

def fuvDopplerRegions (eta, info, xtractab):
    """Determine the region over which Doppler shift should be applied.

    This version is for FUV data.

    @param eta: pixel coordinates in cross-dispersion direction
    @type eta: array
    @param info: keywords and values
    @type info: dictionary
    @param xtractab: name of spectral extraction parameters reference table
    @type xtractab: string

    @return: True for events that are within the region for which it would be
        appropriate to apply Doppler correction
    @rtype: Boolean array
    """

    global active_area

    region_flags = active_area.copy()

    # Protect against the possibility that the aperture keyword is "WCA".
    if info["aperture"] == "BOA":
        aperture = "BOA"
    else:
        aperture = "PSA"
    filter = {"opt_elem": info["opt_elem"], "cenwave": info["cenwave"],
              "segment": info["segment"], "aperture": aperture}
    middle = float (FUV_X) / 2.

    # The computation of the 'boundary' variable makes an assumption
    # about the relative locations of the PSA and WCA regions on the
    # detectors.  The PSA spectral region is at lower Y pixel numbers.

    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_psa = xtract_info.field ("b_spec")[0] + \
                 xtract_info.field ("slope")[0] * middle

    filter["aperture"] = "WCA"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_wca = xtract_info.field ("b_spec")[0] + \
                 xtract_info.field ("slope")[0] * middle

    boundary = int (round ((b_spec_psa + b_spec_wca) / 2.))

    region_flags &= (eta < boundary)

    return region_flags

def dopplerCorrection (time, xi, info, reffiles, stripe=None):
    """Apply orbital and heliocentric Doppler correction.

    @param time: times of events (seconds)
    @type time: numpy array
    @param xi: pixel coordinates of events, in dispersion direction
    @type xi: numpy array
    @param info: keywords and values
    @type info: dictionary
    @param reffiles: dictionary of reference file names
    @type reffiles: dictionary
    @param stripe: name of NUV stripe ("NUVA", "NUVB", "NUVC"), or None for FUV
    @type stripe: string

    @return: array of Doppler-corrected X pixel coordinates
    @rtype: numpy array
    """

    disptab = reffiles["disptab"]

    # Compute the wavelength and dispersion at each pixel.
    filter = {"opt_elem": info["opt_elem"],
              "cenwave": info["cenwave"],
              "aperture": info["aperture"]}
    if stripe is None:
        filter["segment"] = info["segment"]
    else:
        filter["segment"] = stripe
    # If the FPOFFSET column is present in the disptab, include fpoffset
    # in the filter.
    fpoffset_present = cosutil.findColumn (disptab, "fpoffset")
    if fpoffset_present:
        filter["fpoffset"] = info["fpoffset"]
    disp_info = cosutil.getTable (disptab, filter, exactly_one=True)

    ncoeff = disp_info.field ("nelem")[0]
    coeff = disp_info.field ("coeff")[0][0:ncoeff]
    if cosutil.findColumn (disp_info, "delta"):
        delta = disp_info.field ("delta")[0]
    else:
        delta = 0.

    xi = xi.astype (N.float64)
    if fpoffset_present:
        # Compute wavelength and dispersion at each element of xi.
        wavelength = cosutil.evalDisp (xi, coeff, delta)
        dispersion = cosutil.evalDerivDisp (xi, coeff, delta)
    else:
        # Correct for fpoffset when computing wavelength and dispersion
        # (a feature will be at larger pixel number if fpoffset is larger,
        # so the wavelength at a given pixel will be smaller).
        wcp_info = cosutil.getTable (reffiles["wcptab"],
                                     filter={"opt_elem": info["opt_elem"]},
                                     exactly_one=True)
        stepsize = wcp_info.field ("stepsize")[0]
        wavelength = cosutil.evalDisp (xi, coeff, delta)
        xi_temp = xi - info["fpoffset"] * stepsize
        wavelength = cosutil.evalDisp (xi_temp, coeff, delta)
        dispersion = cosutil.evalDerivDisp (xi_temp, coeff, delta)
        del xi_temp, wcp_info

    # Apply the Doppler correction to the pixel coordinates.
    xd = orbitalDoppler (time, xi, wavelength, dispersion, info["expstart"],
                         info["doppmagv"], info["doppzero"], info["orbitper"])

    return xd

def orbitalDoppler (time, xi, wavelength, dispersion, expstart,
                    doppmag_v, doppzero, orbitper):
    """Apply Doppler correction for HST orbital motion.

    @param time: times of events (seconds)
    @type time: numpy array
    @param xi: pixel coordinates of events, in dispersion direction
    @type xi: numpy array
    @param wavelength: wavelengths corresponding to xi (Angstroms)
    @type wavelength: numpy array
    @param dispersion: dispersion at each element of xi (Angstroms/pixel)
    @type dispersion: numpy array
    @param expstart: exposure start time (MJD)
    @type expstart: float
    @param doppmag_v: magnitude of Doppler shift (km/s)
    @type doppmag_v: float
    @param doppzero: time when orbital Doppler shift is zero and increasing
        (MJD)
    @type doppzero: float
    @param orbitper: orbital period of HST (seconds)
    @type orbitper: float
    """

    # t is the time of each event in seconds since doppzero.
    t = (expstart - doppzero) * SEC_PER_DAY + time.astype (N.float64)

    shift = doppmag_v / SPEED_OF_LIGHT * wavelength / dispersion * \
            N.sin (2. * N.pi * t / orbitper)

    return xi - shift

def initHelcorr (events, info, switches, hdr):
    """Compute the radial velocity and update the V_HELIO keyword.

    @param events: the data unit containing the events table
    @type events: record array
    @param info: dictionary of header keywords and values
    @type info: dictionary
    @param reffiles: dictionary of reference file names
    @type reffiles: dictionary
    @param hdr: the events extension header
    @type hdr: pyfits Header object
    """

    if info["obstype"] != "SPECTROSCOPIC":
        return

    # get midpoint of exposure, MJD
    expstart = info["expstart"]
    time = events.field ("time")
    t_mid = expstart + (time[0] + time[len(time)-1]) / 2. / SEC_PER_DAY

    # Compute radial velocity and heliocentric correction factor.
    radvel = heliocentricVelocity (t_mid, info["ra_targ"], info["dec_targ"])
    helio_factor = -radvel  / SPEED_OF_LIGHT
    hdr.update ("v_helio", radvel)
    info["v_helio"] = radvel

def heliocentricVelocity (t, ra_targ, dec_targ):
    """Compute heliocentric radial velocity.

    This is copied from the code for calstis, except that the target
    coordinates will not be precessed to the time of observation.

    @param t: time (MJD)
    @type t: float
    @param ra_targ: right ascension of the target (J2000)
    @type ra_targ: float
    @param dec_targ: declination of the target (J2000)
    @type dec_targ: float

    @return: the contribution of the Earth's velocity around the Sun to the
        radial velocity of the target, in km/s; if the Earth is approaching
        the target, this will be negative (i.e. the sign convention is that
        radial velocity is positive if the distance between the Earth and
        the target is increasing)
    @rtype: float
    """

    REFDATE = 51544.5           # MJD for 2000 Jan 1.5 UT, or JD 2451545.0
    KM_AU   = 1.4959787e8       # astronomical unit in kilometers
    SEC_DAY = 86400.            # seconds per day
    
    deg_to_rad = math.pi / 180.
    eps = 23.439 * deg_to_rad           # obliquity of Earth's axis
    orb_v = 29.786                      # speed of Earth around Sun, km/s

    ra  = ra_targ * deg_to_rad
    dec = dec_targ * deg_to_rad

    # target will be a unit vector toward the target;
    # velocity will be Earth's orbital velocity in km/s.
    target = [0., 0., 0.]
    velocity = [0., 0., 0.]

    target[0] = math.cos (dec) * math.cos (ra)
    target[1] = math.cos (dec) * math.sin (ra)
    target[2] = math.sin (dec)

    # Precess the target coordinates to time t.
    # target = cosutil.precess (t, target)        # note, commented out

    dt = t - REFDATE                    # days since 2000 Jan 1, 12h UT

    g_dot = 0.9856003 * deg_to_rad
    l_dot = 0.9856474 * deg_to_rad

    eps = (23.439 - 0.0000004 * dt) * deg_to_rad

    g = mod2pi ((357.528 + 0.9856003 * dt) * deg_to_rad)
    l = mod2pi ((280.461 + 0.9856474 * dt) * deg_to_rad)

    #       L   1.915 deg                 0.02 deg
    elong = l + 0.033423 * math.sin (g) + 0.000349 * math.sin (2.*g)
    elong_dot = l_dot + \
                0.033423 * math.cos (g) * g_dot + \
                0.000349 * math.cos (2.*g) * 2.*g_dot

    radius = 1.00014 - 0.01671 * math.cos (g) - 0.00014 * math.cos (2.*g)
    radius_dot =       0.01671 * math.sin (g) * g_dot + \
                       0.00014 * math.sin (2.*g) * 2.*g_dot

    x_dot = radius_dot * math.cos (elong) - \
                radius * math.sin (elong) * elong_dot

    y_dot = radius_dot * math.cos (eps) * math.sin (elong) + \
                radius * math.cos (eps) * math.cos (elong) * elong_dot

    z_dot = radius_dot * math.sin (eps) * math.sin (elong) + \
                radius * math.sin (eps) * math.cos (elong) * elong_dot

    velocity[0] = -x_dot * KM_AU / SEC_DAY
    velocity[1] = -y_dot * KM_AU / SEC_DAY
    velocity[2] = -z_dot * KM_AU / SEC_DAY

    dot_product = velocity[0] * target[0] + \
                  velocity[1] * target[1] + \
                  velocity[2] * target[2]
    radvel = -dot_product

    return radvel

def mod2pi (x):
    """Return the argument modulo two pi."""

    (f, i) = math.modf (x / (2.*math.pi))
    if f < 0.:
        f += 1.
    return f * 2. * math.pi

def doFlatcorr (events, info, switches, reffiles, phdr):
    """Apply flat field correction.

    arguments:
    events        the data unit containing the events table
    info          dictionary of header keywords and values
    switches      dictionary of calibration switches
    reffiles      dictionary of reference file names
    phdr          the input primary header
    """

    cosutil.printSwitch ("FLATCORR", switches)

    if switches["flatcorr"] == "PERFORM":

        cosutil.printRef ("FLATFILE", reffiles)

        fd = pyfits.open (reffiles["flatfile"], mode="readonly")

        if info["detector"] == "NUV":
            hdu = fd[1]
        else:
            hdu = fd[(info["segment"],1)]
        flat = hdu.data

        origin_x = hdu.header.get ("origin_x", 0)
        origin_y = hdu.header.get ("origin_y", 0)

        if info["obsmode"] == "ACCUM":
            if info["obstype"] == "SPECTROSCOPIC":
                cosutil.printSwitch ("DOPPCORR", switches)
            if switches["doppcorr"] == "PERFORM":
                convolveFlat (flat, info["dispaxis"], \
                     info["expstart"], info["exptime"],
                     info["dopmagt"], info["dopzerot"], info["orbtpert"])
                phdr["doppcorr"] = "COMPLETE"

        ccos.applyflat (events.field (xcorr), events.field (ycorr),
                        events.field ("epsilon"), flat, origin_x, origin_y)

        fd.close()

        phdr["flatcorr"] = "COMPLETE"

def convolveFlat (flat, dispaxis,
                expstart, exptime, dopmagt, dopzerot, orbtpert):
    """Convolve the flat field file with the Doppler smearing function.

    arguments:
    flat       flat field data array, modified in-place
    dispaxis   dispersion axis (1 or 2)
    expstart   exposure start time, MJD
    exptime    exposure duration, seconds
    dopmagt    magnitude of Doppler shift, pixels
    dopzerot   time when Doppler shift is zero and increasing
    orbtpert   orbital period of HST
    """

    # Round dopmagt up to the next integer; mag is a zero-point offset.
    mag = int (math.ceil (dopmagt))

    # dopp will be the Doppler smoothing function, normalized so its sum is 1.
    dopp = N.zeros (2*mag+1, dtype=N.float32)

    # t is the time in seconds since dopzerot, in one second increments.
    t = N.arange (int (round (exptime)), dtype=N.float32) + \
               (expstart - dopzerot) * SEC_PER_DAY

    # shift is in pixels (wavelengths increase toward larger pixel number).
    shift = -dopmagt * N.sin (2. * N.pi * t / orbtpert)

    # Construct the Doppler smoothing function.
    npts = round (exptime)
    increment = 1. / npts
    npts = int (npts)
    for i in range (npts):                      # one-second increments
        ishift = int (round (shift[i])) + mag
        assert ishift >= 0 and ishift <= 2*mag
        dopp[ishift] += increment

    # Do the convolution (in-place).
    axis = 2 - dispaxis         # 1 --> 1,  2 --> 0
    ccos.convolve1d (flat, dopp, axis)

def doDeadcorr (events, input, info, switches, reffiles, phdr,
            stim_countrate, stim_livetime, livetimefile):
    """Correct for deadtime.

    arguments:
    events          the data unit containing the events table
    input           name of raw file (for writing to livetimefile)
    info            dictionary of header keywords and values
    switches        dictionary of calibration switches
    reffiles        dictionary of reference file names
    phdr            the input primary header
    livetimefile    name of output text file for livetime factors (or None)
    stim_countrate  the observed count rate for a stim (for info)
    stim_livetime   live time computed from the input and observed stim rate
    """

    cosutil.printSwitch ("DEADCORR", switches)
    if switches["deadcorr"] == "PERFORM":
        cosutil.printRef ("DEADTAB", reffiles)
        if info["obsmode"] == "TIME-TAG":
            (dead_rate, dead_method) = deadtimeCorrection (
                          events, reffiles["deadtab"], info,
                          stim_countrate, stim_livetime,
                          input, livetimefile)
        else:
            (dead_rate, dead_method) = deadtimeCorrectionAccum (
                          events, reffiles["deadtab"], info,
                          stim_countrate, stim_livetime,
                          input, livetimefile)
        if info["detector"] == "FUV":
            if info["segment"] == "FUVA":
                phdr.update ("deadrt_a", dead_rate)
                phdr.update ("deadmt_a", dead_method)
            else:
                phdr.update ("deadrt_b", dead_rate)
                phdr.update ("deadmt_b", dead_method)
        else:
            phdr.update ("deadrt", dead_rate)
            phdr.update ("deadmt", dead_method)
        phdr["deadcorr"] = "COMPLETE"

def deadtimeCorrection (events, deadtab, info,
                        stim_countrate, stim_livetime,
                        input, livetimefile):
    """Compute and apply livetime factor to correct for dead time.

    Calculate one livetime factor from the count rate averaged over
    the whole exposure.
    Calculate another livetime factor from keyword deventa, deventb
    or mevents.
    if there are subarrays:
        if the difference between the two livetime factors is > 10%:
            Use the livetime factor based on the keyword (the same
            factor for all events).
        else:
            Calculate and apply the livetime factor based on the
            actual count rate within each TIMESTEP time interval.
    else no subarrays:
        if the difference between the two livetime factors is > 10%:
            Print a warning.
        Calculate and apply the livetime factor based on the actual
        count rate within each TIMESTEP time interval.

    @param events: the data unit containing the events table
    @type events: pyfits record array
    @param deadtab: name of reference table of count rates and livetime factors
    @type deadtab: string
    @param info: header keywords and values
    @type info: dictionary
    @param stim_countrate: the observed count rate for the stims
    @type stim_countrate: float
    @param stim_livetime: livetime computed from the stims
    @type stim_livetime: float
    @param input: name of input raw file (for writing to livetimefile)
    @type input: string
    @param livetimefile: name of output text file for livetime factors (or None)
    @type livetimefile: string

    @return: the count rate used for determining the livetime factor, and
        a string that indicates which method was used for determining the
        livetime factor
    @rtype: tuple
    """

    if livetimefile is None:
        fd = None
    else:
        fd = open (livetimefile, "a")

    # dec_countrate is the count rate from the digital event counter.
    segment = info["segment"]
    dec_countrate = info["countrate"]

    time = cosutil.getColCopy (data=events, column="time")
    epsilon = events.field ("epsilon")
    nevents = len (time)

    live_info = cosutil.getTable (deadtab, filter={"segment": segment},
                                  at_least_one=True)
    # These are the values in the deadtab table columns.
    obs_rate = live_info.field ("obs_rate")
    live_factor = live_info.field ("livetime")

    # This livetime value is based on count rate over the entire exposure.
    if time[nevents-1] > time[0]:
        actual_countrate = float (nevents) / (time[nevents-1] - time[0])
    else:
        actual_countrate = 0.
    actual_rate_livetime = determineLivetime (actual_countrate,
                                              obs_rate, live_factor)

    # dec_countrate is from DEVENTA, DEVENTB or from MEVENTS.
    dec_livetime = determineLivetime (dec_countrate, obs_rate, live_factor)

    print_details = (cosutil.checkVerbosity (VERY_VERBOSE))     # initial value
    if abs (dec_livetime - actual_rate_livetime) > \
            LIVETIME_CRITERION * actual_rate_livetime:
        cosutil.printWarning ("livetime estimates differ.")
        if info["subarray"] and info["nsubarry"] > 0:   # are there subarrays?
            use_actual_rate = False
        else:
            use_actual_rate = True
        print_details = True
    else:
        use_actual_rate = True

    if use_actual_rate:
        livetime_source = "actual count rate"
        dead_rate = actual_countrate
        dead_method = "DATA"
    else:
        dead_rate = dec_countrate
        if info["detector"] == "FUV":
            dead_method = "DEVENT"
            keyword = "DEVENT" + info["segment"][-1]
        else:
            dead_method = "MEVENTS"
            keyword = "MEVENTS"
        livetime_source = "digital event counter (%s)" % keyword

    if print_details:
        printLiveInfo (segment, stim_countrate, stim_livetime,
                       actual_countrate, actual_rate_livetime,
                       dec_countrate, dec_livetime, livetime_source)
    if fd is not None:
        printLiveInfo (segment, stim_countrate, stim_livetime,
                       actual_countrate, actual_rate_livetime,
                       dec_countrate, dec_livetime, livetime_source, fd=fd)

    if use_actual_rate:

        if fd is not None:
            fd.write ("# %s\n" % input)
            fd.write ("# t0 t1 countrate livetime\n")

        # Use counts over dt_deadtime seconds to compute livetime.
        fd_dead = pyfits.open (deadtab, mode="readonly")
        dt_deadtime = fd_dead[1].header["timestep"]
        fd_dead.close()
        cosutil.printMsg ("Compute livetime factor; timestep is %.6g s:" \
                      % dt_deadtime, VERY_VERBOSE)

        t0 = time[0]
        t1 = t0 + dt_deadtime
        last_time = time[nevents-1]
        cosutil.printMsg ("  time range    rate   livetime", VERY_VERBOSE)
        last_livetime = 1.      # use this for saving previous value
        countrate = 0.
        first = True
        while t0 < last_time:

            # time[i:j] matches t0 to t1.
            try:
                (i, j) = ccos.range (time, t0, t1)
            except:
                t0 = t1
                t1 = t0 + dt_deadtime
                continue
            t1_for_printing = t1        # may be changed below
            if i >= j:          # i and j can be equal due to roundoff
                t0 = t1
                t1 = t0 + dt_deadtime
                continue

            if t1 < last_time:
                countrate = (j - i) / dt_deadtime
                livetime = determineLivetime (countrate, obs_rate, live_factor)
            elif t0 < last_time:
                t1_for_printing = last_time
                if (last_time - t0) < 0.5 * dt_deadtime and not first:
                    livetime = last_livetime
                    cosutil.printMsg ("Last time interval is short (%.6g s),"
                                      " so previous livetime will be used." %
                                      (last_time - t0,))
                else:
                    countrate = (j - i) / (last_time - t0)
                    livetime = determineLivetime (countrate,
                                                  obs_rate, live_factor)
            else:
                countrate = 0.
                livetime = 1.
            if livetime > 0.:
                epsilon[i:j] = epsilon[i:j] / livetime
                last_livetime = livetime
            first = False

            if fd is not None:
                fd.write ("%.0f %.0f %.6g %.6g\n" %
                          (t0, t1_for_printing, countrate, livetime))
            cosutil.printMsg ("%6.1f %6.1f   %.6g %.6g" %
                              (t0, t1_for_printing, countrate, livetime),
                              VERY_VERBOSE)

            t0 = t1
            t1 = t0 + dt_deadtime

    else:
        epsilon[:] = epsilon / dec_livetime

    if fd is not None:
        fd.close()

    return (dead_rate, dead_method)

def deadtimeCorrectionAccum (events, deadtab, info,
                             stim_countrate, stim_livetime,
                             input, livetimefile):
    """Determine and apply the livetime factor for ACCUM data.

    If there are subarrays, the livetime factor is gotten from the digital
    event counter.  If there are no subarrays, the livetime factor is based
    on the actual count rate.

    @param events: the data unit containing the events table
    @type events: pyfits record array
    @param deadtab: name of reference table of count rates and livetime factors
    @type deadtab: string
    @param info: header keywords and values
    @type info: dictionary
    @param stim_countrate: the observed count rate for the stims
    @type stim_countrate: float
    @param stim_livetime: livetime computed from the stims
    @type stim_livetime: float
    @param input: name of input raw file (for writing to livetimefile)
    @type input: string
    @param livetimefile: name of output text file for livetime factors (or None)
    @type livetimefile: string

    @return: the count rate used for determining the livetime factor, and
        a string that indicates which method was used for determining the
        livetime factor
    @rtype: tuple
    """

    if livetimefile is None:
        fd = None
    else:
        fd = open (livetimefile, "a")
        fd.write ("# %s\n" % (input,))

    # This is the column that will be modified in-place.
    epsilon = events.field ("epsilon")
    ncounts = len (epsilon)

    live_info = cosutil.getTable (deadtab, filter={"segment": info["segment"]},
                                  at_least_one=True)
    obs_rate = live_info.field ("obs_rate")
    live_factor = live_info.field ("livetime")

    # keyword used if print_details is true or we're writing to a livetimefile.
    if info["segment"] == "FUVA":
        keyword = "DEVENTA"
    elif info["segment"] == "FUVB":
        keyword = "DEVENTB"
    else:
        keyword = "MEVENTS"

    # Output count rate from digital event counter (DEC), and corresponding
    # livetime factor.
    dec_countrate = info["countrate"]
    dec_livetime = determineLivetime (dec_countrate, obs_rate, live_factor)

    if info["exptime"] <= 0.:
        cosutil.printWarning ("Can't do deadcorr, exptime = %.6g." %
                              info["exptime"])
        return (0., "SKIPPED")
    actual_countrate = float (ncounts) / info["exptime"]
    actual_rate_livetime = determineLivetime (actual_countrate,
                                              obs_rate, live_factor)

    if info["subarray"]:
        livetime_source = "digital event counter (%s)" % keyword
        livetime = dec_livetime
        dead_rate = dec_countrate
        if info["detector"] == "FUV":
            dead_method = "DEVENT"
        else:
            dead_method = "MEVENTS"
    else:
        livetime_source = "actual count rate"
        livetime = actual_rate_livetime
        dead_rate = actual_countrate
        dead_method = "DATA"

    epsilon[:] = epsilon / livetime

    print_details = (cosutil.checkVerbosity (VERY_VERBOSE))     # initial value

    if abs (dec_livetime - actual_rate_livetime) > \
            LIVETIME_CRITERION * actual_rate_livetime:
        cosutil.printWarning ("livetime estimates differ.")
        print_details = True

    if print_details:
        cosutil.printMsg ("  actual countrate and livetime:  %.6g, %6.4f" % \
                          (actual_countrate, actual_rate_livetime))
        cosutil.printMsg ("  countrate and livetime from %s:  %.6g, %6.4f" % \
                          (keyword, dec_countrate, dec_livetime))
        cosutil.printMsg ("Livetime %6.4f is based on %s." % \
                          (livetime, livetime_source))
        if info["detector"] == "FUV":
            if stim_countrate is None:
                cosutil.printMsg (
                "  stim countrate and livetime could not be determined")
            else:
                cosutil.printMsg (
                "  stim countrate and livetime:  %.6g, %6.4f" % \
                                  (stim_countrate, stim_livetime))

    if fd is not None:
        fd.write ("actual countrate and livetime:  %.6g, %6.4f\n" %
                  (actual_countrate, actual_rate_livetime))
        fd.write ("countrate and livetime from %s:  %.6g, %6.4f\n" %
                  (keyword, dec_countrate, dec_livetime))
        fd.write ("livetime %6.4f is based on %s.\n" % \
                  (livetime, livetime_source))
        if info["detector"] == "FUV":
            if stim_countrate is None:
                fd.write (
                "stim countrate and livetime could not be determined\n")
            else:
                fd.write ("stim countrate and livetime:  %.6g, %6.4f\n" %
                          (stim_countrate, stim_livetime))

    if fd is not None:
        fd.close()

    return (dead_rate, dead_method)

def printLiveInfo (segment, stim_countrate, stim_livetime,
                   actual_countrate, actual_rate_livetime,
                   dec_countrate, dec_livetime, livetime_source, fd=None):
    """Print or write information about livetime.

    arguments:
    segment         segment name (for setting keyword name for DEC count rate)
    stim_countrate  the observed count rate for the stims, or None
    stim_livetime   livetime factor computed from the input and observed
                      stim rate
    actual_countrate       observed count rate, from events table
    actual_rate_livetime   livetime factor derived from countrate
    dec_countrate   the count rate from the digital event counter
    dec_livetime    livetime factor computed from dec_countrate
    livetime_source a string saying whether actual rate or DEC was used
                      for computing the livetime factor
    fd              None if printing to trailer; an fd for printing to a
                      log file
    """

    if segment == "FUVA":
        keyword = "DEVENTA"
    elif segment == "FUVB":
        keyword = "DEVENTB"
    else:
        keyword = "MEVENTS"

    messages = []
    if segment == "FUVA" or segment == "FUVB":
        if stim_countrate is None:
            messages.append (
                "stim countrate and livetime could not be determined")
        else:
            messages.append ("stim countrate and livetime:  %.6g, %6.4f" %
                             (stim_countrate, stim_livetime))
    messages.append ("actual (average) event rate and livetime:  %.6g, %6.4f" %
                     (actual_countrate, actual_rate_livetime))
    messages.append ("countrate and livetime from %s:  %.6g, %6.4f" %
                     (keyword, dec_countrate, dec_livetime))
    messages.append ("Livetime is based on %s." % livetime_source)

    if fd is None:
        for msg in messages:
            cosutil.printMsg (msg)
    else:
        fd.write ("\n")
        for msg in messages:
            fd.write (msg + "\n")

def determineLivetime (countrate, obs_rate, live_factor):
    """Compute livetime factor from observed count rate.

    This is just linear interpolation in live_factor vs obs_rate.

    arguments:
    countrate     observed count rate
    obs_rate      list of observed count rates, from deadtab reference table
    live_factor   list of livetime factors corresponding to obs_rate, from
                    deadtab

    The function value is the interpolated livetime factor.
    """

    n = len (obs_rate)

    if countrate <= 0.:
        livetime = 1.
    elif n == 1:
        livetime = live_factor[0]
    elif countrate < obs_rate[0]:
        livetime = 1.
    elif countrate >= obs_rate[n-1]:
        livetime = live_factor[n-1]
    else:
        # Find the interval containing the observed count rate, and interpolate.
        for i in range (n-1):
            if countrate < obs_rate[i+1]:
                p = (countrate - obs_rate[i]) / (obs_rate[i+1] - obs_rate[i])
                q = 1. - p
                livetime = live_factor[i] * q + live_factor[i+1] * p
                break

    return livetime

def writeNull (input, output, outcounts, outcsum, info, phdr, events_hdu):
    """Write output files; images will have null data portions.

    The outtag file has already been written, so we only need to write
    the output and outcounts files.

    arguments:
    input         name of input file
    output        name of the output file for flat-fielded count-rate image
    outcounts     name of the output file for count-rate image
    outcsum       name of the output image for OPUS to add to cumulative
                      image (or None)
    info          dictionary of header keywords and values
    phdr          primary header
    events_hdu    hdu for events extension
    """

    cosutil.printWarning ("No data in " + input)
    makeImage (outcounts, phdr, events_hdu.header, None, None, None)
    makeImage (output, phdr, events_hdu.header, None, None, None)
    if outcsum is not None:
        # pha has to be not None to get the correct dimensions for FUV.
        writeCsum (None, None, None, N.zeros (1, dtype=N.int8),
                   info["detector"], info["subarray"],
                   phdr, events_hdu.header, outcsum)

def writeImages (x, y, epsilon, dq,
                 phdr, hdr, dq_array, npix, x_offset, exptime,
                 outcounts=None, output=None):
    """Bin events to images, and write to output files.

    arguments:
    x, y          arrays of pixel coordinates of events
    epsilon       weight column
    dq            data quality column
    phdr          the input primary header
    hdr           the input events extension header
    dq_array      the data quality array
    npix          the array shape (ny, nx)
    x_offset      offset of the detector in a calibrated image
    exptime       the exposure time
    outcounts     name of the output file for count-rate image
    output        name of the output file for flat-fielded count-rate image
    """

    global serious_dq_flags

    # notation:
    # t = exposure time (exptime)
    # C = sum of counts
    # C_rate = C / t
    # E = C / flat_field, i.e. "effective" counts
    # E_rate = E / t
    # the corresponding error arrays are:
    # errC = sqrt (C)
    # errC_rate = sqrt (C) / t
    # errE = sqrt (C) / flat_field = sqrt (C) * (E / C) = E / sqrt (C)
    # errE_rate = errE / t = (E / sqrt (C)) / t
    #           = (E / t) / (sqrt (C) / t) / t
    #           =  E_rate / errC_rate / t

    if outcounts is not None:
        cosutil.printMsg ("writing file %s ..." % outcounts, VERY_VERBOSE)

    # First make an image array in which each input event counts as one,
    # i.e. ignoring flat field and deadtime corrections.
    C_rate = N.zeros (npix, dtype=N.float32)

    if exptime <= 0:
        cosutil.printWarning ("Exposure time is zero, so output files are dummy.")
        E_rate = C_rate.copy()
        errE_rate = C_rate.copy()
        if outcounts is not None:
            makeImage (outcounts, phdr, hdr, E_rate, errE_rate, dq_array)
        if output is not None:
            makeImage (output, phdr, hdr, E_rate, errE_rate, dq_array)
        return

    ccos.binevents (x, y, C_rate, x_offset, dq, serious_dq_flags)

    errC_rate = N.sqrt (C_rate) / exptime

    if outcounts is not None:
        C_rate /= exptime
        makeImage (outcounts, phdr, hdr, C_rate, errC_rate, dq_array)
    del C_rate                          # but we still need errC_rate

    if output is None:
        return                          # nothing further to do

    cosutil.printMsg ("writing file %s ..." % output, VERY_VERBOSE)

    # Make an image array where event number i has weight epsilon[i].
    E_rate = N.zeros (npix, dtype=N.float32)
    ccos.binevents (x, y, E_rate, x_offset, dq, serious_dq_flags, epsilon)

    # errC_rate will likely have a number of zero values, so we
    # have to set those to one before dividing.
    errC_rate = N.where (errC_rate == 0., 1., errC_rate)

    # convert from counts to count rate
    E_rate /= exptime
    errE_rate = E_rate / errC_rate / exptime
    del errC_rate

    makeImage (output, phdr, hdr, E_rate, errE_rate, dq_array)

def makeImage (outimage, phdr, hdr, sci_array, err_array, dq_array):
    """Write a FITS file, based on headers and data arrays.

    arguments:
    output        name of the output file to be written
    phdr          the input primary header
    hdr           the input events extension header
    sci_array     the science data array (may be None)
    err_array     the error estimates array (may be None)
    dq_array      the data quality array (may be None)
    """

    primary_hdu = pyfits.PrimaryHDU (header=phdr)
    fd = pyfits.HDUList (primary_hdu)
    fd[0].header["nextend"] = 3
    cosutil.updateFilename (fd[0].header, outimage)

    makeImageHDU (fd, hdr, sci_array, name="SCI")
    makeImageHDU (fd, hdr, err_array, name="ERR")
    makeImageHDU (fd, hdr, dq_array, name="DQ")

    fd.writeto (outimage, output_verify='silentfix')

def makeImageHDU (fd, table_hdr, data_array, name="SCI"):
    """Make an image hdu from data and a table header and append to fd.

    arguments:
    fd            pyfits object for FITS file (new hdu will be appended)
    table_hdr     a FITS Header object for a table
    data_array    image data to be appended (may be None)
    name          string to be used for EXTNAME
    """

    # Create an image header from the table header.
    imhdr = cosutil.tableHeaderToImage (table_hdr)
    if name == "DQ":
        imhdr.update ("BUNIT", "UNITLESS")
    else:
        imhdr.update ("BUNIT", "count /s")

    hdu = pyfits.ImageHDU (data=data_array, header=imhdr, name=name)
    fd.append (hdu)

def writeCsum (xcorr, ycorr, epsilon, pha, detector, subarray,
               phdr, hdr, outcsum):
    """Write the "calcos sum" (csum) image.

    @param xcorr: column for X coordinates of events
    @type xcorr: numpy array
    @param ycorr: column for Y coordinates of events
    @type ycorr: numpy array
    @param epsilon: column of weights for events
    @type epsilon: numpy array
    @param pha: column for pulse height amplitudes, or None if detector is NUV
    @type pha: numpy array
    @param detector: "FUV" or "NUV"
    @type detector: string
    @param subarray: True if the exposure used one or more subarrays
    @type subarray: boolean
    @param phdr: primary header from input file
    @type phdr: pyfits Header object
    @param hdr: first extension (EVENTS) header from input file
    @type hdr: pyfits Header object
    @param outcsum: name of output "calcos sum" file
    @type outcsum: string
    """

    # This is the number of possible values for the pulse height amplitude,
    # pha = 0..31.
    PHA_RANGE = 32

    cosutil.printMsg ("writing file %s ..." % outcsum, VERY_VERBOSE)

    primary_hdu = pyfits.PrimaryHDU (header=phdr)
    fd = pyfits.HDUList (primary_hdu)
    fd[0].header.update ("nextend", 1)
    fd[0].header.update ("filetype", "CALCOS SUM FILE")
    cosutil.updateFilename (fd[0].header, outcsum)

    # Copy the exposure time keywords to the output primary header.
    cosutil.copyExptimeKeywords (hdr, fd[0].header)

    # Copy the high-voltage keywords to the output primary header.
    cosutil.copyVoltageKeywords (hdr, fd[0].header, detector)

    # Copy the subarray keywords to the output primary header.
    cosutil.copySubKeywords (hdr, fd[0].header, subarray)

    ### This is an example for future reference.
    #if detector == "FUV":
    #    if pha is None:
    #        data = N.zeros ((FUV_Y, FUV_X), dtype=N.float32)
    #        if xcorr is not None:
    #            ccos.csum_2d (data, xcorr, ycorr, epsilon)
    #    else:
    #        data = N.zeros ((PHA_RANGE, FUV_Y, FUV_X), dtype=N.float32)
    #        if xcorr is not None:
    #            ccos.csum_3d (data, xcorr, ycorr, epsilon, pha.astype(N.int16))
    #else:
    #    data = N.zeros ((NUV_Y, NUV_X), dtype=N.float32)
    #    if xcorr is not None:
    #        ccos.csum_2d (data, xcorr, ycorr, epsilon)
    #fd[0].header.update ("counts", data.sum())
    #fd.append (pyfits.CompImageHDU (data, header=hdr, name="SCI",
    #                                compressionType="RICE_1",
    #                                quantizeLevel=-0.001))
    #  the arguments and their defaults are:
    # compressionType='RICE_1', # 'RICE_1', 'PLIO_1', 'GZIP_1', 'HCOMPRESS_1'
    # tileSize=None,                    # shape of tile, default is one row
    # hcompScale=0.,                    # unit is RMS of image
    # hcompSmooth=0,
    # quantizeLevel=16.                 # perhaps use quantizeLevel = -0.001
    #del data
    ###

    if detector == "FUV":
        if pha is None:
            fd.append (pyfits.ImageHDU (data=N.zeros ((FUV_Y, FUV_X),
                                                      dtype=N.float32),
                                        header=hdr, name="SCI"))
            if xcorr is not None:
                ccos.csum_2d (fd[1].data, xcorr, ycorr, epsilon)
        else:
            fd.append (pyfits.ImageHDU (data=N.zeros ((PHA_RANGE,
                                                             FUV_Y, FUV_X),
                                                      dtype=N.float32),
                                        header=hdr, name="SCI"))
            if xcorr is not None:
                ccos.csum_3d (fd[1].data, xcorr, ycorr, epsilon,
                              pha.astype(N.int16))
    else:
        fd.append (pyfits.ImageHDU (data=N.zeros ((NUV_Y, NUV_X),
                                                  dtype=N.float32),
                                    header=hdr, name="SCI"))
        if xcorr is not None:
            ccos.csum_2d (fd[1].data, xcorr, ycorr, epsilon)

    fd[0].header.update ("counts", fd[1].data.sum())
    fd[1].header.update ("BUNIT", "count")

    fd.writeto (outcsum, output_verify="silentfix")

def doStatflag (switches, output, outcounts):
    """Compute statistics and update keywords.

    arguments:
    output        name of the output file for flat-fielded count-rate image
    outcounts     name of the output file for count-rate image
    switches      dictionary of calibration switches
    """

    cosutil.printSwitch ("STATFLAG", switches)
    if switches["statflag"] == "PERFORM":
        cosutil.doImageStat (outcounts)
        cosutil.doImageStat (output)

def appendShift1 (outtag, output, outcounts, shift1_vs_time=None):
    """For tagflash data, append a table of shift1 vs time.

    @param outtag: name of the output corrtag table
    @type outtag: string
    @param output: name of the output flat-fielded count rate image file
    @type output: string
    @param outcounts: name of the output count rate image file
    @type outcounts: string
    @param shift1_vs_time: shift in dispersion dir. at one-second intervals
    @type shift1_vs_time: array, or None
    """

    if shift1_vs_time is None or len (shift1_vs_time) < 1:
        return

    col = []
    col.append (pyfits.Column (name="SHIFT1", format="1E", unit="pixel",
                               array=shift1_vs_time))
    cd = pyfits.ColDefs (col)
    hdu = pyfits.new_table (cd)
    hdu.header.update ("EXTNAME", "INFO", after="TFIELDS")
    hdu.header.update ("EXTVER", 1, after="EXTNAME")

    fd = pyfits.open (outtag, mode="update")
    fd.append (hdu)
    phdr = fd[0].header
    if phdr.has_key ("nextend"):
        phdr["nextend"] = phdr["nextend"] + 1
    fd.close()

    fd = pyfits.open (output, mode="update")
    fd.append (hdu)
    phdr = fd[0].header
    if phdr.has_key ("nextend"):
        phdr["nextend"] = phdr["nextend"] + 1
    fd.close()

    fd = pyfits.open (outcounts, mode="update")
    fd.append (hdu)
    phdr = fd[0].header
    if phdr.has_key ("nextend"):
        phdr["nextend"] = phdr["nextend"] + 1
    fd.close()

def flag_gti (time, dq, gti):
    """Flag events in dq that are outside any good time interval.

    arguments:
    time          the time column in the events table
    dq            the data quality column in the events table (updated in-place)
    gti           list of good time intervals
    """

    SMALL_INCR = 0.02           # smaller than the timestep of 0.032 s

    if len (gti) < 1 or len (time) < 1:
        return

    # Nothing to do if there is only one GTI and it covers the entire
    # time range.
    if len (gti) == 1 and \
      (time[0] >= gti[0][0] and time[-1] <= gti[0][1]):
        return

    dq[:] |= DQ_BAD_TIME

    for (t_start, t_stop) in gti:
        (i0, i1) = ccos.range (time, t_start, t_stop+SMALL_INCR)
        dq[i0:i1] &= ~DQ_BAD_TIME

def updateFromWavecal (events, wavecal_info,
                       info, switches, reffiles, phdr, hdr):
    """Update XFULL and YFULL based on auto or GO wavecal info.

    @param events: the data unit containing the events table
    @type events: record array
    @param wavecal_info: when wavecal exposures were processed, the results
        were stored in this dictionary
    @type wavecal_info: dictionary
    @param info: header keywords and values
    @type info: dictionary
    @param switches: calibration switches
    @type switches: dictionary
    @param reffiles: reference file names
    @type reffiles: dictionary
    @param phdr: the primary header (WAVECORR and WAVECALS can be updated)
    @type phdr: PyFITS Header object
    @param hdr: the events extension header (modified in-place)
    @type hdr: PyFITS Header object

    @return: three objects:  the average offset in the X direction, the
        average offset in the Y direction, and an array of the shifts in
        the dispersion direction at one-second intervals; these values
        will be (0., 0., None) if the current observation is a wavecal
        or if wavecal processing was not done.
    @rtype: tuple
    """

    global xcorr, ycorr, xdopp, ydopp, xfull, yfull
    global active_area

    # Read info from wavecal parameters table.
    wcp_info = cosutil.getTable (reffiles["wcptab"],
                       filter={"opt_elem": info["opt_elem"]},
                       exactly_one=True)
    wcp_info = wcp_info[0]

    xi  = events.field (xdopp)
    eta = events.field (ydopp)
    xi_full  = events.field (xfull)
    eta_full = events.field (yfull)

    # If the current exposure is a wavecal, or for a science exposure if
    # wavecal processing has not been done, there's nothing to do.
    if info["exptype"].find ("WAVE") >= 0 or not wavecal_info:
        return (0., 0., None)

    # Get the shifts in dispersion and cross-dispersion directions at the
    # start of the exposure.  If the science exposure was bracketed by
    # two wavecals, the slope of the shifts can be non-zero.
    shift_info = wavecal.returnWavecalShift (wavecal_info,
                        wcp_info, info["fpoffset"], info["expstart"])
    if shift_info is None:
        return (0., 0., None)

    (shift_dict, slope_dict, filename) = shift_info

    if info["detector"] == "FUV":
        segment_list = [info["segment"]]
    else:
        segment_list = ["NUVA", "NUVB", "NUVC"]
        psa_region_flags_dict = nuvPsaRegions (eta, info, reffiles["xtractab"])
        wca_region_flags_dict = nuvWcaRegions (eta, info, reffiles["xtractab"])

    time = events.field ("TIME")
    t0 = time[0]
    t_mid = (t0 + time[-1]) / 2.

    xi_full[:] = xi.copy()

    for segment in segment_list:

        key = "shift1" + segment[-1].lower()
        if not (shift_dict.has_key (key) and slope_dict.has_key (key)):
            cosutil.printError ("There is no wavecal for segment %s." % segment)
            return (0., 0., None)
        shift1_zero = shift_dict[key]
        shift1_slope = slope_dict[key]
        if info["detector"] == "FUV":
            xi_full[:] = N.where (active_area,
                           xi - ((time - t0) * shift1_slope + shift1_zero),
                           xi_full)
        else:
            xi_full[:] = N.where (psa_region_flags_dict[segment],
                           xi - ((time - t0) * shift1_slope + shift1_zero),
                           xi_full)
            xi_full[:] = N.where (wca_region_flags_dict[segment],
                           xi - ((time - t0) * shift1_slope + shift1_zero),
                           xi_full)
        avg_shift1 = shift1_slope * t_mid + shift1_zero
        key = "SHIFT1" + segment[-1]
        hdr.update (key, avg_shift1)

    if info["detector"] == "NUV":
        segment = "NUVB"

    key = "shift2" + segment[-1].lower()
    shift2_zero = shift_dict[key]
    shift2_slope = slope_dict[key]
    if info["detector"] == "FUV":
        eta_full[:] = N.where (active_area,
                        eta - ((time - t0) * shift2_slope + shift2_zero),
                        eta)
    else:
        # Use the same shift2 for every stripe.
        eta_full[:] = eta - ((time - t0) * shift2_slope + shift2_zero)

    key = "shift1" + segment[-1].lower()        # stripe B if NUV
    shift1_zero = shift_dict[key]
    shift1_slope = slope_dict[key]
    avg_dx = shift1_slope * t_mid + shift1_zero
    avg_dy = shift2_slope * t_mid + shift2_zero

    # These are one-second time bins, so we add 0.5 second to the array t
    # so the values of t will be the times at the middle of each interval.
    nbins = int (math.ceil (time[-1] - time[0]))
    t = N.arange (nbins, dtype=N.float64) + t0 + 0.5
    shift1_vs_time = shift1_slope * t + shift1_zero

    # Set the SHIFT2[A-C] keywords to the average offset in the
    # cross-dispersion direction.
    # Set DPIXEL1[A-C] to the average of the difference xfull minus the
    # nearest integer to xfull, where xfull is the column of that name;
    # this will be used when assigning wavelengths in extract.py.
    xi_diff = xi_full - N.around (xi_full)
    dpixel1 = xi_diff.mean()

    for segment in segment_list:
        key = "SHIFT2" + segment[-1]
        hdr.update (key, avg_dy)
        key = "DPIXEL1" + segment[-1]
        hdr.update (key, dpixel1)
    if info["opt_elem"] == "G230L" and info["cenwave"] == 3360:
        hdr.update ("SHIFT1C", 0.)
        hdr.update ("SHIFT2C", 0.)
        hdr.update ("DPIXEL1C", 0.)

    phdr["wavecorr"] = "COMPLETE"
    filename = cosutil.changeSegment (filename, info["detector"],
                                      info["segment"])
    phdr.update ("wavecals", filename)
    cosutil.printMsg ("Wavecal file(s) '%s'" % filename, VERBOSE)

    return (avg_dx, avg_dy, shift1_vs_time)

def nuvPsaRegions (eta, info, xtractab):
    """Determine the set of events within the NUV regions for the PSA.

    This is only needed for NUV data.

    @param eta: pixel coordinates in cross-dispersion direction
    @type eta: array
    @param info: keywords and values
    @type info: dictionary
    @param xtractab: name of spectral extraction parameters reference table
    @type xtractab: string

    @return: dictionary with stripe name ("NUVA", "NUVB", "NUVC") as the key
        and an array of Boolean flags as the value, true for events for which
        the Y coordinate is within the regions for the PSA.
    @rtype: dictionary of Boolean arrays
    """

    # segment will be added to the filter below.
    filter = {"opt_elem": info["opt_elem"], "cenwave": info["cenwave"],
              "aperture": "PSA"}
    middle = float (NUV_X) / 2.

    # b_spec_a, b_spec_b, b_spec_c, are the locations (at the middle of the
    # detector) of stripes A, B, C for the PSA, and b_spec_wca is the location
    # of stripe A for the WCA.

    filter["segment"] = "NUVA"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_a = xtract_info.field ("b_spec")[0] + \
               xtract_info.field ("slope")[0] * middle

    filter["segment"] = "NUVB"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_b = xtract_info.field ("b_spec")[0] + \
               xtract_info.field ("slope")[0] * middle

    filter["segment"] = "NUVC"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_c = xtract_info.field ("b_spec")[0] + \
               xtract_info.field ("slope")[0] * middle

    filter["segment"] = "NUVA"
    filter["aperture"] = "WCA"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_wca = xtract_info.field ("b_spec")[0] + \
                 xtract_info.field ("slope")[0] * middle

    # Set boundaries midway between adjacent stripes.
    boundary_a_b = round ((b_spec_a + b_spec_b) / 2.)
    boundary_b_c = round ((b_spec_b + b_spec_c) / 2.)
    boundary_c_wca = round ((b_spec_c + b_spec_wca) / 2.)

    region_flags_dict = {}
    region_flags_dict["NUVA"] = (eta < boundary_a_b)
    region_flags_dict["NUVB"] = (eta >= boundary_a_b) & (eta < boundary_b_c)
    region_flags_dict["NUVC"] = (eta >= boundary_b_c) & (eta < boundary_c_wca)

    return region_flags_dict

def nuvWcaRegions (eta, info, xtractab):
    """Determine the set of events within the NUV regions for the WCA.

    This is only needed for NUV data.

    @param eta: pixel coordinates in cross-dispersion direction
    @type eta: array
    @param info: keywords and values
    @type info: dictionary
    @param xtractab: name of spectral extraction parameters reference table
    @type xtractab: string

    @return: dictionary with stripe name ("NUVA", "NUVB", "NUVC") as the key
        and an array of Boolean flags as the value, true for events for which
        the Y coordinate is within the regions for the WCA.
    @rtype: dictionary of Boolean arrays
    """

    # aperture and segment will be added to the filter below.
    filter = {"opt_elem": info["opt_elem"], "cenwave": info["cenwave"]}
    middle = float (NUV_X) / 2.

    # b_spec_c is the location (at the middle of the detector) of stripe C
    # for the PSA, and b_spec_wca is the location
    # of stripe A for the WCA.

    filter["segment"] = "NUVC"
    filter["aperture"] = "PSA"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_c = xtract_info.field ("b_spec")[0] + \
               xtract_info.field ("slope")[0] * middle

    filter["aperture"] = "WCA"

    filter["segment"] = "NUVA"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_wca_a = xtract_info.field ("b_spec")[0] + \
                   xtract_info.field ("slope")[0] * middle

    filter["segment"] = "NUVB"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_wca_b = xtract_info.field ("b_spec")[0] + \
                   xtract_info.field ("slope")[0] * middle

    filter["segment"] = "NUVC"
    xtract_info = cosutil.getTable (xtractab, filter, exactly_one=True)
    b_spec_wca_c = xtract_info.field ("b_spec")[0] + \
                   xtract_info.field ("slope")[0] * middle

    # Set boundaries midway between adjacent stripes.
    boundary_c_wca = round ((b_spec_c + b_spec_wca_a) / 2.)
    boundary_a_b = round ((b_spec_wca_a + b_spec_wca_b) / 2.)
    boundary_b_c = round ((b_spec_wca_b + b_spec_wca_c) / 2.)

    region_flags_dict = {}
    region_flags_dict["NUVA"] = (eta >= boundary_c_wca) & (eta < boundary_a_b)
    region_flags_dict["NUVB"] = (eta >= boundary_a_b) & (eta < boundary_b_c)
    region_flags_dict["NUVC"] = (eta >= boundary_b_c)

    return region_flags_dict

def getWavecalOffsets (events):
    """Get min and max values of shift1 and shift2.

    @param events: the data unit containing the events table
    @type events: record array

    @return: (min_shift1, max_shift1, min_shift2, max_shift2), where
        min_shift1 and max_shift1 are the minimum and maximum values
        of the wavecal shift in the dispersion direction during the
        exposure (positive means a feature was detected at larger pixel
        coordinate than in the template); min_shift2 and max_shift2 are
        the corresponding values in the cross-dispersion direction
    @rtype: tuple
    """

    global active_area

    if active_area.any():
        xi  = events.field (xdopp)
        eta = events.field (ycorr)
        xi_full  = events.field (xfull)
        eta_full = events.field (yfull)

        xdiff = xi - xi_full
        ydiff = eta - eta_full
        xdiff = xdiff[active_area]
        ydiff = ydiff[active_area]

        min_shift1 = xdiff.min()
        max_shift1 = xdiff.max()
        min_shift2 = ydiff.min()
        max_shift2 = ydiff.max()
    else:
        # active_area is all False.
        min_shift1 = 0.
        max_shift1 = 0.
        min_shift2 = 0.
        max_shift2 = 0.

    return (min_shift1, max_shift1, min_shift2, max_shift2)

def copyColumns (events):
    """Copy XCORR and YCORR columns to XDOPP, XFULL and YFULL.

    Copy XCORR (RAWX) and YCORR (RAWY) to XDOPP, XFULL, YFULL as initial
    values, in case this is imaging data or wavecal processing will not
    be done.

    @param events: the data unit containing the events table
    @type events: record array
    """

    global xcorr, ycorr, xdopp, ydopp, xfull, yfull

    xi  = events.field (xcorr)
    eta = events.field (ycorr)
    xi_dopp  = events.field (xdopp)
    xi_full  = events.field (xfull)
    eta_full = events.field (yfull)

    xi_dopp[:] = xi.copy()
    xi_full[:] = xi.copy()
    eta_full[:] = eta.copy()
