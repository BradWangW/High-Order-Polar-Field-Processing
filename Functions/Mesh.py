import numpy as np
from tqdm import tqdm
from scipy.sparse import lil_matrix, eye, bmat, diags, vstack
from Functions.Auxiliary import (accumarray, find_indices, is_in_face, compute_planes, 
                                 complex_projection, obtain_E, compute_V_boundary, 
                                 compute_unfolded_vertex, compute_barycentric_coordinates, 
                                 compute_angle_defect)
from scipy.sparse.linalg import lsqr, spsolve, spilu, LinearOperator
import cvxopt
import networkx as nx
import random


class Triangle_mesh():
    '''
    A class for processing triangle meshes by vertex and edge extending approach.
    '''
    def __init__(self, V, F):
        self.V = V
        self.F = F
        self.E = obtain_E(F)
        
        self.V_boundary = compute_V_boundary(F)
        
        self.G_V = compute_angle_defect(V, F, self.V_boundary)
        
        self.genus = (2 - (V.shape[0] - self.E.shape[0] + F.shape[0])) / 2
        
        self.B1, self.B2, self.normals = compute_planes(V, F)
        
        self.E_non_contractible_cycles = self.get_homology_basis()
        print('Genus of the mesh:', self.genus)
        
    def get_E_dual(self):
        '''
        Construct the dual edges of the mesh.
        '''
        E_dual = np.zeros((len(self.E), 2), dtype=int)
        
        # Construct the dual edges
        for i, edge in tqdm(enumerate(self.E), 
                            desc='Constructing dual edges',
                            total=len(self.E),
                            leave=False):
            faces = np.where(np.isin(self.F, edge).sum(axis=1) == 2)[0] 
            
            if len(faces) == 2:
                E_dual[i] = faces
            else:
                raise ValueError(f'Wrong number of faces found for edge {edge}: {faces}.')
        
        return E_dual

    def get_homology_basis(self):
        E_tuple = [tuple(e) for e in self.E]
        E_dual = self.get_E_dual()
        
        # Create a graph from the mesh edges
        G = nx.Graph()
        G.add_edges_from(E_tuple)
        
        T = nx.minimum_spanning_tree(G)
        T_arr = np.array(T.edges())
        
        E_included = np.any((self.E[:, None] == T_arr).all(-1) | 
                            (self.E[:, None] == T_arr[:, ::-1]).all(-1), axis=1)
        E_dual_tuple = [tuple(e) for e in E_dual[~E_included]]
        
        # Construct the dual graph, where the edges 
        # of the previous spanning tree are removed
        G_dual = nx.Graph()
        G_dual.add_edges_from(E_dual_tuple)
        T_dual = nx.minimum_spanning_tree(G_dual)
        T_dual_arr = np.array(T_dual.edges())
        
        E_dual_included = np.any((E_dual[:, None] == T_dual_arr).all(-1) | 
                                 (E_dual[:, None] == T_dual_arr[:, ::-1]).all(-1), axis=1)
        
        E_either_included = E_included | E_dual_included
        
        E_co = self.E[~E_either_included]
        
        if len(E_co) != 2*self.genus:
            raise ValueError(f"Expected {2*self.genus} non-contractible edges, but found {len(E_co)}")
        
        # List to store non-contractible cycles
        cycles = []
        G_H = []

        for cotree_edge in tqdm(E_co, 
                                desc="Finding non-contractible cycles", 
                                total=len(E_co),
                                leave=False):
            # Add the cotree edge back to form a cycle
            T.add_edge(*cotree_edge)
            
            cycle = nx.find_cycle(T, source=cotree_edge[0])
            
            # Find the cycle created by adding this edge
            cycles.append(cycle)
            
            # Remove the edge again to restore the tree
            T.remove_edge(*cotree_edge)

        return cycles
    
    def initialise_field_processing(self):
        steps = [
            self.construct_extended_mesh,
            self.construct_d1_extended,
            self.compute_face_pair_rotation,
            self.compute_homology_extended
        ]
        for step in tqdm(steps, 
                         desc='Initialising field processing', 
                         total=len(steps),
                         leave=False):
            step()
    
    def construct_extended_mesh(self):
        '''
        Construct the extended mesh from the input mesh, which consists of 
            the extended vertices, twin edges and combinatorial edges,
            face-faces, edge-faces, and vertex-faces.
        '''
        
        V_extended = []
        E_twin = np.zeros((self.F.shape[0] * 3, 2), dtype=int)
        E_comb = []
        F_f = np.zeros((self.F.shape[0], 3), dtype=int)
        F_e = []
        F_v = []
        
        # Mapping from original vertices to (corresponding) extended vertices
        V_map = {v:[] for v in range(self.V.shape[0])}
        
        # Construct face-faces and twin edges
        # Loop over original faces
        for i, f in tqdm(enumerate(self.F), 
                         desc='Constructing face-faces and twin edges',
                         leave=False,
                         total=self.F.shape[0]):
            f_f = []
            
            # For each vertex in the original face
            for v in f:
                # Add the coordinate
                V_extended.append(self.V[v])
                
                # Index of the extended vertex
                index_extended = len(V_extended) - 1
                f_f.append(index_extended)
                
                V_map[v].append(index_extended)

            # Add the face-face
            F_f[i] = f_f
            
            # Add the twin edges
            for j, k in np.stack([np.arange(3), np.roll(np.arange(3), -1)], axis=1):
                E_twin[i * 3 + j] = [f_f[j], f_f[k]]
            
        V_extended = np.array(V_extended)
            
        # Construct edge-faces
        for i, e in tqdm(enumerate(self.E), 
                         desc='Constructing edge-faces',
                         leave=False,
                         total=self.E.shape[0]):
            indices1_extended = V_map[e[0]]
            indices2_extended = V_map[e[1]]
            
            # In a triangle mesh, two faces share at most one edge,
            # so when extracting the twin edges that encompass both extended_vertices of v1 and v2,
            # Either one or two edges will be found.
            e_twin = E_twin[
                np.all(
                    np.isin(E_twin, indices1_extended + indices2_extended), 
                    axis=1
                )
            ]
            
            # If two edges are found, the edge is an interior edge
            # in which case the 4 vertices give an edge-face
            if e_twin.shape[0] == 2:
                # Check if the twin edges are aligned or opposite
                pairing = np.isin(e_twin, indices1_extended)
                
                # If aligned, reverse the second twin edge to make a proper face
                if np.all(pairing[0] == pairing[1]):
                    F_e.append(e_twin[0].tolist() + e_twin[1, ::-1].tolist())
                # If opposite, the twin edges are already proper faces
                elif np.all(pairing[0] == pairing[1][::-1]):
                    F_e.append(e_twin[0].tolist() + e_twin[1].tolist())
                else:
                    raise ValueError('The twin edges are not aligned or opposite.')
                
            # If one edge is found, the edge is a boundary edge, 
            # in which case no edge-face is formed
            elif e_twin.shape[0] == 1:
                pass
            else:
                raise ValueError(f'Wrong number of twin edges found: {e_twin}.')
        
        # Mapping for efficient construction of d1
        F_v_map_E_comb = {}
        
        # Construct vertex-faces and combinatorial edges
        for v, indices_extended in tqdm(V_map.items(), 
                                        desc='Constructing vertex-faces and combinatorial edges',
                                        leave=False):
            # Find the neighbours of the vertex
            neighbours = np.any(self.F == v, axis=1)
            F_neighbours = self.F[neighbours]
            
            mask = ~np.isin(F_neighbours, v)
            E_neighbours = np.sort(F_neighbours[mask].reshape(-1, 2), axis=1)
            
            # Sort the neighbours so that they form a Hamiltonian cycle
            order = [0]
            E_neighbours_in_cycle = np.zeros((len(E_neighbours), 2), dtype=int)
            E_neighbours_in_cycle[0] = E_neighbours[0]
            
            # After considered, the edge is changed to [-1, -1] to avoid re-consideration
            E_neighbours[0] = [-1, -1]
            
            for i in range(1, len(E_neighbours)):
                next_edge = np.where(
                    np.any(E_neighbours == E_neighbours_in_cycle[i-1, 1], axis=1)
                )[0][0]
                order.append(next_edge)
                
                # If the next edge is head-to-tail with the last edge, keep the order
                if E_neighbours[next_edge, 0] == E_neighbours_in_cycle[i-1, 1]:
                    E_neighbours_in_cycle[i] = E_neighbours[next_edge]
                # If the next edge is tail-to-tail with the last edge, reverse the order
                elif E_neighbours[next_edge, 0] != E_neighbours_in_cycle[i-1, 1]:
                    E_neighbours_in_cycle[i] = E_neighbours[next_edge][::-1]
                    
                E_neighbours[next_edge] = [-1, -1]
                
            indices_sorted = [indices_extended[i] for i in order]
            
            # Only if the vertex is adjacent to > 2 faces,
            # the vertex-face is constructed
            # Otherwise, only one combinatorial edge is formed
            if len(indices_extended) > 2:
                F_v_map_E_comb[len(F_v)] = np.arange(len(indices_sorted)) + len(E_comb)
                    
                # Add the vertex-face
                F_v.append(indices_sorted)
                
                # Construct the combinatorial edges
                E_comb += np.stack([
                    indices_sorted, 
                    np.roll(indices_sorted, -1)
                ], axis=1).tolist()

            # If the vertex is adjacent to <= 2 faces
            elif len(indices_extended) == 2:
                # Add the (one) combinatorial edge
                E_comb.append(indices_sorted)
            else:
                raise ValueError(f'Wrong number of extended vertices found for {v}: {indices_extended}.')
            
        self.V_extended = V_extended
        
        self.E_twin = E_twin
        self.E_comb = np.array(E_comb)
        self.E_extended = np.concatenate([E_twin, E_comb])
        
        self.F_f = F_f
        self.F_e = np.array(F_e)
        self.F_v = F_v
        
        self.V_map = V_map
        self.F_v_map_E_comb = F_v_map_E_comb
        
        self.G_F = np.concatenate([
            np.zeros(len(self.F_f) + len(self.F_e)),
            self.G_V
        ])
        
    def construct_d1_extended(self):
        '''
            Construct the incidence matrix d1 for the extended mesh.
        '''
        d1 = lil_matrix((len(self.F_f) + len(self.F_e) + len(self.F_v), len(self.E_extended)))
        
        # The edges of face faces are oriented f[0] -> f[1] -> f[2], 
        # so F_f[:, i:i+1%3] appears exactly the same in E_twin, 
        # thus index-searching is enough.
        for i in range(3):
            # Find the indices of the face edges in the edge list
            indices = find_indices(self.E_extended, np.stack([self.F_f[:, i], self.F_f[:, (i+1)%3]], axis=1))

            d1[np.arange(len(self.F_f)), indices] = 1

        # The edges of the edge faces are not oriented as the faces are,
        # so we check the orientation alignment
        for i, f in tqdm(enumerate(self.F_e),
                         desc='Constructing d1 (edge faces)',
                         total=len(self.F_e),
                         leave=False):
            # Find the indices of the face edges in the edge list
            indices = np.where(np.all(np.isin(self.E_extended, f), axis=1))[0]
            E_f = self.E_extended[indices]
            
            for index, e in zip(indices, E_f):
                # If the edge is aligned with the face, the orientation is positive
                if (np.where(f == e[0])[0] == np.where(f == e[1])[0] - 1) or (f[-1] == e[0] and f[0] == e[1]):
                    d1[len(self.F_f) + i, index] = -1
                # If the edge is opposite to the face, the orientation is negative
                elif (np.where(f == e[0])[0] == np.where(f == e[1])[0] + 1) or (f[-1] == e[1] and f[0] == e[0]):
                    d1[len(self.F_f) + i, index] = 1
                else:
                    raise ValueError(f'The edge {e} is not in the face {f}, or the edge face is wrongly defined.')

        # The edges of the vertex faces are oriented counter-clockwisely (same as the vertex-faces)
        # so we directly use the mapping from the vertex faces to the combinatorial edges
        for i, f in tqdm(enumerate(self.F_v), 
                         desc='Constructing d1 (vertex faces)', 
                         total=len(self.F_v), 
                         leave=False):
            d1[
                len(self.F_f) + len(self.F_e) + i, 
                self.F_v_map_E_comb[i] + len(self.E_twin)
            ] = 1
            
        self.d1 = d1
        
    def compute_face_pair_rotation(self):
        '''
            Compute the rotation between the pair of faces sharing an edge.
        '''
        pair_rotations = np.zeros(self.E_comb.shape[0])
        
        # The ith element in F_e and E represent the same edge
        for f_e in tqdm(self.F_e, 
                        desc='Computing face pair rotations', 
                        total=len(self.F_e), 
                        leave=False):
            # Recall each f_e is formed as v1 -> v2 -> v2 -> v1
            # so that v1 -> v2 is a twin edge and v1 -> v1 is a combinatorial edge
            e1_comb = np.all(np.isin(self.E_comb, f_e[[0, 3]]), axis=1)
            e2_comb = np.all(np.isin(self.E_comb, f_e[[1, 2]]), axis=1)

            vec_e = self.V_extended[f_e[1]] - self.V_extended[f_e[0]]
            
            f1_f = np.where(np.any(np.isin(self.F_f, f_e[0]), axis=1))[0]
            f2_f = np.where(np.any(np.isin(self.F_f, f_e[3]), axis=1))[0]
            
            
            B1, B2, normals = (self.B1[[f1_f, f2_f]].squeeze(), 
                               self.B2[[f1_f, f2_f]].squeeze(), 
                               self.normals[[f1_f, f2_f]].squeeze())
            
            U = complex_projection(B1, B2, normals, vec_e[None, :])
            
            rotation = np.angle(U[1] / U[0])
            
            # The rotation is positive if the edge is aligned with the order face1 -> face2
            if np.all(self.E_comb[e1_comb] == f_e[[0, 3]]):
                pair_rotations[e1_comb] = rotation
            # The rotation is negative if the edge is opposite to the order, i.e. face2 -> face1
            elif np.all(self.E_comb[e1_comb] == f_e[[3, 0]]):
                pair_rotations[e1_comb] = -rotation
            else:
                raise ValueError(f'{self.E_comb[e1_comb]} and {f_e[[3, 0]]} do not match.')
            
            # The rotation is positive if the edge is aligned with the face
            if np.all(self.E_comb[e2_comb] == f_e[[1, 2]]):
                pair_rotations[e2_comb] = rotation
            # The rotation is negative if the edge is opposite to the face
            elif np.all(self.E_comb[e2_comb] == f_e[[2, 1]]):
                pair_rotations[e2_comb] = -rotation
            else:
                raise ValueError(f'{self.E_comb[e2_comb]} and {f_e[[1, 2]]} do not match.')
            
        self.pair_rotations = pair_rotations
        
    def compute_homology_extended(self):
        '''
            Compute the homology basis for the extended mesh.
        '''
        H = np.zeros((len(self.E_non_contractible_cycles), len(self.E_extended)))
        G_H = np.zeros(len(self.E_non_contractible_cycles))
        
        for i, cycle in tqdm(enumerate(self.E_non_contractible_cycles), 
                            desc='Computing extended homology basis', 
                            total=len(self.E_non_contractible_cycles), 
                            leave=False):
            for j, e in tqdm(enumerate(cycle),
                            desc='Processing edges -> extended edges', 
                            total=len(cycle),
                            leave=False):
                
                e_next = cycle[(j+1) % len(cycle)]
                
                indices1_extended = self.V_map[e[0]]
                indices2_extended = self.V_map[e[1]]
                indices1_next_extended = self.V_map[e_next[0]]
                indices2_next_extended = self.V_map[e_next[1]]
                
                # set a corresponding twin edge to 1 or -1, depending on the orientation
                e_twin = np.where(np.all(
                        np.isin(self.E_twin, indices1_extended + indices2_extended), 
                        axis=1
                ))[0][0]
                
                if self.E_twin[e_twin][0] in indices1_extended:
                    H[i, e_twin] = 1
                    reverse = 0
                elif self.E_twin[e_twin][1] in indices1_extended:
                    H[i, e_twin] = -1
                    reverse = 1
                else:
                    raise ValueError('The twin edge is not aligned with the edge.')
                
                # For each additional combinatorial edges, check for the orientation
                e_twin_next = np.where(np.all(
                        np.isin(self.E_twin, indices1_next_extended + indices2_next_extended),
                        axis=1
                ))[0][0]
                f_v = self.F_v[e[1]]
                
                if self.E_twin[e_twin_next][0] in indices1_next_extended:
                    reverse_next = 0
                elif self.E_twin[e_twin_next][1] in indices1_next_extended:
                    reverse_next = 1
                
                start = np.where(np.isin(f_v, self.E_twin[e_twin][1 - reverse]))[0][0]
                end = np.where(np.isin(f_v, self.E_twin[e_twin_next][reverse_next]))[0][0]

                if start < end:
                    for k in range(start, end):
                        H[i, self.F_v_map_E_comb[e[1]][k] + len(self.E_twin)] = 1
                        
                        # G_H[i] += self.pair_rotations[self.F_v_map_E_comb[e[1]][k]]
                                                
                elif end < start:
                    for k in range(end, start):
                        H[i, self.F_v_map_E_comb[e[1]][k] + len(self.E_twin)] = -1
                        
                        # G_H[i] += self.pair_rotations[self.F_v_map_E_comb[e[1]][k]]
                
        self.H = H
        self.G_H = G_H
            
    def compute_thetas(self, singularities=None, indices=None, weight_comb=10, non_contractible_indices=None):
        '''
            Compute the set of thetas for each face singularity 
            by constrained optimisation using the KKT conditions.
        '''
        if len(singularities) != len(indices):
            raise ValueError('The number of singularities and the number of indices do not match.')
        if non_contractible_indices is None:
            non_contractible_indices = np.zeros(len(self.E_non_contractible_cycles))
            
        # Construct the index array and filter singular faces
        self.I_F = np.zeros(len(self.F_f) + len(self.F_e) + len(self.F_v))
        
        Theta = np.zeros(len(self.E_extended))
        
        self.F_singular = []
        self.singularities_f = {}
        self.indices_f = {}
        self.V_singular = []
        
        mask_removed_f = np.ones(len(self.F_f) + len(self.F_e) + len(self.F_v), dtype=bool)
        mask_removed_e = np.ones(len(self.E_extended), dtype=bool)
        
        # Function to compute rotations in singular faces and 
        # update the system for trivial connections
        def deal_singularity(f, singularity, index, in_face=False):
            if f not in self.F_singular:
                self.F_singular.append(f)
                self.singularities_f[f] = [singularity]
                self.indices_f[f] = [index]
            else:
                self.singularities_f[f].append(singularity)
                self.indices_f[f].append(index)
            
            # Find the edges of the face containing the singularity
            e_f = np.all(np.isin(self.E_extended, self.F_f[f]), axis=1)
        
            b1, b2, normal = self.B1[f], self.B2[f], self.normals[f]
            
            V1 = singularity - self.V_extended[self.E_extended[e_f, 0]]
            V2 = singularity - self.V_extended[self.E_extended[e_f, 1]]
            
            Z1 = complex_projection(b1[None, :], b2[None, :], normal[None, :], V1)
            Z2 = complex_projection(b1[None, :], b2[None, :], normal[None, :], V2)
            
            # If the singularity is inside the face
            if in_face:
                rotations = index * np.arccos(
                    np.real(np.conjugate(Z1) * Z2) / (np.abs(Z1) * np.abs(Z2))
                ).squeeze()
            # If the singularity is on the vertex/edge
            else:
                rotations = (np.angle(Z2) - np.angle(Z1)).squeeze()
                rotations = np.mod(rotations + np.pi, 2*np.pi) - np.pi
                
                if np.abs(np.abs(np.sum(rotations)) - 2*np.pi) < 1e-6:
                    rotations[np.argmax(np.abs(rotations))] *= -1
                if np.abs(np.sum(rotations)) > 1e-6:
                    raise ValueError(f'The total rotation {np.sum(rotations)} is not zero.')
                
                rotations *= index
            
            if np.any(np.linalg.norm(V1, axis=1) < 1e-6):
                v_singular = np.where(np.linalg.norm(V1, axis=1) < 1e-6)[0]
                rotations[v_singular] = 0
                rotations[v_singular-1] = 0
            
            Theta[e_f] += rotations            
            
            mask_removed_f[f] = False
            mask_removed_e[e_f] = False
            
        print('Processing singularities and computing thetas...')
        
        for singularity, index in tqdm(zip(singularities, indices), 
                                       desc='Processing singularities and computing thetas', 
                                       total=len(singularities), 
                                       leave=False):
            if index == 0:
                continue
            
            in_F_v = np.where(
                np.all(np.isclose(self.V, singularity[None, :]), axis=1)
            )[0]
            
            # Check if the singularity is in an edge
            vec1 = self.V_extended[self.F_e[:, 0]] - singularity[None, :]
            vec2 = self.V_extended[self.F_e[:, 1]] - singularity[None, :]
            dot = np.einsum('ij,ij->i', vec1, vec2)
            obtuse = dot < 0
            parallel = np.isclose(
                np.abs(dot), np.linalg.norm(vec1, axis=1) * np.linalg.norm(vec2, axis=1)
            )
            in_F_e = np.where(parallel * obtuse)[0]
            
            # Check if the singularity is in a face
            in_F_f = is_in_face(self.V_extended, self.F_f, singularity)
            
            # If the singularity is in a vertex, it gives the thetas for the incident faces
            if len(in_F_v) == 1:
                # self.I_F[len(self.F_f) + len(self.F_e) + in_F_v[0]] = -index
                
                if self.V_map[in_F_v[0]][0] not in self.V_singular:
                    self.V_singular += self.V_map[in_F_v[0]]
                
                # Loop over the faces incident to the vertex
                for f in np.where(np.any(np.isin(self.F, in_F_v), axis=1))[0]:
                    deal_singularity(f, singularity, index)
                
                for e_comb in self.F_v_map_E_comb[in_F_v[0]]:
                    f_e = np.where(
                        np.sum(np.isin(self.F_e, self.E_comb[e_comb]), axis=1) == 2
                    )[0]
                    
                    for idx in [[0, 3], [1, 2]]:
                        e_sub = np.where(
                            np.all(np.isin(self.E_comb, self.F_e[f_e][0, idx]), axis=1)
                        )[0]
                        mask_removed_e[e_sub + len(self.E_twin)] = False
                    
                    mask_removed_f[len(self.F_f) + f_e] = False
                
                mask_removed_f[len(self.F_f) + len(self.F_e) + in_F_v[0]] = False
                
            # If the singularity is in an edge, it gives the thetas for the incident faces
            elif len(in_F_e) == 1:
                # self.I_F[len(self.F_f) + in_F_e[0]] = -index
                
                # Loop over the two faces incident to the edge
                for i in range(2):
                    e = self.F_e[in_F_e[0]][2*i:2*(i+1)]
                    f = np.where(
                        np.sum(np.isin(self.F_f, e), axis=1) == 2
                    )[0][0]
                    
                    deal_singularity(f, singularity, index)
                    
                    comb = [[0, 3], [1, 2]][i]
                    e_comb = np.where(
                        np.all(np.isin(self.E_comb, self.F_e[in_F_e[0]][comb]), axis=1)
                    )[0]
                    mask_removed_e[e_comb + len(self.E_twin)] = False
                
                mask_removed_f[len(self.F_f) + in_F_e[0]] = False
                    
            # If the singularity is in a face, it gives thetas for the face
            elif in_F_f is not False:
                # Obtain the neighbour faces of the face containing the singularity
                # and the unfolded locations of the singularity on those faces
                deal_singularity(in_F_f, singularity, index, in_face=True)
                
                # for i in range(3):
                #     common_edge = np.stack([self.F[in_F_f], np.roll(self.F[in_F_f], -1)], axis=1)[i]
                #     f_neighbour = np.where(np.sum(np.isin(self.F, common_edge), axis=1) == 2)[0]
                #     f_neighbour = f_neighbour[f_neighbour != in_F_f][0]
                    
                #     v_far = self.V[np.setdiff1d(self.F[f_neighbour], common_edge)].squeeze()
                    
                #     singularity_unfolded = compute_unfolded_vertex(
                #         v_far, self.V[common_edge[0]], self.V[common_edge[1]], singularity
                #     )
                    
                #     deal_singularity(f_neighbour, singularity_unfolded, index)
                
                # # The face/edges of incident edge faces are zero and removed
                # for F_e in np.where(np.sum(np.isin(self.F_e, self.F_f[in_F_f]), axis=1) == 2)[0]:
                #     mask_removed_f[len(self.F_f) + F_e] = False
                    
                #     e_comb1 = np.where(np.all(np.isin(self.E_comb, self.F_e[F_e, [0, 3]]), axis=1))[0]
                #     e_comb2 = np.where(np.all(np.isin(self.E_comb, self.F_e[F_e, [1, 2]]), axis=1))[0]
                    
                #     mask_removed_e[e_comb1 + len(self.E_twin)] = False
                #     mask_removed_e[e_comb2 + len(self.E_twin)] = False
                    
            else:
                raise ValueError(f'The singularity {singularity} is not in any face, edge or vertex.')
        print('Finished processing singularities and computing thetas.')  
        
        # lens_E = np.linalg.norm(
        #     self.V_extended[self.E_extended[:, 0]] - self.V_extended[self.E_extended[:, 1]], 
        #     axis=1
        # )[mask_removed_e]
        
        # lens_E[lens_E <= 0] = np.mean(lens_E[lens_E > 0]) ** 2
        
        # weights_E = 1/lens_E
        
        # Q is the weight matrix, whose diagonal is the weights of the edges
        # Q = diags([weights_E], [0])
        Q = eye(len(self.E_extended), format='lil')
        Q[len(self.E_twin):, len(self.E_twin):] *= weight_comb
        Q = Q[mask_removed_e][:, mask_removed_e].tocoo()
        c = np.zeros(np.sum(mask_removed_e))

        # Quantities for quadratic programming dependent on the singularities
        # E = vstack([
        #     self.d1[mask_removed_f][:, mask_removed_e],
        #     self.H[:, mask_removed_e]
        # ], format='coo')
        # d = np.concatenate([
        #     - self.G_F[mask_removed_f] - self.d1[mask_removed_f] @ Theta, 
        #     2 * np.pi * np.array(non_contractible_indices) - self.G_H - self.H @ Theta
        # ])
        E = self.d1[mask_removed_f][:, mask_removed_e]
        d = - self.G_F[mask_removed_f] - self.d1[mask_removed_f] @ Theta

        # Define the system to solve the quadratic programming problem
        KKT_lhs = bmat([
            [Q, E.T],
            [E, np.zeros((E.shape[0], E.shape[0]))]
        ], format='coo')
        KKT_rhs = np.concatenate([-c, d])
        
        # Solve the quadratic programming problem
        solution, _, itn, r1norm = lsqr(KKT_lhs, KKT_rhs)[:4]
        
        Theta[mask_removed_e] = solution[:np.sum(mask_removed_e)]
        
        print('Number of rotations > pi/2: ', np.sum(np.abs(Theta) > np.pi/2))
        
        print(f'Theta computation iteration and residual: {itn}, {r1norm}.')
        
        print(f'Total combinatorial rotations: {np.sum(np.abs(Theta[len(self.E_twin):]))}.')
        
        # true_rot = np.mod(Theta + np.pi, 2*np.pi) - np.pi
        # cycle_sums = vstack([
        #     self.d1,
        #     self.H
        # ]) @ true_rot + np.concatenate([self.G_F, self.G_H])
        
        return Theta
    
    def reconstruct_corners_from_thetas(self, Theta, v_init, z_init):
        '''
            Reconstruct the corner values from the thetas.
            Input:
                Theta: (num_E, ) array of thetas
                v_init: initial value for the corner in the vertex face
                z_init: initial value for the corner in the edge face
            Output:
                Us: (num_V_extended, num_singularities) complex array of corner values
        '''
        Theta_complete = Theta.copy()
        Theta_complete[len(self.E_twin):] += self.pair_rotations

        lhs = lil_matrix((len(self.E_extended)+1, len(self.V_extended)), dtype=complex)
        lhs[np.arange(len(self.E_extended)), self.E_extended[:, 0]] = np.exp(1j * Theta_complete)
        lhs[np.arange(len(self.E_extended)), self.E_extended[:, 1]] = -1
        lhs[-1, v_init] = 1
        
        rhs = np.zeros(len(self.E_extended)+1, dtype=complex)
        rhs[-1] = z_init
        
        U, _, itn, r1norm = lsqr(lhs, rhs)[:4]
        
        print(f'Corner argument reconstruction iterations and residuals',
              itn, r1norm)
        
        return U
    
    def subdivide_faces_over_pi(self, Theta, U):
        # Edges with >pi rotations cannot be handled by linear field, 
        # so subdivisions are needed
        self.F_over_pi = []
        num_subdivisions_f = {}
        
        Theta_over_pi = np.abs(Theta) > np.pi/2
        print('Number of thetas over pi: ', np.sum(Theta_over_pi))
        
        # Loop over the edges with >pi rotations and find the faces to subdivide
        for e in np.where(Theta_over_pi[:len(self.E_twin)])[0]:
            f = np.where(
                np.sum(np.isin(self.F_f, self.E_twin[e]), axis=1) == 2
            )[0][0]
            
            # Only non-singular faces can be subdivided
            if f not in self.F_singular:
                # the number of subdivisions is based on the edge with the largest rotation
                num = np.abs(Theta[e]) // (np.pi/2) + 1
                
                if f not in self.F_over_pi:
                    self.F_over_pi.append(f)
                    num_subdivisions_f[f] = num
                else:
                    if num_subdivisions_f[f] < num:
                        num_subdivisions_f[f] = num
        
        self.U_subdivided = {}
        self.F_subdivided = {}
        self.V_subdivided = {}
            
        # Loop over the faces to subdivide and perform the subdivision
        for f in self.F_over_pi:
            N = int(num_subdivisions_f[f])
            
            # Note the face is oriented v1 -> v2 -> v3
            # so the rotation is along v1 -> v2 -> v3
            V_f = self.V_extended[self.F_f[f]]
            U_f = U[self.F_f[f]]
            
            # Obtain the signs of the rotations in Theta
            rotations = Theta[np.where(np.all(np.isin(self.E_twin, self.F_f[f]), axis=1))[0]]
            
            self.F_subdivided[f] = []
            self.V_subdivided[f] = []
            self.U_subdivided[f] = []

            # Step 2: Create the edge points
            for i in range(N + 1):
                for j in range(N + 1 - i):
                    u = i / N
                    v = j / N
                    w = (N - i - j) / N
                    
                    self.V_subdivided[f].append(u * V_f[0] + v * V_f[1] + w * V_f[2])
                    self.U_subdivided[f].append(U_f[2] * np.exp(-1j * rotations[1] * v) * np.exp(1j * rotations[2] * u))
            
            # Step 3: Create the list of triangles
            for i in range(N):
                for j in range(N - i):
                    # Calculate indices for the top triangle
                    idx1 = (i * (N + 1) - (i * (i - 1)) // 2) + j
                    idx2 = idx1 + 1
                    idx3 = idx1 + (N + 1 - i)
                    self.F_subdivided[f].append([idx1, idx2, idx3])
                    if j < N - i - 1:
                        # Calculate indices for the bottom triangle
                        idx4 = idx2
                        idx5 = idx3
                        idx6 = idx3 + 1
                        self.F_subdivided[f].append([idx4, idx5, idx6])
            
            self.F_subdivided[f] = np.array(self.F_subdivided[f])
            self.V_subdivided[f] = np.array(self.V_subdivided[f])
            self.U_subdivided[f] = np.array(self.U_subdivided[f])
    
    def reconstruct_linear_from_corners(self, U):
        '''
            Reconstruct the coefficients of the linear field from the corner values.
            Input:
                Us: (num_F) complex array of corner values
                singularities_f: (num_singularities, 3) array of singularities in the faces
            Output:
                coeffs: (num_F, 2) complex array of coefficients for the linear fields
        '''
            
        coeffs = np.zeros((len(self.F_f), 3), dtype=complex)
        coeffs_singular = {}
        coeffs_subdivided = {}
        total_err = 0
        mean_itn = 0

        for i, f in tqdm(enumerate(self.F_f),
                         desc=f'Reconstructing linear field coefficients',
                         total=len(self.F_f),
                         leave=False):
            b1 = self.B1[i][None, :]; b2 = self.B2[i][None, :]; normal = self.normals[i][None, :]
            
            # Compute the complex representation of the vertices on the face face
            Zf = complex_projection(b1, b2, normal, self.V_extended[f] - self.V_extended[f[0]])[0]
            
            Uf = U[f]

            # If the face is singular, the last row explicitly specifies 
            # the singularity (zero point of the field)
            if i in self.F_singular:
                coeffs_f = np.zeros((len(self.singularities_f[i]), 2), dtype=complex)
                sub_itn = 0
                
                Zc = np.array([
                    complex_projection(
                        b1, b2, normal, 
                        np.array([singularity - self.V_extended[f[0]]])
                    )[0, 0] for singularity in self.singularities_f[i]
                ])
                
                Uf_sub = Uf/np.sum(np.abs(self.indices_f[i]))
                
                proper_second_point = False
                for zf, uf in zip(Zf, Uf):
                    if zf not in Zc:
                        zj = zf
                        uj = uf ** (1/np.sum(np.abs(self.indices_f[i])))
                        proper_second_point = True
                        break
                    
                while not proper_second_point:
                    x = random.random()
                    y = random.random() * x
                    
                    zf = x * Zf[0] + y * Zf[1] + (1 - x - y) * Zf[2]
                    
                    if zf not in Zc:
                        zj = zf
                        uj = (x * Uf[0] + y * Uf[1] + (1 - x - y) * Uf[2]) ** (1/np.sum(np.abs(self.indices_f[i])))
                        proper_second_point = True
                
                # Divide the argument of the firsr corner by the number of singularities on that face
                # so that the first corner, after the multiplicative superposition,
                # aligns with the first corner of the face
                for j, (singularity, index) in enumerate(zip(self.singularities_f[i], self.indices_f[i])):
                    zc = complex_projection(
                        b1, b2, normal, 
                        np.array([singularity - self.V_extended[f[0]]])
                    )[0, 0]
                    
                    if index > 0:
                        lhs = np.array([
                            [zc.real, -zc.imag, 1, 0],
                            [zc.imag, zc.real, 0, 1],
                            [zj.real, -zj.imag, 1, 0],
                            [zj.imag, zj.real, 0, 1]
                        ], dtype=float)
                    elif index < 0:
                        lhs = np.array([
                            [zc.real, zc.imag, 1, 0],
                            [-zc.imag, zc.real, 0, 1],
                            [zj.real, j.imag, 1, 0],
                            [-zj.imag, zj.real, 0, 1]
                        ], dtype=float)
                    else:
                        raise ValueError('Zero index should have been filtered out.')
                    
                    rhs = np.array([
                        0, 0, uj.real, uj.imag
                    ])
                    
                    result, _, itn, err = lsqr(lhs, rhs)[:4]
                    coeffs_f[j, 0] = result[0] + 1j * result[1]
                    coeffs_f[j, 1] = result[2] + 1j * result[3]
                    
                    total_err += err
                    sub_itn += itn/(len(self.F_f) * len(self.singularities_f[i]))
                
                coeffs_singular[i] = coeffs_f
                mean_itn += sub_itn
                
            # If the face has edge rotation > pi
            elif i in self.F_over_pi:
                v0 = self.V_extended[f[0]]
                coeffs_f = np.zeros((len(self.F_subdivided[i]), 3), dtype=complex)
                sub_itn = 0
                
                # For each subdivided face, the last row aligns the first corner
                for j, f_sub in enumerate(self.F_subdivided[i]):
                    Zf_sub = complex_projection(
                        b1, b2, normal, self.V_subdivided[i][f_sub] - v0
                    )[0]
                    
                    Uf_sub = self.U_subdivided[i][f_sub]
                    
                    lhs = np.array([
                        [Zf_sub[0].real, -Zf_sub[0].imag, Zf_sub[0].real, Zf_sub[0].imag, 1, 0],
                        [Zf_sub[0].imag, Zf_sub[0].real, -Zf_sub[0].imag, Zf_sub[0].real, 0, 1],
                        [Zf_sub[1].real, -Zf_sub[1].imag, Zf_sub[1].real, Zf_sub[1].imag, 1, 0],
                        [Zf_sub[1].imag, Zf_sub[1].real, -Zf_sub[1].imag, Zf_sub[1].real, 0, 1],
                        [Zf_sub[2].real, -Zf_sub[2].imag, Zf_sub[2].real, Zf_sub[2].imag, 1, 0],
                        [Zf_sub[2].imag, Zf_sub[2].real, -Zf_sub[2].imag, Zf_sub[2].real, 0, 1]
                    ], dtype=float)
                    rhs = np.array([
                        Uf_sub[0].real, Uf_sub[0].imag, Uf_sub[1].real, Uf_sub[1].imag, Uf_sub[2].real, Uf_sub[2].imag
                    ])
                    
                    result, _, itn, err = lsqr(lhs, rhs)[:4]
                    coeffs_f[j, 0] = result[0] + 1j * result[1]
                    coeffs_f[j, 1] = result[2] + 1j * result[3]
                    coeffs_f[j, 2] = result[4] + 1j * result[5]
                    
                    total_err += err
                    sub_itn += itn/(len(self.F_f) * len(self.F_subdivided[i]))
                
                coeffs_subdivided[i] = coeffs_f
                mean_itn += sub_itn
                
            # If the face is not singular, the last row aligns the first corner 
            # value up to +-sign and scale, as for the other two corners
            else:
                
                lhs = np.array([
                    [Zf[0].real, -Zf[0].imag, Zf[0].real, Zf[0].imag, 1, 0],
                    [Zf[0].imag, Zf[0].real, -Zf[0].imag, Zf[0].real, 0, 1],
                    [Zf[1].real, -Zf[1].imag, Zf[1].real, Zf[1].imag, 1, 0],
                    [Zf[1].imag, Zf[1].real, -Zf[1].imag, Zf[1].real, 0, 1],
                    [Zf[2].real, -Zf[2].imag, Zf[2].real, Zf[2].imag, 1, 0],
                    [Zf[2].imag, Zf[2].real, -Zf[2].imag, Zf[2].real, 0, 1]
                ], dtype=float)
                rhs = np.array([
                    Uf[0].real, Uf[0].imag, Uf[1].real, Uf[1].imag, Uf[2].real, Uf[2].imag
                ])

                result, _, itn, err = lsqr(lhs, rhs)[:4]
                coeffs[i, 0] = result[0] + 1j * result[1]
                coeffs[i, 1] = result[2] + 1j * result[3]
                coeffs[i, 2] = result[4] + 1j * result[5]
                total_err += err
                mean_itn += itn/len(self.F_f)

        print(f'Linear field reconstruction mean iterations and total residuals',
              round(mean_itn, 3), total_err)
        
        return coeffs, coeffs_singular, coeffs_subdivided
    
    def sample_field(self, coeffs, coeffs_singular, coeffs_subdivided,
        num_samples=3, margin = 0.15, singular_detail=False, 
        num_samples_detail=10, margin_detail=0.05):
        
        def field(X, f_involved):
            '''
                X: (:, 3) array of 3D points
            ''' 
            Z = complex_projection(
                self.B1[f_involved], self.B2[f_involved], self.normals[f_involved],
                X - self.V_extended[self.F_f[f_involved, 0]], 
                diagonal=True
            )
            
            return coeffs[f_involved, 0] * Z + coeffs[f_involved, 1] * np.conjugate(Z) + coeffs[f_involved, 2]
        
        points = []
        vectors_complex = []
        F_involved = []
        F_trivial = np.setdiff1d(np.arange(len(self.F)), np.unique(self.F_singular + self.F_over_pi))
        
        if not singular_detail:
            num_samples_detail = num_samples
            margin_detail = margin
            
        U = []; V = []; W = []
        for j in range(num_samples_detail):
            for k in range(num_samples_detail - j):
                # Barycentric coordinates
                U.append(margin_detail + (j / (num_samples_detail-1)) * (1 - 3 * margin_detail))
                V.append(margin_detail + (k / (num_samples_detail-1)) * (1 - 3 * margin_detail))
                W.append(1 - U[-1] - V[-1])
        U = np.array(U); V = np.array(V); W = np.array(W)
                
        for f in tqdm(np.unique(self.F_singular + self.F_over_pi),
                        desc='Sampling dense points and vectors', 
                        total=len(self.F), 
                        leave=False):
        
            # Interpolate to get the 3D point in the face
            points += (
                U[:, None] * self.V[self.F[f, 0]][None, :] + \
                    V[:, None] * self.V[self.F[f, 1]][None, :] + \
                        W[:, None] * self.V[self.F[f, 2]][None, :]
            ).tolist()
            F_involved += [f] * len(U)
            
            Z = complex_projection(
                self.B1[[f] * len(U)], self.B2[[f] * len(U)], self.normals[[f] * len(U)],
                points[-len(U):] - self.V_extended[self.F_f[f, 0]], 
                diagonal=True
            )
        
            if f in self.F_singular:
                prod = 1
                for j, index in enumerate(self.indices_f[f]):
                    coeff_singular = coeffs_singular[f][j]
                    if index > 0:
                        prod *= (coeff_singular[0] * Z + coeff_singular[1]) ** np.abs(self.indices_f[f][j])
                    elif index < 0:
                        prod *= (coeff_singular[0] * np.conjugate(Z) + coeff_singular[1]) ** np.abs(self.indices_f[f][j])
                    
                vectors_complex += prod.tolist()
            
            elif f in self.F_over_pi:
                f_sub_involved = []
                for i in range(len(U)):
                    f_sub_involved.append(
                        is_in_face(self.V_subdivided[f], self.F_subdivided[f], np.array(points[-len(U):])[i])
                    )
                    
                vectors_complex += (
                    coeffs_subdivided[f][f_sub_involved, 0] * Z + coeffs_subdivided[f][f_sub_involved, 1] * np.conjugate(Z) + coeffs_subdivided[f][f_sub_involved, 2]
                ).tolist()
                
            else:
                raise ValueError(f'The {f}-th face is not singular or over pi. (araised in sampling)')
        
        for j in tqdm(range(num_samples), desc='Sampling rest points and vectors', 
                      total=num_samples, leave=False):
            for k in range(num_samples - j):
                # Barycentric coordinates
                u = margin + (j / (num_samples-1)) * (1 - 3 * margin)
                v = margin + (k / (num_samples-1)) * (1 - 3 * margin)
                w = 1 - u - v
                
                # Interpolate to get the 3D point in the face
                points += (
                    u * self.V[self.F[F_trivial, 0]] + \
                        v * self.V[self.F[F_trivial, 1]] + \
                            w * self.V[self.F[F_trivial, 2]]
                ).tolist()
                F_involved += F_trivial.tolist()
                vectors_complex += field(points[-len(F_trivial):], F_trivial).tolist()

        return np.array(points), np.array(vectors_complex), np.array(F_involved)
    
    def corner_field(self, singularities, indices, v_init, z_init, non_contractible_indices=None):
        Theta = self.compute_thetas(singularities=singularities, indices=indices, non_contractible_indices=non_contractible_indices)
        
        U = self.reconstruct_corners_from_thetas(Theta, v_init, z_init)
        
        self.subdivide_faces_over_pi(Theta, U)
        
        return U
    
    def vector_field(self, U):
        coeffs, coeffs_singular, coeffs_subdivided = self.reconstruct_linear_from_corners(U)
            
        field = self.define_linear_field(coeffs, coeffs_singular, coeffs_subdivided)
        
        return field
    
