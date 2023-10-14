from src_2.io.im_info import ImInfo
from src import xp, ndi, logger
from src_2.utils.general import get_reshaped_image
import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.distance import cdist
from collections import defaultdict


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


    def _get_orthogonal_projections(self, im_frame, sub_volumes, max_radius):
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

    def _get_hu_moments(self, im_frame, sub_volumes, max_radius):
        intensity_projections = self._get_orthogonal_projections(im_frame, sub_volumes, max_radius)
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

        intensity_hus = self._get_hu_moments(intensity_frame, intensity_sub_volumes, max_radius)
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

        # Store each row's and column's minimums as candidates for matching
        for i, (r_idx, r_val) in enumerate(zip(row_min_idx, row_min_val)):
            if r_val > cost_cutoff:
                continue
            candidates.append((int(i), int(r_idx), float(r_val)))
            row_matches.append(int(i))
            col_matches.append(int(r_idx))

        for j, (c_idx, c_val) in enumerate(zip(col_min_idx, col_min_val)):
            if c_val > cost_cutoff:
                continue
            candidates.append((int(c_idx), int(j), float(c_val)))
            row_matches.append(int(c_idx))
            col_matches.append(int(j))

        return row_matches, col_matches

    def _find_confident_matches(self, cost_matrix):
        # Initialize lists to store most confident matches
        row_indices = []
        col_indices = []

        # Find the minimum in each row and each column
        row_min_values = xp.min(cost_matrix, axis=1)
        col_min_values = xp.min(cost_matrix, axis=0)

        # Find the corresponding indices for the row and column minimums
        row_min_indices = xp.argmin(cost_matrix, axis=1)
        col_min_indices = xp.argmin(cost_matrix, axis=0)

        # Remove any indices where the minimum value is infinity
        row_min_indices[row_min_values == xp.inf] = -1
        col_min_indices[col_min_values == xp.inf] = -1

        # Iterate over rows to find confident matches
        for i, row_min_index in enumerate(row_min_indices):
            if col_min_indices[row_min_index] == i:
                # skip any -1
                if row_min_index == -1:
                    continue
                row_indices.append(i)
                col_indices.append(row_min_index)

        return row_indices, col_indices

    def _compute_flow_vectors(self, pre_marker_indices, marker_indices):
        if len(pre_marker_indices) != len(marker_indices):
            raise ValueError("Lists must have the same length.")
        return np.array(marker_indices.get()) - np.array(pre_marker_indices.get())

    def _average_unique_flow_vectors(self, pre_marker_indices, marker_indices):
        flow_vectors = self._compute_flow_vectors(pre_marker_indices, marker_indices)
        unique_vectors = defaultdict(set)

        # Group vectors by their origin (MLP at t0)
        for i, pre_marker in enumerate(pre_marker_indices):
            unique_vectors[tuple(pre_marker.tolist())].add(tuple(flow_vectors[i].tolist()))

        # Compute the average vector for each unique MLP at t0
        avg_vectors = {}
        for pre_marker, vectors in unique_vectors.items():
            avg_vectors[pre_marker] = np.mean(np.array(list(vectors)), axis=0)

        return avg_vectors

    def _get_average_flow_vectors(self, t, row_indices, col_indices):
        pre_marker_frame = xp.array(self.im_marker_memmap[t-1]).astype('float')
        pre_marker_indices = xp.argwhere(pre_marker_frame)[col_indices]
        marker_frame = xp.array(self.im_marker_memmap[t]).astype('float')
        marker_indices = xp.argwhere(marker_frame)[row_indices]

        avg_vectors = self._average_unique_flow_vectors(pre_marker_indices, marker_indices)
        return avg_vectors

    def _get_vectors(self, t, row_indices, col_indices):
        avg_vectors = self._get_average_flow_vectors(t, row_indices, col_indices)
        im_mask = self.label_memmap[t-1] > 0
        im_mask_gpu = xp.array(im_mask)
        mask_pixels = np.argwhere(im_mask)
        # Convert avg_vectors keys to an array
        avg_vector_coords = np.array(list(avg_vectors.keys()))
        avg_vector_coords_um = avg_vector_coords * self.scaling
        ckdtree = cKDTree(avg_vector_coords_um)
        mask_pixels_cpu = xp.asnumpy(mask_pixels) * self.scaling
        distances, indices = ckdtree.query(mask_pixels_cpu, k=1, workers=-1)

        # Remove any indices and mask pixels where the distance is greater than the max distance
        # todo, this removal should be based on each mask voxel's distance im value at that voxel
        indices = indices[distances < self.max_distance_um]
        mask_pixels = mask_pixels[distances < self.max_distance_um]
        nearest_coords = avg_vector_coords[indices]

        # Initialize empty arrays to hold x, y, and z components and counts
        x_comp = xp.zeros_like(im_mask, dtype=xp.float16)
        y_comp = xp.zeros_like(im_mask, dtype=xp.float16)
        z_comp = xp.zeros_like(im_mask, dtype=xp.float16)

        # Populate these arrays
        for i, coord in enumerate(mask_pixels):
            nearest_coord = tuple(nearest_coords[i])
            vec = avg_vectors[nearest_coord]
            x_comp[coord[0], coord[1], coord[2]] += vec[0]
            y_comp[coord[0], coord[1], coord[2]] += vec[1]
            z_comp[coord[0], coord[1], coord[2]] += vec[2]

        # Apply Gaussian filter for smoothing
        # todo, this should probably be more principled than gaussian. Maybe weighted by distance?
        # Create a binary mask where flow vectors are non-zero
        sigma = 1.0  # Standard deviation for Gaussian kernel
        gaussian_filtered_mask = ndi.gaussian_filter(im_mask_gpu.astype(np.float32), sigma=sigma)
        x_comp_smooth = ndi.gaussian_filter(x_comp, sigma) * im_mask_gpu
        y_comp_smooth = ndi.gaussian_filter(y_comp, sigma) * im_mask_gpu
        z_comp_smooth = ndi.gaussian_filter(z_comp, sigma) * im_mask_gpu

        gaussian_filtered_mask[gaussian_filtered_mask == 0] = 1

        # Perform the averaging while ignoring zero vectors
        averaged_vectors_x = x_comp_smooth / gaussian_filtered_mask
        averaged_vectors_y = y_comp_smooth / gaussian_filtered_mask
        averaged_vectors_z = z_comp_smooth / gaussian_filtered_mask

        # Handle NaNs or infs
        averaged_vectors_x[np.isnan(averaged_vectors_x)] = 0
        averaged_vectors_x[np.isinf(averaged_vectors_x)] = 0

        averaged_vectors_y[np.isnan(averaged_vectors_y)] = 0
        averaged_vectors_y[np.isinf(averaged_vectors_y)] = 0

        averaged_vectors_z[np.isnan(averaged_vectors_z)] = 0
        averaged_vectors_z[np.isinf(averaged_vectors_z)] = 0

        vectors = np.stack((averaged_vectors_x.get(), averaged_vectors_y.get(), averaged_vectors_z.get()), axis=0)
        vectors_in_mask = vectors[:, im_mask].T
        vectors_in_mask = vectors_in_mask[distances < self.max_distance_um]
        vector_magnitudes = np.linalg.norm(vectors_in_mask, axis=1)

        self.vector_start_coords.append(mask_pixels)
        self.vectors.append(vectors_in_mask)
        self.vector_magnitudes.append(vector_magnitudes)

        return

    def _run_hu_tracking(self):
        pre_stats_vecs = None
        pre_hu_vecs = None
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
            row_indices, col_indices = self._find_best_matches(cost_matrix)

            # cost_median = xp.median(cost_matrix[row_indices, col_indices])
            # cost_p25 = xp.percentile(cost_matrix[row_indices, col_indices], 25)
            # cost_p75 = xp.percentile(cost_matrix[row_indices, col_indices], 75)

            self._get_vectors(t, row_indices, col_indices)
        print('done')

        tracks = []
        properties = {'vector_magnitudes': []}
        vector_added = self.vector_start_coords[0] + self.vectors[0]
        for track_num, (start_px, end_px) in enumerate(zip(self.vector_start_coords[0], vector_added)):
            # v = vectors_in_mask[track_num]
            properties['vector_magnitudes'].append(self.vector_magnitudes[0][track_num])
            properties['vector_magnitudes'].append(self.vector_magnitudes[0][track_num])
            tracks.append([track_num, 0, start_px[0], start_px[1], start_px[2]])
            tracks.append([track_num, 1, end_px[0], end_px[1], end_px[2]])
        import napari
        viewer = napari.Viewer()
        viewer.add_image(self.im_memmap[:2])
        viewer.add_tracks(tracks, properties=properties)




        # tracks = []
        # for track_num, avg_vector_coord in enumerate(avg_vector_coords):
        #     v = avg_vectors[tuple(avg_vector_coord)]
        #     tracks.append([track_num, 0, avg_vector_coord[0], avg_vector_coord[1], avg_vector_coord[2]])
        #     tracks.append([track_num, 1, avg_vector_coord[0] + v[0], avg_vector_coord[1] + v[1],
        #                    avg_vector_coord[2] + v[2]])
        # viewer.add_tracks(tracks)

        # tracks = []
        # for track_num, mask_px in enumerate(mask_pixels):
        #     v = avg_vectors[tuple(nearest_coords[track_num])]
        #     tracks.append([track_num, 0, mask_px[0], mask_px[1], mask_px[2]])
        #     tracks.append([track_num, 1, mask_px[0]+v[0], mask_px[1]+v[1], mask_px[2]+v[2]])
        # viewer.add_tracks(tracks)


        # # can visualize some stuff:
        # from sklearn.decomposition import PCA
        # from sklearn.cluster import KMeans
        # from sklearn.preprocessing import StandardScaler
        # import napari
        # viewer = napari.Viewer()
        #
        # scaler = StandardScaler()
        # scaled_features = scaler.fit_transform(feature_matrix.get())
        #
        # # Assuming features is a 2D array of shape (num_points, 45)
        # nan_mask = np.isnan(scaled_features).any(axis=1)
        # scaled_features = scaled_features[~nan_mask]
        # pca = PCA(n_components=10)  # or any number that retains enough variance
        # # drop samples with nan
        # reduced_features = pca.fit_transform(scaled_features)
        # # Perform K-means on reduced data
        # kmeans = KMeans(n_clusters=3)
        # cluster_labels = kmeans.fit_predict(reduced_features)
        # marker_frame = xp.array(self.im_marker_memmap[0]).astype('float')
        # marker_indices = xp.argwhere(marker_frame)[~nan_mask]
        # peak_im = xp.zeros_like(marker_frame, dtype='uint8')
        # peak_im[tuple(marker_indices.T)] = cluster_labels + 1
        # viewer.add_labels(peak_im.get())
        # viewer.add_image(self.im_memmap[0])
        #
        # # # get indices where row is minimum
        # # indices_row = xp.argmin(cost_matrix, axis=1)
        # # # get indices where column is minimum
        # # indices_col = xp.argmin(cost_matrix, axis=0)
        # # # get the coordinates of the minimum value in each row
        # # xy_row = xp.stack((xp.arange(len(indices_row)), indices_row), axis=1)
        # # xy_col = xp.stack((indices_col, xp.arange(len(indices_col))), axis=1)
        #
        # import napari
        # viewer = napari.Viewer()
        # viewer.add_image(self.im_memmap[:2])
        #
        # marker_frame_pre = xp.array(self.im_marker_memmap[0]).astype('float')
        # marker_indices_pre = xp.argwhere(marker_frame_pre)
        # test_point_num = 300
        # test_point = marker_indices_pre[test_point_num]
        # test_matches = cost_matrix[:, test_point_num].copy()
        # test_matches[test_matches == xp.inf] = xp.nan
        # # set marker frame post at the marker indices to test_matches values
        # marker_frame_post = xp.array(self.im_marker_memmap[1]).astype('float')
        # marker_indices_post = xp.argwhere(marker_frame_post)
        # marker_frame_post[:] = xp.nan
        # marker_frame_post[tuple(marker_indices_post.T)] = test_matches
        #
        # # viewer.add_image(cost_matrix, colormap='turbo')
        # # viewer.add_image((cost_matrix * distance_mask).get(), colormap='turbo')
        # # viewer.add_points(xy_row.get(), size=10, face_color='blue', opacity=0.5, blending='additive')
        # # viewer.add_points(xy_col.get(), size=10, face_color='green', opacity=0.5, blending='additive')
        # viewer.add_points(test_point.get(), size=3, face_color='green')
        # viewer.add_image(marker_frame_post.get(), colormap='turbo', contrast_limits=[-3, 0])
        # print('done')

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
        hu = HuMomentTracking(im_info, num_t=2)
        hu.run()
        hu_files.append(hu)