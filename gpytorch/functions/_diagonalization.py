#!/usr/bin/env python3

import torch
from torch.autograd import Function

from .. import settings
from ..utils import lanczos


class Diagonalization(Function):
    @staticmethod
    def forward(ctx, representation_tree, device, dtype, matrix_shape, max_iter, batch_shape, *matrix_args):
        r"""
        :param list matrix_args: The arguments representing the symmetric matrix A (or batch of PSD matrices A)

        :rtype: (torch.Tensor, torch.Tensor)
        :return: :attr:`Q`, :attr: `S` such that :math:`Q S Q^T \approx A`
        """
        from gpytorch.lazy import lazify

        ctx.representation_tree = representation_tree
        ctx.device = device
        ctx.dtype = dtype
        ctx.matrix_shape = matrix_shape
        ctx.max_iter = max_iter
        ctx.batch_shape = batch_shape

        # Get closure for matmul
        lazy_tsr = ctx.representation_tree(*matrix_args)
        matmul_closure = lazy_tsr._matmul
        # Do lanczos
        q_mat, t_mat = lanczos.lanczos_tridiag(
            matmul_closure,
            ctx.max_iter,
            dtype=ctx.dtype,
            device=ctx.device,
            matrix_shape=ctx.matrix_shape,
            batch_shape=ctx.batch_shape,
        )

        if ctx.batch_shape is None:
            q_mat = q_mat.unsqueeze(-3)
            t_mat = t_mat.unsqueeze(-3)
        if t_mat.ndimension() == 3:  # If we only used one probe vector
            q_mat = q_mat.unsqueeze(0)
            t_mat = t_mat.unsqueeze(0)

        mins = lazify(t_mat).diag().min(dim=-1, keepdim=True)[0].unsqueeze(-1)
        jitter_mat = (settings.tridiagonal_jitter.value() * mins) * torch.eye(
            t_mat.size(-1), device=t_mat.device, dtype=t_mat.dtype
        ).expand_as(t_mat)
        eigenvalues, eigenvectors = lanczos.lanczos_tridiag_to_diag(t_mat + jitter_mat)

        # Get orthogonal matrix and eigenvalues
        q_mat = q_mat.matmul(eigenvectors)

        if settings.memory_efficient.off():
            ctx._lazy_tsr = lazy_tsr

        if ctx.batch_shape is None:
            q_mat = q_mat.squeeze(1)
        q_mat = q_mat.squeeze(0)
        eigenvalues = eigenvalues.squeeze(0)

        to_save = list(matrix_args) + [q_mat, eigenvalues]
        ctx.save_for_backward(*to_save)
        return eigenvalues, q_mat

    @staticmethod
    def backward(ctx, evals_grad_output, evecs_grad_output):
        # backwards pass uses explicit gradients from
        # Matrix Backpropagation for Deep Networks with Structured Lazyers,
        # Ionescu, et al CVPR, 2015. https://arxiv.org/pdf/1509.07838.pdf
        # TODO: check matrix friendly backpropagation

        q_mat = ctx.saved_tensors[-2]
        eigenvalues = ctx.saved_tensors[-1]

        # (\tilde K)_{ij} = 1_{i\neq j} (\sigma_i - \sigma_j)^{-1}
        # add a small amount of jitter to ensure that no zeros are produced
        kmat = (eigenvalues.unsqueeze(-1) - eigenvalues.unsqueeze(-2) + 1e-10).reciprocal()
        kmat[..., torch.arange(kmat.shape[-1]), torch.arange(kmat.shape[-1])] = 0.0

        # dU = U(\tilde K^T \hadamard (U^T dL/dU)U^T
        inner_term = kmat.transpose(-1, -2) * q_mat.transpose(-1, -2).matmul(evecs_grad_output)
        term1 = q_mat.matmul(inner_term).matmul(q_mat.transpose(-1, -2))

        # d\Sigma = U dL/d\Sigma U^T
        term2 = q_mat.matmul(torch.diag_embed(evals_grad_output)).matmul(q_mat.transpose(-1, -2))

        # finally sum the two
        dL_dM = term1 + term2
        output = tuple([None] * 6 + [dL_dM])

        return output