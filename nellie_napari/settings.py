from qtpy.QtWidgets import QWidget, QCheckBox, QSpinBox, QLabel, QVBoxLayout, QGroupBox, QHBoxLayout
import napari


class Settings(QWidget):
    def __init__(self, napari_viewer: 'napari.viewer.Viewer', nellie, parent=None):
        super().__init__(parent)
        self.nellie = nellie
        self.viewer = napari_viewer

        # Checkbox for 'Remove edges'
        self.remove_edges_checkbox = QCheckBox("Remove image edges")
        self.remove_edges_checkbox.setChecked(False)
        self.remove_edges_checkbox.setEnabled(True)
        self.remove_edges_checkbox.setToolTip(
            "Originally for Snouty deskewed images. If you see weird image edge artifacts, enable this.")

        self.voxel_reassign = QCheckBox("Auto-run voxel reassignment")
        self.voxel_reassign.setChecked(False)
        self.voxel_reassign.setEnabled(True)

        # Analyze node level
        self.analyze_node_level = QCheckBox("Analyze node level (slow)")
        self.analyze_node_level.setChecked(False)
        self.analyze_node_level.setEnabled(True)

        # Track all frames
        self.track_all_frames = QCheckBox("Track all frames' voxels")
        self.track_all_frames.setChecked(True)
        self.track_all_frames.setEnabled(True)

        # Label above the spinner box
        self.skip_vox_label = QLabel("Track every N voxel. N=")

        self.skip_vox = QSpinBox()
        self.skip_vox.setRange(1, 10000)
        self.skip_vox.setValue(5)
        self.skip_vox.setEnabled(False)

        self.set_ui()

        self.initialized = False

    def post_init(self):
        self.initialized = True

    def set_ui(self):
        main_layout = QVBoxLayout()

        # Processor settings
        processor_group = QGroupBox("Processor settings")
        processor_layout = QVBoxLayout()
        processor_layout.addWidget(self.remove_edges_checkbox)
        subprocessor_layout = QHBoxLayout()
        subprocessor_layout.addWidget(self.analyze_node_level)
        subprocessor_layout.addWidget(self.voxel_reassign)
        processor_layout.addLayout(subprocessor_layout)
        processor_group.setLayout(processor_layout)

        # Tracking settings
        tracking_group = QGroupBox("Tracking settings")
        tracking_layout = QVBoxLayout()
        tracking_layout.addWidget(self.track_all_frames)
        skip_vox_layout = QHBoxLayout()
        skip_vox_layout.addWidget(self.skip_vox_label)
        skip_vox_layout.addWidget(self.skip_vox)
        tracking_layout.addLayout(skip_vox_layout)
        tracking_group.setLayout(tracking_layout)

        main_layout.addWidget(processor_group)
        main_layout.addWidget(tracking_group)
        self.setLayout(main_layout)


if __name__ == "__main__":
    import napari
    viewer = napari.Viewer()
    napari.run()
