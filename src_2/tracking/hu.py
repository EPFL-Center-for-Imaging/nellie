from src_2.io.im_info import ImInfo
from src import xp, ndi, logger
from src_2.utils.general import get_reshaped_image
import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.distance import cdist
from collections import defaultdict
import pandas as pd


class HuMomentTracking:
    def __init__(self, im_info: ImInfo, num_t=None,
                 max_distance_um=1):
        self.im_info = im_info
        self.num_t = num_t
        if num_t is None:
            self.num_t = im_info.shape[im_info.axes.index('T')]
        self.scaling = (im_info.dim_sizes['Z'], im_info.dim_sizes['Y'], im_info.dim_sizes['X'])

        self.max_distance_um = max_distance_um

        self.vector_start_coords = []
        self.vectors = []
        self.vector_magnitudes = []

        self.shape = ()

        self.im_memmap = None
        self.im_frangi_memmap = None
        self.im_distance_memmap = None
        self.im_marker_memmap = None

        self.debug = None

    def _calculate_normalized_moments(self, images):
        # I know the broadcasting is super confusing, but it makes it so much faster (400x)...

        # Assuming images is a 3D numpy array of shape (num_images, height, width)
        num_images, height, width = images.shape
        extended_images = images[:, :, :, None, None]  # shape (num_images, height, width, 1, 1)

        # Pre-compute meshgrid
        x, y = xp.meshgrid(xp.arange(width), xp.arange(height))

        # Reshape for broadcasting
        x = x[None, :, :, None, None]  # shape (1, height, width, 1, 1)
        y = y[None, :, :, None, None]  # shape (1, height, width, 1, 1)

        # Raw Moments
        M = xp.sum(extended_images * (x ** xp.arange(4)[None, None, None, :, None]) *
                   (y ** xp.arange(4)[None, None, None, None, :]), axis=(1, 2))  # shape (num_images, 4, 4)

        # Central Moments; compute x_bar and y_bar
        x_bar = M[:, 1, 0] / M[:, 0, 0]  # shape (num_images,)
        y_bar = M[:, 0, 1] / M[:, 0, 0]  # shape (num_images,)

        x_bar = x_bar[:, None, None, None, None]  # shape (num_images, 1, 1, 1, 1)
        y_bar = y_bar[:, None, None, None, None]  # shape (num_images, 1, 1, 1, 1)

        # Calculate mu using broadcasting
        mu = xp.sum(extended_images * (x - x_bar) ** xp.arange(4)[None, None, None, :, None] *
                    (y - y_bar) ** xp.arange(4)[None, None, None, None, :], axis=(1, 2))  # shape (num_images, 4, 4)

        # Normalized moments
        i_plus_j = xp.arange(4)[:, None] + xp.arange(4)[None, :]
        eta = mu / (M[:, 0, 0][:, None, None] ** ((i_plus_j[None, :, :] + 2) / 2))

        return eta

    def _calculate_hu_moments(self, eta):
        num_images = eta.shape[0]
        hu = xp.zeros((num_images, 6))  # initialize Hu moments for each image

        hu[:, 0] = eta[:, 2, 0] + eta[:, 0, 2]
        hu[:, 1] = (eta[:, 2, 0] - eta[:, 0, 2]) ** 2 + 4 * eta[:, 1, 1] ** 2
        hu[:, 2] = (eta[:, 3, 0] - 3 * eta[:, 1, 2]) ** 2 + (3 * eta[:, 2, 1] - eta[:, 0, 3]) ** 2
        hu[:, 3] = (eta[:, 3, 0] + eta[:, 1, 2]) ** 2 + (eta[:, 2, 1] + eta[:, 0, 3]) ** 2
        hu[:, 4] = (eta[:, 3, 0] - 3 * eta[:, 1, 2]) * (eta[:, 3, 0] + eta[:, 1, 2]) * \
                   ((eta[:, 3, 0] + eta[:, 1, 2]) ** 2 - 3 * (eta[:, 2, 1] + eta[:, 0, 3]) ** 2) + \
                   (3 * eta[:, 2, 1] - eta[:, 0, 3]) * (eta[:, 2, 1] + eta[:, 0, 3]) * \
                   (3 * (eta[:, 3, 0] + eta[:, 1, 2]) ** 2 - (eta[:, 2, 1] + eta[:, 0, 3]) ** 2)
        hu[:, 5] = (eta[:, 2, 0] - eta[:, 0, 2]) * \
                   ((eta[:, 3, 0] + eta[:, 1, 2]) ** 2 - (eta[:, 2, 1] + eta[:, 0, 3]) ** 2) + \
                   4 * eta[:, 1, 1] * (eta[:, 3, 0] + eta[:, 1, 2]) * (eta[:, 2, 1] + eta[:, 0, 3])
        # hu[:, 6] = (3 * eta[:, 2, 1] - eta[:, 0, 3]) * (eta[:, 3, 0] + eta[:, 1, 2]) * \
        #            ((eta[:, 3, 0] + eta[:, 1, 2]) ** 2 - 3 * (eta[:, 2, 1] + eta[:, 0, 3]) ** 2) - \
        #            (eta[:, 3, 0] - 3 * eta[:, 1, 2]) * (eta[:, 2, 1] + eta[:, 0, 3]) * \
        #            (3 * (eta[:, 3, 0] + eta[:, 1, 2]) ** 2 - (eta[:, 2, 1] + eta[:, 0, 3]) ** 2)

        return hu  # return the first 5 Hu moments for each image

    def _calculate_mean_and_variance(self, images):
        num_images = images.shape[0]
        features = xp.zeros((num_images, 2))
        mask = images != 0

        count_nonzero = xp.sum(mask, axis=(1, 2, 3))
        sum_nonzero = xp.sum(images * mask, axis=(1, 2, 3))
        sumsq_nonzero = xp.sum((images * mask) ** 2, axis=(1, 2, 3))

        mean = sum_nonzero / count_nonzero
        variance = (sumsq_nonzero - (sum_nonzero ** 2) / count_nonzero) / count_nonzero

        features[:, 0] = mean
        features[:, 1] = variance
        return features

    def _get_im_bounds(self, markers, distance_frame):
        radii = distance_frame[markers[:, 0], markers[:, 1], markers[:, 2]]
        marker_radii = xp.ceil(radii)
        z_low = xp.clip(markers[:, 0] - marker_radii, 0, self.shape[1])
        z_high = xp.clip(markers[:, 0] + (marker_radii + 1), 0, self.shape[1])
        y_low = xp.clip(markers[:, 1] - marker_radii, 0, self.shape[2])
        y_high = xp.clip(markers[:, 1] + (marker_radii + 1), 0, self.shape[2])
        x_low = xp.clip(markers[:, 2] - marker_radii, 0, self.shape[3])
        x_high = xp.clip(markers[:, 2] + (marker_radii + 1), 0, self.shape[3])
        return z_low, z_high, y_low, y_high, x_low, x_high

    def _get_sub_volumes(self, im_frame, im_bounds, max_radius):
        z_low, z_high, y_low, y_high, x_low, x_high = im_bounds

        # Preallocate arrays
        sub_volumes = xp.zeros((len(z_low), max_radius, max_radius, max_radius))  # Change dtype if necessary

        # Extract sub-volumes
        for i in range(len(z_low)):
            zl, zh, yl, yh, xl, xh = z_low[i], z_high[i], y_low[i], y_high[i], x_low[i], x_high[i]
            sub_volumes[i, :zh - zl, :yh - yl, :xh - xl] = im_frame[zl:zh, yl:yh, xl:xh]

        return sub_volumes


    def _get_orthogonal_projections(self, sub_volumes):
        # Max projections along each axis
        z_projections = xp.max(sub_volumes, axis=1)
        y_projections = xp.max(sub_volumes, axis=2)
        x_projections = xp.max(sub_volumes, axis=3)

        return z_projections, y_projections, x_projections

    def _get_t(self):
        if self.num_t is None:
            if self.im_info.no_t:
                self.num_t = 1
            else:
                self.num_t = self.im_info.shape[self.im_info.axes.index('T')]
        else:
            return

    def _allocate_memory(self):
        logger.debug('Allocating memory for mocap marking.')
        label_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_instance_label'])
        self.label_memmap = get_reshaped_image(label_memmap, self.num_t, self.im_info)

        im_memmap = self.im_info.get_im_memmap(self.im_info.im_path)
        self.im_memmap = get_reshaped_image(im_memmap, self.num_t, self.im_info)

        im_frangi_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_frangi'])
        self.im_frangi_memmap = get_reshaped_image(im_frangi_memmap, self.num_t, self.im_info)
        self.shape = self.label_memmap.shape

        im_marker_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_marker'])
        self.im_marker_memmap = get_reshaped_image(im_marker_memmap, self.num_t, self.im_info)

        im_distance_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_distance'])
        self.im_distance_memmap = get_reshaped_image(im_distance_memmap, self.num_t, self.im_info)

    def _get_hu_moments(self, sub_volumes):
        intensity_projections = self._get_orthogonal_projections(sub_volumes)
        etas_z = self._calculate_normalized_moments(intensity_projections[0])
        etas_y = self._calculate_normalized_moments(intensity_projections[1])
        etas_x = self._calculate_normalized_moments(intensity_projections[2])
        hu_moments_z = self._calculate_hu_moments(etas_z)
        hu_moments_y = self._calculate_hu_moments(etas_y)
        hu_moments_x = self._calculate_hu_moments(etas_x)
        hu_moments = xp.concatenate((hu_moments_z, hu_moments_y, hu_moments_x), axis=1)
        return hu_moments

    def _concatenate_hu_matrices(self, hu_matrices):
        return xp.concatenate(hu_matrices, axis=1)

    def _get_feature_matrix(self, t):
        intensity_frame = xp.array(self.im_memmap[t])
        frangi_frame = xp.array(self.im_frangi_memmap[t])
        frangi_frame[frangi_frame>0] = xp.log10(frangi_frame[frangi_frame>0])
        frangi_frame[frangi_frame<0] -= xp.min(frangi_frame[frangi_frame<0])
        distance_frame = xp.array(self.im_distance_memmap[t])

        distance_max_frame = ndi.maximum_filter(distance_frame, size=3)*2
        marker_frame = xp.array(self.im_marker_memmap[t]) > 0
        marker_indices = xp.argwhere(marker_frame)

        region_bounds = self._get_im_bounds(marker_indices, distance_max_frame)
        max_radius = int(xp.ceil(xp.max(distance_frame[marker_frame])))*4+1

        intensity_sub_volumes = self._get_sub_volumes(intensity_frame, region_bounds, max_radius)
        frangi_sub_volumes = self._get_sub_volumes(frangi_frame, region_bounds, max_radius)
        # distance_sub_volumes = self._get_sub_volumes(distance_frame, region_bounds, max_radius)

        intensity_stats = self._calculate_mean_and_variance(intensity_sub_volumes)
        frangi_stats = self._calculate_mean_and_variance(frangi_sub_volumes)
        # distance_stats = self._calculate_mean_and_variance(distance_sub_volumes)
        stats_feature_matrix = self._concatenate_hu_matrices([intensity_stats, frangi_stats])
        # stats_feature_matrix = self._concatenate_hu_matrices([intensity_stats, frangi_stats, distance_stats])

        intensity_hus = self._get_hu_moments(intensity_sub_volumes)
        # frangi_hus = self._get_hu_moments(frangi_frame, frangi_sub_volumes, max_radius)
        # distance_hus = self._get_hu_moments(distance_frame, distance_sub_volumes, max_radius)
        hu_feature_matrix = intensity_hus
        # hu_feature_matrix = self._concatenate_hu_matrices([intensity_hus, frangi_hus])
        # hu_feature_matrix = self._concatenate_hu_matrices([intensity_hus, frangi_hus, distance_hus])
        log_hu_feature_matrix = -1*xp.copysign(1.0, hu_feature_matrix)*xp.log10(xp.abs(hu_feature_matrix))
        log_hu_feature_matrix[xp.isinf(log_hu_feature_matrix)] = xp.nan

        return stats_feature_matrix, log_hu_feature_matrix

    def _get_distance_mask(self, t):
        marker_frame_pre = np.array(self.im_marker_memmap[t-1]) > 0
        marker_indices_pre = np.argwhere(marker_frame_pre)
        marker_indices_pre_scaled = marker_indices_pre * self.scaling
        marker_frame_post = np.array(self.im_marker_memmap[t]) > 0
        marker_indices_post = np.argwhere(marker_frame_post)
        marker_indices_post_scaled = marker_indices_post * self.scaling

        distance_matrix = cdist(marker_indices_post_scaled, marker_indices_pre_scaled)
        distance_mask = xp.array(distance_matrix) < self.max_distance_um
        distance_matrix = distance_matrix / self.max_distance_um  # normalize to furthest possible distance
        return distance_matrix, distance_mask

    def _get_difference_matrix(self, m1, m2):
        m1_reshaped = m1[:, xp.newaxis, :]
        m2_reshaped = m2[xp.newaxis, :, :]
        difference_matrix = xp.abs(m1_reshaped - m2_reshaped)
        return difference_matrix

    def _zscore_normalize(self, m, mask):
        depth = m.shape[2]

        sum_mask = xp.sum(mask)
        mean_vals = xp.zeros(depth)
        std_vals = xp.zeros(depth)

        # Calculate mean values slice by slice
        for d in range(depth):
            slice_m = m[:, :, d]
            mean_vals[d] = xp.sum(slice_m * mask) / sum_mask

        # Calculate std values slice by slice
        for d in range(depth):
            slice_m = m[:, :, d]
            std_vals[d] = xp.sqrt(xp.sum((slice_m - mean_vals[d]) ** 2 * mask) / sum_mask)

        # Normalize and set to infinity where mask is 0
        for d in range(depth):
            slice_m = m[:, :, d]
            slice_m -= mean_vals[d]
            slice_m /= std_vals[d]
            slice_m[mask == 0] = xp.inf

        return m

    def _get_cost_matrix(self, t, stats_vecs, pre_stats_vecs, hu_vecs, pre_hu_vecs):
        distance_matrix, distance_mask = self._get_distance_mask(t)
        z_score_distance_matrix = self._zscore_normalize(xp.array(distance_matrix)[..., xp.newaxis], distance_mask)
        stats_matrix = self._get_difference_matrix(stats_vecs, pre_stats_vecs)
        z_score_stats_matrix = self._zscore_normalize(stats_matrix, distance_mask) / stats_matrix.shape[2]
        hu_matrix = self._get_difference_matrix(hu_vecs, pre_hu_vecs)
        z_score_hu_matrix = self._zscore_normalize(hu_matrix, distance_mask) / hu_matrix.shape[2]

        z_score_matrix = xp.concatenate((z_score_distance_matrix, z_score_stats_matrix, z_score_hu_matrix), axis=2)
        # z_score_matrix = xp.concatenate((z_score_stats_matrix, z_score_hu_matrix), axis=2)
        cost_matrix = xp.nansum(z_score_matrix, axis=2)
        # cost_matrix[distance_mask == 0] = xp.inf

        return cost_matrix

    def _find_best_matches(self, cost_matrix):
        candidates = []
        cost_cutoff = 1

        # Find row-wise minimums
        row_min_idx = xp.argmin(cost_matrix, axis=1)
        row_min_val = xp.min(cost_matrix, axis=1)

        # Find column-wise minimums
        col_min_idx = xp.argmin(cost_matrix, axis=0)
        col_min_val = xp.min(cost_matrix, axis=0)

        row_matches = []
        col_matches = []
        costs = []

        # Store each row's and column's minimums as candidates for matching
        for i, (r_idx, r_val) in enumerate(zip(row_min_idx, row_min_val)):
            if r_val > cost_cutoff:
                continue
            candidates.append((int(i), int(r_idx), float(r_val)))
            row_matches.append(int(i))
            col_matches.append(int(r_idx))
            costs.append(float(r_val))

        for j, (c_idx, c_val) in enumerate(zip(col_min_idx, col_min_val)):
            if c_val > cost_cutoff:
                continue
            candidates.append((int(c_idx), int(j), float(c_val)))
            row_matches.append(int(c_idx))
            col_matches.append(int(j))
            costs.append(float(c_val))

        return row_matches, col_matches, costs

    def _run_hu_tracking(self):
        pre_stats_vecs = None
        pre_hu_vecs = None
        flow_vector_array = None
        for t in range(self.num_t):
            logger.debug(f'Running hu-moment tracking for frame {t + 1} of {self.num_t}')
            stats_vecs, hu_vecs = self._get_feature_matrix(t)
            # todo make distance weighting be dependent on number of seconds between frames (more uncertain with more time)
            #  could also vary with size (radius) based on diffusion coefficient. bigger = probably closer
            if pre_stats_vecs is None or pre_hu_vecs is None:
                pre_stats_vecs = stats_vecs
                pre_hu_vecs = hu_vecs
                continue
            cost_matrix = self._get_cost_matrix(t, stats_vecs, pre_stats_vecs, hu_vecs, pre_hu_vecs)
            row_indices, col_indices, costs = self._find_best_matches(cost_matrix)
            pre_marker_indices = np.argwhere(self.im_marker_memmap[t - 1])[col_indices]
            marker_indices = np.argwhere(self.im_marker_memmap[t])[row_indices]
            vecs = np.array(marker_indices) - np.array(pre_marker_indices)

            # import napari
            # viewer = napari.Viewer()
            # tracks = []
            # for track_num, (coord, vec) in enumerate(zip(pre_marker_indices, vecs)):
            #     tracks.append([track_num, 0, coord[0], coord[1], coord[2]])
            #     tracks.append([track_num, 1, coord[0] + vec[0], coord[1] + vec[1], coord[2] + vec[2]])
            # viewer.add_tracks(tracks)

            pre_stats_vecs = stats_vecs
            pre_hu_vecs = hu_vecs

            costs = np.array(costs)
            vec_z, vec_y, vec_x = vecs.T
            frame_vector_array = np.concatenate((np.array([t]*len(vec_z))[:, np.newaxis], vec_z[:, np.newaxis], vec_y[:, np.newaxis],
                                         vec_x[:, np.newaxis], costs[:, np.newaxis]), axis=1)
            if flow_vector_array is None:
                flow_vector_array = frame_vector_array
            else:
                flow_vector_array = np.concatenate((flow_vector_array, frame_vector_array), axis=0)

        # save the array
        np.save(r"D:\test_files\nelly_tests\output\test_vecs.npy", flow_vector_array)

        print('done')

    def run(self):
        self._get_t()
        self._allocate_memory()
        self._run_hu_tracking()


if __name__ == "__main__":
    import os
    test_folder = r"D:\test_files\nelly_tests"
    all_files = os.listdir(test_folder)
    all_files = [file for file in all_files if not os.path.isdir(os.path.join(test_folder, file))]
    im_infos = []
    for file in all_files:
        im_path = os.path.join(test_folder, file)
        im_info = ImInfo(im_path)
        im_info.create_output_path('im_instance_label')
        im_info.create_output_path('im_frangi')
        im_info.create_output_path('im_marker')
        im_info.create_output_path('im_distance')
        im_infos.append(im_info)

    hu_files = []
    for im_info in im_infos[:1]:
        hu = HuMomentTracking(im_info, num_t=3)
        hu.run()
        hu_files.append(hu)