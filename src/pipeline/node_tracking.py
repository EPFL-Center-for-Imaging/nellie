import numpy as np

from src.pipeline.node_props import Node, NodeConstructor
from src.io.im_info import ImInfo
from src import logger, xp
from src.io.pickle_jar import unpickle_object
from scipy.optimize import linear_sum_assignment


# notes:
# t1 -> t2 means block off assignment both t1 and t2
# t1 -> j2 means block off assignment t1 only
# j1 -> t2 means block off assignment t2 only
# j1 -> j2 is open assignment on both

class NodeTrack:
    def __init__(self, node):
        self.nodes = [node]
        self.time_points = [node.time_point_sec]
        self.centroids_um = [node.centroid_um]
        self.instance_labels = [node.instance_label]
        self.node_types = [node.node_type]

    def add_node(self, node):
        self.nodes.append(node)
        self.time_points.append(node.time_point_sec)
        self.centroids_um.append(node.centroid_um)
        self.instance_labels.append(node.instance_label)
        self.node_types.append(node.node_type)


class NodeTrackConstructor:
    def __init__(self, im_info: ImInfo,
                 distance_thresh_um_per_sec: float = xp.inf,
                 time_thresh_sec: float = xp.inf):
        self.im_info = im_info
        self.node_constructor: NodeConstructor = unpickle_object(self.im_info.path_pickle_node)
        self.nodes: list[list[Node]] = self.node_constructor.nodes
        self.tracks: list[NodeTrack] = []
        self.num_frames = len(self.nodes)
        self.distance_thresh_um_per_sec = distance_thresh_um_per_sec
        self.time_thresh_sec = time_thresh_sec

    def populate_tracks(self, num_t: int = None):
        if num_t is not None:
            num_t = min(num_t, self.num_frames)
            self.num_frames = num_t
        for frame_num in range(self.num_frames):
            if frame_num == 0:
                self._initialize_tracks()
                continue
            assignment_matrix = self._get_assignment_matrix(frame_num)
            self.assignment_matrix = assignment_matrix
            assignments = linear_sum_assignment(assignment_matrix)
        return assignments

    def _initialize_tracks(self):
        for node in self.nodes[0]:
            self.tracks.append(NodeTrack(node))

    def _get_assignment_matrix(self, frame_num):
        frame_nodes = self.nodes[frame_num]
        num_nodes = len(frame_nodes)
        num_tracks = len(self.tracks)
        num_dimensions = len(frame_nodes[0].centroid_um)
        frame_time_s = frame_nodes[0].time_point_sec

        node_centroids = xp.empty((num_dimensions, 1, num_nodes))
        track_centroids = xp.empty((num_dimensions, num_tracks, 1))

        time_matrix = xp.empty((num_tracks, num_nodes))

        for node_num, node in enumerate(frame_nodes):
            node_centroids[:, 0, node_num] = node.centroid_um
        for track_num, track in enumerate(self.tracks):
            time_check = frame_time_s - track.time_points[-1]
            if time_check > self.time_thresh_sec:
                track_centroids[:, track_num, 0] = xp.inf
                continue
            track_centroids[:, track_num, 0] = track.centroids_um[-1]
            time_matrix[track_num, :] = time_check

        distance_matrix = xp.sqrt(xp.sum((node_centroids - track_centroids) ** 2, axis=0))
        distance_matrix /= time_matrix  # this is now a distance/sec matrix
        distance_matrix[distance_matrix > self.distance_thresh_um_per_sec] = xp.inf

        cost_matrix = xp.ones((num_tracks+num_nodes, num_tracks+num_nodes))*self.distance_thresh_um_per_sec
        cost_matrix[:num_tracks, :num_nodes] = distance_matrix

        return cost_matrix

    def _assign_confident_tracks(self, assignment_matrix, frame_num):
        # rows are tracks (t1 nodes), columns are frame nodes (t2 nodes)
        # Find indices of non-nan values
        indices = xp.argwhere(~xp.isnan(assignment_matrix))

        # Count the number of non-nan values in each row and column
        t1_counts = xp.bincount(indices[:, 0])
        t2_counts = xp.bincount(indices[:, 1])

        # Find the rows and columns with exactly one non-nan value
        unique_t1 = xp.where(t1_counts == 1)[0]
        unique_t2 = xp.where(t2_counts == 1)[0]

        # Any t1 nodes that have only one match to a t2 node that has only one match are assigned
        self._assign_unique_matches(unique_t1, unique_t2, assignment_matrix, frame_num)

        # get a node_type-node_type connection look up table
        t1_type_lut = xp.array([[0 if track.node_types[-1] == 'tip' else 2 for track in self.tracks]])
        t2_type_lut = xp.array([[0 if node.node_type == 'tip' else 1 for node in self.nodes[frame_num]]])
        combo_lut = t1_type_lut.T + t2_type_lut  # 0 is t-t, 1 is j-t, 2 is t-j, 3 is j-j
        combo_lut[xp.isnan(assignment_matrix)] = -1  # -1 if no assignment possible

        # key_value_junction_matches (i.e. t2_t1 == {node: [tracks]})
        self._assign_junction_matches(assignment_matrix, combo_lut, frame_num)

    def _assign_unique_matches(self, unique_rows, unique_cols, assignment_matrix, frame_num):
        # Find the common elements between unique_rows and unique_cols
        common_elements = xp.intersect1d(unique_rows, unique_cols)

        # Get the pairs as a list of tuple
        confident_pairs = [(row, col) for row, col in zip(unique_rows, unique_cols)
                           if row in common_elements and col in common_elements]

        # Assign the nodes to the tracks, and sets row and column of assignment matrix to nan:
        for track_num, node_num in confident_pairs:
            self.tracks[track_num].add_node(self.nodes[frame_num][node_num])
        #     assignment_matrix[track_num, :] = xp.nan
        #     assignment_matrix[:, node_num] = xp.nan
        #
        # return assignment_matrix

    def _assign_junction_matches(self, assignment_matrix, combo_lut, frame_num):
        min_t1 = xp.argmin(
            xp.nan_to_num(assignment_matrix, nan=xp.inf, posinf=xp.inf, neginf=xp.inf),
            axis=1).reshape(-1, 1)
        min_t2 = xp.argmin(
            xp.nan_to_num(assignment_matrix, nan=xp.inf, posinf=xp.inf, neginf=xp.inf),
            axis=0).reshape(-1, 1)
        t1_match_types = combo_lut[xp.arange(min_t1.shape[0]).reshape(-1, 1), min_t1[:]]
        t2_match_types = combo_lut[min_t2[:], xp.arange(min_t2.shape[0]).reshape(-1, 1)]

        def _get_junction_matches(junction_idxs, node_matches):
            junction_matches = {}
            for i, node in enumerate(node_matches):
                if node[0] not in junction_matches.keys():
                    junction_matches[node[0]] = [junction_idxs[i]]
                else:
                    junction_matches[node[0]].append(junction_idxs[i])
            return junction_matches

        # a t1 junction can be assigned to any number of t2 junctions and tips:
        t1_junction_idxs = xp.argwhere((t1_match_types == 1) ^ (t1_match_types == 3))[:, 0]
        t2_node_matches = min_t1[t1_junction_idxs]
        # keys are t2 nodes, values are t1 tracks
        t2_t1_junction_matches = _get_junction_matches(t1_junction_idxs, t2_node_matches)

        # a t2 junction can have come from any number of t1 junctions and tips:
        t2_junction_idxs = xp.argwhere((t2_match_types == 2) ^ (t2_match_types == 3))[:, 0]
        t1_node_matches = min_t2[t2_junction_idxs]
        # keys are t1 tracks, values are t2 nodes
        t1_t2_junction_matches = _get_junction_matches(t2_junction_idxs, t1_node_matches)

        def _add_junction_tracks(a_b_junction_matches, b_a_junction_matches):
            # 'a' are keys, 'b' are lists of values
            for a, b in a_b_junction_matches.items():
                if len(a_b_junction_matches[a]) != 1:
                    continue
                if b[0] not in b_a_junction_matches.keys():
                    continue
                if len(b_a_junction_matches[b[0]]) != 1:
                    continue
                if b_a_junction_matches[b[0]][0] == a:
                    self.tracks[a].add_node(self.nodes[frame_num][b[0]])
                    b_a_junction_matches.pop(b[0])

        _add_junction_tracks(t1_t2_junction_matches, t2_t1_junction_matches)
        _add_junction_tracks(t2_t1_junction_matches, t1_t2_junction_matches)

        return t2_t1_junction_matches, t1_t2_junction_matches


if __name__ == "__main__":
    import os
    filepath = r"D:\test_files\nelly\deskewed-single.ome.tif"
    if not os.path.isfile(filepath):
        filepath = "/Users/austin/Documents/Transferred/deskewed-single.ome.tif"
    try:
        test = ImInfo(filepath, ch=0)
    except FileNotFoundError:
        logger.error("File not found.")
        exit(1)
    nodes_test = NodeTrackConstructor(test, distance_thresh_um_per_sec=0.5)
    assignments = nodes_test.populate_tracks()
    # todo visualize whats going on and make sure it's correct

