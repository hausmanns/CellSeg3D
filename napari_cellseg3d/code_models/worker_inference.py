import platform
from pathlib import Path

import numpy as np
import torch

# MONAI
from monai.data import DataLoader, Dataset
from monai.inferers import sliding_window_inference
from monai.transforms import (
    AddChannel,
    AsDiscrete,
    Compose,
    EnsureChannelFirstd,
    EnsureType,
    EnsureTyped,
    LoadImaged,
    SpatialPad,
    SpatialPadd,
    ToTensor,
    Zoom,
)
from napari._qt.qthreading import GeneratorWorker
from tifffile import imwrite

# local
from napari_cellseg3d import config, utils
from napari_cellseg3d.code_models.crf import crf_with_config
from napari_cellseg3d.code_models.instance_segmentation import volume_stats
from napari_cellseg3d.code_models.workers_utils import (
    PRETRAINED_WEIGHTS_DIR,
    InferenceResult,
    LogSignal,
    ONNXModelWrapper,
    QuantileNormalizationd,
    WeightsDownloader,
)
from napari_cellseg3d.utils import LOGGER as logger

"""
Writing something to log messages from outside the main thread needs specific care,
Following the instructions in the guides below to have a worker with custom signals,
a custom worker function was implemented.
"""

# https://python-forum.io/thread-31349.html
# https://www.pythoncentral.io/pysidepyqt-tutorial-creating-your-own-signals-and-slots/
# https://napari-staging-site.github.io/guides/stable/threading.html


class InferenceWorker(GeneratorWorker):
    """A custom worker to run inference jobs in.
    Inherits from :py:class:`napari.qt.threading.GeneratorWorker`"""

    def __init__(
        self,
        worker_config: config.InferenceWorkerConfig,
    ):
        """Initializes a worker for inference with the arguments needed by the :py:func:`~inference` function.

        The config contains the following attributes:
        * device: cuda or cpu device to use for torch
        * model_dict: the :py:attr:`~self.models_dict` dictionary to obtain the model name, class and instance
        * weights_dict: dict with "custom" : bool to use custom weights or not; "path" : the path to weights if custom or name of the file if not custom
        * results_path: the path to save the results to
        * filetype: the file extension to use when saving,
        * transforms: a dict containing transforms to perform at various times.
        * instance: a dict containing parameters regarding instance segmentation
        * use_window: use window inference with specific size or whole image
        * window_infer_size: size of window if use_window is True
        * keep_on_cpu: keep images on CPU or no
        * stats_csv: compute stats on cells and save them to a csv file
        * images_filepaths: the paths to the images of the dataset
        * layer: the layer to run inference on

        Args:
            * worker_config (config.InferenceWorkerConfig): dataclass containing the proper configuration elements

        Note: See :py:func:`~self.inference`
        """

        super().__init__(self.inference)
        self._signals = LogSignal()  # add custom signals
        self.log_signal = self._signals.log_signal
        self.warn_signal = self._signals.warn_signal
        self.error_signal = self._signals.error_signal

        self.config = worker_config

        """These attributes are all arguments of :py:func:~inference, please see that for reference"""

        self.downloader = WeightsDownloader()
        """Download utility"""

    @staticmethod
    def create_inference_dict(images_filepaths):
        """Create a dict for MONAI with "image" keys with all image paths in :py:attr:`~self.images_filepaths`

        Returns:
            dict: list of image paths from loaded folder"""
        return [{"image": image_name} for image_name in images_filepaths]

    def set_download_log(self, widget):
        self.downloader.log_widget = widget

    def log(self, text):
        """Sends a signal that ``text`` should be logged

        Args:
            text (str): text to logged
        """
        self.log_signal.emit(text)

    def warn(self, warning):
        """Sends a warning to main thread"""
        self.warn_signal.emit(warning)

    def raise_error(self, exception, msg):
        """Raises an error in main thread"""
        logger.error(msg, exc_info=True)
        logger.error(exception, exc_info=True)

        self.log_signal.emit("!" * 20)
        self.log_signal.emit("Error occured")
        # self.log_signal.emit(msg)
        # self.log_signal.emit(str(exception))

        self.error_signal.emit(exception, msg)
        self.errored.emit(exception)
        yield exception
        # self.quit()

    def log_parameters(self):
        config = self.config

        self.log("-" * 20)
        self.log("\nParameters summary :")

        self.log(f"Model is : {config.model_info.name}")
        if config.post_process_config.thresholding.enabled:
            self.log(
                f"Thresholding is enabled at {config.post_process_config.thresholding.threshold_value}"
            )

        status = (
            "enabled"
            if config.sliding_window_config.is_enabled()
            else "disabled"
        )

        self.log(f"Window inference is {status}\n")
        if status == "enabled":
            self.log(
                f"Window size is {self.config.sliding_window_config.window_size}"
            )
            self.log(
                f"Window overlap is {self.config.sliding_window_config.window_overlap}"
            )

        if config.keep_on_cpu:
            self.log("Dataset loaded to CPU")
        else:
            self.log(f"Dataset loaded on {config.device} device")

        if config.post_process_config.zoom.enabled:
            self.log(
                f"Scaling factor : {config.post_process_config.zoom.zoom_values} (x,y,z)"
            )

        instance_config = config.post_process_config.instance
        if instance_config.enabled:
            self.log(
                f"Instance segmentation enabled, method : {instance_config.method.name}\n"
            )
        self.log("-" * 20)

    def load_folder(self):
        images_dict = self.create_inference_dict(self.config.images_filepaths)

        # TODO : better solution than loading first image always ?
        data_check = LoadImaged(keys=["image"])(images_dict[0])
        check = data_check["image"].shape
        pad = utils.get_padding_dim(check)

        if self.config.sliding_window_config.is_enabled():
            load_transforms = Compose(
                [
                    LoadImaged(keys=["image"]),
                    # AddChanneld(keys=["image"]), #already done
                    EnsureChannelFirstd(keys=["image"]),
                    # Orientationd(keys=["image"], axcodes="PLI"),
                    # anisotropic_transform,
                    QuantileNormalizationd(keys=["image"]),
                    EnsureTyped(keys=["image"]),
                ]
            )
        else:
            load_transforms = Compose(
                [
                    LoadImaged(keys=["image"]),
                    # AddChanneld(keys=["image"]), #already done
                    EnsureChannelFirstd(keys=["image"]),
                    # NormalizeIntensityd(keys=["image"]),  # TODO normalize
                    # Orientationd(keys=["image"], axcodes="PLI"),
                    # anisotropic_transform,
                    SpatialPadd(keys=["image"], spatial_size=pad),
                    EnsureTyped(keys=["image"]),
                ]
            )

        self.log("\nLoading dataset...")
        inference_ds = Dataset(data=images_dict, transform=load_transforms)
        inference_loader = DataLoader(
            inference_ds, batch_size=1, num_workers=2
        )
        self.log("Done")
        return inference_loader

    def load_layer(self):
        self.log("\nLoading layer\n")
        data = np.squeeze(self.config.layer.data)

        volume = np.array(data, dtype=np.int16)

        volume_dims = len(volume.shape)
        if volume_dims != 3:
            raise ValueError(
                f"Data array is not 3-dimensional but {volume_dims}-dimensional,"
                f" please check for extra channel/batch dimensions"
            )

        volume = np.swapaxes(
            volume, 0, 2
        )  # for anisotropy to be monai-like, i.e. zyx # FIXME rotation not always correct

        dims_check = volume.shape

        # logger.debug(volume.shape)
        # logger.debug(volume.dtype)
        if self.config.sliding_window_config.is_enabled():
            load_transforms = Compose(
                [
                    ToTensor(),
                    # anisotropic_transform,
                    AddChannel(),
                    # SpatialPad(spatial_size=pad),
                    AddChannel(),
                    EnsureType(),
                ],
                map_items=False,
                log_stats=True,
            )
        else:
            self.log("Checking dimensions...")
            pad = utils.get_padding_dim(dims_check)
            load_transforms = Compose(
                [
                    ToTensor(),
                    # anisotropic_transform,
                    AddChannel(),
                    SpatialPad(spatial_size=pad),
                    AddChannel(),
                    EnsureType(),
                ],
                map_items=False,
                log_stats=True,
            )

        input_image = load_transforms(volume)
        self.log("Done")
        return input_image

    def model_output(
        self,
        inputs,
        model,
        post_process_transforms,
        post_process=True,
        aniso_transform=None,
    ):
        inputs = inputs.to("cpu")

        # def model_output(inputs):
        #     return post_process_transforms(
        #         self.config.model_info.get_model().get_output(model, inputs)
        #     )

        dataset_device = (
            "cpu" if self.config.keep_on_cpu else self.config.device
        )

        if self.config.sliding_window_config.is_enabled():
            window_size = self.config.sliding_window_config.window_size
            window_size = [window_size, window_size, window_size]
            window_overlap = self.config.sliding_window_config.window_overlap
        else:
            window_size = None
            window_overlap = 0
        try:
            # logger.debug(f"model : {model}")
            logger.debug(f"inputs shape : {inputs.shape}")
            logger.debug(f"inputs type : {inputs.dtype}")
            try:
                # outputs = model(inputs)
                inputs = utils.remap_image(inputs)

                def model_output_wrapper(inputs):
                    result = model(inputs)
                    return post_process_transforms(result)

                with torch.no_grad():
                    outputs = sliding_window_inference(
                        inputs,
                        roi_size=window_size,
                        sw_batch_size=1,  # TODO add param
                        predictor=model_output_wrapper,
                        sw_device=self.config.device,
                        device=dataset_device,
                        overlap=window_overlap,
                        mode="gaussian",
                        sigma_scale=0.01,
                        progress=True,
                    )
            except Exception as e:
                logger.exception(e)
                logger.debug("failed to run sliding window inference")
                self.raise_error(e, "Error during sliding window inference")
            logger.debug(f"Inference output shape: {outputs.shape}")
            self.log("Post-processing...")
            out = outputs.detach().cpu().numpy()
            if aniso_transform is not None:
                out = aniso_transform(out)
            if post_process:
                out = np.array(out).astype(np.float32)
                out = np.squeeze(out)
            return out
        except Exception as e:
            logger.exception(e)
            self.raise_error(e, "Error during sliding window inference")
            # sys.stdout = old_stdout
            # sys.stderr = old_stderr

    def create_inference_result(
        self,
        semantic_labels,
        instance_labels,
        crf_results=None,
        from_layer: bool = False,
        original=None,
        stats=None,
        i=0,
    ) -> InferenceResult:
        if not from_layer and original is None:
            raise ValueError(
                "If the image is not from a layer, an original should always be available"
            )

        if from_layer:
            if i != 0:
                raise ValueError(
                    "A layer's ID should always be 0 (default value)"
                )

            if semantic_labels is not None:
                semantic_labels = utils.correct_rotation(semantic_labels)
            if crf_results is not None:
                crf_results = utils.correct_rotation(crf_results)
            if instance_labels is not None:
                instance_labels = utils.correct_rotation(instance_labels)

        return InferenceResult(
            image_id=i + 1,
            original=original,
            instance_labels=instance_labels,
            crf_results=crf_results,
            stats=stats,
            result=semantic_labels,
            model_name=self.config.model_info.name,
        )

    def get_original_filename(self, i):
        return Path(self.config.images_filepaths[i]).stem

    def get_instance_result(self, semantic_labels, from_layer=False, i=-1):
        if not from_layer and i == -1:
            raise ValueError(
                "An ID should be provided when running from a file"
            )

        if self.config.post_process_config.instance.enabled:
            instance_labels = self.instance_seg(
                semantic_labels,
                i + 1,
            )
            data_dict = self.stats_csv(instance_labels)
        else:
            instance_labels = None
            data_dict = None
        return instance_labels, data_dict

    def save_image(
        self,
        image,
        from_layer=False,
        i=0,
        additional_info="",
    ):
        if not from_layer:
            original_filename = "_" + self.get_original_filename(i) + "_"
            filetype = self.config.filetype
        else:
            original_filename = "_"
            filetype = ".tif"

        time = utils.get_date_time()

        file_path = (
            self.config.results_path
            + "/"
            + f"{additional_info}"
            + f"Prediction_{i+1}"
            + original_filename
            + self.config.model_info.name
            + f"_{time}"
            + filetype
        )
        try:
            imwrite(file_path, image)
        except ValueError as e:
            self.raise_error(e, "Error during image saving")
        filename = Path(file_path).stem

        if from_layer:
            self.log(f"\nLayer prediction saved as : {filename}")
        else:
            self.log(f"\nFile n°{i+1} saved as : {filename}")

    def aniso_transform(self, image):
        if self.config.post_process_config.zoom.enabled:
            zoom = self.config.post_process_config.zoom.zoom_values
            anisotropic_transform = Zoom(
                zoom=zoom,
                keep_size=False,
                padding_mode="empty",
            )
            return anisotropic_transform(image[0])
        return image

    def instance_seg(
        self, semantic_labels, image_id=0, original_filename="layer"
    ):
        if image_id is not None:
            self.log(f"\nRunning instance segmentation for image n°{image_id}")

        method = self.config.post_process_config.instance.method
        instance_labels = method.run_method_on_channels(semantic_labels)
        self.log(f"DEBUG instance results shape : {instance_labels.shape}")

        filetype = (
            ".tif"
            if self.config.filetype == ""
            else "_" + self.config.filetype
        )

        instance_filepath = (
            self.config.results_path
            + "/"
            + f"Instance_seg_labels_{image_id}_"
            + original_filename
            + "_"
            + self.config.model_info.name
            + f"_{utils.get_date_time()}"
            + filetype
        )

        imwrite(instance_filepath, instance_labels)
        self.log(
            f"Instance segmentation results for image n°{image_id} have been saved as:"
        )
        self.log(Path(instance_filepath).name)
        return instance_labels

    def inference_on_folder(self, inf_data, i, model, post_process_transforms):
        self.log("-" * 10)
        self.log(f"Inference started on image n°{i + 1}...")

        inputs = inf_data["image"]

        out = self.model_output(
            inputs,
            model,
            post_process_transforms,
            aniso_transform=self.aniso_transform,
        )

        self.save_image(out, i=i)
        instance_labels, stats = self.get_instance_result(out, i=i)
        if self.config.use_crf:
            crf_in = inputs.detach().cpu().numpy()
            try:
                crf_results = self.run_crf(
                    crf_in,
                    out,
                    aniso_transform=self.aniso_transform,
                    image_id=i,
                )

            except ValueError as e:
                self.log(f"Error occurred during CRF : {e}")
                crf_results = None
        else:
            crf_results = None

        original = np.array(inf_data["image"]).astype(np.float32)

        self.log(f"Inference completed on image n°{i+1}")

        return self.create_inference_result(
            out,
            instance_labels,
            crf_results,
            from_layer=False,
            original=original,
            stats=stats,
            i=i,
        )

    def run_crf(self, image, labels, aniso_transform, image_id=0):
        try:
            if aniso_transform is not None:
                image = aniso_transform(image)
            crf_results = crf_with_config(
                image, labels, config=self.config.crf_config, log=self.log
            )
            self.save_image(
                crf_results,
                i=image_id,
                additional_info="CRF_",
                from_layer=True,
            )
            return crf_results
        except ValueError as e:
            self.log(f"Error occurred during CRF : {e}")
            return None

    def stats_csv(self, instance_labels):
        try:
            if self.config.compute_stats:
                if len(instance_labels.shape) == 4:
                    stats = [volume_stats(c) for c in instance_labels]
                else:
                    stats = [volume_stats(instance_labels)]
            else:
                stats = None
            return stats
        except ValueError as e:
            self.log(f"Error occurred during stats computing : {e}")
            return None

    def inference_on_layer(self, image, model, post_process_transforms):
        self.log("-" * 10)
        self.log("Inference started on layer...")

        image = image.type(torch.FloatTensor)
        image = utils.quantile_normalization(image)

        out = self.model_output(
            image,
            model,
            post_process_transforms,
            aniso_transform=self.aniso_transform,
        )

        self.save_image(out, from_layer=True)

        instance_labels, stats = self.get_instance_result(
            semantic_labels=out, from_layer=True
        )

        crf_results = (
            self.run_crf(image, out, aniso_transform=self.aniso_transform)
            if self.config.use_crf
            else None
        )

        return self.create_inference_result(
            semantic_labels=out,
            instance_labels=instance_labels,
            crf_results=crf_results,
            from_layer=True,
            stats=stats,
        )

    # @thread_worker(connect={"errored": self.raise_error})
    def inference(self):
        """
        Requires:
            * device: cuda or cpu device to use for torch

            * model_dict: the :py:attr:`~self.models_dict` dictionary to obtain the model name, class and instance

            * weights: the loaded weights from the model

            * images_filepaths: the paths to the images of the dataset

            * results_path: the path to save the results to

            * filetype: the file extension to use when saving,

            * transforms: a dict containing transforms to perform at various times.

            * use_window: use window inference with specific size or whole image

            * window_infer_size: size of window if use_window is True

            * keep_on_cpu: keep images on CPU or no

            * stats_csv: compute stats on cells and save them to a csv file

        Yields:
            dict: contains :
                * "image_id" : index of the returned image

                * "original" : original volume used for inference

                * "result" : inference result

        """
        sys = platform.system()
        logger.debug(f"OS is {sys}")
        if sys == "Darwin":
            torch.set_num_threads(1)  # required for threading on macOS ?
            self.log("Number of threads has been set to 1 for macOS")

        try:
            dims = self.config.model_info.model_input_size
            self.log(f"MODEL DIMS : {dims}")
            model_name = self.config.model_info.name
            model_class = self.config.model_info.get_model()
            self.log(f"Model name : {model_name}")

            weights_config = self.config.weights_config
            post_process_config = self.config.post_process_config
            if Path(weights_config.path).suffix == ".pt":
                self.log("Instantiating PyTorch jit model...")
                model = torch.jit.load(weights_config.path)
            # try:
            elif Path(weights_config.path).suffix == ".onnx":
                self.log("Instantiating ONNX model...")
                model = ONNXModelWrapper(weights_config.path)
            else:  # assume is .pth
                self.log("Instantiating model...")
                model = model_class(
                    input_img_size=[dims, dims, dims],
                    # device=self.config.device,
                    num_classes=self.config.model_info.num_classes,
                )
                # try:
                model = model.to(self.config.device)
                # except Exception as e:
                #     self.raise_error(e, "Issue loading model to device")
                # logger.debug(f"model : {model}")
                if model is None:
                    raise ValueError("Model is None")
                # try:
                self.log("\nLoading weights...")
                if weights_config.custom:
                    weights = weights_config.path
                else:
                    self.downloader.download_weights(
                        model_name,
                        model_class.weights_file,
                    )
                    weights = str(
                        PRETRAINED_WEIGHTS_DIR / Path(model_class.weights_file)
                    )

                model.load_state_dict(  # note that this is redefined in WNet_
                    torch.load(
                        weights,
                        map_location=self.config.device,
                    )
                )
                self.log("Done")
            # except Exception as e:
            #     self.raise_error(e, "Issue loading weights")
            # except Exception as e:
            #     self.raise_error(e, "Issue instantiating model")

            # if model_name == "SegResNet":
            #     model = model_class(
            #         input_image_size=[
            #             dims,
            #             dims,
            #             dims,
            #         ],
            #     )
            # elif model_name == "SwinUNetR":
            #     model = model_class(
            #         img_size=[dims, dims, dims],
            #         use_checkpoint=False,
            #     )
            # else:
            #     model = model_class.get_net()

            self.log_parameters()

            # load_transforms = Compose(
            #     [
            #         LoadImaged(keys=["image"]),
            #         # AddChanneld(keys=["image"]), #already done
            #         EnsureChannelFirstd(keys=["image"]),
            #         # Orientationd(keys=["image"], axcodes="PLI"),
            #         # anisotropic_transform,
            #         SpatialPadd(keys=["image"], spatial_size=pad),
            #         EnsureTyped(keys=["image"]),
            #     ]
            # )

            if not post_process_config.thresholding.enabled:
                post_process_transforms = EnsureType()
            else:
                t = post_process_config.thresholding.threshold_value
                post_process_transforms = Compose(
                    AsDiscrete(threshold=t), EnsureType()
                )

            is_folder = self.config.images_filepaths is not None
            is_layer = self.config.layer is not None

            if is_layer and is_folder:
                raise ValueError(
                    "Both a layer and a folder have been specified, please specify only one of the two. Aborting."
                )
            elif is_folder:
                inference_loader = self.load_folder()
                ##################
                ##################
                # DEBUG
                # from monai.utils import first
                #
                # check_data = first(inference_loader)
                # image = check_data[0][0]
                # logger.debug(image.shape)
                ##################
                ##################
            elif is_layer:
                input_image = self.load_layer()
            else:
                raise ValueError("No data has been provided. Aborting.")

            if model is None:
                raise ValueError("Model is None")

            model.eval()
            with torch.no_grad():
                ################################
                ################################
                ################################
                if is_folder:
                    for i, inf_data in enumerate(inference_loader):
                        yield self.inference_on_folder(
                            inf_data, i, model, post_process_transforms
                        )
                elif is_layer:
                    yield self.inference_on_layer(
                        input_image, model, post_process_transforms
                    )
            model.to("cpu")
            # self.quit()
        except Exception as e:
            logger.exception(e)
            self.raise_error(e, "Inference failed")
            self.quit()
        finally:
            self.quit()