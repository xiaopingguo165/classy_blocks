import collections

import numpy as np
import scipy.optimize

from ..util import functions as f
from ..util import constants

from .primitives import Vertex

class DeferredFunction:
    """ stores a function and its variables to be used at a later time """
    def __init__(self, callable, *args, **kwargs):
        self.callable = callable
        self.args = args
        self.kwargs = kwargs
    
    def call(self):
        return self.callable(*self.args, **self.kwargs)

class Block():
    """ a direct representation of a blockMesh block;
    contains all necessary data to create it. """
    # a more intuitive and quicker way to set patches,
    # according to this sketch: https://www.openfoam.com/documentation/user-guide/blockMesh.php
    # the same for all blocks
    face_map = {
        'bottom': (0, 1, 2, 3),
        'top': (4, 5, 6, 7),
        'left': (4, 0, 3, 7),
        'right': (5, 1, 2, 6),
        'front': (4, 5, 1, 0), 
        'back': (7, 6, 2, 3)
    }

    # pairs of vertices (index in block.vertices) along axes
    axis_pair_indexes = (
        ([0, 1], [3, 2], [4, 5], [7, 6]), # x
        ([0, 3], [1, 2], [5, 6], [4, 7]), # y
        ([0, 4], [1, 5], [2, 6], [3, 7]), # z
    )

    def __init__(self, vertices, edges):
        # a list of 8 Vertex and Edge objects for each corner/edge of the block
        self.vertices = vertices
        self.edges = edges

        # number of block cells, one number for x, y, and z direction
        # when None, try to get it from neighbour blocks
        self.n_cells = [None, None, None]

        # block grading; when None, default to 1
        self.grading = [None, None, None]

        # cellZone to which the block belongs to
        self.cell_zone = ""

        # written as a comment after block definition
        # (visible in blockMeshDict, useful for debugging)
        self.description = ""

        # patches: an example
        # self.patches = {
        #     'volute_rotating': ['left', 'top' ],
        #     'volute_walls': ['bottom'],
        # }
        self.patches = { }

        # set in Mesh.prepare_data()
        self.mesh_index = None
        
        # functions like count_to_size and some other
        # can only run after mesh.prepare_data() has been
        # done its job; this is a queue 
        self.deferred_counts = []
        self.deferred_gradings = []

    @property
    def is_count_defined(self):
        return all(self.n_cells)
    
    @property
    def is_any_count_defined(self):
        return any(self.n_cells)
    
    @property
    def is_grading_defined(self):
        return all(self.grading)

    @property
    def is_any_grading_defined(self):
        return any(self.grading)


    @classmethod
    def create_from_points(cls, points, edges=[]):
        """ create a block from a raw list of 8 points;
        edges are optional; edge's 2 vertex indexes refer to
        block.vertices list (0 - 7) """
        block = cls(
            [Vertex(p) for p in points],
            edges
        )

        block.feature = 'From points'

        return block

    ###
    ### Information
    ###
    def get_faces(self, patch):
        if patch not in self.patches:
            return []

        sides = self.patches[patch]
        faces = []

        for side in sides:
            faces.append(self.format_face(side))
        
        return faces

    def get_size(self, axis):
        # returns an approximate block dimensions:
        # if an edge is defined, use the edge.get_length(),
        # otherwise simply distance between two points
        def find_edge(index_1, index_2):
            for e in self.edges:
                if {e.block_index_1, e.block_index_2} == {index_1, index_2}:
                    return e
            return None

        def vertex_distance(index_1, index_2):
            return f.norm(
                self.vertices[index_1].point - self.vertices[index_2].point
            )
    
        def block_size(index_1, index_2):
            edge = find_edge(index_1, index_2)
            if edge:
                return edge.get_length()

            return vertex_distance(index_1, index_2)
        
        sum_edges = 0
        for pair in self.axis_pair_indexes[axis]:
            sum_edges += block_size(pair[0], pair[1])
        
        return sum_edges/4

    def get_axis_vertex_pairs(self, axis):
        """ returns 4 pairs of Vertex.mesh_indexes along given axis """
        pairs = []

        for pair in self.axis_pair_indexes[axis]:
            # TEST
            if self.vertices[pair[0]].mesh_index == self.vertices[pair[1]].mesh_index:
                # omit vertices in the same spot; there is no edge anyway
                # (wedges and pyramids)
                continue

            pairs.append({
                self.vertices[pair[0]].mesh_index,
                self.vertices[pair[1]].mesh_index
            })
        
        return pairs

    def get_axis_from_pair(self, pair):
        """ returns axis index from a given pair of vertices;
        returns None if this block does not have an edge between given pair """
        for i in range(3):
            if set(pair) in self.get_axis_vertex_pairs(i):
                return i
        
        return None

    ###
    ### Manipulation
    ###
    def set_patch(self, sides, patch_name):
        """ assign one or more block faces (self.face_map)
        to a chosen patch name """
        # see patches: an example in __init__()

        if type(sides) == str:
            sides = [sides]

        if patch_name not in self.patches:
            self.patches[patch_name] = []
        
        self.patches[patch_name] += sides
    
    def count_to_size(self, axis, cell_size):
        """ Takes the average length of all edges of the block along given axis
        and sets the number of cells to obtain the desired cell size. """
        df = DeferredFunction(self._count_to_size, axis, cell_size)
        self.deferred_counts.append(df)

    def _count_to_size(self, axis, cell_size):
        block_size = self.get_size(axis)
        count = int(block_size/cell_size)

        self.n_cells[axis] = count
        return count

    def grade_to_size(self, axis, cell_size, inverse=False):
        """ Calculate grading for given axis so that first cell will be of cell_size;
        If a negative cell_size is given, the last cell size is being set. """
        df = DeferredFunction(self._grade_to_size, axis, cell_size, inverse)
        self.deferred_gradings.append(df)

    def _grade_to_size(self, axis, cell_size, inverse=False):
        # calculate grading so that first cell will be of cell_size
        # for this axis.
        n_cells = self.n_cells[axis]
        block_size = self.get_size(axis)

        if abs(cell_size) > block_size:
            raise AssertionError(f"Cell size is larger than block size: {abs(cell_size)} > {block_size}")

        cell_sizes = []

        def fcell_size(grading):
            nonlocal cell_sizes

            cell_sizes = [1] # will be scaled later

            # returns last cell size for given grading;
            # scales everything so that block size matches the original
            l_block = 0

            for _ in range(n_cells):
                l_block += cell_sizes[-1]
                cell_sizes.append(cell_sizes[-1]*grading)

                if cell_sizes[-1] < constants.tol:
                    return
            
            cell_sizes = [c*block_size/l_block for c in cell_sizes]

            return cell_size - cell_sizes[-1]

        # find a grading that produces correct last_cell_size
        scipy.optimize.newton(fcell_size, 1)

        if inverse:
            self.grading[axis] = cell_sizes[-1]/cell_sizes[0]
        else:
            self.grading[axis] = cell_sizes[0]/cell_sizes[-1]

    ###
    ### Output/formatting
    ###
    def format_face(self, side):
        indexes = self.face_map[side]
        vertices = np.take(self.vertices, indexes)

        return "({} {} {} {})".format(
            vertices[0].mesh_index,
            vertices[1].mesh_index,
            vertices[2].mesh_index,
            vertices[3].mesh_index
        )
    
    def __repr__(self):
        """ outputs block's definition for blockMeshDict file """
        # hex definition
        output = "hex "
        # vertices
        output += " ( " + " ".join(str(v.mesh_index) for v in self.vertices) + " ) "
    
        # cellZone
        output += self.cell_zone
        # number of cells
        output += f" ({self.n_cells[0]} {self.n_cells[1]} {self.n_cells[2]}) "
        # grading: use 1 if not defined
        grading = [self.grading[i] if self.grading[i] is not None else 1 for i in range(3)]
        output += f" simpleGrading ({grading[0]} {grading[1]} {grading[2]})"

        # add a comment with block index
        output += f" // {self.mesh_index} {self.description}"

        return output
