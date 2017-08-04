"""
Copyright 2016 Jaehyun Park, 2017 Robin Verschueren, 2017 Akshay Agrawal

This file is part of CVXPY.

CVXPY is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXPY is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXPY.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import division

import operator

import canonInterface
import numpy as np
import scipy.sparse as sp

import cvxpy
import cvxpy.lin_ops.lin_utils as lu
from cvxpy.reductions.inverse_data import InverseData
from cvxpy.utilities.replace_quad_forms import replace_quad_forms
from cvxpy.lin_ops.lin_op import LinOp, NO_OP
from cvxpy.problems.objective import Minimize


# TODO find best format for sparse matrices: csr, csc, dok, lil, ...
class CoeffExtractor(object):

    def __init__(self, inverse_data):
        self.id_map = inverse_data.var_offsets
        self.N = inverse_data.x_length
        self.var_shapes = inverse_data.var_shapes

    def get_coeffs(self, expr):
        if expr.is_constant():
            return self.constant(expr)
        elif expr.is_affine():
            return self.affine(expr)
        elif expr.is_quadratic():
            return self.quad_form(expr)
        else:
            raise Exception("Unknown expression type %s." % type(expr))

    def constant(self, expr):
        size = expr.size
        return sp.csr_matrix((size, self.N)), np.reshape(expr.value, (size, 1),
                                                         order='F')

    def affine(self, expr):
        """Extract A, b from an expression that is reducable to A*x + b.

        Parameters
        ----------
        expr : Expression
            The expression to process.

        Returns
        -------
        SciPy CSR matrix
            The coefficient matrix A of shape (np.prod(expr.shape), self.N).
        NumPy.ndarray
            The offset vector b of shape (np.prod(expr.shape,)).
        """
        if not expr.is_affine():
            raise ValueError("Expression is not affine")
        s, _ = expr.canonical_form
        V, I, J, b = canonInterface.get_problem_matrix([lu.create_eq(s)],
                                                       self.id_map)
        A = sp.csr_matrix((V, (I, J)), shape=(expr.size, self.N))
        return A, b.flatten()

    def extract_quadratic_coeffs(self, affine_expr, quad_forms):
        # TODO(akshayka): What are the assumptions on the affine expression
        # supplied to this method?

        # Extract affine data.
        affine_problem = cvxpy.Problem(Minimize(affine_expr), [])
        affine_inverse_data = InverseData(affine_problem)
        affine_id_map = affine_inverse_data.id_map
        affine_var_shapes = affine_inverse_data.var_shapes
        extractor = CoeffExtractor(affine_inverse_data)
        c, b = extractor.affine(affine_problem.objective.expr)

        # Combine affine data with quadforms.
        coeffs = {}
        for var in affine_problem.variables():
            if var.id in quad_forms:
                var_id = var.id
                orig_id = quad_forms[var_id][2].args[0].id
                var_offset = affine_id_map[var_id][0]
                var_size = affine_id_map[var_id][1]
                if quad_forms[var_id][2].P.value[0, 0] is not None:
                    c_part = c[0, var_offset:var_offset+var_size].toarray().flatten()
                    P = quad_forms[var_id][2].P.value
                    if sp.issparse(P):
                        P = P.toarray()
                    P = c_part * P
                else:
                    P = sp.diags(c[0, var_offset:var_offset+var_size].toarray().flatten())
                if orig_id in coeffs:
                    coeffs[orig_id]['P'] += P
                    coeffs[orig_id]['q'] += np.zeros(P.shape[0])
                else:
                    coeffs[orig_id] = dict()
                    coeffs[orig_id]['P'] = P
                    coeffs[orig_id]['q'] = np.zeros(P.shape[0])
            else:
                var_offset = affine_id_map[var.id][0]
                var_size = np.prod(affine_var_shapes[var.id], dtype=int)
                if var.id in coeffs:
                    coeffs[var.id]['P'] += sp.csr_matrix((var_size, var_size))
                    coeffs[var.id]['q'] += c[
                        0, var_offset:var_offset+var_size].toarray().flatten()
                else:
                    coeffs[var.id] = dict()
                    coeffs[var.id]['P'] = sp.csr_matrix((var_size, var_size))
                    coeffs[var.id]['q'] = c[
                        0, var_offset:var_offset+var_size].toarray().flatten()
        return coeffs, b

    def quad_form(self, expr):
        """Extract quadratic, linear constant parts of a quadratic objective.
        """
        # Insert no-op such that root is never a quadratic form, for easier
        # processing
        root = LinOp(NO_OP, expr.shape, [expr], [])

        # Replace quadratic forms with dummy variables.
        quad_forms = replace_quad_forms(root, {})

        # Calculate affine parts and combine them with quadratic forms to get
        # the coefficients.
        coeffs, constant = self.extract_quadratic_coeffs(root.args[0],
                                                         quad_forms)

        # Sort variables corresponding to their starting indices, in ascending
        # order.
        offsets = sorted(self.id_map.items(), key=operator.itemgetter(1))

        # Concatenate quadratic matrices and vectors
        P = sp.csr_matrix((0, 0))
        q = np.zeros(0)
        for var_id, offset in offsets:
            if var_id in coeffs:
                P = sp.block_diag([P, coeffs[var_id]['P']])
                q = np.concatenate([q, coeffs[var_id]['q']])
            else:
                shape = self.var_shapes[var_id]
                size = shape[0]*shape[1]
                P = sp.block_diag([P, sp.csr_matrix((size, size))])
                q = np.concatenate([q, np.zeros(size)])

        # TODO(akshayka): This chain of != smells of a bug.
        if P.shape[0] != P.shape[1] != self.N or q.shape[0] != self.N:
            raise RuntimeError("Resulting quadratic form does not have "
                               "appropriate dimensions")
        if constant.size != 1:
            raise RuntimeError("Constant must be a scalar")
        return P.tocsr(), q, constant[0]
