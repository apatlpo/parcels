from datetime import timedelta as delta
from operator import attrgetter  # noqa: F401

from ctypes import c_void_p  # noqa: F401

import numpy as np
import sys

from parcels.collection.collections import ParticleCollection
from parcels.collection.iterators import BaseParticleAccessor
from parcels.collection.iterators import BaseParticleCollectionIterator
from parcels.collection.iterators import BaseParticleCollectionIterable
from parcels.particle import ScipyParticle, JITParticle  # noqa: F401
from parcels.nodes.PyNode import Node, NodeJIT
from parcels.nodes.nodelist import DoubleLinkedNodeList
from parcels.field import Field
from parcels.tools.statuscodes import OperationCode
from scipy.spatial import distance
from parcels.tools.loggers import logger

try:
    from mpi4py import MPI
except:
    MPI = None
if MPI:
    try:
        from sklearn.cluster import KMeans
    except:
        raise EnvironmentError('sklearn needs to be available if MPI is installed. '
                               'See http://oceanparcels.org/#parallel_install for more information')

__all__ = ['ParticleCollectionNodes', 'ParticleCollectionIterableNodes', 'ParticleCollectionIteratorNodes']


def _to_write_particles(begin_node, time):
    """We don't want to write a particle that is not started yet.
    Particle will be written if particle.time is between time-dt/2 and time+dt (/2)
    :arg begin_node: first node of the node-list, typically: list.begin()
    :returns list of Particle IDs
    """
    return NotImplementedError("ParticleSetNodes::to_write_particles undefined.")


def _is_particle_started_yet(particle, time):
    """We don't want to write a particle that is not started yet.
    Particle will be written if:
      * particle.time is equal to time argument of pfile.write()
      * particle.time is before time (in case particle was deleted between previous export and current one)
    """
    return (particle.dt*particle.time <= particle.dt*time or np.isclose(particle.time, time))


def _convert_to_flat_array(var):
    """Convert lists and single integers/floats to one-dimensional numpy arrays

    :param var: list or numeric to convert to a one-dimensional numpy array
    """
    if isinstance(var, np.ndarray):
        return var.flatten()
    elif isinstance(var, (int, float, np.float32, np.int32)):
        return np.array([var])
    else:
        return np.array(var)


class ParticleCollectionNodes(ParticleCollection):
    _data_c = None
    _nclass = None
    _ngrid = -1
    _idgen = None
    _c_lib_register = None
    _kwarg_keys = None

    def __init__(self, idgen, c_lib_register, pclass, lon, lat, depth, time, lonlatdepth_dtype, pid_orig, partitions=None, ngrid=1, **kwargs):
        """
        :param ngrid: number of grids in the fieldset of the overarching ParticleSet - required for initialising the
        field references of the ctypes-link of particles that are allocated
        """
        # print("Init - lon: {}, lat: {}, depth: {}, time: {}".format(lon, lat, depth, time))

        super(ParticleCollection, self).__init__()
        self._idgen = idgen
        self._c_lib_register = c_lib_register
        self._ngrid = ngrid

        assert pid_orig is not None, "particle IDs are None - incompatible with the collection. Invalid state."
        pid = None if pid_orig is None else pid_orig if isinstance(pid_orig, list) or isinstance(pid_orig, np.ndarray) else pid_orig + self._idgen.total_length

        assert depth is not None, "particle's initial depth is None - incompatible with the collection. Invalid state."
        assert lon.size == lat.size and lon.size == depth.size, (
            'lon, lat, depth do not all have the same lenghts')

        assert lon.size == time.size and lon.size == depth.size, (
            'time and positions (lon, lat, depth) do not have the same lengths.')

        # If partitions is false, the partitions are already initialised
        if partitions is not None and partitions is not False:
            self._pu_indicators = _convert_to_flat_array(partitions)

        for kwvar in kwargs:
            assert lon.size == kwargs[kwvar].size, (
                '%s and positions (lon, lat, depth) do nott have the same lengths.' % kwvar)

        offset = np.max(pid) if (pid is not None) and type(pid) in [list, tuple, np.ndarray] and len(pid) > 0 else -1
        if MPI:
            mpi_comm = MPI.COMM_WORLD
            mpi_rank = mpi_comm.Get_rank()
            mpi_size = mpi_comm.Get_size()

            if lon.size < mpi_size and mpi_size > 1:
                raise RuntimeError('Cannot initialise with fewer particles than MPI processors')

            if mpi_size > 1:
                if partitions is not False:
                    if self._pu_indicators is None:
                        if mpi_rank == 0:
                            coords = np.vstack((lon, lat)).transpose()
                            kmeans = KMeans(n_clusters=mpi_size, random_state=0).fit(coords)
                            self._pu_indicators = kmeans.labels_
                            self._pu_centers = kmeans.cluster_centers_
                        else:
                            self._pu_indicators = None
                        self._pu_indicators = mpi_comm.bcast(self._pu_indicators, root=0)
                        self._pu_centers = mpi_comm.bcast(self._pu_centers, root=0)
                    elif np.max(self._pu_indicators) >= mpi_size:
                        raise RuntimeError('Particle partitions must vary between 0 and the number of mpi procs')
                    lon = lon[self._pu_indicators == mpi_rank]
                    lat = lat[self._pu_indicators == mpi_rank]
                    time = time[self._pu_indicators == mpi_rank]
                    depth = depth[self._pu_indicators == mpi_rank]
                    pid = pid[self._pu_indicators == mpi_rank]
                    for kwvar in kwargs:
                        kwargs[kwvar] = kwargs[kwvar][self._pu_indicators == mpi_rank]
                offset = MPI.COMM_WORLD.allreduce(offset, op=MPI.MAX)

        pclass.setLastID(offset+1)

        if lonlatdepth_dtype is None:
            self._lonlatdepth_dtype = np.float32
        else:
            self._lonlatdepth_dtype = lonlatdepth_dtype
        assert self._lonlatdepth_dtype in [np.float32, np.float64], \
            'lon lat depth precision should be set to either np.float32 or np.float64'
        pclass.set_lonlatdepth_dtype(self._lonlatdepth_dtype)
        self._pclass = pclass

        self._ptype = self._pclass.getPType()
        if self._ptype.uses_jit:
            self._nclass = NodeJIT
        else:
            self._nclass = Node
        self._kwarg_keys = kwargs.keys()
        self._data = DoubleLinkedNodeList(dtype=self._nclass, c_lib_register=self._c_lib_register)
        initialised = set()

        if lon is not None and lat is not None:
            # Initialise from lists of lon/lat coordinates
            assert len(lon) == len(lat), (
                'Size of ParticleSet does not match length of lon and lat.')

            for i in range(len(lon)):
                init_time = time[i] if time is not None and len(time) > 0 and time[i] is not None else 0
                pdata_id = None
                # index = -1
                if pid is not None and (isinstance(pid, list) or isinstance(pid, np.ndarray)):
                    # index = pid[i]
                    pdata_id = pid[i]
                else:
                    # index = idgen.total_length
                    pdata_id = idgen.nextID(lon[i], lat[i], depth[i], abs(init_time))
                pdata = self._pclass(lon[i], lat[i], pid=pdata_id, ngrids=ngrid, depth=depth[i], time=init_time)
                # Set other Variables if provided
                for kwvar in kwargs:
                    if isinstance(kwvar, Field):
                        continue
                    if not hasattr(pdata, kwvar):
                        raise RuntimeError('Particle class does not have Variable %s' % kwvar)
                    setattr(pdata, kwvar, kwargs[kwvar][i])
                    if kwvar not in initialised:
                        initialised.add(kwvar)
                ndata = self._nclass(id=pdata_id, data=pdata, c_lib_register=self._c_lib_register, idgen=self._idgen)
                self._data.add(ndata)

            initialised |= {'lat', 'lon', 'depth', 'time', 'id'}

            for v in self._ptype.variables:
                if v.name in initialised:
                    continue
                if isinstance(v.initial, Field):
                    i = 0
                    ndata = self.begin()
                    while i < len(self._data):
                        pdata = ndata.data
                        # ==== ==== ==== #
                        if (pdata.time is None) or (np.isnan(pdata.time)):
                            raise RuntimeError('Cannot initialise a Variable with a Field if no time provided (time-type: {} values: {}). Add a "time=" to ParticleSet construction'.format(type(time), time))
                        init_time = pdata.time if pdata.time not in [None, np.nan] and np.count_nonzero([tval is not None for tval in time]) == len(time) else 0
                        init_field = v.initial
                        init_field.fieldset.computeTimeChunk(init_time, 0)
                        # setattr(pdata, v.name, init_field[pdata.time, pdata.depth, pdata.lat, pdata.lon])
                        setattr(pdata, v.name, init_field[init_time, pdata.depth, pdata.lat, pdata.lon])
                        logger.warning_once("Particle initialisation from field can be very slow as it is computed in scipy mode.")
                        # ==== ==== ==== #
                        ndata.set_data(pdata)
                        ndata = ndata.next
                        i += 1
                if v not in initialised:
                    initialised.add(v)
        else:
            raise ValueError("Latitude and longitude required for generating ParticleSet")

        self._ncount = len(self._data)
        # ==== fill c-pointer ==== #
        if self._ptype.uses_jit:
            self._data_c = []
            for i in range(len(self._data)):
                node = self._data[i]
                self._data_c.append(node.data.get_cptr())
            # self._data_c = np.array(self.data_c, dtype=self._ptype.dtype)

        self._iterator = None
        self._riterator = None

    def __del__(self):
        """
        Collection - Destructor
        """
        # logger.info("ParticleCollectionNodes.del() called.")
        if self._data is not None and isinstance(self._data, DoubleLinkedNodeList):
            # self._data.clear()
            del self._data
        self._data = None
        if self._data_c is not None:
            # self._data_c.clear()
            del self._data_c
        self._data_c = None
        super(ParticleCollectionNodes, self).__del__()

    def iterator(self):
        """

        """
        self._iterator = ParticleCollectionIteratorNodes(self)
        return self._iterator

    def __iter__(self):
        """
        Returns an Iterator that allows for forward iteration over the
        elements in the ParticleCollection (e.g. `for p in pset:`).
        """
        return self.iterator()

    def reverse_iterator(self):
        """

        """
        self._riterator = ParticleCollectionIteratorNodes(self, True)
        return self._riterator

    def __reversed__(self):
        """Returns an Iterator that allows for backwards iteration over
        the elements in the ParticleCollection (e.g.
        `for p in reversed(pset):`).
        """
        return self.reverse_iterator()

    def __getitem__(self, index):
        """
        Access a particle in this collection using the fastest access
        method for this collection - by its index.

        :param index: int or np.int32 index of a particle in this collection
        """
        return self.get_single_by_index(index)

    def __getattr__(self, name):
        """
        Access a single property of all particles.
        CAUTION: this function is not(!) in-place and is REALLY slow

        :param name: name of the property
        """
        pdtype = None
        for var in self._ptype.variables:
            if name == var.name:
                pdtype = var.dtype
        if pdtype is None:
            return None
        result = np.zeros(self._ncount, dtype=pdtype)
        for index in range(self._ncount):
            if hasattr(self._data[index].data, name):
                result[index] = getattr(self._data[index].data, name)
        return result

    @property
    def data_c(self):
        return self._data_c

    @property
    def particle_data(self):
        """
        'particle_data' is a reference to the actual barebone-storage of the particle data, and thus depends directly on the
        specific collection in question. This property is just available for convenience and backward-compatibility, and
        this returns the same as 'data'.
        """
        return self._data_c

    @property
    def kernel_class(self):
        return self._kclass

    @kernel_class.setter
    def kernel_class(self, value):
        self._kclass = value

    def cptr(self, index):
        if self._ptype.uses_jit:
            node = self._data[index]
            return node.data.get_cptr()
        else:
            return None

    def empty(self):
        return len(self._data) <= 0

    def begin(self):
        """
        Returns the begin of the linked particle list (like C++ STL begin() function)
        :return: begin Node (Node whose prev element is None); returns None if ParticleSet is empty
        """
        if not self.empty():
            start_index = 0
            node = self._data[start_index]
            while not node.is_valid() and start_index < len(self._data):
                start_index += 1
                node = self._data[start_index]
            while node.prev is not None:
                node = node.prev
            node = None if not node.is_valid() else node
            assert node is not None
            return node
        return None

    def end(self):
        """
        Returns the end of the linked partile list. UNLIKE in C++ STL, it returns the last element (valid element),
        not the element past the last element (invalid element). (see http://www.cplusplus.com/reference/list/list/end/)
        :return: end Node (Node whose next element is None); returns None if ParticleSet is empty
        """
        if not self.empty():
            node = self._data[len(self._data) - 1]
            while node.next is not None:
                # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
                node_candidate = node
                while not node_candidate.is_valid() and node_candidate.next is not None:
                    node_candidate = node_candidate.next
                if node_candidate is not node and node_candidate.next is not None:
                    node = node_candidate
                node = node.next
            return node
        return None

    def __repr__(self):
        result = "\n"
        node = self.begin()
        while node.next is not None:
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            if not node.is_valid():
                node = node.next
                continue
            result += str(node) + "\n"
            node = node.next
        result += str(node) + "\n"
        return result

    def get_index_by_node(self, ndata):
        index = None
        try:
            index = self._data.index(ndata)
        except ValueError:
            pass
        return index

    def get_index_by_ID(self, id):
        """
        Provides a simple function to search / get the index for a particle of the requested ID.
        Returns the particle's index. Divide-and-conquer search of SORTED list - needed because the node list
        internally can only be scanned for (a) its list index (non-coherent) or (b) a node itself, but not for a
        specific Node property alone. That is why using the 'bisect' module alone won't work.
        :param id: search Node ID
        """
        # super().get_index_by_ID(id)  # has no super-class implementation
        lower = 0
        upper = len(self._data) - 1
        pos = lower + int((upper - lower) / 2.0)
        current_node_data = self._data[pos].data
        _found = False
        _search_done = False
        while current_node_data.id != id and not _search_done:
            prev_upper = upper
            prev_lower = lower
            if id < current_node_data.id:
                lower = lower
                upper = pos - 1
                pos = lower + int((upper - lower) / 2.0)
            else:
                lower = pos
                upper = upper
                pos = lower + int((upper - lower) / 2.0) + 1
            if (prev_upper == upper and prev_lower == lower):
                _search_done = True
            current_node_data = self._data[pos].data
        if current_node_data.id == id:
            _found = True
        if _found:
            return pos
        return None

    def get_indices_by_IDs(self, ids):
        indices = []
        for id in ids:
            indices.append(self.get_index_by_ID(id))
        return indices

    def get_single_by_index(self, index):
        """
        This function gets a (particle) object from the collection based on its index within the collection. For
        collections that are not based on random access (e.g. ordered lists, sets, trees), this function involves a
        translation of the index into the specific object reference in the collection - or (if unavoidable) the
        translation of the collection from a none-indexable, none-random-access structure into an indexable structure.
        In cases where a get-by-index would result in a performance malus, it is highly-advisable to use a different
        get function, e.g. get-by-ID.
        """
        super().get_single_by_index(index)
        result = None
        if index >= 0 and index < len(self._data):
            try:
                result = self._data[index].data
            except ValueError:
                pass
        return result

    def get_single_by_object(self, particle_obj):
        """
        This function gets a (particle) object from the collection based on its actual object. For collections that
        are random-access and based on indices (e.g. unordered list, vectors, arrays and dense matrices), this function
        would involve a parsing of the whole list and translation of the object into an index in the collection - which
        results in a significant performance malus.
        In cases where a get-by-object would result in a performance malus, it is highly-advisable to use a different
        get function, e.g. get-by-index or get-by-ID.

        In this specific implementation, we cannot look for the object
        directly, so we will look for one of its properties (the ID) that
        has the nice property of being stored in an ordered list (if the
        collection is sorted)
        """
        super().get_single_by_object(particle_obj)
        id = particle_obj.id
        lower = 0
        upper = len(self._data) - 1
        pos = lower + int((upper - lower) / 2.0)
        current_node_data = self._data[pos].data
        _found = False
        _search_done = False
        while current_node_data.id != id and not _search_done:
            prev_upper = upper
            prev_lower = lower
            if id < current_node_data.id:
                lower = lower
                upper = pos - 1
                pos = lower + int((upper - lower) / 2.0)
            else:
                lower = pos
                upper = upper
                pos = lower + int((upper - lower) / 2.0) + 1
            if (prev_upper == upper and prev_lower == lower):
                _search_done = True
            current_node_data = self._data[pos].data
        if current_node_data.id == id:
            _found = True
        if _found:
            return current_node_data
        else:
            return None

    def get_single_by_ID(self, id):
        """
        This function gets a (particle) object from the collection based on the object's ID. For some collections,
        this operation may involve a parsing of the whole list and translation of the object's ID into an index  or an
        object reference in the collection - which results in a significant performance malus.
        In cases where a get-by-ID would result in a performance malus, it is highly-advisable to use a different
        get function, e.g. get-by-index.

        This function uses binary search if we know the ID list to be sorted, and linear search otherwise. We assume
        IDs are unique.
        :param id: search Node-ID
        :return Object attached to ID
        """
        super().get_single_by_ID(id)
        lower = 0
        upper = len(self._data) - 1
        pos = lower + int((upper - lower) / 2.0)
        current_node_data = self._data[pos].data
        _found = False
        _search_done = False
        while current_node_data.id != id and not _search_done:
            prev_upper = upper
            prev_lower = lower
            if id < current_node_data.id:
                lower = lower
                upper = pos - 1
                pos = lower + int((upper - lower) / 2.0)
            else:
                lower = pos
                upper = upper
                pos = lower + int((upper - lower) / 2.0) + 1
            if (prev_upper == upper and prev_lower == lower):
                _search_done = True
            current_node_data = self._data[pos].data
        if current_node_data.id == id:
            _found = True
        if _found:
            return current_node_data
        return None

    def get_node_by_ID(self, id):
        """
        divide-and-conquer search of SORTED list - needed because the node list internally
        can only be scanned for (a) its list index (non-coherent) or (b) a node itself, but not for a specific
        Node property alone. That is why using the 'bisect' module alone won't work.
        :param id: search Node ID
        :return: Node attached to ID - if node not in list: return None
        """
        lower = 0
        upper = len(self._data) - 1
        pos = lower + int((upper - lower) / 2.0)
        current_node = self._data[pos]
        _found = False
        _search_done = False
        while current_node.data.id != id and not _search_done:
            prev_upper = upper
            prev_lower = lower
            if id < current_node.data.id:
                lower = lower
                upper = pos - 1
                pos = lower + int((upper - lower) / 2.0)
            else:
                lower = pos
                upper = upper
                pos = lower + int((upper - lower) / 2.0) + 1
            if (prev_upper == upper and prev_lower == lower):
                _search_done = True
            current_node = self._data[pos]
        if current_node.data.id == id:
            _found = True
        if _found:
            return current_node
        else:
            return None

    def get_same(self, same_class):
        """
        This function gets particles from this collection that are themselves stored in another object of an equi-
        structured ParticleCollection.
        """
        super().get_same(same_class)
        results = []
        other_node = same_class.begin()
        this_node = self.begin()
        while (other_node is not None) and (this_node is not None):
            if this_node.data.id < other_node.data.id:
                this_node = this_node.next
                continue
            if other_node.data.id < this_node.data.id:
                other_node = other_node.next
                continue
            if this_node.data.id == other_node.data.id:
                results.append(this_node.data)
        return results

    def get_collection(self, pcollection):
        """
        This function gets particles from this collection that are themselves stored in a ParticleCollection, which
        is differently structured than this one. That means the other-collection has to be re-formatted first in an
        intermediary format.
        """
        super().get_collection(pcollection)
        if (self._ncount <= 0) or (len(pcollection) <= 0):
            return None
        results = []
        for item in pcollection:
            # here, we really need a 'contains_ID' function
            node = self.get_node_by_ID(item.id)
            if node is not None:
                results.append(node)
        if len(results) == 0:
            results = None
        return results

    def get_multi_by_PyCollection_Particles(self, pycollectionp):
        """
        This function gets particles from this collection, which are themselves in common Python collections, such as
        lists, dicts and numpy structures. We can either directly get the referred Particle instances (for internally-
        ordered collections, e.g. ordered lists, sets, trees) or we may need to parse each instance for its index (for
        random-access structures), which results in a considerable performance malus.

        For collections where get-by-object incurs a performance malus, it is advisable to multi-get particles
        by indices or IDs.
        """
        super().get_multi_by_PyCollection_Particles(pycollectionp)
        if (self._ncount <= 0) or (len(pycollectionp) <= 0):
            return None
        results = []
        for item in pycollectionp:
            # here, we really need a 'contains_ID' function
            node = self.get_node_by_ID(item.id)
            if node is not None:
                results.append(node)
        if len(results) == 0:
            results = None
        return results

    def get_multi_by_indices(self, indices):
        """
        This function gets particles from this collection based on their indices. This works best for random-access
        collections (e.g. numpy's ndarrays, dense matrices and dense arrays), whereas internally ordered collections
        shall rather use a get-via-object-reference strategy.
        """
        super().get_multi_by_indices(indices)
        results = []
        if type(indices) is dict:
            indices = list(indices.values())
        for index in indices:
            res = self.get_single_by_index(index)
            if res is not None:
                results.append(res)
        return results

    def get_multi_by_IDs(self, ids):
        """
        This function gets particles from this collection based on their IDs. For collections where this removal
        strategy would require a collection transformation or by-ID parsing, it is advisable to rather apply a get-
        by-objects or get-by-indices scheme.

        Note that this implementation assumes that IDs of particles are strictly increasing with increasing index. So
        a particle with a larger index will always have a larger ID as well. The assumption often holds for this
        datastructure as new particles always get a larger ID than any existing particle (IDs are not recycled)
        and their data are appended at the end of the list (largest index). This allows for the use of binary search
        in the look-up. The collection maintains a `sorted` flag to indicate whether this assumption holds.
        """
        super().get_multi_by_IDs(ids)
        results = []
        if type(ids) is dict:
            ids = list(ids.values())
        for id in ids:
            res = self.get_single_by_ID(id)
            if res is not None:
                results.append(res)
        return results

    def add_collection(self, pcollection):
        """
        Adds another, differently structured ParticleCollection to this collection. This is done by, for example,
        appending/adding the items of the other collection to this collection.
        """
        # ==== first approach - still need to incorporate the MPI re-centering ==== #
        super().add_collection(pcollection)
        results = []
        for item_index, item in enumerate(pcollection):
            # self._pclass(lon=item.lon, lat=item.lat, pid=item.pid, ngrids=ngrids, depth=item.depth, time=item.time)
            pdata_item = self._pclass(lon=item.lon, lat=item.lat, pid=item.pid, ngrids=self._ngrids, depth=item.depth, time=item.time)
            results.append(self.add_single(pdata_item))
        pcollection.clear()
        self._ncount = len(self._data)
        return results

    def add_multiple(self, data_array):
        """
        Add multiple particles from an array-like structure (i.e. list or tuple or np.ndarray)
        to the collection.
        :arg data_array: one of the following:
            i) a list or tuples containing multple Particle instances
            ii) a Numpy.ndarray of dtype = Particle dtype
            iii) a Numpy.ndarray of shape N x M, with N = # particles and
                 M = variables [lon, lat, [depth, [time, [dt, [id=-1, [kwargs]]]]]]
        """
        results = []
        if len(data_array) <= 0:
            return results
        if isinstance(data_array, list) or isinstance(data_array, tuple):
            for item in data_array:
                results.append(self.add_entity(item))
        elif isinstance(data_array, np.ndarray):
            if data_array.dtype == self._ptype:
                for i in range(data_array.shape[0]):
                    # ndata = self._nclass(id=data_array[i].id, data=data_array[i], c_lib_register=self._c_lib_register, idgen=self._idgen)
                    # self._data.add(ndata)
                    # results.append(self._data.bisect_right(ndata))
                    pdata = data_array[i]
                    results.append(self.add_single(pdata))
            else:
                # expect this to be a nD (2 <= n <= 5) array with [lon, lat, [depth, [time, [dt]]]]
                pu_data = None
                if MPI and MPI.COMM_WORLD.Get_size() > 1:
                    mpi_comm = MPI.COMM_WORLD
                    mpi_size = mpi_comm.Get_size()
                    mpi_rank = mpi_comm.Get_rank()
                    spdata = data_array[:, 0:2]
                    min_pu = None
                    if mpi_rank == 0:
                        dists = distance.cdist(spdata, self._pu_centers)
                        min_pu = np.argmax(dists, axis=1)
                        self._pu_indicators = np.concatenate((self._pu_indicators, min_pu), axis=0)
                    min_pu = mpi_comm.bcast(min_pu, root=0)
                    self._pu_indicators = mpi_comm.bcast(self._pu_indicators, root=0)
                    pu_data = data_array[min_pu == mpi_rank]
                    pu_center = np.array(np.mean(spdata, axis=0), dtype=self._lonlatdepth_dtype)
                    n_pu_data = pu_data.shape[0]
                    pu_ncenters = None
                    if mpi_rank == 0:
                        pu_ncenters = np.empty([mpi_size, pu_center.shape[0]], dtype=self._latlondepth_dtype)
                    mpi_comm.Gather(pu_center, pu_ncenters, root=0)
                    pu_ndata = mpi_comm.gather(n_pu_data, root=0)
                    if mpi_rank == 0:
                        for i in range(self._pu_centers.shape[0]):
                            ax = float(pu_ndata[i]) / float(len(np.nonzero(self._pu_indicators == i)[0]))
                            self._pu_centers[i, :] += ax*pu_ncenters[i, :]
                    mpi_comm.Bcast(self._pu_centers, root=0)
                else:
                    pu_data = data_array
                for i in range(pu_data.shape[0]):
                    pdata = self._pclass(lon=pu_data[i, 0], lat=pu_data[i, 1], pid=np.iinfo(np.uint64).max, ngrids=self._ngrids)
                    if pu_data.shape[1] > 2:
                        pdata.depth = pu_data[i, 2]
                    if pu_data.shape[1] > 3:
                        pdata.time = pu_data[i, 3]
                    if pu_data.shape[1] > 4:
                        pdata.dt = pu_data[i, 4]
                    if pu_data.shape[1] > 5:  # that is the provided ID that we skip - should not be defined
                        pass
                    if pu_data.shape[1] > 6:
                        attr_index = 6
                        for key in self._kwarg_keys:
                            setattr(pdata, key, pu_data[i, attr_index])
                            attr_index += 1
                    results.append(self.add_single(pdata, pu_checked=True))
        else:
            self._ncount = len(self._data)
            return results
        self._ncount = len(self._data)
        return results

    def add_single(self, particle_obj, pu_checked=False):
        """
        Adding a single Particle to the collection - either as a 'Particle' object in parcels itself, or
        via its ParticleAccessor.
        """
        # ==== first approach - still need to incorporate the MPI re-centering ==== #
        super().add_single(particle_obj)
        assert isinstance(particle_obj, ScipyParticle)
        # Comment: by current workflow, pset modification is only done on the front node, thus
        # the distance determination and assigment is also done on the front node
        _add_to_pu = True
        # if MPI:
        if MPI and MPI.COMM_WORLD.Get_size() > 1 and not pu_checked:
            if self._pu_centers is not None and isinstance(self._pu_centers, np.ndarray):
                mpi_comm = MPI.COMM_WORLD
                mpi_rank = mpi_comm.Get_rank()
                mpi_size = mpi_comm.Get_size()
                min_dist = np.finfo(self._lonlatdepth_dtype).max
                min_pu = 0
                spdata = None
                if mpi_size > 1 and mpi_rank == 0:
                    ppos = particle_obj
                    if isinstance(particle_obj, self._nclass):
                        ppos = particle_obj.data
                    spdata = np.array([ppos.lat, ppos.lon], dtype=self._lonlatdepth_dtype)
                    n_clusters = self._pu_centers.shape[0]
                    for i in range(n_clusters):
                        diff = self._pu_centers[i, :] - spdata
                        dist = np.dot(diff, diff)
                        if dist < min_dist:
                            min_dist = dist
                            min_pu = i
                    self._pu_indicators = np.concatenate((self._pu_indicators, min_pu), axis=0)
                # NOW: move the related center by: (center-spdata) * 1/(cluster_size+1)
                min_pu = mpi_comm.bcast(min_pu, root=0)
                self._pu_indicators = mpi_comm.bcast(self._pu_indicators, root=0)
                if mpi_rank == 0:
                    ax = 1.0 / float(len(np.nonzero(self._pu_indicators == min_pu)[0]))
                    self._pu_centers[min_pu, :] += ax * spdata
                mpi_comm.Bcast(self._pu_centers, root=0)

                if mpi_rank == min_pu:
                    _add_to_pu = True
                else:
                    _add_to_pu = False
        if _add_to_pu:
            index = -1
            pid = np.iinfo(np.uint64).max
            if isinstance(particle_obj, self._nclass):
                self._data.add(particle_obj)
                index = self._data.bisect_right(particle_obj)
            else:
                if particle_obj.id == pid:
                    index = self._idgen.total_length
                    pid = self._idgen.nextID(particle_obj.lon, particle_obj.lat, particle_obj.depth, particle_obj.time)
                    particle_obj.id = pid
                    # particle_obj.index = index
                else:
                    pid = particle_obj.id
                    # index = particle_obj.index
                node = self._nclass(id=pid, data=particle_obj, c_lib_register=self._c_lib_register, idgen=self._idgen)
                self._data.add(node)
                index = self._data.bisect_right(node)
            if index >= 0:
                # return self._nodes[index]
                self._ncount = len(self._data)
                return index
        self._ncount = len(self._data)
        return None

    def add_same(self, same_class):
        """
        Adds another, equi-structured ParticleCollection to this collection. This is done by concatenating
        both collections. The fact that they are of the same ParticleCollection's derivative simplifies
        parsing and concatenation.
        """
        super(ParticleCollectionNodes, self).add_same(same_class)
        results = []
        if same_class.ncount <= 0:
            return

        for i in range(len(same_class)):
            pdata = same_class.get_single_by_index(i)  # get() returns the particle data
            # pdata = same_class.pop(i).data  # pop() returns the node
            results.append(self.add_single(pdata))
        # self._ncount = len(self._data) -> done by add_single()
        return results

    def __iadd__(self, same_class):
        """
        Performs an incremental addition of the equi-structured ParticleCollections, such to allow

        a += b,

        with 'a' and 'b' begin the two equi-structured objects (or: 'b' being and individual object).
        This operation is equal to an in-place addition of (an) element(s).
        """
        self.add_same(same_class)
        return self

    def insert(self, obj, index=None):
        """
        This function allows to 'insert' a Particle (as object or via its accessor) into this collection. This method
        needs to be specified to each collection individually. Some collections (e.g. unordered list) allow to define
        the index where the object is to be inserted. Some collections can optionally insert an object at a specific
        position - at a significant speed- and memory malus cost (e.g. vectors, arrays, dense matrices). Some
        collections that manage a specified indexing order internally (e.g. ordered lists, sets, trees), and thus
        have no use for an 'index' parameter. For those collections with an internally-enforced order, the function
        mapping equates to:

        insert(obj) -> add_single(obj)

        For AoS, insert with 'index==None', the function equates to 'add'. If 'index' is specified, split the array,
        insert the item and splice the arrays.
        """
        return self.add_single(obj)

    def push(self, particle_obj):
        """
        This function pushes a Particle (as object or via its accessor) to the end of a collection ('end' definition
        depends on the specific collection itself). For collections with an inherent indexing order (e.g. ordered lists,
        sets, trees), the function just includes the object at its pre-defined position (i.e. not necessarily at the
        end). For the collections, the function mapping equates to:

        int32 push(particle_obj) -> add_single(particle_obj); return -1;

        This function further returns the index, at which position the Particle has been inserted. By definition,
        the index is positive, thus: a return of '-1' indicates push failure, NOT the last position in the collection.
        Furthermore, collections that do not work on an index-preserving manner also return '-1'.
        """
        return self.add_single(particle_obj)

    def append(self, particle_obj):
        """
        This function appends a Particle (as object or via its accessor) to the end of a collection ('end' definition
        depends on the specific collection itself). For collections with an inherent indexing order (e.g. ordered lists,
        sets, trees), the function just includes the object at its pre-defined position (i.e. not necessarily at the
        end). For the collections, the function mapping equates to:

        append(particle_obj) -> add_single(particle_obj)

        The function - in contrast to 'push' - does not return the index of the inserted object.
        """
        return self.add_single(particle_obj)

    def __delitem__(self, key):
        """
        This is the high-performance method to delete a specific object from this collection.
        As the most-performant way depends on the specific collection in question, the function is abstract.

        Highlight for the specific implementation:
        The 'key' parameter should still be evaluated for being a single or a multi-entry delete, and needs to check
        that it received the correct type of 'indexing' argument (i.e. index, id or iterator).

        This should actually delete the item instead of just marking the particle as 'to be deleted'.
        """
        self.remove_single_by_index(key)

    def delete_by_index(self, index):
        """
        This method deletes a particle from the  the collection based on its index. It does not return the deleted item.
        Semantically, the function appears similar to the 'remove' operation. That said, the function in OceanParcels -
        instead of directly deleting the particle - just raises the 'deleted' status flag for the indexed particle.
        In result, the particle still remains in the collection. The functional interpretation of the 'deleted' status
        is handled by 'recovery' dictionary during simulation execution.
        """
        super().delete_by_index(index)
        # self._collection[index].state = OperationCode.Delete
        if index >= 0 and index < len(self._data):
            try:
                self._data[index].data.state = OperationCode.Delete
            except ValueError:
                pass

    def delete_by_ID(self, id):
        """
        This method deletes a particle from the  the collection based on its ID. It does not return the deleted item.
        Semantically, the function appears similar to the 'remove' operation. That said, the function in OceanParcels -
        instead of directly deleting the particle - just raises the 'deleted' status flag for the indexed particle.
        In result, the particle still remains in the collection. The functional interpretation of the 'deleted' status
        is handled by 'recovery' dictionary during simulation execution.
        """
        super().delete_by_ID(id)
        index = self.get_index_by_ID(id)
        pdata = self._data[index].data
        pdata.state = OperationCode
        self._data[index].set_data(pdata)

    def remove_single_by_index(self, index):
        """
        This function removes a (particle) object from the collection based on its index within the collection. For
        collections that are not based on random access (e.g. ordered lists, sets, trees), this function involves a
        translation of the index into the specific object reference in the collection - or (if unavoidable) the
        translation of the collection from a none-indexable, none-random-access structure into an indexable structure,
        and then perform the removal.
        In cases where a removal-by-index would result in a performance malus, it is highly-advisable to use a different
        removal functions, e.g. remove-by-object or remove-by-ID.
        """
        super().remove_single_by_index(index)
        result = self._data[index].data
        self._data[index].unlink()
        del self._data[index]
        return result
        # return self._data.pop(index, deepcopy_elem=False).data

    def remove_single_by_object(self, particle_obj):
        """
        This function removes a (particle) object from the collection based on its actual object. For collections that
        are random-access and based on indices (e.g. unordered list, vectors, arrays and dense matrices), this function
        would involves a parsing of the whole list and translation of the object into an index in the collection to
        perform the removal - which results in a significant performance malus.
        In cases where a removal-by-object would result in a performance malus, it is highly-advisable to use a different
        removal functions, e.g. remove-by-index or remove-by-ID.
        """
        super().remove_single_by_object(particle_obj)

        node = self.begin()
        result = None
        while node is not None:
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            if not node.is_valid():
                node = node.next
                continue
            result = node.data
            next_node = node.next
            if node.data == particle_obj:
                node.unlink()
                self._data.remove(node)
                break
            node = next_node
        self._ncount = len(self._data)
        return result

    def remove_single_by_node(self, ndata):
        """
        This function removes a node from the collection based on the (expected) node itself.
        :return boolean indicator if item has been located (and deleted) or not
        """
        result = True
        try:
            ndata.unlink()
            self._data.remove(ndata)
        except ValueError:
            result = False
        return result

    def remove_single_by_ID(self, id):
        """
        This function removes a (particle) object from the collection based on the object's ID. For some collections,
        this operation may involve a parsing of the whole list and translation of the object's ID into an index  or an
        object reference in the collection in order to perform the removal - which results in a significant performance
        malus.
        In cases where a removal-by-ID would result in a performance malus, it is highly-advisable to use a different
        removal functions, e.g. remove-by-object or remove-by-index.
        """
        super().remove_single_by_ID(id)

        node = self.begin()
        result = None
        while node is not None:
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            if not node.is_valid():
                node = node.next
                continue
            result = node.data
            next_node = node.next
            if node.data.id == id:
                node.unlink()
                self._data.remove(node)
                break
            node = next_node
        self._ncount = len(self._data)
        return result

    def remove_same(self, same_class):
        """
        This function removes particles from this collection that are themselves stored in another object of an equi-
        structured ParticleCollection. As the structures of both collections are the same, a more efficient M-in-N
        removal can be applied without an in-between reformatting.
        """
        super().remove_same(same_class)
        other_node = same_class.begin()
        this_node = self.begin()
        while this_node is not None and other_node is not None:
            if this_node.data.id < other_node.data.id:
                this_node = this_node.next
                continue
            if other_node.data.id < this_node.data.id:
                other_node = other_node.next
                continue
            next_node = this_node.next
            if this_node.data.id == other_node.data.id:
                this_node.unlink()
                self._data.remove(this_node)
            this_node = next_node

    def remove_collection(self, pcollection):
        """
        This function removes particles from this collection that are themselves stored in a ParticleCollection, which
        is differently structured than this one. That means the removal first requires the removal-collection to be re-
        formatted in an intermediary format, before executing the removal.
        That said, this method should still be at least as efficient as a removal via common Python collections (i.e.
        lists, dicts, numpy's nD arrays & dense arrays). Despite this, due to the reformatting, in some cases it may
        be more efficient to remove items then rather by IDs oder indices.
        """
        super().remove_collection(pcollection)
        ids = [p.id for p in pcollection]
        data_ids = [n.data.id for n in self._data]
        indices = np.in1d(data_ids, ids)
        indices = None if len(indices) == 0 else np.nonzero(indices)[0]
        if indices is not None:
            mutual_ids = data_ids[indices]
            self.remove_multi_by_IDs(mutual_ids)

    def remove_multi_by_PyCollection_Particles(self, pycollectionp):
        """
        This function removes particles from this collection, which are themselves in common Python collections, such as
        lists, dicts and numpy structures. In order to perform the removal, we can either directly remove the referred
        Particle instances (for internally-ordered collections, e.g. ordered lists, sets, trees) or we may need to parse
        each instance for its index (for random-access structures), which results in a considerable performance malus.

        For collections where removal-by-object incurs a performance malus, it is advisable to multi-remove particles
        by indices or IDs.
        """
        super().remove_multi_by_PyCollection_Particles(pycollectionp)
        ids = [p.id for p in pycollectionp]
        data_ids = [n.data.id for n in self._data]
        indices = np.in1d(data_ids, ids)
        indices = None if len(indices) == 0 else np.nonzero(indices)[0]
        if indices is not None:
            mutual_ids = data_ids[indices]
            self.remove_multi_by_IDs(mutual_ids)

    def remove_multi_by_indices(self, indices):
        """
        This function removes particles from this collection based on their indices. This works best for random-access
        collections (e.g. numpy's ndarrays, dense matrices and dense arrays), whereas internally ordered collections
        shall rather use a removal-via-object-reference strategy.
        """
        super().remove_multi_by_indices(indices)
        if type(indices) is dict:
            indices = list(indices.values())

        if len(indices) > 0:
            indices.sort(reverse=True)
            for index in indices:
                self._data[index].unlink()
                del self._data[index]
        self._ncount = len(self._data)

    def remove_multi_by_IDs(self, ids):
        """
        This function removes particles from this collection based on their IDs. For collections where this removal
        strategy would require a collection transformation or by-ID parsing, it is advisable to rather apply a removal-
        by-objects or removal-by-indices scheme.
        """
        super().remove_multi_by_IDs(ids)
        if type(ids) is dict:
            ids = list(ids.values())
        if len(ids) == 0:
            return

        node = self.begin()
        while node is not None:
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            if not node.is_valid():
                node = node.next
                continue
            next_node = node.next
            if node.data.id in ids:
                node.unlink()
                self._data.remove(node)
            node = next_node
        self._ncount = len(self._data)

    def remove_deleted(self):
        self._clear_deleted_()

    def __isub__(self, other):
        """
        This method performs an incremental removal of the equi-structured ParticleCollections, such to allow

        a -= b,

        with 'a' and 'b' begin the two equi-structured objects (or: 'b' being and individual object).
        This operation is equal to an in-place removal of (an) element(s).
        """
        if other is None:
            return
        if type(other) is type(self):
            self.remove_same(other)
        elif isinstance(other, ParticleCollection):
            self.remove_collection(other)
        elif isinstance(other, ScipyParticle):
            self.remove_single_by_object(other)
        elif isinstance(other, Node):
            self.remove_single_by_node(other)
        else:
            raise TypeError("Trying to do an incremental removal of an element of type %s, which is not supported." % type(other))
        return self

    def pop_single_by_index(self, index):
        """
        Searches for Particle at index 'index', removes that Particle from the Collection and returns that Particle (or: ParticleAccessor).
        If index is None, return last item (-1);
        If index < 0: return from 'end' of collection.
        If index is out of bounds, throws and OutOfRangeException.
        If Particle cannot be retrieved, returns None.
        """
        logger.info("pop_single_by_index() called with index = {}".format(index))
        super().pop_single_by_index(index)
        return self._data.pop(index)

    def pop_single_by_ID(self, id):
        """
        Searches for Particle with ID 'id', removes that Particle from the Collection and returns that Particle (or: ParticleAccessor).
        If Particle cannot be retrieved (e.g. because the ID is not available), returns None.
        """
        super().pop_single_by_ID(id)
        node = self.get_node_by_ID(id)
        index = self._data.bisect_left(node)
        return self._data.pop(index)

    def pop_multi_by_indices(self, indices):
        """
        Searches for Particles with the indices registered in 'indices', removes the Particles from the Collection and returns the Particles (or: their ParticleAccessors).
        If indices is None -> Particle cannot be retrieved -> Assert-Error and return None
        If index is None, return last item (-1);
        If index < 0: return from 'end' of collection.
        If index in 'indices' is out of bounds, throws and OutOfRangeException.
        If Particles cannot be retrieved, returns None.
        """
        super().pop_multi_by_indices(indices)
        results = []
        for index in indices:
            results.append(self.pop_single_by_index(index))
        return results

    def pop_multi_by_IDs(self, ids):
        """
        Searches for Particles with the IDs registered in 'ids', removes the Particles from the Collection and returns the Particles (or: their ParticleAccessors).
        If Particles cannot be retrieved (e.g. because the IDs are not available), returns None.
        """
        super().pop_multi_by_IDs(ids)
        results = []
        for id in ids:
            results.append(self.pop_single_by_ID(id))
        return results

    def _clear_deleted_(self):
        """
        This (protected) function physically removes particles from the collection whose status is set to 'DELETE'.
        It is the logical finalisation method of physically deleting particles that have been marked for deletion and
        that have not otherwise been recovered.
        This methods in heavily dependent on the actual collection type and should be implemented very specific
        to the actual data structure, to remove objects 'the fastest way possible'.
        """
        node = self.begin()
        while node is not None:
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            if not node.is_valid():
                node = node.next
                continue
            next_node = node.next
            if node.data.state == OperationCode.Delete:
                node.unlink()
                self._data.remove(node)
            node = next_node
        self._ncount = len(self._data)

    # ================================================================================================================ #
    def merge(self, same_class=None):
        """
        This function merge two strictly equally-structured ParticleCollections into one. This can be, for example,
        quite handy to merge two particle subsets that - due to continuous removal - become too small to be effective.

        On the other hand, this function can also internally merge individual particles that are tagged by status as
        being 'merged' (see the particle status for information on that).

        In order to distinguish both use cases, we can evaluate the 'same_class' parameter. In cases where this is
        'None', the merge operation semantically refers to an internal merge of individual particles - otherwise,
        it performs a 2-collection merge.

        Comment: the function can be simplified later by pre-evaluating the function parameter and then reference
        the individual, specific functions for internal- or external merge.

        The function shall return the merged ParticleCollection.
        """
        # TODO
        raise NotImplementedError

    def split(self, indices=None):
        """
        This function splits this collection into two disect equi-structured collections. The reason for it can, for
        example, be that the set exceeds a pre-defined maximum number of elements, which for performance reasons
        mandates a split.

        On the other hand, this function can also internally split individual particles that are tagged byt status as
        to be 'split' (see the particle status for information on that).

        In order to distinguish both use cases, we can evaluate the 'indices' parameter. In cases where this is
        'None', the split operation semantically refers to an internal split of individual particles - otherwise,
        it performs a collection-split.

        Comment: the function can be simplified later by pre-evaluating the function parameter and then reference
        the individual, specific functions for element- or collection split.

        The function shall return the newly created or extended Particle collection, i.e. either the collection that
        results from a collection split or this very collection, containing the newly-split particles.
        """
        # TODO
        raise NotImplementedError
    # ================================================================================================================ #

    # ==== high-level functions to execute operations (Add, Delete, Merge, Split) requested by the ==== #
    # ==== internal :variables Particle.state of each Node.                                        ==== #
    def get_deleted_item_indices(self):
        indices = [i for i, n in enumerate(self._data) if n.data.state == OperationCode.Delete]
        return indices

    def get_deleted_item_IDs(self):
        indices = [ndata.data.id for ndata in self._data if ndata.data.state == OperationCode.Delete]
        return indices

    def __sizeof__(self):
        """
        This function returns the size in actual bytes required in memory to hold the collection. Ideally and simply,
        the size is computed as follows:

        sizeof(self) = len(self) * sizeof(pclass)
        """
        size_bytes = 0
        i = 0
        node = self.begin()
        while node is not None:
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            if not node.is_valid():
                node = node.next
                continue
            size_bytes += sys.getsizeof(node.data)
            size_bytes += sys.getsizeof(self._data_c[i])
            i += 1
            node = node.next
        return size_bytes

    def clear(self):
        """
        This function physically removes all elements of the collection, yielding an empty collection as result of the
        operation.
        """
        if self._data is not None:
            self._data.clear()
        if self._data_c is not None:
            del self._data_c[:]
        self._ncount = 0

    def cstruct(self):
        """
        'cstruct' returns the ctypes mapping of the particle data. This depends on the specific structure in question.

        Nodes-structure doesn't work this way
        """
        raise NotImplementedError

    def toDictionary(self, pfile, time, deleted_only=False):
        """
        Convert all Particle data from one time step to a python dictionary.
        :param pfile: ParticleFile object requesting the conversion
        :param time: Time at which to write ParticleSet
        :param deleted_only: Flag to write only the deleted Particles or one of the following options:
            i) boolean [True, False], where if 'True', we gather deleted indices internally
            ii) list or np.array (type: [u]int[32]) of deleted indices to write
            iii) list or np.array (type: [u]int64) of deleted IDs to write
            iv) list of type(Node or derivatives) of deleted nodes to write
            v) list of type(ScipyParticle or derivatives) of deleted Particles to write
        :returns two dictionaries: one for all variables to be written each outputdt,
         and one for all variables to be written once; the recurrent-written dict includes entries for attribute 'index'

        This function depends on the specific collection in question and thus needs to be specified in specific
        derivative classes.
        """
        data_dict = {}
        data_dict_once = {}

        time = time.total_seconds() if isinstance(time, delta) else time

        indices_to_write = []
        if pfile.lasttime_written != time and \
           (pfile.write_ondelete is False or deleted_only):
            if self._ncount == 0:
                logger.warning("ParticleSet is empty on writing as array at time %g" % time)
            else:
                if deleted_only:
                    if type(deleted_only) not in [list, np.ndarray] and deleted_only in [True, 1]:
                        indices_to_write = [self.get_index_by_node(ndata) for ndata in self._data if ndata.data.state in [OperationCode.Delete, ]]
                    elif type(deleted_only) in [list, np.ndarray] and len(deleted_only) > 0:
                        if type(deleted_only[0]) in [int, np.int32, np.uint32]:
                            indices_to_write = deleted_only
                        if type(deleted_only[0]) in [np.int64, np.uint64]:
                            indices_to_write = [self.get_index_by_ID(id) for id in deleted_only]
                        elif isinstance(deleted_only[0], Node):
                            indices_to_write = [self.get_index_by_node(ndata) for ndata in deleted_only if self.get_index_by_node(ndata) is not None]
                        elif isinstance(deleted_only[0], ScipyParticle):
                            indices_to_write = [self.get_index_by_ID(pdata.id) for pdata in deleted_only if self.get_index_by_ID(pdata.id) is not None]
                else:
                    # return [i for i, p in enumerate(pd) if time - np.abs(p.dt/2) <= p.time < time + np.abs(p.dt) and np.isfinite(p.id)]
                    # logger.warn("Node list status: {}".format([n.is_valid() for n in self._data]))
                    node = self.begin()
                    while node is not None:
                        if not node.is_valid():
                            node = node.next
                            continue
                        if (time - np.abs(node.data.dt / 2)) <= node.data.time < (time + np.abs(node.data.dt)) and np.isfinite(node.data.id):
                            node_index = self.get_index_by_node(node)
                            indices_to_write.append(node_index)
                        node = node.next
                    # indices_to_write = _to_write_particles(self._data, time)
                if len(indices_to_write) > 0:
                    for var in pfile.var_names:
                        # data_dict[var] = np.array([getattr(p, var) for p in pset_towrite])
                        if 'id' in var:
                            data_dict[var] = np.array([np.int64(getattr(self._data[index].data, var)) for index in indices_to_write])
                        elif var == 'index':
                            data_dict[var] = np.array([np.int32(index) for index in indices_to_write])
                        else:
                            data_dict[var] = np.array([getattr(self._data[index].data, var) for index in indices_to_write])

                    pfile.max_index_written = np.maximum(pfile.max_index_written, np.max(indices_to_write))

                pset_errs = [self._data[index].data for index in indices_to_write if self._data[index].data.state != OperationCode.Delete and abs(time-self._data[index].data.time) > 1e-3 and np.isfinite(self._data[index].data.time)]
                for p in pset_errs:
                    logger.warning_once('time argument in pfile.write() is %g, but a particle has time % g.' % (time, p.time))
                indices_to_write.clear()

                if time not in pfile.time_written:
                    pfile.time_written.append(time)

                if len(pfile.var_names_once) > 0:
                    first_write = []
                    written_once_indices = []
                    node = self.begin()
                    while node is not None:
                        # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
                        if not node.is_valid():
                            node = node.next
                            continue
                        node_index = self.get_index_by_node(node)
                        # node.data.index
                        # if (node_index is not None) and (node_index not in pfile.written_once) and _is_particle_started_yet(node.data, time):
                        if (node_index is not None) and (node.data.id not in pfile.written_once) and _is_particle_started_yet(node.data, time):
                            first_write.append(node.data)
                            written_once_indices.append(node_index)
                        node = node.next
                    if np.any(first_write):
                        data_dict_once['id'] = np.array([p.id for p in first_write])
                        data_dict_once['index'] = np.array(written_once_indices, dtype=np.int32)
                        for var in pfile.var_names_once:
                            data_dict_once[var] = np.array([getattr(p, var) for p in first_write])
                        # data_dict_once['index'] = np.array(written_once_indices, dtype=np.int32)
                        # pfile.written_once.extend(written_once_indices)
                        pfile.written_once.extend(np.array(data_dict_once['id']).astype(dtype=np.int64).tolist())
                        first_write.clear()

            if deleted_only is False:
                pfile.lasttime_written = time

        return data_dict, data_dict_once

    def toArray(self):
        """
        This function converts (or: transforms; reformats; translates) this collection into an array-like structure
        (e.g. Python list or numpy nD array) that can be addressed by index. In the common case of 'no ID recovery',
        the global ID and the index match exactly.

        While this function may be very convenient for may users, it is STRONGLY DISADVISED to use the function too
        often, and the performance- and memory overhead malus may exceed any speed-up one could get from optimised
        data structures - in fact, for large collections with an implicit-order structure (i.e. ordered lists, sets,
        trees, etc.), this may be 'the most constly' function in any kind of simulation.

        It can be - though - useful at the final stage of a simulation to dump the results to disk.
        """
        results = []
        node = self.begin()
        while node is not None:
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            if not node.is_valid():
                node = node.next
                continue
            results.append(node.data)
            node = node.next
        return results

    def set_variable_write_status(self, var, write_status):
        """
        Method to set the write status of a Variable
        :param var: Name of the variable (string)
        :param status: Write status of the variable (True, False or 'once')
        """
        var_changed = False
        for v in self._ptype.variables:
            if v.name == var and hasattr(v, 'to_write'):
                v.to_write = write_status
                var_changed = True
        if var_changed:
            for p in self._data:
                pdata = p.data
                attrib = getattr(pdata, var)
                if hasattr(attrib, 'to_write'):
                    attrib.to_write = write_status
                setattr(pdata, var, attrib)
                p.set_data(pdata)
        if not var_changed:
            raise SyntaxError('Could not change the write status of %s, because it is not a Variable name' % var)


class ParticleAccessorNodes(BaseParticleAccessor):
    """Wrapper that provides access to particle data in the collection,
    as if interacting with the particle itself.

    :param pcoll: ParticleCollection that the represented particle
                  belongs to.
    :param index: The index at which the data for the represented
                  particle is stored in the corresponding data arrays
                  of the ParticleCollecion.
    """
    _ndata = None
    _next_dt = None

    def __init__(self, pcoll, node_data):
        """Initializes the ParticleAccessor to provide access to one
        specific particle.
        """
        super(ParticleAccessorNodes, self).__init__(pcoll)
        self._ndata = node_data
        # self._next_dt = None

    def __getattr__(self, name):
        """Get the value of an attribute of the particle.

        :param name: Name of the requested particle attribute.
        :return: The value of the particle attribute in the underlying
                 collection data array.
        """
        result = None
        if name == 'data':  # decision: return the ACTUAL particle
            result = self._ndata.data
        elif name in BaseParticleAccessor.__dict__.keys():
            result = super(ParticleAccessorNodes, self).__getattr__(name)
        elif name in type(self).__dict__.keys():
            result = object.__getattribute__(self, name)
        elif name in Node.__dict__.keys():
            result = getattr(self._ndata, name)
        else:
            try:
                result = getattr(self._ndata.data, name)
                # result = result[0] if type(result) in [np.ndarray,] else result
            except ValueError:
                pass
        return result

    def __setattr__(self, name, value):
        """Set the value of an attribute of the particle.

        :param name: Name of the particle attribute.
        :param value: Value that will be assigned to the particle
                      attribute in the underlying collection data array.
        """
        if name == 'data':
            self._ndata.set_data(value)
        if name in BaseParticleAccessor.__dict__.keys():
            super(ParticleAccessorNodes, self).__setattr__(name, value)
        elif name in type(self).__dict__.keys():
            object.__setattr__(self, name, value)
        elif name in Node.__dict__.keys():
            Node.__setattr__(self._ndata, name, value)
        else:
            setattr(self._ndata.data, name, value)

    def getPType(self):
        return self._ndata.data.getPType()

    def update_next_dt(self, next_dt=None):
        if self._ndata is not None and self._ndata.data is not None:
            self._ndata.data.update_next_dt(next_dt)
        # if next_dt is None:
        #     if self._next_dt is not None:
        #         self._ndata.data.dt = self._next_dt
        #         self._next_dt = None
        # else:
        #     self._next_dt = next_dt

    def __repr__(self):
        return repr(self._ndata.data)


class ParticleCollectionIterableNodes(BaseParticleCollectionIterable):

    def __init__(self, pcoll, reverse=False, subset=None):
        super(ParticleCollectionIterableNodes, self).__init__(pcoll, reverse, subset)

    def __iter__(self):
        return ParticleCollectionIterableNodes(pcoll=self._pcoll_immutable, reverse=self._reverse, subset=self._subset)


class ParticleCollectionIteratorNodes(BaseParticleCollectionIterator):
    """Iterator for looping over the particles in the ParticleCollection.

    :param pcoll: ParticleCollection that stores the particles.
    :param reverse: Flag to indicate reverse iteration (i.e. starting at
                    the largest index, instead of the smallest).
    :param subset: parameter not applicable, as nodes are not based on indices
    """

    def __init__(self, pcoll, reverse=False, subset=None):
        # ==== no indices ==== #
        # if subset is not None:
        #     if len(subset) > 0 and type(subset[0]) not in [int, np.int32, np.intp]:
        #         raise TypeError("Iteration over a subset of particles in the"
        #                         " particleset requires a list or numpy array"
        #                         " of indices (of type int or np.int32).")
        #     if reverse:
        #         self._indices = subset.reverse()
        #     else:
        #         self._indices = subset
        #     self.max_len = len(subset)
        # else:
        #     self.max_len = len(pcoll)
        #     if reverse:
        #         self._indices = range(self.max_len - 1, -1, -1)
        #     else:
        #         self._indices = range(self.max_len)

        self._reverse = reverse
        self._pcoll = pcoll
        self._head = None
        self._tail = None
        if not self._reverse:
            self._head = self._pcoll.begin()
            self._tail = self._pcoll.end()
        else:
            self._head = self._pcoll.end()
            self._tail = self._pcoll.begin()
        self.p = self._head

    def __next__(self):
        """Returns a ParticleAccessor for the next particle in the
        ParticleSet.
        """
        if self.p is not None:
            result = ParticleAccessorNodes(self._pcoll, self.p)
            # ==== we need to skip here deleted nodes that have been queued for deletion, but are still bound in memory ==== #
            self.p = self.p.prev if self._reverse else self.p.next
            while self.p is not None and not self.p.is_valid():
                self.p = self.p.prev if self._reverse else self.p.next
            return result
        raise StopIteration

    @property
    def current(self):
        if self.p is not None:
            return ParticleAccessorNodes(self._pcoll, self.p)
        raise IndexError

    def __repr__(self):
        dir_str = 'Backward' if self._reverse else 'Forward'
        return "%s iteration at id %s." % (dir_str, self.p.data.id)