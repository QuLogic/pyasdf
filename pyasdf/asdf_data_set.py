#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Python implementation of the Adaptable Seismic Data Format (ASDF).

:copyright:
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2013-2015
:license:
    BSD 3-Clause ("BSD New" or "BSD Simplified")
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

# Import ObsPy first as import h5py on some machines will some reset paths
# and lxml cannot be loaded anymore afterwards...
import obspy

import collections
import io
import itertools
import math
import os
import sys
import time
import warnings

import numpy as np
import h5py


# Minimum compatibility wrapper between Python 2 and 3.
try:
    filter = itertools.ifilter
except AttributeError:
    # Python 3 is a bit more aggressive when buffering warnings but here it
    # is fairly important that they are shown, thus we monkey-patch it to
    # flush stderr afterwards.
    def get_warning_fct():
        closure_warn = warnings.warn

        def __warn(self, *args, **kwargs):
            closure_warn(self, *args, **kwargs)
            sys.stderr.flush()

        return __warn

    warnings.warn = get_warning_fct()


from .header import ASDFException, ASDFWarning, COMPRESSIONS, FORMAT_NAME, \
    FORMAT_VERSION, MSG_TAGS, MAX_MEMORY_PER_WORKER_IN_MB, POISON_PILL
from .utils import is_mpi_env, StationAccessor, sizeof_fmt, ReceivedMessage,\
    pretty_receiver_log, pretty_sender_log, JobQueueHelper, StreamBuffer, \
    AuxiliaryDataGroupAccessor, AuxiliaryDataContainer, get_multiprocessing
from .inventory_utils import isolate_and_merge_station, merge_inventories


class ASDFDataSet(object):
    """
    Object dealing with Adaptable Seismic Data Format (ASDF).

    Central object of this Python package.
    """
    def __init__(self, filename, compression="gzip-3", debug=False,
                 mpi=None):
        """
        :type filename: str
        :param filename: The filename of the HDF5 file (to be).
        :type compression: str
        :param compression: The compression to use. Defaults to
            ``"gzip-3"`` which yielded good results in the past. Will
            only be applied to newly created data sets. Existing ones are not
            touched. Using parallel I/O will also disable compression as it
            is not possible to use both at the same time.

            **Available compressions choices (all of them are lossless):**

            * ``None``: No compression
            * ``"gzip-0"`` - ``"gzip-9"``: Gzip compression level 0  (worst
              but fast) to 9 (best but slow)
            * ``"lzf"``: LZF compression
            * ``"szip-ec-8"``: szip compression
            * ``"szip-ec-10"``: szip compression
            * ``"szip-nn-8"``: szip compression
            * ``"szip-nn-10"``: szip compression

        :type debug: bool
        :param debug: If True, print debug messages. Potentially very verbose.
        :param mpi: Force MPI on/off. Don't touch this unless you have a
            reason.
        :type mpi: bool
        """
        self.__force_mpi = mpi
        self.debug = debug

        # Deal with compression settings.
        if compression not in COMPRESSIONS:
            msg = "Unknown compressions '%s'. Available compressions: \n\t%s" \
                % (compression, "\n\t".join(sorted(
                    [str(i) for i in COMPRESSIONS.keys()])))
            raise Exception(msg)
        self.__compression = COMPRESSIONS[compression]
        # Turn off compression for parallel I/O. Any already written
        # compressed data will be fine.
        if self.__compression[0] and self.mpi:
            msg = "Compression will be disabled as parallel HDF5 does not " \
                  "support compression"
            warnings.warn(msg, ASDFWarning)
            self.__compression = COMPRESSIONS[None]

        # Open file or take an already open HDF5 file object.
        if not self.mpi:
            self.__file = h5py.File(filename, "a")
        else:
            self.__file = h5py.File(filename, "a", driver="mpio",
                                    comm=self.mpi.comm)

        # Workaround to HDF5 only storing the relative path by default.
        self.__original_filename = os.path.abspath(filename)

        # Write file format and version information to the file.
        if "file_format" in self.__file.attrs:
            if self.__file.attrs["file_format"].decode() != FORMAT_NAME:
                msg = "Not a '%s' file." % FORMAT_NAME
                raise ASDFException(msg)
            if "file_format_version" not in self.__file.attrs:
                msg = ("No file format version given for file '%s'. The "
                       "program will continue but the result is undefined." %
                       self.filename)
                warnings.warn(msg, ASDFWarning)
            elif self.__file.attrs["file_format_version"].decode() != \
                    FORMAT_VERSION:
                msg = ("The file '%s' has version number '%s'. The reader "
                       "expects version '%s'. The program will continue but "
                       "the result is undefined." % (
                           self.filename,
                           self.__file.attrs["file_format_version"],
                           FORMAT_VERSION))
                warnings.warn(msg, ASDFWarning)
        else:
            self.__file.attrs["file_format"] = \
                self._zeropad_ascii_string(FORMAT_NAME)
            self.__file.attrs["file_format_version"] = \
                self._zeropad_ascii_string(FORMAT_VERSION)

        # Create the waveform and provenance groups.
        if "Waveforms" not in self.__file:
            self.__file.create_group("Waveforms")
        if "Provenance" not in self.__file:
            self.__file.create_group("Provenance")
        if "AuxiliaryData" not in self.__file:
            self.__file.create_group("AuxiliaryData")

        # Easy access to the waveforms.
        self.waveforms = StationAccessor(self)
        self.auxiliary_data = AuxiliaryDataGroupAccessor(self)

        # Create the QuakeML data set if it does not exist.
        if "QuakeML" not in self.__file:
            self.__file.create_dataset("QuakeML", dtype=np.dtype("byte"),
                                       shape=(0,), maxshape=(None,),
                                       fletcher32=not bool(self.mpi))

        # Force synchronous init if run in an MPI environment.
        if self.mpi:
            self.mpi.comm.barrier()

    def __del__(self):
        """
        Cleanup. Force flushing and close the file.

        If called with MPI this will also enable MPI to cleanly shutdown in
        all cases.
        """
        try:
            self._flush()
            self._close()
        except (ValueError, TypeError, AttributeError):
            pass

    def __eq__(self, other):
        """
        More or less comprehensive equality check. Potentially quite slow as
        it checks all data.

        :type other:`~pyasdf.asdf_data_set.ASDFDDataSet`
        """
        if type(self) != type(other):
            return False
        if self._waveform_group.keys() != other._waveform_group.keys():
            return False
        if self._provenance_group.keys() != other._provenance_group.keys():
            return False
        if self.events != other.events:
            return False
        for station, group in self._waveform_group.items():
            other_group = other._waveform_group[station]
            for tag, data_set in group.items():
                other_data_set = other_group[tag]
                try:
                    if tag == "StationXML":
                        np.testing.assert_array_equal(data_set.value,
                                                      other_data_set.value)
                    else:
                        np.testing.assert_allclose(
                            data_set.value, other_data_set.value)
                except AssertionError:
                    return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def _flush(self):
        """
        Flush the underlying HDF5 file.
        """
        self.__file.flush()

    def _close(self):
        """
        Close the underlying HDF5 file.
        """
        self.__file.close()

    def _zeropad_ascii_string(self, text):
        """
        Returns a zero padded ASCII string in the most compatible way possible.

        Might later need to handle bytes/unicode.

        :param text: The text to be converted.
        """
        return np.string_((text + "\x00").encode())

    @property
    def _waveform_group(self):
        return self.__file["Waveforms"]

    @property
    def _provenance_group(self):
        return self.__file["Provenance"]

    @property
    def _auxiliary_data_group(self):
        return self.__file["AuxiliaryData"]

    @property
    def asdf_format_version(self):
        """
        Returns the version of the ASDF file.
        """
        return self.__file.attrs["file_format_version"].decode()

    @property
    def filename(self):
        """
        Get the path of the underlying file on the filesystem. Works in most
        circumstances.
        """
        return self.__original_filename

    @property
    def mpi(self):
        """
        Returns a named tuple with ``comm``, ``rank``, ``size``, and ``MPI``
        if run with MPI and ``False`` otherwise.
        """
        # Simple cache as this is potentially accessed a lot.
        if hasattr(self, "__is_mpi"):
            return self.__is_mpi

        if self.__force_mpi is True:
            self.__is_mpi = True
        elif self.__force_mpi is False:
            self.__is_mpi = False
        else:
            self.__is_mpi = is_mpi_env()

        # If it actually is an mpi environment, set the communicator and the
        # rank.
        if self.__is_mpi:

            # Check if HDF5 has been complied with parallel I/O.
            c = h5py.get_config()
            if not hasattr(c, "mpi") or not c.mpi:
                is_parallel = False
            else:
                is_parallel = True

            if not is_parallel:
                msg = "Running under MPI requires HDF5/h5py to be complied " \
                      "with support for parallel I/O."
                raise RuntimeError(msg)

            import mpi4py

            # This is not needed on most mpi4py installations.
            if not mpi4py.MPI.Is_initialized():
                mpi4py.MPI.Init()

            # Set mpi tuple to easy class wide access.
            mpi_ns = collections.namedtuple("mpi_ns", ["comm", "rank",
                                                       "size", "MPI"])
            comm = mpi4py.MPI.COMM_WORLD
            self.__is_mpi = mpi_ns(comm=comm, rank=comm.rank,
                                   size=comm.size, MPI=mpi4py.MPI)

        return self.__is_mpi

    @property
    def events(self):
        """
        Get all events stored in the data set.

        :rtype: An ObsPy :class:`~obspy.core.event.Catalog` object.
        """
        data = self.__file["QuakeML"]
        if not len(data.value):
            return obspy.core.event.Catalog()

        with io.BytesIO(data.value.tostring()) as buf:
            cat = obspy.readEvents(buf, format="quakeml")

        return cat

    @events.setter
    def events(self, event):
        """
        Set the events of the dataset.

        :param event: One or more events. Will replace all existing ones.
        :type event: :class:`~obspy.core.event.Event` or
            :class:`~obspy.core.event.Catalog`
        """
        if isinstance(event, obspy.core.event.Event):
            cat = obspy.core.event.Catalog(events=[event])
        elif isinstance(event, obspy.core.event.Catalog):
            cat = event
        else:
            raise TypeError("Must be an ObsPy event or catalog instance")

        with io.BytesIO() as buf:
            cat.write(buf, format="quakeml")
            buf.seek(0, 0)
            data = np.frombuffer(buf.read(), dtype=np.dtype("byte"))

        self.__file["QuakeML"].resize(data.shape)
        self.__file["QuakeML"][:] = data

    def add_auxiliary_data(self, data, data_type, tag, parameters,
                           provenance=None):
        """
        Adds auxiliary data to the file.

        :param data: The actual data as a n-dimensional numpy array.
        :param data_type: The type of data, think of it like a subfolder.
        :param tag: The tag of the data. Must be unique per data_type.
        :param parameters: Any additional options, as a Python dictionary.
        :param provenance:
        :return:
        """
        # Complicated multi-step process but it enables one to use
        # parallel I/O with the same functions.
        info = self._add_auxiliary_data_get_collective_information(
            data, data_type, tag, parameters, provenance)
        if info is None:
            return
        self._add_auxiliary_data_write_collective_information(info)
        self._add_auxiliary_data_write_independent_information(info, data)

    def _add_auxiliary_data_get_collective_information(
            self, data, data_type, tag, parameters, provenance=None):
        """
        The information required for the collective part of adding some
        auxiliary data.

        This will extract the group name, the parameters of the dataset to
        be created, and the attributes of the dataset.
        """
        group_name = "%s/%s" % (data_type, tag)
        if group_name in self._auxiliary_data_group:
            msg = "Data '%s' already exists in file. Will not be added!" % \
                  group_name
            warnings.warn(msg, ASDFWarning)
            return

        # XXX: Figure out why this is necessary. It should work according to
        # the specs.
        if self.mpi:
            fletcher32 = False
        else:
            fletcher32 = True

        info = {
            "data_name": group_name,
            "data_type": data_type,
            "dataset_creation_params": {
                "name": tag,
                "shape": data.shape,
                "dtype": data.dtype,
                "compression": self.__compression[0],
                "compression_opts": self.__compression[1],
                "fletcher32": fletcher32,
                "maxshape": (None,)
            },
            "dataset_attrs": parameters,
        }
        return info

    def _add_auxiliary_data_write_independent_information(self, info, data):
        """
        Writes the independent part of auxiliary data to the file.

        :param info:
        :param trace:
        :return:
        """
        self._auxiliary_data_group[info["data_name"]][:] = data

    def _add_auxiliary_data_write_collective_information(self, info):
        """
        Writes the collective part of auxiliary data to the file.

        :param info:
        :return:
        """
        data_type = info["data_type"]
        if data_type not in self._auxiliary_data_group:
            self._auxiliary_data_group.create_group(data_type)
        group = self._auxiliary_data_group[data_type]

        ds = group.create_dataset(**info["dataset_creation_params"])
        for key, value in info["dataset_attrs"].items():
            ds.attrs[key] = value

    def add_quakeml(self, event):
        """
        Adds a QuakeML file or an existing ObsPy event to the data set.

        An exception will be raised if an event is attempted to be added
        that already exists within the data set. Duplicates are detected
        based on the public ids of the events.

        :param event: Filename or existing ObsPy event object.
        :type event: :class:`~obspy.core.event.Event` or
            :class:`~obspy.core.event.Catalog`
        :raises: ValueError

        .. rubric:: Example

        For now we will create a new ASDF file but one can also use an
        existing one.

        >>> impory pyasdf
        >>> import obspy
        >>> ds = pyasdf.ASDFDataSet("new_file.h5")

        One can add an event either by passing a filename ...

        >>> ds.add_quakeml("/path/to/quake.xml")

        ... or by passing an existing event or catalog object.

        >>> cat = obspy.readEvents("/path/to/quakem.xml")
        >>> ds.add_quakeml(cat)
        """
        if isinstance(event, obspy.core.event.Event):
            cat = obspy.core.event.Catalog(events=[event])
        elif isinstance(event, obspy.core.event.Catalog):
            cat = event
        else:
            cat = obspy.readEvents(event, format="quakeml")

        old_cat = self.events
        existing_resource_ids = set([_i.resource_id.id for _i in old_cat])
        new_resource_ids = set([_i.resource_id.id for _i in cat])
        intersection = existing_resource_ids.intersection(new_resource_ids)
        if intersection:
            msg = ("Event id(s) %s already present in ASDF file. Adding "
                   "events cancelled")
            raise ValueError(msg % ", ".join(intersection))
        old_cat.extend(cat)

        self.events = old_cat

    def get_data_for_tag(self, station_name, tag):
        """
        Returns the waveform and station data for the requested station and
        tag.

        :param station_name: A string with network id and station id,
            e.g. ``"IU.ANMO"``
        :type station_name: str
        :param tag: The tag of the waveform.
        :type tag: str
        :return: tuple of the waveform and the inventory.
        :rtype: (:class:`~obspy.core.stream.Stream`,
                 :class:`~obspy.station.inventory.Inventory`)
        """
        station_name = station_name.replace(".", "_")
        station = getattr(self.waveforms, station_name)
        st = getattr(station, tag)
        inv = getattr(station, "StationXML")
        return st, inv

    def _get_waveform(self, waveform_name):
        """
        Retrieves the waveform for a certain tag name as a Trace object. For
        internal use only, use the dot accessors for outside access.
        """
        network, station, location, channel = waveform_name.split(".")[:4]
        channel = channel[:channel.find("__")]
        data = self.__file["Waveforms"]["%s.%s" % (network, station)][
            waveform_name]
        tr = obspy.Trace(data=data.value)
        # Starttime is a timestamp in nanoseconds.
        tr.stats.starttime = obspy.UTCDateTime(
            float(data.attrs["starttime"]) / 1.0E9)
        tr.stats.sampling_rate = float(data.attrs["sampling_rate"])
        tr.stats.network = network
        tr.stats.station = station
        tr.stats.location = location
        tr.stats.channel = channel
        # Set some format specific details.
        tr.stats._format = FORMAT_NAME
        details = obspy.core.util.AttribDict()
        setattr(tr.stats, FORMAT_NAME.lower(), details)
        details.format_version = FORMAT_VERSION

        # Read all the ids if they are there.
        ids = ["event_id", "origin_id", "magnitude_id", "focal_mechanism_id"]
        for name in ids:
            if name in data.attrs:
                setattr(details, name,
                        obspy.core.event.ResourceIdentifier(
                            data.attrs[name].tostring().decode()))
        return tr

    def _get_auxiliary_data(self, data_type, tag):
        group = self._auxiliary_data_group[data_type][tag]
        return AuxiliaryDataContainer(
            data=group, data_type=data_type, tag=tag,
            parameters={i: j for i, j in group.attrs.items()})

    def __str__(self):
        """
        Pretty string formatting.
        """
        filesize = sizeof_fmt(os.path.getsize(self.filename))
        ret = "{format} file [format version: {version}]: '{filename}' ({" \
              "size})".format(
                  format=FORMAT_NAME,
                  version=self.asdf_format_version,
                  filename=os.path.relpath(self.filename),
                  size=filesize)
        ret += "\n\tContains %i event(s)" % len(self.events)
        ret += "\n\tContains waveform data from {len} station(s).".format(
            len=len(self.__file["Waveforms"])
        )
        if len(self.auxiliary_data):
            ret += "\n\tContains %i type(s) of auxiliary data: %s" % (
                len(self.auxiliary_data),
                ", ".join(sorted(dir(self.auxiliary_data))))
        return ret

    def add_waveforms(self, waveform, tag, event_id=None, origin_id=None,
                      magnitude_id=None, focal_mechanism_id=None):
        """
        Adds one or more waveforms to the current ASDF file.

        :param waveform: The waveform to add. Can either be an ObsPy Stream or
            Trace object or something ObsPy can read.
        :type waveform: :class:`obspy.core.stream.Stream`,
            :class:`obspy.core.trace.Trace`, str, ...
        :param tag: The tag that will be given to all waveform files. It is
            mandatory for all traces and facilitates identification of the data
            within one ASDF volume. The ``"raw_record"`` tag is,
            by convention, reserved to raw, recorded, unprocessed data.
        :type tag: str
        :param event_id: The event or id which the waveform is associated
            with. This is useful for recorded data if a clear association is
            given, but also for synthetic data.
        :type event_id: :class:`obspy.core.event.Event`,
            :class:`obspy.core.event.ResourceIdentifier`, or str
        :param origin_id: The particular origin this waveform is associated
            with. This is mainly useful for synthetic data where the origin
            is precisely known.
        :type origin_id: :class:`obspy.core.event.Origin`,
            :class:`obspy.core.event.ResourceIdentifier`, or str
        :param magnitude_id: The particular magnitude this waveform is
            associated with. This is mainly useful for synthetic data where
            the magnitude is precisely known.
        :type magnitude_id: :class:`obspy.core.event.Magnitude`,
            :class:`obspy.core.event.ResourceIdentifier`, or str
        :param focal_mechanism_id: The particular focal mechanism this
            waveform is associated with. This is mainly useful for synthetic
            data where the mechanism is precisely known.
        :type focal_mechanism_id: :class:`obspy.core.event.FocalMechanism`,
            :class:`obspy.core.event.ResourceIdentifier`, or str

        .. rubric:: Examples

        We first setup an example ASDF file with a single event.

        >>> from pyasdf import ASDFDataSet
        >>> ds = ASDFDataSet("event_tests.h5")
        >>> ds.add_quakeml("quake.xml")
        >>> event = ds.events[0]

        Now assume we have a MiniSEED file that is an unprocessed
        observation of that earthquake straight from a datacenter called
        ``recording.mseed``. We will now add it to the file, give it the
        ``"raw_recording"`` tag (which is reserved for raw, recorded,
        and unproceseed data) and associate it with the event. Keep in mind
        that this association is optional.

        >>> ds.add_waveforms("recording.mseed", tag="raw_recording",
        ...                  event_id=event)

        It is also possible to directly add
        :class:`obspy.core.stream.Stream` objects containing an arbitrary
        number of :class:`obspy.core.trace.Trace` objects.

        >>> import obspy
        >>> st = obspy.read()  # Reads an example file without argument.
        >>> print(st)
        3 Trace(s) in Stream:
        BW.RJOB..EHZ | 2009-08-24T00:20:03.00Z - ... | 100.0 Hz, 3000 samples
        BW.RJOB..EHN | 2009-08-24T00:20:03.00Z - ... | 100.0 Hz, 3000 samples
        BW.RJOB..EHE | 2009-08-24T00:20:03.00Z - ... | 100.0 Hz, 3000 samples
        >>> ds.add_waveforms(st, tag="obspy_example")

        Just to demonstrate that all waveforms can also be retrieved again.

        >>> print(print(ds.waveforms.BW_RJOB.obspy_example))
        3 Trace(s) in Stream:
        BW.RJOB..EHZ | 2009-08-24T00:20:03.00Z - ... | 100.0 Hz, 3000 samples
        BW.RJOB..EHN | 2009-08-24T00:20:03.00Z - ... | 100.0 Hz, 3000 samples
        BW.RJOB..EHE | 2009-08-24T00:20:03.00Z - ... | 100.0 Hz, 3000 samples

        For the last example lets assume we have the result of a simulation
        stored in the ``synthetics.sac`` file. In this case we know the
        precise source parameters (we specified them before running the
        simulation) so it is a good idea to add that association to the
        waveform. Please again keep in mind that they are all optional and
        depending on your use case they might or might not be
        useful/meaningful.

        >>> origin = event.preferred_origin()
        >>> magnitude = event.preferred_magnitude()
        >>> focal_mechanism = event.preferred_focal_mechansism()
        >>> ds.add_waveforms("synthetics.sac", event_id=event,
        ...                  origin_id=origin, magnitude_id=magnitude,
        ...                  focal_mechanism_id=focal_mechanism)

        """
        # Extract the event_id from the different possibilities.
        if event_id:
            if isinstance(event_id, obspy.core.event.Event):
                event_id = str(event_id.resource_id.id)
            elif isinstance(event_id, obspy.core.event.ResourceIdentifier):
                event_id = str(event_id.id)
            else:
                try:
                    event_id = str(
                        obspy.core.event.ResourceIdentifier(event_id))
                except:
                    msg = "Invalid type for event_id."
                    raise TypeError(msg)

        # Do the same for the origin, magnitude, and focal mechanism.
        if origin_id:
            if isinstance(origin_id, obspy.core.event.Origin):
                origin_id = str(origin_id.resource_id.id)
            elif isinstance(origin_id, obspy.core.event.ResourceIdentifier):
                origin_id = str(origin_id.id)
            else:
                try:
                    origin_id = str(
                        obspy.core.event.ResourceIdentifier(origin_id))
                except:
                    msg = "Invalid type for origin_id."
                    raise TypeError(msg)
        if magnitude_id:
            if isinstance(magnitude_id, obspy.core.event.Magnitude):
                magnitude_id = str(magnitude_id.resource_id.id)
            elif isinstance(magnitude_id, obspy.core.event.ResourceIdentifier):
                magnitude_id = str(magnitude_id.id)
            else:
                try:
                    magnitude_id = str(
                        obspy.core.event.ResourceIdentifier(magnitude_id))
                except:
                    msg = "Invalid type for magnitude_id."
                    raise TypeError(msg)
        if focal_mechanism_id:
            if isinstance(focal_mechanism_id, obspy.core.event.FocalMechanism):
                focal_mechanism_id = str(focal_mechanism_id.resource_id.id)
            elif isinstance(focal_mechanism_id,
                            obspy.core.event.ResourceIdentifier):
                focal_mechanism_id = str(focal_mechanism_id.id)
            else:
                try:
                    focal_mechanism_id = str(
                        obspy.core.event.ResourceIdentifier(
                            focal_mechanism_id))
                except:
                    msg = "Invalid type for focal_mechanism_id."
                    raise TypeError(msg)

        tag = tag.strip()
        if tag.lower() == "stationxml":
            msg = "Tag '%s' is invalid." % tag
            raise ValueError(msg)
        # The next function expects some kind of iterable that yields traces.
        if isinstance(waveform, obspy.Trace):
            waveform = [waveform]
        elif isinstance(waveform, obspy.Stream):
            pass
        # Delegate to ObsPy's format/input detection.
        else:
            waveform = obspy.read(waveform)

        # Actually add the data.
        for trace in waveform:
            # Complicated multi-step process but it enables one to use
            # parallel I/O with the same functions.
            info = self._add_trace_get_collective_information(
                trace, tag, event_id=event_id, origin_id=origin_id,
                magnitude_id=magnitude_id,
                focal_mechanism_id=focal_mechanism_id)
            if info is None:
                continue
            self._add_trace_write_collective_information(info)
            self._add_trace_write_independent_information(info, trace)

    def _add_trace_write_independent_information(self, info, trace):
        """
        Writes the independent part of a trace to the file.

        :param info:
        :param trace:
        :return:
        """
        self._waveform_group[info["data_name"]][:] = trace.data

    def _add_trace_write_collective_information(self, info):
        """
        Writes the collective part of a trace to the file.

        :param info:
        :return:
        """
        station_name = info["station_name"]
        if station_name not in self._waveform_group:
            self._waveform_group.create_group(station_name)
        group = self._waveform_group[station_name]

        ds = group.create_dataset(**info["dataset_creation_params"])
        for key, value in info["dataset_attrs"].items():
            ds.attrs[key] = value

    def _add_trace_get_collective_information(
            self, trace, tag, event_id=None, origin_id=None,
            magnitude_id=None, focal_mechanism_id=None):
        """
        The information required for the collective part of adding a trace.

        This will extract the group name, the parameters of the dataset to
        be created, and the attributes of the dataset.

        :param trace: The trace to add.
        :param tag: The tag of the trace.
        """
        station_name = "%s.%s" % (trace.stats.network, trace.stats.station)
        # Generate the name of the data within its station folder.
        data_name = "{net}.{sta}.{loc}.{cha}__{start}__{end}__{tag}".format(
            net=trace.stats.network,
            sta=trace.stats.station,
            loc=trace.stats.location,
            cha=trace.stats.channel,
            start=trace.stats.starttime.strftime("%Y-%m-%dT%H:%M:%S"),
            end=trace.stats.endtime.strftime("%Y-%m-%dT%H:%M:%S"),
            tag=tag)

        group_name = "%s/%s" % (station_name, data_name)
        if group_name in self._waveform_group:
            msg = "Data '%s' already exists in file. Will not be added!" % \
                  group_name
            warnings.warn(msg, ASDFWarning)
            return

        # XXX: Figure out why this is necessary. It should work according to
        # the specs.
        if self.mpi:
            fletcher32 = False
        else:
            fletcher32 = True

        info = {
            "station_name": station_name,
            "data_name": group_name,
            "dataset_creation_params": {
                "name": data_name,
                "shape": (trace.stats.npts,),
                "dtype": trace.data.dtype,
                "compression": self.__compression[0],
                "compression_opts": self.__compression[1],
                "fletcher32": fletcher32,
                "maxshape": (None,)
            },
            "dataset_attrs": {
                # Starttime is the epoch time in nanoseconds.
                "starttime":
                    int(round(trace.stats.starttime.timestamp * 1.0E9)),
                "sampling_rate": trace.stats.sampling_rate
            }
        }

        # Add all the event ids.
        ids = {
            "event_id": event_id,
            "origin_id": origin_id,
            "magnitude_id": magnitude_id,
            "focal_mechanism_id": focal_mechanism_id}
        for name, obj in ids.items():
            if obj is None and \
                    hasattr(trace.stats, "asdf") and \
                    hasattr(trace.stats.asdf, name):
                obj = str(getattr(trace.stats.asdf, name).id)
            if obj:
                info["dataset_attrs"][name] = \
                    self._zeropad_ascii_string(str(obj))

        return info

    def _get_station(self, station_name):
        """
        Retrieves the specified StationXML as an obspy.station.Inventory
        object. For internal use only, use the dot accessors for external
        access.

        :param station_name: A string with network id and station id,
            e.g. ``"IU.ANMO"``
        :type station_name: str
        """
        if station_name not in self.__file["Waveforms"] or \
                "StationXML" not in self.__file["Waveforms"][station_name]:
            return None

        data = self.__file["Waveforms"][station_name]["StationXML"]

        with io.BytesIO(data.value.tostring()) as buf:
            inv = obspy.read_inventory(buf, format="stationxml")

        return inv

    def _add_inventory_object(self, inv, network_id, station_id):
        station_name = "%s.%s" % (network_id, station_id)

        # Write the station information to a numpy array that will then be
        # written to the HDF5 file.
        with io.BytesIO() as buf:
            inv.write(buf, format="stationxml")
            buf.seek(0, 0)
            data = np.frombuffer(buf.read(), dtype=np.dtype("byte"))

        if station_name not in self._waveform_group:
            self._waveform_group.create_group(station_name)
        station_group = self._waveform_group[station_name]

        # If it already exists, overwrite the existing one.
        if "StationXML" in station_group:
            station_group["StationXML"].resize(data.shape)
            station_group["StationXML"][:] = data
        else:
            # maxshape takes care to create an extendable data set.
            station_group.create_dataset(
                "StationXML", data=data,
                maxshape=(None,),
                fletcher32=True)

    def add_stationxml(self, stationxml):
        """
        Adds the StationXML to the data set object.

        This does some fairly exhaustive processing and will happily
        split the StationXML file and merge it with existing ones.

        If you care to have an a more or less unchanged StationXML file in
        the data set object be sure to add it one station at a time.

        :param stationxml: Filename of StationXML file or an ObsPy inventory
            object containing the same.
        :type stationxml: str or :class:`~obspy.station.inventory.Inventory`
        """
        # If not already an inventory object, delegate to ObsPy and see if
        # it can read it.
        if not isinstance(stationxml, obspy.station.Inventory):
            stationxml = obspy.read_inventory(stationxml, format="stationxml")

        # Now we essentially walk the whole inventory, see what parts are
        # already available and add only the new ones. This involved quite a
        # bit of splitting and merging of the inventory objects.
        network_station_codes = set()
        for network in stationxml:
            for station in network:
                network_station_codes.add((network.code, station.code))

        for network_id, station_id in network_station_codes:
            station_name = "%s.%s" % (network_id, station_id)

            # Get any existing station information.
            existing_inventory = self._get_station(station_name)
            # If it does not exist yet, make sure its well behaved and add it.
            if existing_inventory is None:
                self._add_inventory_object(
                    inv=isolate_and_merge_station(
                        stationxml, network_id=network_id,
                        station_id=station_id),
                    network_id=network_id, station_id=station_id)
            # Otherwise merge with the existing one and overwrite the
            # existing one.
            else:
                self._add_inventory_object(
                    inv=merge_inventories(
                        inv_a=existing_inventory, inv_b=stationxml,
                        network_id=network_id, station_id=station_id),
                    network_id=network_id, station_id=station_id)

    def validate(self):
        """
        Validate and ASDF file. It currently checks that each waveform file
        has a corresponding station file.
        """
        summary = {"no_station_information": 0, "no_waveforms": 0,
                   "good_stations": 0}
        for station_id in dir(self.waveforms):
            station = getattr(self.waveforms, station_id)
            contents = dir(station)
            if not contents:
                continue
            if "StationXML" not in contents and contents:
                print("No station information available for station '%s'" %
                      station_id)
                summary["no_station_information"] += 1
                continue
            contents.remove("StationXML")
            if not contents:
                print("Station with no waveforms: '%s'" % station_id)
                summary["no_waveforms"] += 1
                continue
            summary["good_stations"] += 1

        print("\nChecked %i stations:" % len(dir(self.waveforms)))
        print("\t%i stations have no available station information" %
              summary["no_station_information"])
        print("\t%i stations with no waveforms" %
              summary["no_waveforms"])
        print("\t%i good stations" % summary["good_stations"])

    def itertag(self, tag):
        """
        Iterate over stations. Yields a tuple of an obspy Stream object and
        an inventory object for the station information. The returned
        inventory object can be None.

        >>> for st, inv in data_set.itertag("raw_recording"):
        ...     st.detrend("linear")
        """
        for station in dir(self.waveforms):
            station = getattr(self.waveforms, station)
            if tag not in dir(station):
                continue
            if "StationXML" in dir(station):
                inv = station.StationXML
            else:
                inv = None
            st = getattr(station, tag)
            yield st, inv
        raise StopIteration

    def get_station_list(self):
        """
        Helper function returning a list of all stations in this ASDF file.
        """
        return sorted(self.__file["Waveforms"].keys())

    def process_two_files_without_parallel_output(self, other_ds,
                                                  process_function):
        if not self.mpi:
            raise ASDFException("Currently only works with MPI.")

        this_stations = set(self.get_station_list())
        other_stations = set(other_ds.get_station_list())

        # Usable stations are those that are part of both.
        usable_stations = list(this_stations.intersection(other_stations))

        # Divide into chunks, each rank takes their corresponding chunks.
        def chunks(l, n):
            """
            Yield successive n-sized chunks from l.
            From http://stackoverflow.com/a/312464/1657047
            """
            for i in range(0, len(l), n):
                yield l[i:i+n]

        chunksize = int(math.ceil(len(usable_stations) / self.mpi.size))
        all_chunks = list(chunks(usable_stations, chunksize))

        results = {}

        for station in all_chunks[self.mpi.rank]:
            try:
                result = process_function(
                    getattr(self.waveforms, station),
                    getattr(other_ds.waveforms, station))
            except Exception as e:
                print("Could not process station '%s' due to: %s" % (
                    station, str(e)))
            results[station] = result

        # Gather and create a final dictionary of results.
        gathered_results = self.mpi.comm.allgather(results)
        results = {}
        for result in gathered_results:
            results.update(result)
        return results

    def process(self, process_function, output_filename, tag_map):
        if os.path.exists(output_filename):
            msg = "Output file '%s' already exists." % output_filename
            raise ValueError(msg)

        stations = self.get_station_list()

        # Get all possible station and waveform tag combinations and let
        # each process read the data it needs.
        station_tags = []
        for station in stations:
            # Get the station and all possible tags.
            waveforms = self.__file["Waveforms"][station].keys()

            # Only care about stations that have station information.
            if "StationXML" not in waveforms:
                continue

            tags = set()

            for waveform in waveforms:
                if waveform == "StationXML":
                    continue
                tags.add(waveform.split("__")[-1])

            for tag in tags:
                if tag not in tag_map.keys():
                    continue
                station_tags.append((station, tag))

        if not station_tags:
            raise ValueError("No data matching the tag map found.")

        # Copy the station and event data only on the master process.
        if not self.mpi or (self.mpi and self.mpi.rank == 0):
            # Deactivate MPI even if active to not run into any barriers.
            output_data_set = ASDFDataSet(output_filename, mpi=False)
            for station_name, station_group in self._waveform_group.items():
                for tag, data in station_group.items():
                    if tag != "StationXML":
                        continue
                    if station_name not in output_data_set._waveform_group:
                        group = output_data_set._waveform_group.create_group(
                            station_name)
                    else:
                        group = output_data_set[station_name]
                    station_group.copy(source=data, dest=group,
                                       name="StationXML")

            # Copy the events.
            if self.events:
                output_data_set.events = self.events
            del output_data_set

        if self.mpi:
            self.mpi.comm.barrier()

        output_data_set = ASDFDataSet(output_filename)

        # Check for MPI, if yes, dispatch to MPI worker, if not dispatch to
        # the multiprocessing handler.
        if self.mpi:
            self._dispatch_processing_mpi(process_function, output_data_set,
                                          station_tags, tag_map)
        else:
            self._dispatch_processing_multiprocessing(
                process_function, output_data_set, station_tags, tag_map)

    def _dispatch_processing_mpi(self, process_function, output_data_set,
                                 station_tags, tag_map):
        # Make sure all processes enter here.
        self.mpi.comm.barrier()

        if self.mpi.rank == 0:
            self._dispatch_processing_mpi_master_node(process_function,
                                                      output_data_set,
                                                      station_tags, tag_map)
        else:
            self._dispatch_processing_mpi_worker_node(process_function,
                                                      output_data_set, tag_map)

    def _dispatch_processing_mpi_master_node(self, process_function,
                                             output_dataset, station_tags,
                                             tag_map):
        """
        The master node. It distributes the jobs and takes care that
        metadata modifying actions are collective.
        """
        from mpi4py import MPI

        worker_nodes = range(1, self.mpi.comm.size)
        workers_requesting_write = []

        jobs = JobQueueHelper(jobs=station_tags,
                              worker_names=worker_nodes)

        __last_print = time.time()

        print("Launching processing using MPI on %i processors." %
              self.mpi.comm.size)

        # Reactive event loop.
        while not jobs.all_done:
            time.sleep(0.01)

            # Informative output.
            if time.time() - __last_print > 2.0:
                print(jobs)
                __last_print = time.time()

            if (len(workers_requesting_write) >= 0.5 * self.mpi.comm.size) or \
                    (len(workers_requesting_write) and
                     jobs.all_poison_pills_received):
                if self.debug:
                    print("MASTER: initializing metadata synchronization.")

                # Send force write msgs to all workers and block until all
                # have been sent. Don't use blocking send cause then one
                # will have to wait each time anew and not just once for each.
                # The message will ready each worker for a collective
                # operation once its current operation is ready.
                requests = [self._send_mpi(None, rank, "MASTER_FORCES_WRITE",
                                           blocking=False)
                            for rank in worker_nodes]
                self.mpi.MPI.Request.waitall(requests)

                self._sync_metadata(output_dataset, tag_map=tag_map)

                # Reset workers requesting a write.
                workers_requesting_write[:] = []
                if self.debug:
                    print("MASTER: done with metadata synchronization.")
                continue

            # Retrieve any possible message and "dispatch" appropriately.
            status = MPI.Status()
            msg = self.mpi.comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG,
                                     status=status)
            tag = MSG_TAGS[status.tag]
            source = status.source

            if self.debug:
                pretty_receiver_log(source, self.mpi.rank, status.tag, msg)

            if tag == "WORKER_REQUESTS_ITEM":
                # Send poison pill if no more work is available. After
                # that the worker should not request any more jobs.
                if jobs.queue_empty:
                    self._send_mpi(POISON_PILL, source, "MASTER_SENDS_ITEM")
                else:
                    # And send a new station tag to process it.
                    station_tag = jobs.get_job_for_worker(source)
                    self._send_mpi(station_tag, source, "MASTER_SENDS_ITEM")

            elif tag == "WORKER_DONE_WITH_ITEM":
                station_tag, result = msg
                jobs.received_job_from_worker(station_tag, result, source)

            elif tag == "WORKER_REQUESTS_WRITE":
                workers_requesting_write.append(source)

            elif tag == "POISON_PILL_RECEIVED":
                jobs.poison_pill_received()

            else:
                raise NotImplementedError

        print("Master done, shutting down workers...")
        # Shutdown workers.
        for rank in worker_nodes:
            self._send_mpi(None, rank, "ALL_DONE")

        self.mpi.comm.barrier()
        print(jobs)

    def _dispatch_processing_mpi_worker_node(self, process_function,
                                             output_dataset, tag_map):
        """
        A worker node. It gets jobs, processes them and periodically waits
        until a collective metadata update operation has happened.
        """
        self.stream_buffer = StreamBuffer()

        worker_state = {
            "poison_pill_received": False,
            "waiting_for_write": False,
            "waiting_for_item": False
        }

        # Loop until the 'ALL_DONE' message has been sent.
        while not self._get_msg(0, "ALL_DONE"):
            time.sleep(0.01)

            # Check if master requested a write.
            if self._get_msg(0, "MASTER_FORCES_WRITE"):
                self._sync_metadata(output_dataset, tag_map=tag_map)
                for key, value in self.stream_buffer.items():
                    for trace in value:
                        output_dataset.\
                            _add_trace_write_independent_information(
                                trace.stats.__info, trace)
                    self._send_mpi((key, str(value)), 0,
                                   "WORKER_DONE_WITH_ITEM",
                                   blocking=False)
                self.stream_buffer.clear()
                worker_state["waiting_for_write"] = False

            if worker_state["waiting_for_write"]:
                continue

            if worker_state["poison_pill_received"]:
                continue

            if not worker_state["waiting_for_item"]:
                # Send message that the worker requires work.
                self._send_mpi(None, 0, "WORKER_REQUESTS_ITEM", blocking=False)
                worker_state["waiting_for_item"] = True
                continue

            msg = self._get_msg(0, "MASTER_SENDS_ITEM")
            if msg:
                station_tag = msg.data
                worker_state["waiting_for_item"] = False

                # If no more work to be done, store state and keep looping as
                # stuff still might require to be written.
                if station_tag == POISON_PILL:
                    if self.stream_buffer:
                        self._send_mpi(None, 0, "WORKER_REQUESTS_WRITE",
                                       blocking=False)
                        worker_state["waiting_for_write"] = True
                    worker_state["poison_pill_received"] = True
                    self._send_mpi(None, 0, "POISON_PILL_RECEIVED",
                                   blocking=False)
                    continue

                # Otherwise process the data.
                stream, inv = self.get_data_for_tag(*station_tag)
                try:
                    process_function(stream, inv)
                except Exception as e:
                    print("Error during processing function. Will be "
                          "skipped: %s" % str(e))

                # Add stream to buffer.
                self.stream_buffer[station_tag] = stream

                # If the buffer is too large, request from the master to stop
                # the current execution.
                if self.stream_buffer.get_size() >= \
                        MAX_MEMORY_PER_WORKER_IN_MB * 1024 ** 2:
                    self._send_mpi(None, 0, "WORKER_REQUESTS_WRITE",
                                   blocking=False)
                    worker_state["waiting_for_write"] = True

        print("Worker %i shutting down..." % self.mpi.rank)
        self.mpi.comm.barrier()

    def _sync_metadata(self, output_dataset, tag_map):
        """
        Method responsible for synchronizing metadata across all processes
        in the HDF5 file. All metadata changing operations must be collective.
        """
        if hasattr(self, "stream_buffer"):
            sendobj = []
            for key, stream in self.stream_buffer.items():
                for trace in stream:
                    info = \
                        output_dataset._add_trace_get_collective_information(
                            trace, tag_map[key[1]])
                    trace.stats.__info = info
                    sendobj.append(info)
        else:
            sendobj = [None]

        data = self.mpi.comm.allgather(sendobj=sendobj)
        # Chain and remove None.
        trace_info = filter(lambda x: x is not None,
                            itertools.chain.from_iterable(data))
        # Write collective part.
        for info in trace_info:
            output_dataset._add_trace_write_collective_information(info)

        # Make sure all remaining write requests are processed before
        # proceeding.
        if self.mpi.rank == 0:
            for rank in range(1, self.mpi.size):
                msg = self._get_msg(rank, "WORKER_REQUESTS_WRITE")
                if self.debug and msg:
                    print("MASTER: Ignoring write request by worker %i" %
                          rank)

        self.mpi.comm.barrier()

    def _dispatch_processing_multiprocessing(
            self, process_function, output_data_set, station_tags, tag_map):
        multiprocessing = get_multiprocessing()

        input_filename = self.filename
        output_filename = output_data_set.filename

        # Make sure all HDF5 file handles are closed before fork() is called.
        # Might become irrelevant if the HDF5 library sees some changes but
        # right now it is necessary.
        self._flush()
        self._close()
        output_data_set._flush()
        output_data_set._close()
        del output_data_set

        # Lock for input and output files. Probably not needed for the input
        # files but better be safe.
        input_file_lock = multiprocessing.Lock()
        output_file_lock = multiprocessing.Lock()

        cpu_count = min(multiprocessing.cpu_count(), len(station_tags))

        # Create the input queue containing the jobs.
        input_queue = multiprocessing.JoinableQueue(
            maxsize=int(math.ceil(1.1 * (len(station_tags) + cpu_count))))

        for _i in station_tags:
            input_queue.put(_i)

        # Put some poison pills.
        for _ in range(cpu_count):
            input_queue.put(POISON_PILL)

        # Give a short time for the queues to play catch-up.
        time.sleep(0.1)

        # The output queue will collect the reports from the jobs.
        output_queue = multiprocessing.Queue()

        class Process(multiprocessing.Process):
            def __init__(self, in_queue, out_queue, in_filename,
                         out_filename, in_lock, out_lock,
                         processing_function):
                super(Process, self).__init__()
                self.input_queue = in_queue
                self.output_queue = out_queue
                self.input_filename = in_filename
                self.output_filename = out_filename
                self.input_file_lock = in_lock
                self.output_file_lock = out_lock
                self.processing_function = processing_function

            def run(self):
                while True:
                    stationtag = self.input_queue.get(timeout=1)
                    if stationtag == POISON_PILL:
                        self.input_queue.task_done()
                        break
                    import time
                    print("Processing!!!", time.time())

                    station, tag = stationtag

                    with self.input_file_lock:
                        input_data_set = ASDFDataSet(self.input_filename)
                        stream, inv = \
                            input_data_set.get_data_for_tag(station, tag)
                        input_data_set._flush()
                        del input_data_set

                    output_stream = self.processing_function(stream, inv)

                    if output_stream:
                        with self.output_file_lock:
                            output_data_set = ASDFDataSet(self.output_filename)
                            output_data_set.add_waveforms(
                                output_stream, tag=tag_map[tag])
                            del output_data_set

                    self.input_queue.task_done()

        # Create n processes, with n being the number of available CPUs.
        processes = []
        for _ in range(cpu_count):
            processes.append(Process(input_queue, output_queue,
                                     input_filename, output_filename,
                                     input_file_lock, output_file_lock,
                                     process_function))

        print("Launching processing using multiprocessing on %i cores." %
              cpu_count)

        for process in processes:
            process.start()

        for process in processes:
            process.join()

        ASDFDataSet.__init__(self, self.__original_filename)

        return

    def _get_msg(self, source, tag):
        """
        Helper method to get a message if available, returns a
        ReceivedMessage instance in case a message is available, None
        otherwise.
        """
        tag = MSG_TAGS[tag]
        if not self.mpi.comm.Iprobe(source=source, tag=tag):
            return
        msg = ReceivedMessage(self.mpi.comm.recv(source=source, tag=tag))
        if self.debug:
            pretty_receiver_log(source, self.mpi.rank, tag, msg.data)
        return msg

    def _send_mpi(self, obj, dest, tag, blocking=True):
        """
        Helper method to send a message via MPI.
        """
        tag = MSG_TAGS[tag]
        if blocking:
            value = self.mpi.comm.send(obj=obj, dest=dest, tag=tag)
        else:
            value = self.mpi.comm.isend(obj=obj, dest=dest, tag=tag)
        if self.debug:
            pretty_sender_log(dest, self.mpi.rank, tag, obj)
        return value

    def _recv_mpi(self, source, tag):
        """
        Helper method to receive a message via MPI.
        """
        tag = MSG_TAGS[tag]
        msg = self.mpi.comm.recv(source=source, tag=tag)
        if self.debug:
            pretty_receiver_log(source, self.mpi.rank, tag, msg)
        return msg
