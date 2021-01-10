import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

import spconv
from pointnet2.pointnet2_modules import PointnetSAModuleMSG
from pointnet2.pointnet2_utils import furthest_point_sample

from pvrcnn.config import PvrcnnConfig
from pvrcnn.data_classes import Boxes3D
from pvrcnn.roi_grid_pool import RoiGridPool
from pvrcnn.backbone import SparseCNN, VoxelFeatureExtractor


class BEV_FeatureGatherer(nn.Module):

    def __init__(self, cfg, voxel_offset, base_voxel_size):
        super(BEV_FeatureGatherer, self).__init__()
        self.cfg = cfg
        self.voxel_offset = voxel_offset
        self.base_voxel_size = base_voxel_size

    def normalize_grid_sample_indices(self, indices, H, W):
        """F.grid_sample expects normalized indices on (-1, +1)."""
        image_dims = torch.cuda.FloatTensor([H - 1, W - 1])
        indices = torch.min(torch.clamp(indices, 0), image_dims)
        indices = 2 * (indices / (image_dims - 1)) - 1
        return indices

    def compute_bev_indices(self, keypoint_xyz, H, W):
        """Convert xyz coordinates to fractional BEV indices."""
        indices = (keypoint_xyz[:, None, :, :2] - self.voxel_offset[:2])
        indices = indices / (self.base_voxel_size[:2] * self.cfg.strides[-1])
        indices = self.normalize_grid_sample_indices(indices, H, W)
        return indices

    def forward(self, volume, keypoint_xyz):
        """
        Project 3D voxel grid to XY-plane and gather
        BEV features using bilinear interpolation.
        """
        volume = volume.dense()
        N, C, D, H, W = volume.shape
        volume = volume.view(N, C * D, H, W)
        indices = self.compute_bev_indices(keypoint_xyz, H, W)
        features = F.grid_sample(volume, indices).squeeze(2)
        return features


class PV_RCNN(nn.Module):
    """
    For each feature volume stride, convert keypoint locations to
    continuous voxel index coordinates. Then fetch voxels within ball query.
    Raw input points are treated as an additional stride-1 voxel stage.
    """

    def __init__(self, cfg):
        super(PV_RCNN, self).__init__()
        self.pnets = self.build_pointnets(cfg)
        self.roi_grid_pool = RoiGridPool(cfg)
        self.voxel_generator, grid_shape = self.build_voxel_generator(cfg)
        self.vfe = VoxelFeatureExtractor()
        self.cnn = SparseCNN(grid_shape, cfg)
        self.bev_gatherer = self.build_bev_gatherer(cfg)
        self.cfg = cfg

    def build_voxel_generator(self, cfg):
        """Voxel-grid is reversed XYZ -> ZYX and padded in Z-axis."""
        voxel_generator = spconv.utils.VoxelGenerator(
            voxel_size=cfg.voxel_size,
            point_cloud_range=cfg.grid_bounds,
            max_voxels=cfg.max_voxels,
            max_num_points=cfg.max_num_points,
        )
        grid_shape = np.r_[voxel_generator.grid_size[::-1]] + [1, 0, 0]
        return voxel_generator, grid_shape

    def build_pointnets(self, cfg):
        """Copy channel list because PointNet modifies it in-place."""
        pnets = []
        for i in range(len(cfg.mlps)):
            pnets += [PointnetSAModuleMSG(
                npoint=-1, radii=cfg.radii[i], nsamples=cfg.nsamples[i],
                mlps=cfg.mlps[i].copy(), use_xyz=True,
            )]
        return nn.Sequential(*pnets)

    def build_bev_gatherer(self, cfg):
        bev = BEV_FeatureGatherer(
            cfg, self.cnn.voxel_offset, self.cnn.base_voxel_size)
        return bev

    def voxelize(self, points):
        """
        Compute sparse voxel grid.
        :points_in np.ndarray of shape (Np, 4)
        :points_out FloatTensor of shape (Np, 4)
        :features FloatTensor of shape (Nv, 1)
        :coordinates IntTensor of shape (Nv, 4)
        """
        features, coordinates, occupancy = self.voxel_generator.generate(points)
        coordinates = np.pad(coordinates, ((0, 0), (1, 0)), mode="constant", constant_values=0)
        from_numpy = lambda x: torch.from_numpy(x).cuda()
        points, features, coordinates, occupancy = \
            map(from_numpy, (points, features, coordinates, occupancy))
        features = self.vfe(features, occupancy)
        return points, features, coordinates

    def sample_keypoints(self, points):
        """
        Sample keypoints from raw pointcloud. Assumes unit batch size.
        :points FloatTensor of shape (N, 4).
        :return FloatTensor of shape (n_keypoints, 3),
        """
        points = points.unsqueeze(0).contiguous()
        indices = furthest_point_sample(points, self.cfg.n_keypoints)
        keypoints = points[:, indices.squeeze(0).long(), :3].contiguous()
        return keypoints

    def pnet_forward(self, cnn_out, keypoint_xyz):
        """
        Call PointNet modules to gather keypoint features
        from the intermediate 3D CNN feature maps.

        :param xyz: (B, N, 3) tensor of the xyz coordinates of the features
        :param features: (B, N, C) tensor of the descriptors of the the features
        :param new_xyz: (B, npoint, 3) tensor of the new features' xyz
        :return (B, npoint, \sum_k(mlps[k][-1])) tensor of the new_features descriptors

        """
        pnet_out = []
        for (voxel_xyz, voxel_features), pnet in zip(cnn_out, self.pnets):
            voxel_xyz = voxel_xyz.unsqueeze(0).contiguous()
            voxel_features = voxel_features.t().unsqueeze(0).contiguous()
            out = pnet(voxel_xyz, voxel_features, keypoint_xyz)[1]
            pnet_out += [out]
        return pnet_out

    def forward(self, points):
        """
        TODO: Document intermediate tensor shapes.
        """
        points, features, coordinates = self.voxelize(points)
        cnn_out, final_volume = self.cnn(features, coordinates, batch_size=1)
        cnn_out = [torch.split(points, [3, 1], dim=-1)] + cnn_out
        keypoints_xyz = self.sample_keypoints(points)
        pnet_out = self.pnet_forward(cnn_out, keypoints_xyz)
        bev_out = self.bev_gatherer(final_volume, keypoints_xyz)
        features = torch.cat(pnet_out + [bev_out], dim=1)
        proposals = Boxes3D(20 * torch.rand((25, 7)).cuda())
        pooled_features = self.roi_grid_pool(proposals, keypoints_xyz, features)
        return pooled_features


def main():
    cfg = PvrcnnConfig()
    net = PV_RCNN(cfg).cuda()
    points = np.random.uniform(0, 50, size=(120000, cfg.raw_C_in)).astype(np.float32)
    out = net(points)


if __name__ == '__main__':
    main()
