# Copyright 2013 mdtraj developers
#
# This file is part of mdtraj
#
# mdtraj is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# mdtraj is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# mdtraj. If not, see http://www.gnu.org/licenses/.
"""
This module implements the MDTraj HDF5 format described in
https://github.com/rmcgibbo/mdtraj/issues/36
"""

##############################################################################
# Imports
##############################################################################

# stdlib
import os
import warnings
import inspect
from functools import wraps

import operator
from collections import namedtuple
try:
    import simplejson as json
except ImportError:
    import json

# 3rd party
import numpy as np

# ours
from mdtraj import version
import mdtraj.pdb.element as elem
from mdtraj.topology import Topology
from mdtraj.utils import in_units_of, ensure_type, import_

__all__ = ['HDF5TrajectoryFile']

##############################################################################
# Utilities
##############################################################################


def ensure_mode(*m):
    """This is a little decorator that is used inside HDF5Trajectory
    to validate that the file is open in the correct mode before doing
    a a method

    Parameters
    ----------
    m : str or list
        One or more of ['w', 'r', 'a'], giving the allowable modes
        for the method

    Examples
    --------
    class HDF5Trajectory:
        @ensure_mode('w')
        def method_that_is_only_allowed_to_be_called_in_write_mode(self):
            print 'i must be in write mode!'
    """
    def inner(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # args[0] is self on the method
            if args[0].mode in m:
                return f(*args, **kwargs)
            raise ValueError('This operation is only available when a file '
                             'is open in mode="%s".' % args[0].mode)
        # hack for to set argpsec for our custon numpydoc sphinx extension
        setattr(wrapper, '__argspec__', inspect.getargspec(f))
        return wrapper
    return inner


Frames = namedtuple('Frames', ['coordinates', 'time', 'cell_lengths', 'cell_angles',
                               'velocities', 'kineticEnergy', 'potentialEnergy',
                               'temperature', 'alchemicalLambda'])

##############################################################################
# Classes
##############################################################################


class HDF5TrajectoryFile(object):
    """Interface for reading and writing to a MDTraj HDF5 molecular
    dynanmics trajectory file, whose format is described
    `here <https://github.com/rmcgibbo/mdtraj/issues/36>`_.

    This is a file-like object, that both reading or writing depending
    on the `mode` flag. It implements the context manager protocol,
    so you can also use it with the python 'with' statement.

    The format is extremely flexible and high performance. It can hold a wide
    variety of information about a trajectory, including fields like the
    temperature and energies. Because it's built on the fantastic HDF5 library,
    it's easily extensible too.

    Parameters
    ----------
    filename : str
        Path to the file to open
    mode :  {'r, 'w'}
        Mode in which to open the file. 'r' is for reading and 'w' is for
        writing
    force_overwrite : bool
        In mode='w', how do you want to behave if a file by the name of `filename`
        already exists? if `force_overwrite=True`, it will be overwritten.
    compression : {'zlib', None}
        Apply compression to the file? This will save space, and does not
        cost too many cpu cycles, so it's recommended.

    Attributes
    ----------
    root
    title
    application
    topology
    randomState
    forcefield
    reference
    constraints

    See Also
    --------
    mdtraj.load_hdf5 : High-level wrapper that returns a ``md.Trajectory``
    """
    distance_unit = 'nanometers'

    def __init__(self, filename, mode='r', force_overwrite=True, compression='zlib'):
        self._open = False  # is the file handle currently open?
        self.mode = mode  # the mode in which the file was opened?

        if not mode in ['r', 'w', 'a']:
            raise ValueError("mode must be one of ['r', 'w', 'a']")

        if mode == 'w' and not force_overwrite and os.path.exists(filename):
            raise IOError('"%s" already exists' % filename)

        # import tables
        self.tables = import_('tables')

        if compression == 'zlib':
            compression = self.tables.Filters(complib='zlib', shuffle=True, complevel=1)
        elif compression is None:
            compression = None
        else:
            raise ValueError('compression must be either "zlib" or None')

        self._handle = self.tables.openFile(filename, mode=mode, filters=compression)
        self._open = True

        if mode == 'w':
            # what frame are we currently reading or writing at?
            self._frame_index = 0
            # do we need to write the header information?
            self._needs_initialization = True
            if not filename.endswith('.h5'):
                warnings.warn('The .h5 extension is recommended.')

        elif mode == 'a':
            try:
                self._frame_index = len(self._handle.root.coordinates)
                self._needs_initialization = False
            except tables.NoSuchNodeError:
                self._frame_index = 0
                self._needs_initialization = False
        elif mode == 'r':
            self._frame_index = 0
            self._needs_initialization = False

    @property
    @ensure_mode('r')
    def root(self):
        """Direct access to the root group of the underlying Tables HDF5 file handle.

        This can be used for random or specific access to the underlying arrays
        on disk
        """
        return self._handle.root

    #####################################################
    # title global attribute (optional)
    #####################################################

    @property
    def title(self):
        """User-defined title for the data represented in the file"""
        if hasattr(self._handle.root._v_attrs, 'title'):
            return self._handle.root._v_attrs.title
        return None

    @title.setter
    @ensure_mode('w', 'a')
    def title(self, value):
        """Set the user-defined title for the data represented in the file"""
        self._handle.root._v_attrs.title = str(value)

    #####################################################
    # application global attribute (optional)
    #####################################################

    @property
    def application(self):
        "Suite of programs that created the file"
        if hasattr(self._handle.root._v_attrs, 'application'):
            return self._handle.root._v_attrs.application
        return None

    @application.setter
    @ensure_mode('w', 'a')
    def application(self, value):
        "Set the suite of programs that created the file"
        self._handle.root._v_attrs.application = str(value)

    #####################################################
    # topology global attribute (optional, recommended)
    #####################################################

    @property
    def topology(self):
        """Get the topology out from the file

        Returns
        -------
        topology : mdtraj.Topology
            A topology object
        """
        try:
            topology_dict = json.loads(self._handle.getNode('/', name='topology')[0])
        except self.tables.NoSuchNodeError:
            return None

        topology = Topology()

        for chain_dict in sorted(topology_dict['chains'], key=operator.itemgetter('index')):
            chain = topology.add_chain()
            for residue_dict in sorted(chain_dict['residues'], key=operator.itemgetter('index')):
                residue = topology.add_residue(residue_dict['name'], chain)
                for atom_dict in sorted(residue_dict['atoms'], key=operator.itemgetter('index')):
                    try:
                        element = elem.get_by_symbol(atom_dict['element'])
                    except KeyError:
                        raise ValueError('The symbol %s isn\'t a valid element' % atom_dict['element'])
                    topology.add_atom(atom_dict['name'], element, residue)

        atoms = list(topology.atoms)
        for index1, index2 in topology_dict['bonds']:
            topology.add_bond(atoms[index1], atoms[index2])

        return topology

    @topology.setter
    @ensure_mode('w', 'a')
    def topology(self, topology_object):
        """Set the topology in the file

        Parameters
        -------
        topology_object : mdtraj.Topology
            A topology object
        """

        try:
            topology_dict = {
                'chains': [],
                'bonds': []
            }
            
            # we want to be able to handle the simtk.openmm Topology object
            # here too, for the purpose of having a reporter
            # it doesnt use decorators on the chains/residues/atoms/bonds
            # so we need to call the methods explicitly. this adds some
            # annoying bulk to the code
            chain_iter = topology_object.chains
            if not hasattr(chain_iter, '__iter__'):
                chain_iter = chain_iter()

            for chain in chain_iter:
                chain_dict = {
                    'residues': [],
                    'index': int(chain.index)
                }
                residue_iter = chain.residues
                if not hasattr(residue_iter, '__iter__'):
                    residue_iter = residue_iter()
                for residue in residue_iter:
                    residue_dict = {
                        'index': int(residue.index),
                        'name': str(residue.name),
                        'atoms': []
                    }
                    atom_iter = residue.atoms
                    if not hasattr(atom_iter, '__iter__'):
                        atom_iter = atom_iter()
                    for atom in atom_iter:
                        residue_dict['atoms'].append({
                            'index': int(atom.index),
                            'name': str(atom.name),
                            'element': str(atom.element.symbol)
                        })
                    chain_dict['residues'].append(residue_dict)
                topology_dict['chains'].append(chain_dict)

            bond_iter = topology_object.bonds
            if not hasattr(bond_iter, '__iter__'):
                 bond_iter = bond_iter()
            for atom1, atom2 in bond_iter:
                topology_dict['bonds'].append([
                    int(atom1.index),
                    int(atom2.index)
                ])

        except AttributeError as e:
            raise AttributeError('topology_object fails to implement the'
                'chains() -> residue() -> atoms() and bond() protocol. '
                'Specifically, we encountered the following %s' % e)

        # actually set the tables
        try:
            self._handle.removeNode(where='/', name='topology')
        except self.tables.NoSuchNodeError:
            pass

        data = [str(json.dumps(topology_dict))]
        if self.tables.__version__ >= '3.0.0':
            self._handle.createArray(where='/', name='topology', obj=data)
        else:
            self._handle.createArray(where='/', name='topology', object=data)

    #####################################################
    # randomState global attribute (optional)
    #####################################################

    @property
    def randomState(self):
        "State of the creators internal random number generator at the start of the simulation"
        if hasattr(self._handle.root._v_attrs, 'randomState'):
            return self._handle.root._v_attrs.randomState
        return None

    @randomState.setter
    @ensure_mode('w', 'a')
    def randomState(self, value):
        "Set the state of the creators internal random number generator at the start of the simulation"
        self._handle.root._v_attrs.randomState = str(value)

    #####################################################
    # forcefield global attribute (optional)
    #####################################################

    @property
    def forcefield(self):
        "Description of the hamiltonian used. A short, human readable string, like AMBER99sbildn."
        if hasattr(self._handle.root._v_attrs, 'forcefield'):
            return self._handle.root._v_attrs.forcefield
        return None

    @forcefield.setter
    @ensure_mode('w', 'a')
    def forcefield(self, value):
        "Set the description of the hamiltonian used. A short, human readable string, like AMBER99sbildn."
        self._handle.root._v_attrs.forcefield = str(value)

    #####################################################
    # reference global attribute (optional)
    #####################################################

    @property
    def reference(self):
        "A published reference that documents the program or parameters used to generate the data"
        if hasattr(self._handle.root._v_attrs, 'reference'):
            return self._handle.root._v_attrs.reference
        return None

    @reference.setter
    @ensure_mode('w', 'a')
    def reference(self, value):
        "Set a published reference that documents the program or parameters used to generate the data"
        self._handle.root._v_attrs.reference = str(value)

    #####################################################
    # constraints array (optional)
    #####################################################

    @property
    def constraints(self):
        """Constraints applied to the bond lengths

        Returns
        -------
        constraints : {None, np.array, dtype=[('atom1', '<i4'), ('atom2', '<i4'), ('distance', '<f4')])}
            A one dimensional array with the a int, int, float type giving
            the index of the two atoms involved in the constraints and the
            distance of the constraint. If no constraint information
            is in the file, the return value is None.
        """
        if hasattr(self._handle.root, 'constraints'):
            return self._handle.root.constraints[:]
        return None

    @constraints.setter
    @ensure_mode('w', 'a')
    def constraints(self, value):
        """Set the constraints applied to bond lengths

        Returns
        -------
        valu : np.array, dtype=[('atom1', '<i4'), ('atom2', '<i4'), ('distance', '<f4')])
            A one dimensional array with the a int, int, float type giving
            the index of the two atoms involved in the constraints and the
            distance of the constraint.
        """
        dtype = np.dtype([
                ('atom1', np.int32),
                ('atom2', np.int32),
                ('distance', np.float32)
        ])
        if not value.dtype == dtype:
            raise ValueError('Constraints must be an array with dtype=%s. '
                             'currently, I don\'t do any casting' % dtype)

        if not hasattr(self._handle.root, 'constraints'):
            self._handle.createTable(where='/', name='constraints',
                                     description=dtype)

        self._handle.root.constraints.truncate(0)
        self._handle.root.constraints.append(value)

    #####################################################
    # read/write methods for file-like behavior
    #####################################################

    @ensure_mode('r')
    def read(self, n_frames=None, stride=None, atom_indices=None):
        """Read one or more frames of data from the file

        Parameters
        ----------
        n_frames : {int, None}
            The number of frames to read. If not supplied, all of the
            remaining frames will be read.
        stride : {int, None}
            By default all of the frames will be read, but you can pass this
            flag to read a subset of of the data by grabbing only every
            `stride`-th frame from disk.
        atom_indices : {int, None}
            By default all of the atom  will be read, but you can pass this
            flag to read only a subsets of the atoms for the `coordinates` and
            `velocities` fields. Note that you will have to carefully manage
            the indices and the offsets, since the `i`-th atom in the topology
            will not necessarily correspond to the `i`-th atom in your subset.

        Notes
        -----
        If you'd like more flexible access to the data, that is available by
        using the pytables group directly, which is accessible via the
        `root` property on this class.

        Returns
        -------
        frames : namedtuple
            The returned namedtuple will have the fields "coordinates", "time", "cell_lengths",
            "cell_angles", "velocities", "kineticEnergy", "potentialEnergy",
            "temperature" and "alchemicalLambda". Each of the fields in the
            returned namedtuple will either be a numpy array or None, dependening
            on if that data was saved in the trajectory. All of the data shall be
            n units of "nanometers", "picoseconds", "kelvin", "degrees" and
            "kilojoules_per_mole".
        """
        if n_frames is None:
            n_frames = np.inf
        if stride is not None:
            stride = int(stride)

        total_n_frames = len(self._handle.root.coordinates)
        frame_slice = slice(self._frame_index, min(self._frame_index + n_frames, total_n_frames), stride)
        if frame_slice.stop - frame_slice.start == 0:
            return []

        if atom_indices is None:
            # get all of the atoms
            atom_slice = slice(None)
        else:
            atom_slice = ensure_type(atom_indices, dtype=np.int, ndim=1,
                                     name='atom_indices', warn_on_cast=False)
            if not np.all(atom_slice < self._handle.root.coordinates.shape[1]):
                raise ValueError('As a zero-based index, the entries in '
                    'atom_indices must all be less than the number of atoms '
                    'in the trajectory, %d' % self._handle.root.coordinates.shape[1])
            if not np.all(atom_slice >= 0):
                raise ValueError('The entries in atom_indices must be greater '
                    'than or equal to zero')

        def get_field(name, slice, out_units, can_be_none=True):
            try:
                node = self._handle.getNode(where='/', name=name)
                data = node.__getitem__(slice)
                data =  in_units_of(data, out_units, str(node.attrs.units))
                return data
            except self.tables.NoSuchNodeError:
                if can_be_none:
                    return None
                raise

        frames = Frames(
            coordinates = get_field('coordinates', (frame_slice, atom_slice, slice(None)),
                                    out_units='nanometers', can_be_none=False),
            time = get_field('time', frame_slice, out_units='picoseconds'),
            cell_lengths = get_field('cell_lengths', (frame_slice, slice(None)), out_units='nanometers'),
            cell_angles = get_field('cell_angles', (frame_slice, slice(None)), out_units='degrees'),
            velocities = get_field('velocities', (frame_slice, atom_slice, slice(None)), out_units='nanometers/picosecond'),
            kineticEnergy = get_field('kineticEnergy', frame_slice, out_units='kilojoules_per_mole'),
            potentialEnergy = get_field('potentialEnergy', frame_slice, out_units='kilojoules_per_mole'),
            temperature = get_field('temperature', frame_slice, out_units='kelvin'),
            alchemicalLambda = get_field('lambda', frame_slice, out_units='dimensionless')
        )

        self._frame_index += min(n_frames, total_n_frames)
        return frames

    @ensure_mode('w', 'a')
    def write(self, coordinates, time=None, cell_lengths=None, cell_angles=None,
                    velocities=None, kineticEnergy=None, potentialEnergy=None,
                    temperature=None, alchemicalLambda=None):
        """Write one or more frames of data to the file

        This method saves data that is associated with one or more simulation
        frames. Note that all of the arguments can either be raw numpy arrays
        or unitted arrays (with simtk.unit.Quantity). If the arrays are unittted,
        a unit conversion will be automatically done from the supplied units
        into the proper units for saving on disk. You won't have to worry about
        it.

        Furthermore, if you wish to save a single frame of simulation data, you
        can do so naturally, for instance by supplying a 2d array for the
        coordinates and a single float for the time. This "shape deficiency"
        will be recognized, and handled appropriately.

        Parameters
        ----------
        coordinates : np.ndarray, shape=(n_frames, n_atoms, 3)
            The cartesian coordinates of the atoms to write. By convention, the
            lengths should be in units of nanometers.
        time : np.ndarray, shape=(n_frames,), optional
            You may optionally specify the simulation time, in picoseconds
            corresponding to each frame.
        cell_lengths : np.ndarray, shape=(n_frames, 3), dtype=float32, optional
            You may optionally specify the unitcell lengths.
            The length of the periodic box in each frame, in each direction,
            `a`, `b`, `c`. By convention the lengths should be in units
            of angstroms.
        cell_angles : np.ndarray, shape=(n_frames, 3), dtype=float32, optional
            You may optionally specify the unitcell angles in each frame.
            Organized analogously to cell_lengths. Gives the alpha, beta and
            gamma angles respectively. By convention, the angles should be
            in units of degrees.
        velocities :  np.ndarray, shape=(n_frames, n_atoms, 3), optional
            You may optionally specify the cartesian components of the velocity
            for each atom in each frame. By convention, the velocities
            should be in units of nanometers / picosecond.
        kineticEnergy : np.ndarray, shape=(n_frames,), optional
            You may optionally specify the kinetic energy in each frame. By
            convention the kinetic energies should b in units of kilojoules per
            mole.
        potentialEnergy : np.ndarray, shape=(n_frames,), optional
            You may optionally specify the potential energy in each frame. By
            convention the kinetic energies should b in units of kilojoules per
            mole.
        temperature : np.ndarray, shape=(n_frames,), optional
            You may optionally specify the temperature in each frame. By
            convention the temperatures should b in units of Kelvin.
        alchemicalLambda : np.ndarray, shape=(n_frames,), optional
            You may optionally specify the alchemical lambda in each frame. These
            have no units, but are generally between zero and one.
        """


        # these must be either both present or both absent. since
        # we're going to throw an error if one is present w/o the other,
        # lets do it now.
        if cell_lengths is None and cell_angles is not None:
            raise ValueError('cell_lengths were given, but no cell_angles')
        if cell_lengths is not None and cell_angles is None:
            raise ValueError('cell_angles were given, but no cell_lengths')

        # if the input arrays are simtk.unit.Quantities, convert them
        # into md units. Note that this acts as a no-op if the user doesn't
        # have simtk.unit installed (e.g. they didn't install OpenMM)
        coordinates = in_units_of(coordinates, 'nanometers')
        time = in_units_of(time, 'picoseconds')
        cell_lengths = in_units_of(cell_lengths, 'nanometers')
        cell_angles = in_units_of(cell_angles, 'degrees')
        velocities = in_units_of(velocities, 'nanometers/picosecond')
        kineticEnergy = in_units_of(kineticEnergy, 'kilojoules_per_mole')
        potentialEnergy = in_units_of(potentialEnergy, 'kilojoules_per_mole')
        temperature = in_units_of(temperature, 'kelvin')
        alchemicalLambda = in_units_of(alchemicalLambda, 'dimensionless')

        # do typechecking and shapechecking on the arrays
        # this ensure_type method has a lot of options, but basically it lets
        # us validate most aspects of the array. Also, we can upconvert
        # on defficent ndim, which means that if the user sends in a single
        # frame of data (i.e. coordinates is shape=(n_atoms, 3)), we can
        # realize that. obviously the default mode is that they want to
        # write multiple frames at a time, so the coordinate shape is
        # (n_frames, n_atoms, 3)
        coordinates = ensure_type(coordinates, dtype=np.float32, ndim=3,
            name='coordinates', shape=(None, None, 3), can_be_none=False,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        n_frames, n_atoms, = coordinates.shape[0:2]
        time = ensure_type(time, dtype=np.float32, ndim=1,
            name='time', shape=(n_frames,), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        cell_lengths = ensure_type(cell_lengths, dtype=np.float32, ndim=2,
            name='cell_lengths', shape=(n_frames, 3), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        cell_angles = ensure_type(cell_angles, dtype=np.float32, ndim=2,
            name='cell_angles', shape=(n_frames, 3), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        velocities = ensure_type(velocities, dtype=np.float32, ndim=3,
            name='velocoties', shape=(n_frames, n_atoms, 3), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        kineticEnergy = ensure_type(kineticEnergy, dtype=np.float32, ndim=1,
            name='kineticEnergy', shape=(n_frames,), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        potentialEnergy = ensure_type(potentialEnergy, dtype=np.float32, ndim=1,
            name='potentialEnergy', shape=(n_frames,), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        temperature = ensure_type(temperature, dtype=np.float32, ndim=1,
            name='temperature', shape=(n_frames,), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)
        alchemicalLambda = ensure_type(alchemicalLambda, dtype=np.float32, ndim=1,
            name='alchemicalLambda', shape=(n_frames,), can_be_none=True,
            warn_on_cast=False, add_newaxis_on_deficient_ndim=True)

        # if this is our first call to write(), we need to create the headers
        # and the arrays in the underlying HDF5 file
        if self._needs_initialization:
            self._initialize_headers(
                n_atoms=n_atoms,
                set_coordinates=True,
                set_time=(time is not None),
                set_cell=(cell_lengths is not None or cell_angles is not None),
                set_velocities=(velocities is not None),
                set_kineticEnergy=(kineticEnergy is not None),
                set_potentialEnergy=(potentialEnergy is not None),
                set_temperature=(temperature is not None),
                set_alchemicalLambda=(alchemicalLambda is not None))
            self._needs_initialization = False

            # we need to check that that the entries that the user is trying
            # to save are actually fields in OUR file

        try:
            # try to get the nodes for all of the fields that we have
            # which are not None
            for name in ['coordinates', 'time', 'cell_angles', 'cell_lengths',
                         'velocities', 'kineticEnergy', 'potentialEnergy', 'temperature']:
                contents = locals()[name]
                if contents is not None:
                    self._handle.getNode(where='/', name=name).append(contents)
                if contents is None:
                    # for each attribute that they're not saving, we want
                    # to make sure the file doesn't explect it
                    try:
                        self._handle.getNode(where='/', name=name)
                        raise AssertionError()
                    except self.tables.NoSuchNodeError:
                        pass


            # lambda is different, since the name in the file is lambda
            # but the name in this python function is alchemicalLambda
            name = 'lambda'
            if alchemicalLambda is not None:
                self._handle.getNode(where='/', name=name).append(alchemicalLambda)
            else:
                try:
                    self._handle.getNode(where='/', name=name)
                    raise AssertionError()
                except self.tables.NoSuchNodeError:
                    pass

        except self.tables.NoSuchNodeError:
            raise ValueError("The file that you're trying to save to doesn't "
                "contain the field %s. You can always save a new trajectory "
                "and have it contain this information, but I don't allow 'ragged' "
                "arrays. If one frame is going to have %s information, then I expect "
                "all of them to. So I can't save it for just these frames. Sorry "
                "about that :)" % (name, name))
        except AssertionError:
            raise ValueError("The file that you're saving to expects each frame "
                            "to contain %s information, but you did not supply it."
                            "I don't allow 'ragged' arrays. If one frame is going "
                            "to have %s information, then I expect all of them to. "
                            % (name, name))

        self._frame_index += n_frames
        self.flush()

    def _initialize_headers(self, n_atoms, set_coordinates, set_time, set_cell,
                            set_velocities, set_kineticEnergy, set_potentialEnergy,
                            set_temperature, set_alchemicalLambda):
        self._n_atoms = n_atoms

        self._handle.root._v_attrs.conventions = 'Pande'
        self._handle.root._v_attrs.conventionVersion = '1.0'
        self._handle.root._v_attrs.program = 'MDTraj'
        self._handle.root._v_attrs.programVersion = version.short_version
        self._handle.root._v_attrs.title = 'title'

        # if the client has not the title attribute themselves, we'll
        # set it to MDTraj as a default option.
        if not hasattr(self._handle.root._v_attrs, 'application'):
            self._handle.root._v_attrs.application = 'MDTraj'

        # create arrays that store frame level informat
        if set_coordinates:
            self._handle.createEArray(where='/', name='coordinates',
                atom=self.tables.Float32Atom(), shape=(0, self._n_atoms, 3))
            self._handle.root.coordinates.attrs['units'] = 'nanometers'

        if set_time:
            self._handle.createEArray(where='/', name='time',
                atom=self.tables.Float32Atom(), shape=(0,))
            self._handle.root.time.attrs['units'] = 'picoseconds'

        if set_cell:
            self._handle.createEArray(where='/', name='cell_lengths',
                atom=self.tables.Float32Atom(), shape=(0, 3))
            self._handle.createEArray(where='/', name='cell_angles',
                atom=self.tables.Float32Atom(), shape=(0, 3))
            self._handle.root.cell_lengths.attrs['units'] = 'nanometers'
            self._handle.root.cell_angles.attrs['units'] = 'degrees'

        if set_velocities:
            self._handle.createEArray(where='/', name='velocities',
                atom=self.tables.Float32Atom(), shape=(0, self._n_atoms, 3))
            self._handle.root.velocities.attrs['units'] = 'nanometers/picosecond'

        if set_kineticEnergy:
            self._handle.createEArray(where='/', name='kineticEnergy',
                atom=self.tables.Float32Atom(), shape=(0,))
            self._handle.root.kineticEnergy.attrs['units'] = 'kilojoules_per_mole'

        if set_potentialEnergy:
            self._handle.createEArray(where='/', name='potentialEnergy',
                atom=self.tables.Float32Atom(), shape=(0,))
            self._handle.root.potentialEnergy.attrs['units'] = 'kilojoules_per_mole'

        if set_temperature:
            self._handle.createEArray(where='/', name='temperature',
                atom=self.tables.Float32Atom(), shape=(0,))
            self._handle.root.temperature.attrs['units'] = 'kelvin'

        if set_alchemicalLambda:
            self._handle.createEArray(where='/', name='lambda',
                atom=self.tables.Float32Atom(), shape=(0,))
            self._handle.getNode('/', name='lambda').attrs['units'] = 'dimensionless'

    def _validate(self):
        raise NotImplemented

        # check that all of the shapes are consistent
        # check that everything has units


    def close(self):
        "Close the HDF5 file handle"
        if self._open:
            self._handle.close()
            self._open = False

    def flush(self):
        "Write all buffered data in the to the disk file."
        if self._open:
            self._handle.flush()

    def __del__(self):
        self.close()

    def __enter__(self):
        "Support the context manager protocol"
        return self

    def __exit__(self, *exc_info):
        "Support the context manager protocol"
        self.close()