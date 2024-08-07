
from nellie import xp, ndi, logger, device_type
from nellie.im_info.verifier import ImInfo
from nellie.utils.gpu_functions import otsu_threshold, triangle_threshold


class Label:
    def __init__(self, im_info: ImInfo,
                 num_t=None,
                 threshold=None,
                 snr_cleaning=False, otsu_thresh_intensity=False,
                 viewer=None):
        self.im_info = im_info
        self.num_t = num_t
        if num_t is None and not self.im_info.no_t:
            self.num_t = im_info.shape[im_info.axes.index('T')]
        self.threshold = threshold
        self.snr_cleaning = snr_cleaning
        self.otsu_thresh_intensity = otsu_thresh_intensity

        self.im_memmap = None
        self.frangi_memmap = None

        self.max_label_num = 0

        if not self.im_info.no_z:
            self.min_z_radius_um = min(self.im_info.dim_res['Z'], 0.2)

        self.semantic_mask_memmap = None
        self.instance_label_memmap = None
        self.shape = ()

        self.debug = {}

        self.viewer = viewer

    def _get_t(self):
        if self.num_t is None:
            if self.im_info.no_t:
                self.num_t = 1
            else:
                self.num_t = self.im_info.shape[self.im_info.axes.index('T')]
        else:
            return

    def _allocate_memory(self):
        logger.debug('Allocating memory for semantic segmentation.')
        self.im_memmap = self.im_info.get_memmap(self.im_info.im_path)
        self.frangi_memmap = self.im_info.get_memmap(self.im_info.pipeline_paths['im_frangi'])
        self.shape = self.frangi_memmap.shape

        im_instance_label_path = self.im_info.pipeline_paths['im_instance_label']
        self.instance_label_memmap = self.im_info.allocate_memory(im_instance_label_path,
                                                                  dtype='int32',
                                                                  description='instance segmentation',
                                                                  return_memmap=True)

    def _get_labels(self, frame):
        ndim = 2 if self.im_info.no_z else 3
        footprint = ndi.generate_binary_structure(ndim, 1)

        triangle = 10 ** triangle_threshold(xp.log10(frame[frame > 0]))
        otsu, _ = otsu_threshold(xp.log10(frame[frame > 0]))
        otsu = 10 ** otsu
        min_thresh = min([triangle, otsu])

        mask = frame > min_thresh

        if not self.im_info.no_z:
            mask = ndi.binary_fill_holes(mask)

        if not self.im_info.no_z and self.im_info.dim_res['Z'] >= self.min_z_radius_um:
            mask = ndi.binary_opening(mask, structure=xp.ones((2, 2, 2)))
        elif self.im_info.no_z:
            mask = ndi.binary_opening(mask, structure=xp.ones((2, 2)))

        labels, _ = ndi.label(mask, structure=footprint)
        # remove anything 4 pixels or under using bincounts
        areas = xp.bincount(labels.ravel())[1:]
        mask = xp.where(xp.isin(labels, xp.where(areas >= 4)[0]+1), labels, 0) > 0
        labels, _ = ndi.label(mask, structure=footprint)
        return mask, labels

    def _get_subtraction_mask(self, original_frame, labels_frame):
        subtraction_mask = original_frame.copy()
        subtraction_mask[labels_frame > 0] = 0
        return subtraction_mask

    def _get_object_snrs(self, original_frame, labels_frame):
        logger.debug('Calculating object SNRs.')
        subtraction_mask = self._get_subtraction_mask(original_frame, labels_frame)
        unique_labels = xp.unique(labels_frame)
        extend_bbox_by = 1
        keep_labels = []
        for label in unique_labels:
            if label == 0:
                continue
            coords = xp.nonzero(labels_frame == label)
            z_coords, r_coords, c_coords = coords

            zmin, zmax = xp.min(z_coords), xp.max(z_coords)
            rmin, rmax = xp.min(r_coords), xp.max(r_coords)
            cmin, cmax = xp.min(c_coords), xp.max(c_coords)

            zmin, zmax = xp.clip(zmin - extend_bbox_by, 0, labels_frame.shape[0]), xp.clip(zmax + extend_bbox_by, 0,
                                                                                           labels_frame.shape[0])
            rmin, rmax = xp.clip(rmin - extend_bbox_by, 0, labels_frame.shape[1]), xp.clip(rmax + extend_bbox_by, 0,
                                                                                           labels_frame.shape[1])
            cmin, cmax = xp.clip(cmin - extend_bbox_by, 0, labels_frame.shape[2]), xp.clip(cmax + extend_bbox_by, 0,
                                                                                           labels_frame.shape[2])

            # only keep objects over 1 std from its surroundings
            local_intensity = subtraction_mask[zmin:zmax, rmin:rmax, cmin:cmax]
            local_intensity_mean = local_intensity[local_intensity > 0].mean()
            local_intensity_std = local_intensity[local_intensity > 0].std()
            label_intensity_mean = original_frame[coords].mean()
            intensity_cutoff = label_intensity_mean / (local_intensity_mean + local_intensity_std)
            if intensity_cutoff > 1:
                keep_labels.append(label)

        keep_labels = xp.asarray(keep_labels)
        labels_frame = xp.where(xp.isin(labels_frame, keep_labels), labels_frame, 0)
        return labels_frame

    def _run_frame(self, t):
        logger.info(f'Running semantic segmentation, volume {t}/{self.num_t - 1}')
        original_in_mem = xp.asarray(self.im_memmap[t, ...])
        frangi_in_mem = xp.asarray(self.frangi_memmap[t, ...])
        if self.otsu_thresh_intensity or self.threshold is not None:
            if self.otsu_thresh_intensity:
                thresh, _ = otsu_threshold(original_in_mem[original_in_mem > 0])
            else:
                thresh = self.threshold
            mask = original_in_mem > thresh
            original_in_mem *= mask
            frangi_in_mem *= mask
        _, labels = self._get_labels(frangi_in_mem)
        if self.snr_cleaning:
            labels = self._get_object_snrs(original_in_mem, labels)
        labels[labels > 0] += self.max_label_num
        self.max_label_num = xp.max(labels)
        return labels

    def _run_segmentation(self):
        for t in range(self.num_t):
            if self.viewer is not None:
                self.viewer.status = f'Extracting organelles. Frame: {t + 1} of {self.num_t}.'
            labels = self._run_frame(t)
            if device_type == 'cuda':
                labels = labels.get()
            if self.im_info.no_t or self.num_t == 1:
                self.instance_label_memmap[:] = labels[:]
            else:
                self.instance_label_memmap[t, ...] = labels

            self.instance_label_memmap.flush()

    def run(self):
        logger.info('Running semantic segmentation.')
        self._get_t()
        self._allocate_memory()
        self._run_segmentation()


if __name__ == "__main__":
    im_path = r"F:\2024_06_26_SD_ExM_nhs_u2OS_488+578_cropped.tif"
    im_info = ImInfo(im_path, dim_res={'T': 1, 'Z': 0.2, 'Y': 0.1, 'X': 0.1}, dimension_order='ZYX')
    segment_unique = Label(im_info)
    segment_unique.run()
