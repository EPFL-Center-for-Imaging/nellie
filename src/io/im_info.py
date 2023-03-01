import os

import tifffile
import ome_types
from src.utils.base_logger import logger
import numpy as np
from typing import Union, Type


# todo make this work with no "t" dimension. Just have it segment, no tracking.
# todo also make this work in 2d
class ImInfo:
    """
    A class that extracts metadata and image size information from a TIFF file.
    This will accept a path to an image, store useful info, and produce output directories for downstream functions.

    Attributes:
        im_path (str): Path to the input TIFF file.
        output_dirpath (str, optional): Path to the output top directory. im_path if none given.
        ch (int, optional): Channel index for multichannel TIFF files.
        dim_sizes (dict, optional): Dictionary mapping dimension names to physical voxel sizes.
    """
    def __init__(self, im_path: str, output_dirpath: str = None, ch: int = 0, dim_sizes: dict = None):
        """
        Initialize an ImInfo object for a TIFF file.

        Args:
            im_path (str): Path to the input TIFF file.
            output_dirpath (str, optional): Path to the output top directory. im_path if none given.
            ch (int, optional): Channel index for multichannel TIFF files.
            dim_sizes (dict, optional): Dictionary mapping dimension names to physical voxel sizes.

        Returns:
            ImInfo object.
        """
        self.im_path = im_path
        self.ch = ch
        self.dim_sizes = dim_sizes
        self.extension = self.im_path.split('.')[-1]
        self.sep = os.sep if os.sep in self.im_path else '/'
        self.filename = self.im_path.split(self.sep)[-1].split('.'+self.extension)[0]
        try:
            self.dirname = self.im_path.split(self.sep)[-2]
        except IndexError:
            self.dirname = ''
        self.input_dirpath = self.im_path.split(self.sep+self.filename)[0]
        self.axes = None
        self.shape = None
        self.metadata = None
        self._get_metadata()
        if self.dim_sizes is None:
            self._get_dim_sizes()
        if self.dim_sizes['X'] != self.dim_sizes['Y']:
            logger.warning('X and Y dimensions do not match. Rectangular pixels not yet supported, '
                           'so unexpected results and wrong measurements will occur.')

        self.output_dirpath = None
        self.output_images_dirpath = None
        self.output_pickles_dirpath = None
        self.output_csv_dirpath = None
        self._create_output_dirs(output_dirpath)

        self.path_im_frangi = None
        self.path_im_mask = None
        self.path_im_skeleton = None
        self.path_im_label_obj = None
        self.path_im_label_seg = None
        self.path_im_network = None
        self.path_im_event = None
        self.path_pickle_obj = None
        self.path_pickle_seg = None
        self.path_pickle_track = None
        self._set_output_filepaths()

    def _get_metadata(self):
        """
        Load metadata, axes and shape information from the image file using tifffile.

        Raises:
            Exception: If there was an error loading the image file, an error message is logged and the program exits.
        """
        logger.debug('Getting metadata.')
        try:
            with tifffile.TiffFile(self.im_path) as tif:
                if tif.is_imagej:
                    self.metadata = tif.imagej_metadata
                    self.metadata_type = 'imagej'
                elif tif.is_ome:
                    ome_xml = tifffile.tiffcomment(self.im_path)
                    ome = ome_types.from_xml(ome_xml, parser="lxml")
                    self.metadata = ome
                    self.metadata_type = 'ome'
                self.axes = tif.series[0].axes
                self.shape = tif.series[0].shape
        except Exception as e:
            logger.error(f"Error loading file {self.im_path}: {str(e)}")
            exit(1)
        if self.axes not in ['TZYX', 'TYX', 'TZCYX', 'TCYX', 'TCZYX']:
            logger.error(f"File dimensions must be in one of these orders: 'TZYX', 'TYX', 'TZCYX', 'TCYX', 'TCZYX'")
            exit(1)

    def _get_dim_sizes(self):
        """Extract physical dimensions of image from its metadata and populate the dim_sizes attribute."""
        logger.debug('Getting dimension sizes.')
        try:
            self.dim_sizes = {'X': None, 'Y': None, 'Z': None, 'T': None}
            if self.metadata_type == 'imagej':
                if 'physicalsizex' in self.metadata:
                    self.dim_sizes['X'] = self.metadata['physicalsizex']
                if 'physicalsizey' in self.metadata:
                    self.dim_sizes['Y'] = self.metadata['physicalsizey']
                if 'spacing' in self.metadata:
                    self.dim_sizes['Z'] = self.metadata['spacing']
                if 'finterval' in self.metadata:
                    self.dim_sizes['T'] = self.metadata['finterval']
            elif self.metadata_type == 'ome':
                self.dim_sizes['X'] = self.metadata.images[0].pixels.physical_size_x
                self.dim_sizes['Y'] = self.metadata.images[0].pixels.physical_size_y
                self.dim_sizes['Z'] = self.metadata.images[0].pixels.physical_size_z
                self.dim_sizes['T'] = self.metadata.images[0].pixels.time_increment
            self.dim_sizes['C'] = 1
        except Exception as e:
            logger.error(f"Error loading metadata for image {self.im_path}: {str(e)}")
            self.metadata = {}
            self.dim_sizes = {}

    def _create_output_dirs(self, output_dirpath=None):
        """
        Create output directories for a given file path if they don't exist.
        Specifically, creates output subdirectories for output images, pickle files, and csv files.

        Args:
            output_dirpath (str): The path to the directory where "output_dirpath/output" directory will be added to.
            The "output_dirpath/output" directory will be created if it doesn't exist.

        Returns:
            None
        """
        logger.debug('Creating output directories')
        if output_dirpath is None:
            output_dirpath = self.input_dirpath
        self.output_dirpath = os.path.join(output_dirpath, 'output')
        self.output_images_dirpath = os.path.join(self.output_dirpath, 'images')
        self.output_pickles_dirpath = os.path.join(self.output_dirpath, 'pickles')
        self.output_csv_dirpath = os.path.join(self.output_dirpath, 'csv')
        dirs_to_make = [self.output_images_dirpath, self.output_pickles_dirpath, self.output_csv_dirpath]
        for dir_to_make in dirs_to_make:
            os.makedirs(dir_to_make, exist_ok=True)

    def _set_output_filepaths(self):
        """
        Set the output file paths for various file types. These file paths are based on the input file path and output
        directory.
        """
        logger.debug('Setting output filepaths.')
        self.path_im_frangi = os.path.join(self.output_images_dirpath, f'frangi-{self.filename}.tif')
        self.path_im_mask = os.path.join(self.output_images_dirpath, f'mask-{self.filename}.tif')
        self.path_im_skeleton = os.path.join(self.output_images_dirpath, f'skeleton-{self.filename}.tif')
        self.path_im_label_obj = os.path.join(self.output_images_dirpath, f'label_obj-{self.filename}.tif')
        self.path_im_label_seg = os.path.join(self.output_images_dirpath, f'label_seg-{self.filename}.tif')
        self.path_im_network = os.path.join(self.output_images_dirpath, f'network-{self.filename}.tif')
        self.path_im_event = os.path.join(self.output_images_dirpath, f'event-{self.filename}.tif')
        self.path_pickle_obj = os.path.join(self.output_pickles_dirpath, f'obj-{self.filename}.pkl')
        self.path_pickle_seg = os.path.join(self.output_pickles_dirpath, f'seg-{self.filename}.pkl')
        self.path_pickle_track = os.path.join(self.output_pickles_dirpath, f'track-{self.filename}.pkl')

    def allocate_memory(
            self,
            path_im: str, dtype: Union[Type, str] = 'float', data=None,
            shape: tuple = None,
            description: str = 'No description.'):
        axes = self.axes
        axes = axes.replace('C', '') if 'C' in axes else axes
        logger.debug(f'Saving axes as {axes}')
        if data is None:
            assert shape is not None
            tifffile.imwrite(
                path_im, shape=shape, dtype=dtype, bigtiff=True, metadata={"axes": axes}
            )
        else:
            tifffile.imwrite(
                path_im, data, bigtiff=True, metadata={"axes": axes}
            )
        ome_xml = tifffile.tiffcomment(path_im)
        ome = ome_types.from_xml(ome_xml, parser="lxml")
        ome.images[0].pixels.physical_size_x = self.dim_sizes['X']
        ome.images[0].pixels.physical_size_y = self.dim_sizes['Y']
        ome.images[0].pixels.physical_size_z = self.dim_sizes['Z']
        ome.images[0].pixels.time_increment = self.dim_sizes['T']
        ome.images[0].description = description
        ome.images[0].pixels.type = dtype
        # try:
        #     ome.images[0].pixels.type = dtype
        # except:
        #     logger.debug('dtype not accepted, using bit instead.')
        #     dtype = 'bit'
        #     ome.images[0].pixels.significant_bits = 1
        #     ome.images[0].pixels.type = dtype
        ome_xml = ome.to_xml()
        tifffile.tiffcomment(path_im, ome_xml)

    def get_im_memmap(self, path_im: str):
        """
        Loads an image from a TIFF file located at `path_im` using the `tifffile.memmap` function,
        and returns a memory-mapped array of the image data.

        If the `C` axis is present in the image and the image shape matches the number of dimensions specified in
        `self.axes`, only the channel specified in `self.ch` will be returned, otherwise the entire image will be
        returned.

        Args:
            path_im (str): The path to the TIFF file containing the image to load.

        Returns:
            np.ndarray: A memory-mapped array of the image data, with shape and data type determined by the file.
        """
        logger.debug('Getting and returning read-only memmap.')
        im_memmap = tifffile.memmap(path_im, mode='r')

        # Only get wanted channel
        if ('C' in self.axes) and (len(im_memmap.shape) == len(self.axes)):
            im_memmap = np.take(im_memmap, self.ch, axis=self.axes.index('C'))
        return im_memmap


if __name__ == "__main__":
    filepath = r"D:\test_files\nelly\deskewed-single.ome.tif"
    test = ImInfo(filepath)
    memmap = test.get_im_memmap(test.im_path)
