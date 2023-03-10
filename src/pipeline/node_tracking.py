from src.pipeline.node_props import Node, NodeConstructor
from src.io.im_info import ImInfo
from src import logger
from src.io.pickle_jar import unpickle_object
from scipy.optimize import linear_sum_assignment
import numpy as xp

# todo keep track of those mito that are well separated from others (some kind of # nearest neighbors)
# todo reassign unconfident tracks that are pretty sure to be valid, maybe based on # nearest neighbors?

class NodeTrack:
    def __init__(self, node, frame_num):
        self.nodes = [node]
        self.time_points = [node.time_point_sec]
        self.frame_nums = [frame_num]
        self.centroids_um = [node.centroid_um]
        self.instance_labels = [node.instance_label]
        self.node_num = [node.instance_label - 1]
        self.node_types = [node.node_type]
        self.assignment_cost = [0]
        self.confidence = [0]

        self.possible_merges_to = {}
        self.possible_emerges_from = {}

    def add_node(self, node, frame_num, assignment_cost, confident, node_num):
        self.nodes.append(node)
        self.time_points.append(node.time_point_sec)
        self.frame_nums.append(frame_num)
        self.centroids_um.append(node.centroid_um)
        self.instance_labels.append(node.instance_label)
        self.node_num.append(node_num)
        self.node_types.append(node.node_type)
        self.assignment_cost.append(assignment_cost)
        self.confidence.append(confident)

    def possibly_merged_to(self, node, frame_num, assignment_cost):
        if frame_num not in self.possible_merges_to.keys():
            self.possible_emerges_from[frame_num] = [(node, assignment_cost)]
        else:
            self.possible_merges_to[frame_num].append((node, assignment_cost))

    def possibly_emerged_from(self, track, frame_num, assignment_cost):
        if frame_num not in self.possible_emerges_from.keys():
            self.possible_emerges_from[frame_num] = [(track, assignment_cost)]
        else:
            self.possible_emerges_from[frame_num].append((track, assignment_cost))


class NodeTrackConstructor:
    def __init__(self, im_info: ImInfo,
                 distance_thresh_um_per_sec: float = 2,
                 time_thresh_sec: float = xp.inf):

        self.im_info = im_info
        self.node_constructor: NodeConstructor = unpickle_object(self.im_info.path_pickle_node)
        self.nodes: list[list[Node]] = self.node_constructor.nodes
        self.tracks: list[NodeTrack] = []

        self.distance_thresh_um_per_sec = distance_thresh_um_per_sec
        self.time_thresh_sec = time_thresh_sec

        self.average_assignment_cost = {}
        self.average_std_assignment_cost_unconfident = {}
        self.average_std_assignment_cost_confident = {}

        self.num_frames = len(self.nodes)
        self.num_nodes = 0
        self.num_tracks = 0

        self.assigned_unassigned = {}

        self.tracks_to_assign = []
        self.nodes_to_assign = []

    def populate_tracks(self, num_t: int = None):
        if num_t is not None:
            num_t = min(num_t, self.num_frames)
            self.num_frames = num_t
        for frame_num in range(self.num_frames):
            if frame_num == 0:
                self._initialize_tracks(frame_num)
                continue
            cost_matrix = self._get_assignment_matrix(frame_num)
            # self.assignment_matrix = assignment_matrix
            track_nums, node_nums = linear_sum_assignment(cost_matrix)
            self.track_nums = track_nums
            self.node_nums = node_nums
            self.average_assignment_cost[frame_num] = cost_matrix[track_nums, node_nums].sum() / len(track_nums)
            self._assign_confidence_1_matches(track_nums, node_nums, cost_matrix, frame_num)
            self._assign_confidence_2_matches(track_nums, node_nums, cost_matrix, frame_num)
            self._assign_confidence_3_matches(track_nums, node_nums, cost_matrix, frame_num)
            self._check_unassigned_tracks(track_nums, node_nums, cost_matrix, frame_num)
            self._check_new_tracks(track_nums, node_nums, cost_matrix, frame_num)
            # go through nodes. if unassigned, find lowest assignment (if not the unassigned one)
            #   and assign it as the fission point
            # if the fusion or fission point has too many nodes fusing / fissioning from it, keep the N lowest cost
            #   the rest should be rightfully unassigned.
        # return assignments

    def _initialize_tracks(self, frame_num):
        for node in self.nodes[0]:
            self.tracks.append(NodeTrack(node, frame_num))

    def _get_assignment_matrix(self, frame_num):
        frame_nodes = self.nodes[frame_num]
        self.num_nodes = len(frame_nodes)
        self.num_tracks = len(self.tracks)
        self.tracks_to_assign = list(range(self.num_tracks))
        self.nodes_to_assign = list(range(self.num_nodes))

        num_dimensions = len(frame_nodes[0].centroid_um)
        frame_time_s = frame_nodes[0].time_point_sec

        node_centroids = xp.empty((num_dimensions, 1, self.num_nodes))
        track_centroids = xp.empty((num_dimensions, self.num_tracks, 1))

        time_matrix = xp.empty((self.num_tracks, self.num_nodes))

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

        cost_matrix = xp.ones(
            (self.num_tracks+self.num_nodes, self.num_tracks+self.num_nodes)
        ) * self.distance_thresh_um_per_sec
        cost_matrix[:self.num_tracks, :self.num_nodes] = distance_matrix
        self.cost_matrix = cost_matrix
        return cost_matrix

    def _assign_confidence_1_matches(self, track_nums, node_nums, cost_matrix, frame_num):

        # First for nodes
        for check_node_num in self.nodes_to_assign:
            check_match_idx = xp.where(node_nums == check_node_num)[0][0]
            check_track_num = track_nums[check_match_idx]
            # Skip if this node was assigned to start a new track
            if check_track_num >= self.num_tracks:
                continue
            node_to_assign = self.nodes[frame_num][check_node_num]
            assignment_cost = cost_matrix[check_track_num, check_node_num]

            possible_assignments = xp.array(
                cost_matrix[cost_matrix[:, check_node_num] < self.distance_thresh_um_per_sec, check_node_num]
            )
            sorted_possible = xp.sort(possible_assignments)


            # Assign nodes to their matched tracks if that match is the lowest possible match cost
            if assignment_cost == sorted_possible[0]:
                # check the track's costs. If cost of assignment is higher than its min match cost, skip.
                track_possible_assignments = xp.array(
                    cost_matrix[check_track_num, cost_matrix[check_track_num, :] < self.distance_thresh_um_per_sec]
                )
                min_track_cost = xp.min(track_possible_assignments)
                if assignment_cost-min_track_cost > 0:
                    continue
                self.tracks[check_track_num].add_node(node_to_assign, frame_num, assignment_cost,
                                                      confident=1, node_num=check_node_num)
                self.tracks_to_assign.remove(check_track_num)
                self.nodes_to_assign.remove(check_node_num)

        # Then for tracks
        for check_track_num in self.tracks_to_assign:
            check_match_idx = xp.where(track_nums == check_track_num)[0][0]
            check_node_num = node_nums[check_match_idx]
            # Skip if this track was assigned to be lost
            if check_node_num >= self.num_nodes:
                continue
            node_to_assign = self.nodes[frame_num][check_node_num]
            assignment_cost = cost_matrix[check_track_num, check_node_num]

            possible_assignments = xp.array(
                cost_matrix[check_track_num, cost_matrix[check_track_num, :] < self.distance_thresh_um_per_sec]
            )
            sorted_possible = xp.sort(possible_assignments)

            # Assign tracks to their matched nodes if that match is the lowest possible match cost
            if assignment_cost == sorted_possible[0]:
                # check the node's costs. If cost of assignment is higher than its min match cost, skip.
                node_possible_assignments = xp.array(
                    cost_matrix[cost_matrix[:, check_node_num] < self.distance_thresh_um_per_sec, check_node_num]
                )
                min_node_cost = xp.min(node_possible_assignments)
                if assignment_cost - min_node_cost > 0:
                    continue
                self.tracks[check_track_num].add_node(node_to_assign, frame_num, assignment_cost,
                                                      confident=1, node_num=check_node_num)
                self.tracks_to_assign.remove(check_track_num)
                self.nodes_to_assign.remove(check_node_num)

    def _assign_confidence_2_matches(self, track_nums, node_nums, cost_matrix, frame_num):
        # First for nodes
        for check_node_num in self.nodes_to_assign:
            check_match_idx = xp.where(node_nums == check_node_num)[0][0]
            check_track_num = track_nums[check_match_idx]
            # todo could make this faster by only feeding in list of nodes/tracks that are valid.
            # Skip if this node was assigned to start a new track
            if check_track_num >= self.num_tracks:
                continue
            node_to_assign = self.nodes[frame_num][check_node_num]
            assignment_cost = cost_matrix[check_track_num, check_node_num]

            possible_assignments = xp.array(
                cost_matrix[cost_matrix[:, check_node_num] < self.distance_thresh_um_per_sec, check_node_num]
            )
            possible_assignments = xp.concatenate([possible_assignments,
                                    xp.array(cost_matrix[
                                        check_track_num,
                                        cost_matrix[check_track_num, :] < self.distance_thresh_um_per_sec]
                                    )])
            # If it has only one other possible match, and it's assigned to it, assign it.
            if len(possible_assignments) == 2:
                self.tracks[check_track_num].add_node(node_to_assign, frame_num, assignment_cost,
                                                      confident=2, node_num=check_node_num)
                self.tracks_to_assign.remove(check_track_num)
                self.nodes_to_assign.remove(check_node_num)

        # Then for tracks
        for check_track_num in self.tracks_to_assign:
            check_match_idx = xp.where(track_nums == check_track_num)[0][0]
            check_node_num = node_nums[check_match_idx]
            # Skip if this track was assigned to be lost
            if check_node_num >= self.num_nodes:
                continue
            node_to_assign = self.nodes[frame_num][check_node_num]
            assignment_cost = cost_matrix[check_track_num, check_node_num]

            possible_assignments = xp.array(
                cost_matrix[check_track_num, cost_matrix[check_track_num, :] < self.distance_thresh_um_per_sec]
            )
            possible_assignments = xp.concatenate([possible_assignments,
                                    xp.array(cost_matrix[
                                        cost_matrix[:, check_node_num] < self.distance_thresh_um_per_sec,
                                        check_node_num]
                                    )])
            # If it has only one other possible match, and it's assigned to it, assign it.
            if len(possible_assignments) == 2:
                self.tracks[check_track_num].add_node(node_to_assign, frame_num, assignment_cost,
                                                      confident=2, node_num=check_node_num)
                self.tracks_to_assign.remove(check_track_num)
                self.nodes_to_assign.remove(check_node_num)

    def _assign_confidence_3_matches(self, track_nums, node_nums, cost_matrix, frame_num):
        valid_cost_matrix = cost_matrix[self.tracks_to_assign, :][:, self.nodes_to_assign]
        valid_costs = valid_cost_matrix[valid_cost_matrix<self.distance_thresh_um_per_sec]
        valid_costs.sort()
        hold_tracks_to_assign = self.tracks_to_assign.copy()
        hold_nodes_to_assign = self.nodes_to_assign.copy()

        min_each_col = xp.min(valid_cost_matrix, axis=0)
        min_col = xp.argmin(min_each_col)
        min_each_row = xp.min(valid_cost_matrix, axis=1)
        min_row = xp.argmin(min_each_row)
        min_all = xp.argmin([min_each_col[min_col], min_each_row[min_row]])
        if min_all == 0:
            col = min_col
            row = xp.argmin(valid_cost_matrix[col, :])
        elif min_all == 1:
            row = min_row
            col = xp.argmin(valid_cost_matrix[:, row])
            # col = xp.argmin(valid_cost_matrix[min_col_of_min_row, :])
            print(col)
            # row, col = min_row, min_row_of_min_col
        # min_col = xp.min(valid_cost_matrix, axis=1)
        print(min_row, min_col)
        return
        #do a loop, remove track/node from list, check if in list next iteration
        for valid_cost in valid_costs:
            rows, cols = xp.where(valid_cost_matrix == valid_cost)
            for idx in range(len(rows)):
                track_to_check = hold_tracks_to_assign[rows[idx]]
                node_to_check = hold_nodes_to_assign[cols[idx]]
                if (track_to_check not in self.tracks_to_assign) or (node_to_check not in self.nodes_to_assign):
                    continue
                lowest_costs = cost_matrix[track_to_check, :]
                lowest_costs = xp.concatenate([lowest_costs, cost_matrix[:, node_to_check]])
                if xp.min(lowest_costs) == valid_cost:
                    node_to_assign = self.nodes[frame_num][node_to_check]
                    self.tracks[track_to_check].add_node(node_to_assign, frame_num, valid_cost,
                                                         confident=3, node_num=node_to_check)
                    self.tracks_to_assign.remove(track_to_check)
                    self.nodes_to_assign.remove(node_to_check)

    def _check_unassigned_tracks(self, track_nums, node_nums, cost_matrix, frame_num):
        # Get a list of all the unassigned tracks
        unassigned_tracks_all = track_nums[xp.where(node_nums > self.num_nodes)]
        unassigned_tracks_all = unassigned_tracks_all[xp.where(unassigned_tracks_all < self.num_tracks)]

        # Get the cost matrix only of unassigned tracks and all nodes
        unassigned_track_cost_matrix = cost_matrix[unassigned_tracks_all, :]

        # Get coordinates of all possible nodes where the track could have merged to and save those
        unassigned_track_idx, nearby_nodes = xp.where(unassigned_track_cost_matrix < self.distance_thresh_um_per_sec)
        for idx in range(len(unassigned_track_idx)):
            unassigned_track_num = unassigned_tracks_all[unassigned_track_idx[idx]]
            assignment_cost = cost_matrix[unassigned_track_num, nearby_nodes[idx]]
            self.tracks[unassigned_track_num].possibly_merged_to(nearby_nodes[idx], frame_num, assignment_cost)

    def _check_new_tracks(self, track_nums, node_nums, cost_matrix, frame_num):
        # Get a list of all the new tracks
        new_tracks_all = node_nums[xp.where(track_nums > self.num_tracks)]
        new_tracks_all = new_tracks_all[xp.where(new_tracks_all < self.num_nodes)]

        # Get the cost matrix only of all existing tracks and nodes that will form new tracks
        new_track_cost_matrix = cost_matrix[:, new_tracks_all]

        # Get coordinates of all possible existing tracks where the new track could have emerged
        nearby_tracks, new_track_node_idx = xp.where(new_track_cost_matrix < self.distance_thresh_um_per_sec)
        new_track_nodes = new_tracks_all[new_track_node_idx]

        for idx, new_track_node in enumerate(new_tracks_all):
            # Create a new track
            possibly_emerged_from_tracks = nearby_tracks[xp.where(new_track_nodes == new_track_node)]
            new_track = NodeTrack(self.nodes[frame_num][new_track_node], frame_num)

            # Save what tracks the new track may have emerged from
            for possible_track in possibly_emerged_from_tracks:
                assignment_cost = cost_matrix[possible_track, new_track_node]
                new_track.possibly_emerged_from(self.tracks[possible_track], frame_num, assignment_cost)

            # Append new track to existing track list
            self.tracks.append(new_track)


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
    nodes_test = NodeTrackConstructor(test, distance_thresh_um_per_sec=1)
    nodes_test.populate_tracks()
    print('hi')

    visualize = False

    if visualize:
        from src.utils.visualize import track_list_to_napari_track
        import napari
        import tifffile

        napari_tracks, properties = track_list_to_napari_track(nodes_test.tracks)
        viewer = napari.Viewer(ndisplay=3)
        viewer.add_image(tifffile.memmap(test.path_im_mask),
                         scale=[test.dim_sizes['Z'], test.dim_sizes['Y'], test.dim_sizes['X']],
                         rendering='iso', iso_threshold=0, opacity=0.2, contrast_limits=[0, 1])
        viewer.add_tracks(napari_tracks, properties=properties, color_by='confidence')
        neighbor_layer = viewer.add_image(tifffile.memmap(test.path_im_neighbors),
                         scale=[test.dim_sizes['Z'], test.dim_sizes['Y'], test.dim_sizes['X']],
                         contrast_limits=[0, 3], colormap='turbo', interpolation='nearest', opacity=0.2)
        neighbor_layer.interpolation = 'nearest'
