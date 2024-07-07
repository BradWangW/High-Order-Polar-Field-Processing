import numpy as np
from tqdm import tqdm
from scipy.sparse import lil_matrix, eye, bmat
from Functions.Auxiliary import accumarray, find_indices, is_in_face, compute_planes, complex_projection, obtain_E, compute_V_boundary
from scipy.sparse.linalg import lsqr
import cvxopt



class Triangle_mesh():
    '''
    A class for processing triangle meshes by vertex and edge extending approach.
    '''
    def __init__(self, V, F):
        self.V = V
        self.F = F
        self.E = obtain_E(F)
        
        self.V_boundary = compute_V_boundary(F)
        
        self.B1, self.B2, self.normals = compute_planes(V, F)
        
        self.G_V = self.compute_angle_defect()
        
    def initialise_field_processing(self):
        self.construct_extended_mesh()
        self.construct_d1_extended()
        self.compute_face_pair_rotation()
        
    def compute_angle_defect(self):
        angles = np.zeros(self.F.shape)
        
        # Compute the angles of each face
        for i, face in tqdm(enumerate(self.F), 
                            desc='Computing angle defect', 
                            total=self.F.shape[0],
                            leave=False):
            v1, v2, v3 = self.V[face]
            angles[i, 0] = np.arccos(np.dot(v2 - v1, v3 - v1) / 
                                    (np.linalg.norm(v2 - v1) * np.linalg.norm(v3 - v1)))
            angles[i, 1] = np.arccos(np.dot(v1 - v2, v3 - v2) / 
                                    (np.linalg.norm(v1 - v2) * np.linalg.norm(v3 - v2)))
            angles[i, 2] = np.pi - angles[i, 0] - angles[i, 1]
        
        # Accumulate the angles of each vertex
        angles = accumarray(
            self.F, 
            angles
        )
        
        # For interior (boundary) vertices, the angle defect is 2pi - sum angles (pi - sum angles)
        boundVerticesMask = np.zeros(self.V.shape[0])
        if len(self.V_boundary) > 0:
            boundVerticesMask[self.V_boundary] = 1
        
        G_V = (2 - boundVerticesMask) * np.pi - angles
        return G_V
      
    def sort_neighbours(self, V, F, v, neighbours=None, extended=True):
        '''
        Sort the neighbour faces of a vertex in counter-clockwise order.
        '''
        # if extended:
        #     V = self.V_extended
        #     F = self.F_f
        # else:
        #     V = self.V
        #     F = self.F
        
        if neighbours is None:
            neighbours = np.any(F == v, axis=1)
            
        v1 = V[v].copy()
        F_neighbour = F[neighbours]
        
        # Compute the centroids of the neighbour faces
        v2s = np.mean(V[F_neighbour], axis=1).copy()
        
        # Avoid division by zero
        # while np.any(np.linalg.norm(v2s, axis=1) == 0) or np.linalg.norm(v1) == 0:
        #     v1 += 1e-6
        #     v2s += 1e-6
        
        # Sort by the angles between the centroids and the vertex
        epsilon = 1e-10
        angles = np.arccos(np.sum(v1 * v2s, axis=1) / ((np.linalg.norm(v1) + epsilon) * (np.linalg.norm(v2s, axis=1) + epsilon)))
        order = np.argsort(angles)
        
        return order
    
    def construct_extended_mesh(self):
        '''
        Construct the extended mesh from the input mesh, which consists of 
            the extended vertices, twin edges and combinatorial edges,
            face-faces, edge-faces, and vertex-faces.
        '''
        
        # sum_degree = np.sum(np.count_nonzero(self.E == v, axis=1) for v in range(self.V.shape[0]))
        
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
                    d1[len(self.F_f) + i, index] = 1
                # If the edge is opposite to the face, the orientation is negative
                elif np.where(f == e[0])[0] == np.where(f == e[1])[0] + 1 or (f[-1] == e[1] and f[0] == e[0]):
                    d1[len(self.F_f) + i, index] = -1
                else:
                    raise ValueError(f'The edge {e} is not in the face {f}, or the edge face is wrongly defined.')

        # The edges of the vertex faces are oriented counter-clockwisely, 
        # so similar to the face faces, we can directly index-search.
        for i, f in tqdm(enumerate(self.F_v), 
                         desc='Constructing d1 (face faces)', 
                         total=len(self.F_v), 
                         leave=False):
            # Find the indices of the combinatorial edges in the edge list
            indices = find_indices(
                self.E_extended, 
                np.stack([f, np.roll(f, -1)], axis=1)
            )

            d1[len(self.F_f) + len(self.F_e) + i, indices] = 1
        
        # print(d1.shape)
        # # print(d1_arr, d1_arr[:-1])
        # print(np.linalg.matrix_rank(d1.toarray()))
        # print(np.linalg.matrix_rank(d1[:-1].toarray()))
            
        self.d1 = d1
        
    def compute_face_pair_rotation(self):
        pair_rotations = np.zeros(self.E_comb.shape[0])
        
        # The ith element in F_e and E represent the same edge
        for f_e in tqdm(self.F_e, 
                        desc='Computing face pair rotations', 
                        total=len(self.F_e), 
                        leave=False):
            # Recall each f_e is formed as v1 -> v2 -> v2 -> v1
            # so that v1 -> v2 is a twin edge and v1 -> v1 is a combinatorial edge
            e1_comb = np.all(np.sort(self.E_comb, axis=1) == np.sort(f_e[[0, 3]]), axis=1)
            e2_comb = np.all(np.sort(self.E_comb, axis=1) == np.sort(f_e[[1, 2]]), axis=1)

            vec_e = self.V_extended[f_e[1]] - self.V_extended[f_e[0]]
            
            f1_f = np.where(np.any(np.isin(self.F_f, f_e[0]), axis=1))[0]
            f2_f = np.where(np.any(np.isin(self.F_f, f_e[3]), axis=1))[0]
            
            B1, B2, normals = compute_planes(self.V_extended, self.F_f[[f1_f, f2_f]][:, 0])
            f1 = self.F_f[f1_f]
            f2 = self.F_f[f2_f]
        
            U = complex_projection(B1, B2, normals, vec_e[None, :])
            # print(U)
            # U[U > 0] = U[U > 0] / np.abs(U[U > 0])
            # rotation = np.arccos(
            #     np.real(np.conjugate(U[0]) * U[1]) / (np.abs(U[0]) * np.abs(U[1]))
            # )
            rotation = np.angle(U[1]) - np.angle(U[0])
            # rotation = np.arccos((U[0] * np.conjugate(U[1])).real)
            # print(np.mod(rotation + np.pi, 2*np.pi) - np.pi)
            
            if np.all(self.E_comb[e1_comb] == f_e[[0, 3]]):
                pair_rotations[e1_comb] = rotation
            elif np.all(self.E_comb[e1_comb] == f_e[[3, 0]]):
                pair_rotations[e1_comb] = -rotation
            else:
                raise ValueError(f'{self.E_comb[e1_comb]} and {f_e[[0, 3]]} do not match.')
            
            if np.all(self.E_comb[e2_comb] == f_e[[1, 2]]):
                pair_rotations[e2_comb] = rotation
            elif np.all(self.E_comb[e2_comb] == f_e[[2, 1]]):
                pair_rotations[e2_comb] = -rotation
            else:
                raise ValueError(f'{self.E_comb[e2_comb]} and {f_e[[1, 2]]} do not match.')
            
        self.pair_rotations = pair_rotations
        
        
    def compute_thetas(self, singularities=None, indices=None):
        '''
            Compute the set of thetas for each face singularity 
            by constrained optimisation using the KKT conditions.
        '''
        if len(singularities) != len(indices):
            raise ValueError('The number of singularities and the number of indices do not match.')
            
        # Construct the index array and filter singular faces
        self.I_F = np.zeros(len(self.F_f) + len(self.F_e) + len(self.F_v))
        
        Theta = np.zeros(len(self.E_extended))
        self.F_singular = []
        self.singularities_F = {}
        self.indices_F = {}
        
        mask_removed_f = np.ones(len(self.F_f) + len(self.F_e) + len(self.F_v), dtype=bool)
        mask_removed_e = np.ones(len(self.E_extended), dtype=bool)
        
        rhs_correction = np.zeros(len(self.F_f) + len(self.F_e) + len(self.F_v))
        
        for singularity, index in tqdm(zip(singularities, indices), 
                                       desc='Processing singularities and computing thetas', 
                                       total=len(singularities), 
                                       leave=False):
            
            # Check if the singularity is in an edge or vertex
            in_F_e = np.where(np.all(
                (self.V_extended[self.F_e[:, 0]] > singularity) * (self.V_extended[self.F_e[:, 1]] < singularity), 
                axis=1
            ))[0]
            in_F_v = np.where(np.all(
                self.V_extended[[f_v[0] for f_v in self.F_v]] == singularity,
                axis=1
            ))[0]
            in_F_f = is_in_face(self.V_extended, self.F_f, singularity)
            
            # If the singularity is in an edge or vertex, assign the index
            if np.any(in_F_e):
                self.I_F[in_F_e + len(self.F_f)] = index
            elif np.any(in_F_v):
                self.I_F[in_F_v + len(self.F_f) + len(self.F_e)] = index
                
            # If the singularity is in a face, it gives thetas for the face
            elif in_F_f != None:
                self.F_singular.append(in_F_f)
                if in_F_f not in self.singularities_F.keys():
                    self.singularities_F[in_F_f] = [singularity]
                    self.indices_F[in_F_f] = [index]
                else:
                    self.singularities_F[in_F_f].append(singularity)
                    self.indices_F[in_F_f].append(index)
                
                # Find the edges of the face containing the singularity
                e_f = np.all(np.isin(self.E_extended, self.F_f[in_F_f]), axis=1)
            
                b1, b2, normal = self.B1[in_F_f], self.B2[in_F_f], self.normals[in_F_f]
                
                V1 = singularity - self.V_extended[self.E_extended[e_f, 0]]
                V2 = singularity - self.V_extended[self.E_extended[e_f, 1]]
                
                Z1 = complex_projection(b1[None, :], b2[None, :], normal[None, :], V1)
                Z2 = complex_projection(b1[None, :], b2[None, :], normal[None, :], V2)
                
                rotations = index * np.arccos(
                    np.real(np.conjugate(Z1) * Z2) / (np.abs(Z1) * np.abs(Z2))
                ).squeeze()
                
                Theta[e_f] += rotations            
                
                mask_removed_f[in_F_f] = False
                mask_removed_e[e_f] = False
                
                # For the other edge faces involving one of the computed edges, 
                # the rhs of the system needs to minus the rotation of that edge
                for j in range(3):
                    e_involved = np.where(e_f)[0][j]
                    
                    f_involved = len(self.F_f) + np.where(
                        np.sum(np.isin(self.F_e, self.E_extended[e_f][j]), axis=1) == 2
                    )[0][0]
                    
                    affect_in_d1 = -self.d1[f_involved, e_involved]
                    
                    rhs_correction[f_involved] += affect_in_d1 * rotations[j]
                
            else:
                raise ValueError(f'The singularity {singularity} is not in any face, edge or vertex.')
                
        # Independent quantities for quadratic programming
        Q = eye(np.sum(mask_removed_e), format='coo')
        c = np.zeros(np.sum(mask_removed_e))
        
        # Quantities for quadratic programming dependent on the singularities
        E = self.d1[mask_removed_f][:, mask_removed_e]
        d = (2 * np.pi * self.I_F - self.G_F + rhs_correction)[mask_removed_f]
            
        # Define the system to solve the quadratic programming problem
        KKT_lhs = bmat([
            [Q, E.T],
            [E, np.zeros((E.shape[0], E.shape[0]))]
        ], format='coo')
        KKT_rhs = np.concatenate([-c, d])
        
        # Solve the quadratic programming problem
        solution, _, itn, r1norm = lsqr(KKT_lhs, KKT_rhs)[:4]
        
        Theta[mask_removed_e] = solution[:np.sum(mask_removed_e)]
        
        print(f'Theta computation iteration and residual: {itn}, {r1norm}.')

        # -----------------------------------------------------------------------------------#
        # Using package cvxopt to solve the quadratic programming problem
        # Verified: gave the same result
        # Q = cvxopt.matrix(Q.toarray())
        # c = cvxopt.matrix(c)
        # E = cvxopt.matrix(E.toarray())
        # d = cvxopt.matrix(d)

        # # Define G and h for no inequality constraints
        # G = cvxopt.matrix(np.zeros(dim_E))
        # h = cvxopt.matrix(np.zeros(dim_E[0]))

        # # Solve the quadratic program
        # solution = cvxopt.solvers.qp(Q, c, G, h, E, d)

        # # Extract the optimal solution
        # print(solution['x'])
        # -----------------------------------------------------------------------------------#
        
        return Theta
    
    def reconstruct_corners_from_thetas(self, Theta, v_init, z_init, Theta_include_pairface=False):
        '''
            Reconstruct the corner values from the thetas.
            Input:
                Theta: (num_E, ) array of thetas
                v_init: initial value for the corner in the vertex face
                z_init: initial value for the corner in the edge face
            Output:
                Us: (num_V_extended, num_singularities) complex array of corner values
        '''
        if not Theta_include_pairface:
            Theta[len(self.E_twin):] += self.pair_rotations
            # Theta = np.mod(Theta + np.pi, 2 * np.pi) - np.pi
            
        lhs = lil_matrix((len(self.E_extended) + 1, len(self.V_extended)), dtype=complex)
        lhs[np.arange(len(self.E_extended)), self.E_extended[:, 0]] = np.exp(1j * Theta)
        lhs[np.arange(len(self.E_extended)), self.E_extended[:, 1]] = -1
        lhs[-1, v_init] = 1
        
        rhs = np.zeros(len(self.E_extended) + 1, dtype=complex)
        rhs[-1] = z_init / np.abs(z_init)
        
        U, _, itn, r1norm = lsqr(lhs.tocsr(), rhs)[:4]
            
        print(f'Corner reconstruction iterations and residuals',
              itn, r1norm)
        
        return U
    
    def reconstruct_linear_from_corners(self, U):
        '''
            Reconstruct the coefficients of the linear field from the corner values.
            Input:
                Us: (num_F) complex array of corner values
                singularities_F: (num_singularities, 3) array of singularities in the faces
            Output:
                coeffs: (num_F, 2) complex array of coefficients for the linear fields
        '''
        coeffs = np.zeros((len(self.F_f), 2), dtype=complex)
        coeffs_singular = {}
        total_err = 0
        mean_itn = 0
        F_singular = {}

        for i, f in tqdm(enumerate(self.F_f),
                         desc=f'Reconstructing linear field coefficients',
                         total=len(self.F_f),
                         leave=False):
            b1 = self.B1[i][None, :]; b2 = self.B2[i][None, :]; normal = self.normals[i][None, :]
            
            # Compute the complex representation of the vertices on the face face
            Zf = complex_projection(b1, b2, normal, self.V_extended[f] - self.V_extended[f[0]])[0]
            
            Uf = U[f]
            prod = np.conjugate(Uf) * Zf

            # If the face is singular, the last row explicitly specifies 
            # the singularity (zero point of the field)
            if i in self.F_singular:
                coeffs_f = np.zeros((len(self.singularities_F[i]), 2), dtype=complex)
                sub_itn = 0
                
                # Divide the argument of the firsr corner by the number of singularities on that face
                # so that the first corner, after the multiplicative superposition,
                # aligns with the first corner of the face
                uf0 = np.exp(1j * np.angle(Uf[0]) / len(self.singularities_F[i]))
                for j, (singularity, index) in enumerate(zip(self.singularities_F[i], self.indices_F[i])):
                    zc = complex_projection(
                        b1, b2, normal, 
                        np.array([singularity - self.V_extended[f[0]]])
                    )[0, 0]
                    
                    if index == 1:
                        lhs = np.array([
                            [zc.real, -zc.imag, 1, 0],
                            [zc.imag, zc.real, 0, 1],
                            [Zf[0].real, -Zf[0].imag, 1, 0],
                            [Zf[0].imag, Zf[0].real, 0, 1]
                        ], dtype=float)
                    elif index == -1:
                        lhs = np.array([
                            [zc.real, zc.imag, 1, 0],
                            [-zc.imag, zc.real, 0, 1],
                            [Zf[0].real, Zf[0].imag, 1, 0],
                            [-Zf[0].imag, Zf[0].real, 0, 1]
                        ], dtype=float)
                    else:
                        raise ValueError('The field cannot handle face singularities with index > 1 or < 1 yet.')
                    
                    rhs = np.array([
                        0, 0, uf0.real, uf0.imag
                    ])
                    
                    result, _, itn, err = lsqr(lhs, rhs)[:4]
                    coeffs_f[j, 0] = result[0] + 1j * result[1]
                    coeffs_f[j, 1] = result[2] + 1j * result[3]
                    total_err += err
                    sub_itn += itn/(len(self.F_f) * len(self.singularities_F[i]))
                
                coeffs_singular[i] = coeffs_f
                mean_itn += sub_itn
                
            # If the face is not singular, the last row aligns the first corner 
            # value up to +-sign and scale, as for the other two corners
            else:
                lhs = np.array([
                    [prod[0].imag, prod[0].real, -Uf[0].imag, Uf[0].real],
                    [prod[1].imag, prod[1].real, -Uf[1].imag, Uf[1].real],
                    [Zf[0].real, -Zf[0].imag, 1, 0],
                    [Zf[0].imag, Zf[0].real, 0, 1]
                ], dtype=float)
                rhs = np.array([
                    0, 0, Uf[0].real, Uf[0].imag
                ])

                result, _, itn, err = lsqr(lhs, rhs)[:4]
                coeffs[i, 0] = result[0] + 1j * result[1]
                coeffs[i, 1] = result[2] + 1j * result[3]
                total_err += err
                mean_itn += itn/len(self.F_f)

        print(f'Linear field reconstruction mean iterations and total residuals',
              mean_itn, total_err)
        
        return coeffs, coeffs_singular
    
    def define_linear_field(self, coeffs, coeffs_singular):
        '''
            Define the linear field from the coefficients.
            Input:
                coeffs: (num_F, num_singularities, 2) complex array of coefficients for the linear fields
                indices_F: (num_singularities, ) array of indices for the singularities
            Output:
                linear_field: function of the linear field
        '''

        def linear_field(posis):

            # Find the faces where the points are located
            # For points on vertices or edges, all adjacent faces are considered
            # For points in faces with more than one singularities,
            # multiplicative fields are computed
            posis_extended = []
            F_involved = []
            idx_singular = []
            
            for posi in tqdm(posis, 
                             desc='Computing the linear field at the points', 
                             total=len(posis), 
                             leave=False):
                f_involved = is_in_face(self.V_extended, self.F_f, posi, include_EV=True)
                
                for i, f in enumerate(f_involved):
                    if f in self.F_singular:
                        idx_singular.append(len(posis_extended) + i)
                
                posis_extended += [posi] * len(f_involved)
                F_involved += f_involved

            posis_extended = np.array(posis_extended)
            F_involved = np.array(F_involved).flatten()
            mask = np.zeros(len(posis_extended), dtype=bool)
            mask[idx_singular] = True
            
            # Compute the linear field
            B1 = self.B1[F_involved]; B2 = self.B2[F_involved]; normals = self.normals[F_involved]
            
            Z = complex_projection(
                B1, B2, normals, 
                posis_extended - self.V_extended[self.F_f[F_involved, 0]], 
                diagonal=True
            )
            
            vectors_complex = np.zeros(len(posis_extended), dtype=complex)
            vectors_complex[~mask] = coeffs[F_involved[~mask], 0] * Z[~mask] + coeffs[F_involved[~mask], 1]
            
            for i in idx_singular:
                f = F_involved[i]
                prod = 1
                for j, index in enumerate(self.indices_F[f]):
                    coeff_singular = coeffs_singular[f][j]
                    if index == 1:
                        prod *= coeff_singular[0] * Z[i] + coeff_singular[1]
                    elif index == -1:
                        prod *= coeff_singular[0] * np.conjugate(Z[i]) + coeff_singular[1]
                    else:
                        raise ValueError('The field cannot handle face singularities with index > 1 or < 1 yet.')
                    
                vectors_complex[i] = prod

            vectors = B1 * vectors_complex.real[:, None] + B2 * vectors_complex.imag[:, None]

            return posis_extended, vectors
        
        return linear_field
    
    def vector_field(self, singularities, indices, v_init, z_init, conj=False):
        Theta = self.compute_thetas(singularities=singularities, indices=indices)
        
        U = self.reconstruct_corners_from_thetas(Theta, v_init, z_init)
        
        if conj:
            coeffs, coeffs_singular = self.reconstruct_linear_conj_from_corners(U)
            
            field = self.define_linear_conj_field(coeffs, coeffs_singular)
        
        else:
            coeffs, coeffs_singular = self.reconstruct_linear_from_corners(U)
            
            field = self.define_linear_field(coeffs, coeffs_singular)
        
        return field
    
    def vector_field_from_truth(self, coeffs_truth, singularities, indices):
        '''
            Field - dictate a,b,c,d - thetas - reconstruct U - reconstruct coefficients
            Input: 
                coeffs: (num_F, num_singularities_F, 2) complex array of coefficients for the linear fields
            Output:
                field: function of the reconstructed vector field
                field_truth: function of the truth vector field
        '''
        self.initialise_field_processing()

        F_singular = []
        singularities_F = []
        indices_F = []

        if singularities is not None:
            for singularity, index in zip(singularities, indices):
                # If the singularity is in a face, it contributes a set of thetas
                F_candidate = is_in_face(self.V_extended, self.F_f, singularity)
                
                if F_candidate is not False:
                    F_singular.append(F_candidate)
                    singularities_F.append(singularity)
                    indices_F.append(index)

        F_singular = np.array(F_singular)
        singularities_F = np.array(singularities_F)
        indices_F = np.array(indices_F)

        U_truth = np.zeros(len(self.V_extended), dtype=complex)
        
        for i, f in enumerate(self.F_f):
            b1 = self.B1[i][None, :]; b2 = self.B2[i][None, :]; normal = self.normals[i][None, :]

            V_f = self.V_extended[f]

            Z_f = complex_projection(b1, b2, normal, V_f - V_f[0])[0]

            # coeffs_truth[i, :, 0]: (num_singularities_F, )
            # Z_f: (3, )
            for j in range(3):
                U_truth[f[j]] = coeffs_truth[i, 0] * Z_f[j] + coeffs_truth[i, 1]
        U_truth = U_truth / np.abs(U_truth)

        Theta = np.angle(U_truth[self.E_extended[:, 1]]) - np.angle(U_truth[self.E_extended[:, 0]])
        # Theta = np.mod(Theta + np.pi, 2 * np.pi) - np.pi

        U = self.reconstruct_corners_from_thetas(Theta, v_init=0, z_init=U_truth[0], Theta_include_pairface=True)

        coeffs = self.reconstruct_linear_from_corners(U, singularities_F) 

        field_truth = self.define_linear_field(coeffs_truth, indices_F)

        field = self.define_linear_field(coeffs, indices_F)
        
        return field_truth, field

    def sample_points_and_vectors(self, field, num_samples=3, margin = 0.15, singular_detail=False):
        points = []
        margins = [margin] * len(self.F)
        nums_samples = [num_samples] * len(self.F)
        
        if singular_detail:
            for f in self.F_singular:
                nums_samples[f] = 20
                margins[f] = 0.025
        
        for i, f in tqdm(enumerate(self.F), desc='Sampling points and vectors', 
                         total=len(self.F), leave=False):
            num_samples = nums_samples[i]
            margin = margins[i]
            for j in range(num_samples):
                for k in range(num_samples - j):
                    # Barycentric coordinates
                    u = margin + (j / (num_samples-1)) * (1 - 3 * margin)
                    v = margin + (k / (num_samples-1)) * (1 - 3 * margin)
                    w = 1 - u - v
                    
                    # Interpolate to get the 3D point in the face
                    points.append(
                        u * self.V[f[0]] + v * self.V[f[1]] + w * self.V[f[2]]
                    )
                    
        points = np.array(points)

        posis, vectors = field(points)
        
        return posis, vectors
    





