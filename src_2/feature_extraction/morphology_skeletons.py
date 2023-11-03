from src import logger, xp, ndi
from src_2.im_info.im_info import ImInfo
from src_2.utils.general import get_reshaped_image
import skimage.measure
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree


class MorphologySkeletonFeatures:
    def __init__(self, im_info: ImInfo,
                 max_radius_um=1):
        self.im_info = im_info
        if self.im_info.no_z:
            self.spacing = (self.im_info.dim_sizes['Y'], self.im_info.dim_sizes['X'])
        else:
            self.spacing = (self.im_info.dim_sizes['Z'], self.im_info.dim_sizes['Y'], self.im_info.dim_sizes['X'])

        self.im_memmap = None
        self.network_memmap = None
        self.pixel_class_memmap = None
        self.morphology_skeleton_features_path = None

        self.max_radius_um = max_radius_um
        self.max_radius_px = self.max_radius_um / self.im_info.dim_sizes['X']

        self.label_objects_intensity = None

        self.features = {}
        self.branch_features = {}

    def _get_pixel_class(self, skel):
        skel_mask = xp.array(skel > 0).astype('uint8')
        if self.im_info.no_z:
            weights = xp.ones((3, 3))
        else:
            weights = xp.ones((3, 3, 3))
        skel_mask_sum = ndi.convolve(skel_mask, weights=weights, mode='constant', cval=0) * skel_mask
        skel_mask_sum[skel_mask_sum > 4] = 4
        return skel_mask_sum

    def _distance_check(self, mask, check_coords):
        border_mask = ndi.binary_dilation(mask, iterations=1) ^ mask

        border_mask_coords = xp.argwhere(border_mask).get() * self.spacing

        border_tree = cKDTree(border_mask_coords)
        dist, _ = border_tree.query(check_coords.get() * self.spacing, k=1)
        return dist

    def _get_branches(self):
        if self.im_info.no_z:
            structure = xp.ones((3, 3))
        else:
            structure = xp.ones((3, 3, 3))
        network_gpu = xp.array(self.network_memmap[0])
        pixel_class = self._get_pixel_class(network_gpu)
        # everywhere where the image does not equal 0 or 4
        branch_mask = (pixel_class != 0) * (pixel_class != 4)
        branch_pixel_class = self._get_pixel_class(branch_mask)
        branch_labels, _ = ndi.label(branch_mask, structure=structure)

        branch_px = xp.where(branch_mask)
        px_class = branch_pixel_class[branch_px]
        px_branch_label = branch_labels[branch_px]
        px_main_label = network_gpu[branch_px]

        # distance matrix between all branch_px, vectorized
        coord_array_1 = xp.array(branch_px).T
        coord_array_2 = xp.array(branch_px).T[:, None, :]
        dist = xp.linalg.norm(coord_array_1 - coord_array_2, axis=-1)
        dist[dist >= 2] = 0

        # only keep lower diagonal
        dist = xp.tril(dist)
        pixel_neighbors = xp.where(dist > 0)
        valid_branch_labels = px_branch_label[pixel_neighbors[0]]

        scaled_coords = coord_array_1 * xp.array(self.spacing)
        scaled_coords_1 = scaled_coords[pixel_neighbors[0]]
        scaled_coords_2 = scaled_coords[pixel_neighbors[1]]
        scaled_coords_dist = xp.linalg.norm(scaled_coords_1 - scaled_coords_2, axis=-1).get()

        branch_length_list = {label: [] for label in xp.unique(px_branch_label).tolist()}
        for i, label in enumerate(valid_branch_labels.tolist()):
            branch_length_list[label].append(scaled_coords_dist[i])

        lone_tips = branch_pixel_class == 1
        tips = branch_pixel_class == 2

        lone_tip_coords = xp.argwhere(lone_tips)
        tip_coords = xp.argwhere(tips)

        # match tips to branch labels, and find distance between them (should always be 2 tips)
        tip_branch_labels = branch_labels[tuple(tip_coords.T)]

        # get distance between tips
        gpu_spacing = xp.array(self.spacing)
        tip_coord_labels = {label: [] for label in np.unique(tip_branch_labels).tolist()}
        for i, label in enumerate(tip_branch_labels.tolist()):
            tip_coord_labels[label].append(tip_coords[i] * gpu_spacing)
        tip_coord_distances = {label: [] for label in np.unique(tip_branch_labels).tolist()}
        for label, coords in tip_coord_labels.items():
            tip_coord_distances[label] = xp.linalg.norm(coords[0] - coords[1])
        branch_tortuosities = {label: [] for label in np.unique(px_branch_label).tolist()}
        for label, length_list in branch_length_list.items():
            if len(length_list) == 0:
                branch_tortuosities[label] = 1.0
            elif tip_coord_distances.get(label) is None:
                branch_tortuosities[label] = float((xp.sum(xp.array(length_list)) / self.im_info.dim_sizes['X']).get())
            else:
                branch_tortuosities[label] = float((xp.sum(xp.array(length_list)) / tip_coord_distances[label]).get())

        lone_tip_radii = self._distance_check(xp.array(self.label_memmap[0])>0, lone_tip_coords) * 2
        tip_radii = self._distance_check(xp.array(self.label_memmap[0])>0, tip_coords)

        lone_tip_labels = branch_labels[tuple(lone_tip_coords.T)]
        tip_labels = branch_labels[tuple(tip_coords.T)]

        for label, radius in zip(lone_tip_labels.tolist(), lone_tip_radii):
            branch_length_list[label].append(radius)

        for label, radius in zip(tip_labels.tolist(), tip_radii):
            branch_length_list[label].append(radius)

        self.branch_features['label'] = [label for label in xp.unique(px_branch_label).tolist()]
        self.branch_features['branch_lengths'] = {label: np.sum(np.array(length_list)) for label, length_list in branch_length_list.items()}
        self.branch_features['branch_tortuosities'] = branch_tortuosities

        self.features['label'] = [label for label in xp.unique(px_main_label).tolist()]
        self.features['length'] = [np.sum(np.array(length_list)) for label, length_list in branch_length_list.items()]

    def _skeleton_morphology(self):
        self._get_branches()
        self.skel_objects_intensity = skimage.measure.regionprops(self.network_memmap[0], self.im_memmap[0], spacing=self.spacing)
        # get branches -> pixel class with edge, remove rest, label, then get features

    def _get_memmaps(self):
        logger.debug('Allocating memory for spatial feature extraction.')
        im_memmap = self.im_info.get_im_memmap(self.im_info.im_path)
        self.im_memmap = get_reshaped_image(im_memmap, 1, self.im_info)

        network_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_skel'])
        self.network_memmap = get_reshaped_image(network_memmap, 1, self.im_info)

        pixel_class_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_pixel_class'])
        self.pixel_class_memmap = get_reshaped_image(pixel_class_memmap, 1, self.im_info)

        self.im_info.create_output_path('morphology_skeleton_features', ext='.csv')
        self.morphology_skeleton_features_path = self.im_info.pipeline_paths['morphology_skeleton_features']

        label_memmap = self.im_info.get_im_memmap(self.im_info.pipeline_paths['im_instance_label'])
        self.label_memmap = get_reshaped_image(label_memmap, 1, self.im_info)

        self.shape = self.network_memmap.shape

    def _save_features(self):
        logger.debug('Saving spatial features.')
        features_df = pd.DataFrame.from_dict(self.features)
        features_df.to_csv(self.morphology_skeleton_features_path, index=False)

    def run(self):
        self._get_memmaps()
        self._skeleton_morphology()
        self._save_features()


if __name__ == "__main__":
    im_path = r"D:\test_files\nelly_tests\deskewed-2023-07-13_14-58-28_000_wt_0_acquire.ome.tif"
    im_info = ImInfo(im_path)
    im_info.create_output_path('im_skel')
    im_info.create_output_path('im_pixel_class')
    im_info.create_output_path('im_instance_label')

    morphology_skeleton_features = MorphologySkeletonFeatures(im_info)
    morphology_skeleton_features.run()