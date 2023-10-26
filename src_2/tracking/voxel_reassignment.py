import numpy as np
from scipy.spatial import cKDTree
from tifffile import tifffile

from src import logger
from src_2.io.im_info import ImInfo
from src_2.tracking.flow_interpolation import FlowInterpolator

class VoxelReassigner:
    def __init__(self, im_info: ImInfo,
                 flow_interpolator_fw: FlowInterpolator,
                 flow_interpolator_bw: FlowInterpolator,
                 num_t=None, ):
        self.im_info = im_info
        self.num_t = num_t
        if num_t is None:
            self.num_t = im_info.shape[im_info.axes.index('T')]

        self.flow_interpolator_fw = flow_interpolator_fw
        self.flow_interpolator_bw = flow_interpolator_bw

        self.debug = None

    def _match_forward(self, flow_interpolator, vox_prev, vox_next, t):
        vectors_interpx_prev = flow_interpolator.interpolate_coord(vox_prev, t)
        if vectors_interpx_prev is None:
            return [], []
        # only keep voxels that are not nan
        kept_prev_vox_idxs = ~np.isnan(vectors_interpx_prev).any(axis=1)
        # only keep vectors where the voxel is not nan
        vectors_interpx_prev = vectors_interpx_prev[kept_prev_vox_idxs]
        # get centroids in t1 from voxels in t0 + interpolated flow at that voxel
        centroids_next_interpx = vox_prev[kept_prev_vox_idxs] + vectors_interpx_prev
        if len(centroids_next_interpx) == 0:
            return []
        # now we have estimated centroids in t1 (centroids_next_interpx) and linked voxels in t0 (vox_prev[kept_prev_vox_idxs]).
        # we then have to match t1 voxels (vox_next) to estimated t1 centroids (centroids_next_interpx)
        match_dist, matched_idx = self._match_voxels_to_centroids(vox_next, centroids_next_interpx)
        vox_matched_to_centroids = vox_next[matched_idx.tolist()]
        # then link those t1 voxels back to the t0 voxels
        vox_prev_kept = vox_prev[kept_prev_vox_idxs]
        # now we have linked t0 voxels (vox_prev_kept) to t1 voxels (vox_matched_to_centroids)
        # but we have to make sure the link is within a distance constraint.
        vox_next_matched_valid, vox_prev_matched_valid, distances_valid = self._distance_threshold(
            vox_prev_kept, vox_matched_to_centroids
        )
        return vox_next_matched_valid, vox_prev_matched_valid, distances_valid

    def _match_backward(self, flow_interpolator, vox_next, vox_prev, t):
        vectors = flow_interpolator.interpolate_coord(vox_next, t)
        if vectors is None:
            return [], []
        kept_next_vox_idxs = ~np.isnan(vectors).any(axis=1)
        vectors = vectors[kept_next_vox_idxs]
        centroids_prev_interpx = vox_next[kept_next_vox_idxs] - vectors
        # now we have estimated centroids in t0 and linked voxels in t1.
        # we then have to match t0 voxels to estimated t0 centroids
        match_dist, matched_idx = self._match_voxels_to_centroids(vox_prev, centroids_prev_interpx)
        # then link those t0 voxels back to the t1 voxels
        matched_coords = vox_prev[matched_idx.tolist()]
        match_tuple, kept_next_vox_idxs = self._distance_threshold(vox_next, matched_coords, kept_next_vox_idxs)
        vox_next_matches, vox_prev_matches = self._assign_unique_matches(*match_tuple)
        return vox_prev_matches, vox_next_matches

    def _match_voxels_to_centroids(self, coords_real, coords_interpx):
        coords_interpx = np.array(coords_interpx) * self.flow_interpolator_fw.scaling
        coords_real = np.array(coords_real) * self.flow_interpolator_fw.scaling
        tree = cKDTree(coords_real)
        dist, idx = tree.query(coords_interpx, k=1, workers=-1)
        return dist, idx

    def _assign_unique_matches(self, matches, distances, kept_coords):
        match_dict = {}
        for idx, match in enumerate(matches):
            match_tuple = tuple(match)
            if match_tuple not in match_dict.keys():
                match_dict[match_tuple] = [[], []]
            match_dict[match_tuple][0].append(distances[idx])
            match_dict[match_tuple][1].append(kept_coords[idx])
        vox_prev_matches = []
        vox_next_matches = []
        # final_matches = []
        for match_tuple, (distance_matches, coord_matches) in match_dict.items():
            if len(distance_matches) == 1:
                vox_prev_matches.append(coord_matches[0])
                vox_next_matches.append(match_tuple)
                # final_matches.append((tuple(coord_matches[0]), match_tuple))
                continue
            min_idx = np.argmin(distance_matches)
            vox_prev_matches.append(coord_matches[min_idx])
            vox_next_matches.append(match_tuple)
            # final_matches.append((tuple(coord_matches[min_idx]), match_tuple))
        return vox_prev_matches, vox_next_matches

    def _distance_threshold(self, vox_prev_matched, vox_next_matched):
        distances = np.linalg.norm((vox_prev_matched - vox_next_matched) * self.flow_interpolator_fw.scaling, axis=1)
        distance_mask = distances < self.flow_interpolator_fw.max_distance_um
        vox_next_matched_valid = vox_next_matched[distance_mask]
        vox_prev_matched_valid = vox_prev_matched[distance_mask]
        distances_valid = distances[distance_mask]
        return vox_next_matched_valid, vox_prev_matched_valid, distances_valid

    def match_voxels(self, vox_prev, vox_next, t):
        # forward interpolation:
        # from t0 voxels and interpolated flow, get t1 centroids.
        vox_prev_matches_fw, vox_next_matches_fw, distances_fw = self._match_forward(
            self.flow_interpolator_fw, vox_prev, vox_next, t
        )
        #  match nearby t1 voxels to t1 centroids, which are linked to t0 voxels.

        # backward interpolation:
        # from t0 centroids and real flow, get t1 centroids.
        vox_prev_matches_bw, vox_next_matches_bw, distances_bw = self._match_backward(
            self.flow_interpolator_bw, vox_next, vox_prev, t + 1
        )
        #  interpolate flow at nearby t1 voxels. subtract flow from voxels to get t0 centroids.
        #  match nearby t0 voxels to t0 centroids, which are linked to t1 voxels.
        vox_prev_matches = np.concatenate([vox_prev_matches_fw, vox_prev_matches_bw])
        vox_next_matches = np.concatenate([vox_next_matches_fw, vox_next_matches_bw])

        vox_prev_matches, vox_next_matches = self._assign_unique_matches(*match_tuple)

        # if len(centroids_next_interpx) == 0:
        #     return []
        # match_dist, matched_idx = self._match_voxels(centroids_next_interpx, vox_next)
        # matched_coords = vox_next[matched_idx.tolist()]
        # match_tuple, kept_idxs = self._distance_threshold(vox_prev, matched_coords, kept_idxs)
        # final_matches = self._assign_unique_matches(*match_tuple)
        # prev_coords_matched = np.array([match[0] for match in final_matches])
        # next_coords_matched = np.array([match[1] for match in final_matches])
        # unmatched coords are coords in next_coords_real not in next_coords_matched
        tuple_next_matched = set([tuple(coord) for coord in next_coords_matched])
        unmatched_coords = np.array([coord for coord in vox_next if tuple(coord) not in tuple_next_matched])
        unmatched_diff = 1
        while unmatched_diff:
            logger.debug(f'unmatched diff: {unmatched_diff}')
            num_unmatched = len(unmatched_coords)
            tree = cKDTree(next_coords_matched * self.flow_interpolator_fw.scaling)
            dists, idxs = tree.query(unmatched_coords * self.flow_interpolator_fw.scaling, k=1, workers=-1)
            unmatched_matches = np.array([[prev_coords_matched[idx], unmatched_coords[i]] for i, idx in enumerate(idxs) if dists[i] < self.flow_interpolator_fw.max_distance_um])
            if len(unmatched_matches) == 0:
                break
            # add unmatched matches to coords_matched
            prev_coords_matched = np.concatenate([prev_coords_matched, unmatched_matches[:, 0]])
            next_coords_matched = np.concatenate([next_coords_matched, unmatched_matches[:, 1]])
            tuple_next_matched = set([tuple(coord) for coord in next_coords_matched])
            unmatched_coords = np.array([coord for coord in vox_next if tuple(coord) not in tuple_next_matched])
            unmatched_diff = num_unmatched - len(unmatched_coords)
        # todo deal with unmatched coords. after matching, find all nearby coords with dist less than max val, assign those to closest label
        #  do this while there are still unmatched coords, or constant number of unmatched coords between two iterations.
        return prev_coords_matched, next_coords_matched


if __name__ == "__main__":
    import os
    import napari
    viewer = napari.Viewer()
    # test_folder = r"D:\test_files\nelly_tests"
    # test_skel = tifffile.memmap(r"D:\test_files\nelly_tests\output\deskewed-2023-07-13_14-58-28_000_wt_0_acquire.ome-ch0-im_skel.ome.tif", mode='r')
    # test_label = tifffile.memmap(r"D:\test_files\nelly_tests\output\deskewed-2023-07-13_14-58-28_000_wt_0_acquire.ome-ch0-im_instance_label.ome.tif", mode='r')

    test_folder = r"D:\test_files\beading"
    test_skel = tifffile.memmap(r"D:\test_files\beading\output\deskewed-single.ome-ch0-im_skel.ome.tif", mode='r')
    test_label = tifffile.memmap(r"D:\test_files\beading\output\deskewed-single.ome-ch0-im_instance_label.ome.tif",
                                 mode='r')

    all_files = os.listdir(test_folder)
    all_files = [file for file in all_files if not os.path.isdir(os.path.join(test_folder, file))]
    im_infos = []
    for file in all_files[:1]:
        im_path = os.path.join(test_folder, file)
        im_info = ImInfo(im_path)
        im_info.create_output_path('flow_vector_array', ext='.npy')
        im_infos.append(im_info)

    flow_interpx_fw = FlowInterpolator(im_infos[0])
    flow_interpx_bw = FlowInterpolator(im_infos[0], forward=False)
    # viewer.add_labels(test_label)

    label_nums = list(range(1, np.max(test_label[0])))
    # get 100 random coords
    np.random.seed(0)
    labels = np.random.choice(len(label_nums), 10, replace=False)
    # label_num = 100
    all_mask_coords = [np.argwhere(test_label[t] > 0) for t in range(im_info.shape[0])]

    voxel_reassigner = VoxelReassigner(im_infos[0], flow_interpx_fw, flow_interpx_bw)
    new_label_im = np.zeros_like(test_label)
    # where test_label == any number in labels
    # label_coords = np.argwhere(np.isin(test_label[0], labels))
    vox_prev = np.argwhere(test_label[0] > 0)
    new_label_im[0][tuple(vox_prev.T)] = test_label[0][tuple(vox_prev.T)]
    # for t in range(1):
    for t in range(im_info.shape[0]-1):
        print(f't: {t} / {im_info.shape[0]-1}')
        vox_next = all_mask_coords[t + 1]
        if len(vox_prev) == 0:
            break
        matched_prev, matched_next = voxel_reassigner.match_voxels(vox_prev, vox_next, t)
        if len(matched_prev) == 0:
            break
        # old_label_coords = np.array([match[0] for match in matches])
        # label_coords = np.array([match[1] for match in matches])
        new_label_im[t+1][tuple(matched_next.T)] = new_label_im[t][tuple(matched_prev.T)]
        vox_prev = matched_next
    viewer.add_image(flow_interpx.im_memmap)
    viewer.add_labels(new_label_im)
    # napari.run()
    # print('hi')

    # last_t = 2
    # voxel_reassigner = VoxelReassigner(im_infos[0], flow_interpx)
    # new_label_im = np.zeros_like(test_label)
    # new_label_im[last_t][tuple(np.argwhere(test_label[last_t] == label_num).T)] = label_num
    # inverted_range = np.arange(last_t+1)[::-1][:-1]
    # wanted_coords = np.argwhere(test_label[last_t] == label_num)
    # for t in inverted_range:
    #     # label_coords = np.argwhere(test_label[t] == label_num)
    #     prev_mask_coords = np.argwhere(test_label[t-1] > 0)
    #     # all_coords = np.argwhere(test_label[t] > 0)
    #
    #     # new_labels = voxel_reassigner.get_new_label(label_coords, t, prev_mask_coords, test_label[t-1][test_label[t-1] > 0])
    #     new_labels, wanted_coords = voxel_reassigner.get_new_label(wanted_coords, t, prev_mask_coords, test_label[t-1][test_label[t-1] > 0])
    #
    #     new_label_coords = list(new_labels.keys())
    #     new_label_im[t][tuple(np.array(new_label_coords).T)] = list(new_labels.values())
    # viewer.add_labels(new_label_im)
