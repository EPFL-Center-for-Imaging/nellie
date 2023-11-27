from src_2.im_info.im_info import ImInfo
from src_2.segmentation.filtering import Filter
from src_2.segmentation.labelling import Label
from src_2.segmentation.mocap_marking import Markers
from src_2.segmentation.networking import Network
from src_2.tracking.hu_tracking import HuMomentTracking


def run(im_path, num_t=None, remove_edges=True):
    im_info = ImInfo(im_path)

    preprocessing = Filter(im_info, num_t, remove_edges=remove_edges)
    preprocessing.run()

    segmenting = Label(im_info, num_t)
    segmenting.run()

    networking = Network(im_info, num_t)
    networking.run()

    mocap_marking = Markers(im_info, num_t)
    mocap_marking.run()

    hu_tracking = HuMomentTracking(im_info, num_t)
    hu_tracking.run()

    return im_info

if __name__ == "__main__":
    im_path = r"D:\test_files\nelly_tests\deskewed-2023-07-13_14-58-28_000_wt_0_acquire.ome.tif"
    im_info = run(im_path, remove_edges=True, num_t=3)
    # import os
    # # top_dir = r"D:\test_files\stress_granules"
    # top_dir = r"D:\test_files\nelly_gav_tests"
    # # get all non-folder files
    # all_files = os.listdir(top_dir)
    # all_files = [os.path.join(top_dir, file) for file in all_files if not os.path.isdir(os.path.join(top_dir, file))]
    # all_files = [r"D:\test_files\nelly_tests\deskewed-2023-07-13_14-58-28_000_wt_0_acquire.ome.tif"]
    # for file_num, tif_file in enumerate(all_files):
    #     print(f'Processing file {file_num + 1} of {len(all_files)}')
    #     im_info = run(tif_file, remove_edges=True, num_t=2)
