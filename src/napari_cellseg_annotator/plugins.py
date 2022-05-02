from napari_cellseg_annotator.plugin_helper import Helper
from napari_cellseg_annotator.plugin_model_inference import Inferer
from napari_cellseg_annotator.plugin_model_training import Trainer
from napari_cellseg_annotator.plugin_review import Reviewer
from napari_cellseg_annotator.plugin_utilities import Utilities


def napari_experimental_provide_dock_widget():
    return [
        (Reviewer, {"name": "Review loader"}),
        (Helper, {"name": "Help/About..."}),
        (Inferer, {"name": "Inference loader"}),
        (Trainer, {"name": "Training loader"}),
        (Utilities, {"name": "Utilities"}),
    ]
