from src_2.io.im_info import ImInfo
from src import xp, ndi, logger
from src_2.utils.general import get_reshaped_image
from src_2.utils.gpu_functions import triangle_threshold
from scipy.spatial import cKDTree, distance
import numpy as np


class Markers:
    def __init__(self, im_info: ImInfo, num_t=None,
                 min_radius_um=0.20, max_radius_um=1):
        self.im_info = im_info
        self.num_t = num_t
        if num_t is None:
            self.num_t = im_info.shape[im_info.axes.index('T')]
        self.z_ratio = self.im_info.dim_sizes['Z'] / self.im_info.dim_sizes['X']

        self.min_radius_um = max(min_radius_um, self.im_info.dim_sizes['X'])
        self.max_radius_um = max_radius_um

        self.min_radius_px = self.min_radius_um / self.im_info.dim_sizes['X']
        self.max_radius_px = self.max_radius_um / self.im_info.dim_sizes['X']


        self.shape = ()

        self.im_memmap = None
        self.im_frangi_memmap = None
        self.label_memmap = None
        self.im_marker_memmap = None

        self.debug = None

    def _get_sigma_vec(self, sigma):
        if self.im_info.no_z:
            sigma_vec = (sigma, sigma)
        else:
            sigma_vec = (sigma / self.z_ratio, sigma, sigma)
        return sigma_vec

    def _set_default_sigmas(self):
        logger.debug('Setting to sigma values.')
        min_sigma_step_size = 0.2
        num_sigma = 5

        self.sigma_min = self.min_radius_px
        self.sigma_max = self.max_radius_px

        sigma_step_size_calculated = (self.sigma_max - self.sigma_min) / num_sigma
        sigma_step_size = max(min_sigma_step_size, sigma_step_size_calculated)  # Avoid taking too small of steps.

        self.sigmas = list(xp.arange(self.sigma_min, self.sigma_max, sigma_step_size))
        logger.debug(f'Calculated sigma step size = {sigma_step_size_calculated}. Sigmas = {self.sigmas}')

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

        im_frangi_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_frangi'])
        self.im_frangi_memmap = get_reshaped_image(im_frangi_memmap, self.num_t, self.im_info)
        self.shape = self.label_memmap.shape

        im_marker_path = self.im_info.create_output_path('im_marker')
        self.im_marker_memmap = self.im_info.allocate_memory(im_marker_path, shape=self.shape,
                                                            dtype='uint8',
                                                            description='mocap marker image',
                                                            return_memmap=True)
    def _distance_im(self, mask):
        border_mask = ndi.binary_dilation(mask, iterations=1) ^ mask

        mask_coords = xp.argwhere(mask).get()
        border_mask_coords = xp.argwhere(border_mask).get()

        # print('Getting distance image')
        border_tree = cKDTree(border_mask_coords)
        dist, _ = border_tree.query(mask_coords, k=1, distance_upper_bound=self.max_radius_px*2)
        distances_im_frame = xp.zeros_like(mask, dtype='float32')
        distances_im_frame[mask_coords[:, 0], mask_coords[:, 1], mask_coords[:, 2]] = dist
        return distances_im_frame

    def _remove_close_peaks(self, coords_gpu, distance_im_gpu):
        coord = coords_gpu.get()
        distance_im = distance_im_gpu.get()
        intensities = distance_im[coord]
        idx_maxsort = np.argsort(-intensities)
        coord = np.transpose(coord)[idx_maxsort]

        # print('Removing peaks that are too close')
        tree = cKDTree(coord)
        min_dist = self.min_radius_px * 2
        indices = tree.query_ball_point(coord, r=min_dist, p=2, workers=-1)
        rejected_peaks_indices = set()
        naccepted = 0
        for idx, candidates in enumerate(indices):
            if idx not in rejected_peaks_indices:
                # keep current point and the points at exactly spacing from it
                candidates.remove(idx)
                dist = distance.cdist([coord[idx]],
                                      coord[candidates],
                                      distance.minkowski,
                                      p=2).reshape(-1)
                candidates = [c for c, d in zip(candidates, dist)
                              if d < min_dist]

                # candidates.remove(keep)
                rejected_peaks_indices.update(candidates)
                naccepted += 1

        cleaned_coords = np.delete(coord, tuple(rejected_peaks_indices), axis=0)

        return cleaned_coords

    def _local_max_peak(self, distance_im):
        mask = distance_im > 0
        lapofg = xp.empty(((len(self.sigmas),) + distance_im.shape), dtype=float)
        for i, s in enumerate(self.sigmas):
            sigma_vec = self._get_sigma_vec(s)
            current_lapofg = -ndi.gaussian_laplace(distance_im, sigma_vec) * xp.mean(s) ** 2
            current_lapofg = current_lapofg * mask
            current_lapofg[current_lapofg < 0] = 0
            lapofg[i] = current_lapofg

        filt_footprint = xp.ones((3,) * (distance_im.ndim + 1))
        max_filt = ndi.maximum_filter(lapofg, footprint=filt_footprint, mode='nearest')
        peaks = xp.empty(lapofg.shape, dtype=bool)
        # max_filt_mask = mask
        for filt_slice, max_filt_slice in enumerate(max_filt):
            thresh = 10**triangle_threshold(xp.log10(max_filt_slice[max_filt_slice > 0]))
            max_filt_mask = xp.asarray(max_filt_slice > thresh) * mask
            peaks[filt_slice] = (xp.asarray(lapofg[filt_slice]) == xp.asarray(max_filt_slice)) * max_filt_mask
        # get the coordinates of all true pixels in peaks
        coords = xp.max(peaks, axis=0)
        coords_idx = xp.argwhere(coords)
        coords_cleaned = self._remove_close_peaks(coords_idx, distance_im)
        peak_im = xp.zeros_like(mask)
        peak_im[tuple(coords_cleaned.T)] = 1
        return peak_im

    def _run_frame(self, t):
        logger.info(f'Running motion capture marking, volume {t}/{self.num_t - 1}')
        # frangi_frame = xp.asarray(self.im_frangi_memmap[t])
        mask_frame = xp.asarray(self.label_memmap[t] > 0)
        distance_im = self._distance_im(mask_frame)
        marker_frame = self._local_max_peak(distance_im)
        return marker_frame

    def _run_mocap_marking(self):
        for t in range(self.num_t):
            marker_frame = self._run_frame(t)
            self.im_marker_memmap[t] = marker_frame.get()

    def run(self):
        self._get_t()
        self._allocate_memory()
        self._set_default_sigmas()
        self._run_mocap_marking()


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
        im_infos.append(im_info)

    marker_files = []
    for im_info in im_infos:
        markers = Markers(im_info, num_t=2)
        markers.run()
        marker_files.append(markers)
