import math
from typing import Dict

import torch
import torch.nn as nn
from e3nn.o3 import Irreps, SphericalHarmonics
from e3nn.util.jit import compile_mode

import sevenn._keys as KEY
from sevenn._const import AtomGraphDataType


@compile_mode('script')
class EdgePreprocess(nn.Module):
    """
    preprocessing pos to edge vectors and edge lengths
    actually, this is calculation is redundant.
    but required for pos.requires_grad to work

    initialize edge_vec and edge_length
    from pos & edge_index & cell & cell_shift

    Only used for stress training and deleted in deploy.
    """

    def __init__(self, is_stress):
        super().__init__()
        # controlled by the upper most wrapper 'AtomGraphSequential'
        self.is_stress = is_stress
        self._is_batch_data = True

    def forward(self, data: AtomGraphDataType) -> AtomGraphDataType:
        if self._is_batch_data:
            cell = data[KEY.CELL].view(-1, 3, 3)
        else:
            cell = data[KEY.CELL].view(3, 3)
        cell_shift = data[KEY.CELL_SHIFT]
        pos = data[KEY.POS]

        batch = data[KEY.BATCH]  # for deploy, must be defined first
        if self.is_stress:
            if self._is_batch_data:  # Only for training mode
                num_batch = int(batch.max().cpu().item()) + 1
                strain = torch.zeros(
                    (num_batch, 3, 3),
                    dtype=pos.dtype,
                    device=pos.device,
                )
                strain.requires_grad_(True)
                data['_strain'] = strain

                sym_strain = 0.5 * (strain + strain.transpose(-1, -2))
                # Do not modify it to pos += or cell += !!!!!
                pos = pos + torch.bmm(
                    pos.unsqueeze(-2), sym_strain[batch]
                ).squeeze(-2)
                cell = cell + torch.bmm(cell, sym_strain)
            else:
                strain = torch.zeros(
                    (3, 3),
                    dtype=pos.dtype,
                    device=pos.device,
                )
                strain.requires_grad_(True)
                data['_strain'] = strain

                sym_strain = 0.5 * (strain + strain.transpose(-1, -2))
                pos = pos + torch.mm(pos, sym_strain)
                cell = cell + torch.mm(cell, sym_strain)

        idx_src = data[KEY.EDGE_IDX][0]
        idx_dst = data[KEY.EDGE_IDX][1]

        edge_vec = pos[idx_dst] - pos[idx_src]

        if self._is_batch_data:
            edge_vec = edge_vec + torch.einsum(
                'ni,nij->nj', cell_shift, cell[batch[idx_src]]
            )
        else:
            edge_vec = edge_vec + torch.einsum(
                'ni,ij->nj', cell_shift, cell.squeeze(0)
            )
        data[KEY.EDGE_VEC] = edge_vec
        data[KEY.EDGE_LENGTH] = torch.linalg.norm(edge_vec, dim=-1)
        return data


class BesselBasis(nn.Module):
    """
    f : (*, 1) -> (*, num_basis)
    ? make coeffs to be trainable ?
    """

    def __init__(
        self,
        num_basis: int,
        cutoff_length: float,
        trainable_coeff: bool = True,
    ):
        super().__init__()
        self.num_basis = num_basis
        self.prefactor = 2.0 / cutoff_length
        self.coeffs = torch.FloatTensor(
            [n * math.pi / cutoff_length for n in range(1, num_basis + 1)]
        )
        if trainable_coeff:
            self.coeffs = nn.Parameter(self.coeffs)

    def forward(self, r: torch.Tensor) -> torch.Tensor:
        ur = r.unsqueeze(-1)  # to fit dimension
        return self.prefactor * torch.sin(self.coeffs * ur) / ur


class PolynomialCutoff(nn.Module):
    """
    f : (*, 1) -> (*, 1)
    https://arxiv.org/pdf/2003.03123.pdf
    """

    def __init__(
        self,
        p: int,
        cutoff_length: float,
    ):
        super().__init__()
        self.cutoff_length = cutoff_length
        self.p = p
        self.coeff_p0 = (p + 1.0) * (p + 2.0) / 2.0
        self.coeff_p1 = p * (p + 2.0)
        self.coeff_p2 = p * (p + 1.0) / 2.0

    def forward(self, r: torch.Tensor) -> torch.Tensor:
        r = r / self.cutoff_length
        return (
            1
            - self.coeff_p0 * torch.pow(r, self.p)
            + self.coeff_p1 * torch.pow(r, self.p + 1.0)
            - self.coeff_p2 * torch.pow(r, self.p + 2.0)
        )


class XPLORCutoff(nn.Module):
    """
    https://hoomd-blue.readthedocs.io/en/latest/module-md-pair.html
    """

    def __init__(
        self,
        cutoff_on: float,
        cutoff_length: float,
    ):
        super().__init__()
        self.r_on = cutoff_on
        self.r_cut = cutoff_length

    def forward(self, r: torch.Tensor) -> torch.Tensor:
        # r > r_cut switch is not necessary since edges are already based on cutoff
        r_sq = r * r
        r_on_sq = self.r_on * self.r_on
        r_cut_sq = self.r_cut * self.r_cut
        return torch.where(
            r < self.r_on,
            1.0,
            (r_cut_sq - r_sq) ** 2
            * (r_cut_sq + 2 * r_sq - 3 * r_on_sq)
            / (r_cut_sq - r_on_sq) ** 3,
        )


@compile_mode('script')
class SphericalEncoding(nn.Module):
    """
    Calculate spherical harmonics from 0 to lmax
    taking displacement vector (EDGE_VEC) as input.

    lmax: maximum angular momentum quantum number used in model
    normalization : {'integral', 'component', 'norm'}
        normalization of the output tensors
        Valid options:
        * *component*: :math:`\|Y^l(x)\|^2 = 2l+1, x \in S^2`
        * *norm*: :math:`\|Y^l(x)\| = 1, x \in S^2`, ``component / sqrt(2l+1)``
        * *integral*: :math:`\int_{S^2} Y^l_m(x)^2 dx = 1`, ``component / sqrt(4pi)``

    Returns
    -------
    `torch.Tensor`
        a tensor of shape ``(..., (lmax+1)^2)``
    """

    def __init__(
        self, lmax: int, parity: int = -1, normalization: str = 'component'
    ):
        super().__init__()
        self.lmax = lmax
        self.normalization = normalization
        self.irreps_in = Irreps('1x1o') if parity == -1 else Irreps('1x1e')
        self.irreps_out = Irreps.spherical_harmonics(lmax, parity)
        self.sph = SphericalHarmonics(
            self.irreps_out,
            normalize=False,
            normalization=normalization,
            irreps_in=self.irreps_in,
        )

    def forward(self, r: torch.Tensor) -> torch.Tensor:
        return self.sph(r)


@compile_mode('script')
class EdgeEmbedding(nn.Module):
    """
    embedding layer of |r| by
    RadialBasis(|r|)*CutOff(|r|)
    f : (N_edge) -> (N_edge, basis_num)

    since this result in weights of tensor product in e3nn,
    it is nothing to do with irreps of SO(3) or something
    """

    def __init__(
        self,
        basis_module: nn.Module,
        cutoff_module: nn.Module,
        spherical_module: nn.Module,
    ):
        super().__init__()
        self.basis_function = basis_module
        self.cutoff_function = cutoff_module
        self.spherical = spherical_module

    def forward(self, data: AtomGraphDataType) -> AtomGraphDataType:
        # r = data[KEY.EDGE_LENGTH
        # TODO: consider compatibility with edge preprocess for stress
        # TODO: how about removing force from edge_vec?
        rvec = data[KEY.EDGE_VEC]
        r = torch.linalg.norm(data[KEY.EDGE_VEC], dim=-1)
        data[KEY.EDGE_LENGTH] = r

        data[KEY.EDGE_EMBEDDING] = self.basis_function(
            r
        ) * self.cutoff_function(r).unsqueeze(-1)
        data[KEY.EDGE_ATTR] = self.spherical(rvec)

        return data
