# detector_ctbd.py
import math
import numpy as np
import cv2
import torch
from PIL import Image
from typing import Tuple, List, Callable, Any, Optional
import os
import logging

logging.getLogger("transformers.configuration_utils").setLevel(logging.WARNING)
logging.getLogger("transformers.modeling_utils").setLevel(logging.WARNING)
logging.getLogger("transformers.image_processing_base").setLevel(logging.WARNING)
logging.getLogger("transformers.image_processing_utils").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub.file_download").setLevel(logging.WARNING)


# --- Imports from your application structure (or dummy classes for standalone use) ---
try:
    from .base import (
        register_textdetectors, TextDetectorBase, DEFAULT_DEVICE,
        DEVICE_SELECTOR, ProjImgTrans
    )
    from utils.textblock import TextBlock
    from utils.registry import Registry
    from utils.logger import logger as LOGGER
except ImportError:
    print("Warning: Using dummy base classes/utils for CTBD.")
    LOGGER = logging.getLogger("dummy_ctbd_logger")
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[%(levelname)-5s] %(name)s:%(funcName)s:%(lineno)d - %(message)s")
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.DEBUG)

    class BaseModule:
        def __init__(self, **params):
            self.params = params
            self.logger = logging.getLogger(self.__class__.__name__)
            if not self.logger.handlers:
                _handler = logging.StreamHandler()
                _formatter = logging.Formatter("[%(levelname)-5s] %(name)s:%(funcName)s:%(lineno)d - %(message)s")
                _handler.setFormatter(_formatter)
                self.logger.addHandler(_handler)
            self.logger.setLevel(logging.DEBUG if os.environ.get("CTBD_DEBUG") else logging.INFO)
            self.name = self.__class__.__name__

        def get_param_value(self, key): param = self.params.get(key); return param.get("value") if isinstance(param, dict) else param
        def updateParam(self, key: str, content):
            if key in self.params:
                if isinstance(self.params[key], dict) and "value" in self.params[key]: self.params[key]["value"] = content
                else: self.params[key] = content
        def all_model_loaded(self): return hasattr(self, 'model') and self.model is not None and hasattr(self, 'processor') and self.processor is not None
        def load_model(self): self._load_model()
        def _load_model(self): pass

    TextDetectorBase = BaseModule
    DEFAULT_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    def DEVICE_SELECTOR(): return {"type": "selector", "options": ["cpu", "cuda"], "value": DEFAULT_DEVICE}
    class ProjImgTrans: pass
    class TextBlock:
        def __init__(self, xyxy=(0,0,0,0), text_bbox=(0,0,0,0), bubble_xyxy=None, text_class='text_free', lines=None, **kwargs):
            self.xyxy = xyxy if xyxy is not None else text_bbox
            self.text_bbox = text_bbox if text_bbox is not None else xyxy
            self.bubble_xyxy, self.text_class, self.lines = bubble_xyxy, text_class, lines or []
            self.det_model = kwargs.get('det_model', None)
    class Registry:
        def __init__(self, n): self.name = n; self.module_dict = {}
        def register_module(self, name, module=None):
            def decorator(cls): return self.module_dict.setdefault(name, cls) or cls
            return decorator if module is None else self.module_dict.setdefault(name, module) or module
    TEXTDETECTORS = Registry("textdetectors")
    register_textdetectors = TEXTDETECTORS.register_module

# --- Hugging Face Transformers ---
try:
    from transformers import RTDetrV2ForObjectDetection, RTDetrImageProcessor
    from huggingface_hub import constants
except ImportError:
    LOGGER.error("ERROR: 'transformers' or 'huggingface_hub' not found. Install dependencies for CTBD.")
    class RTDetrV2ForObjectDetection: pass
    class RTDetrImageProcessor: pass
    class constants: HF_HUB_DISABLE_SYMLINKS_WARNINGS = None

# === Helper Functions ===
def calculate_iou(rect1, rect2) -> float:
    x1, y1, x2, y2 = rect1; px1, py1, px2, py2 = rect2
    xi1, yi1, xi2, yi2 = max(x1, px1), max(y1, py1), min(x2, px2), min(y2, py2)
    inter_area = max(0, xi2 - xi1) * max(0, yi2 - yi1)
    r1_area, r2_area = (x2 - x1) * (y2 - y1), (px2 - px1) * (py2 - py1)
    union_area = r1_area + r2_area - inter_area
    return inter_area / union_area if union_area > 0 else 0.0

def do_rectangles_overlap(rect1, rect2, iou_threshold: float = 0.2) -> bool: return calculate_iou(rect1, rect2) >= iou_threshold
def does_rectangle_fit(bigger_rect, smaller_rect) -> bool:
    x1, y1, x2, y2 = bigger_rect; px1, py1, px2, py2 = smaller_rect
    return x1 <= px1 and y1 <= py1 and x2 >= px2 and y2 >= py2

def filter_bounding_boxes(bboxes, width_tolerance=5, height_tolerance=5):
    if bboxes is None or len(bboxes) == 0: return np.array([])
    return np.array([b for b in bboxes if (b[2] - b[0]) > width_tolerance and (b[3] - b[1]) > height_tolerance])

def adjust_text_line_coordinates(line, x_off, y_off, image_shape):
    if line is None or len(line) != 4: return None
    h, w = image_shape[:2]; x1, y1, x2, y2 = line
    return (int(np.clip(x1 - x_off, 0, w)), int(np.clip(y1 - y_off, 0, h)),
            int(np.clip(x2 + x_off, 0, w)), int(np.clip(y2 + y_off, 0, h)))

def detect_content_in_bbox(image_crop):
    if image_crop is None or image_crop.size == 0: return []
    gray = cv2.cvtColor(image_crop, cv2.COLOR_BGR2GRAY); bboxes = []
    h_crop, w_crop = image_crop.shape[:2]; min_area = 10
    for thresh_type in [cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV]:
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, thresh_type, 11, 2)
        num_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        for i in range(1, num_labels):
            if stats[i, cv2.CC_STAT_AREA] > min_area:
                x, y = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
                bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
                if x > 0 and y > 0 and x + bw < w_crop and y + bh < h_crop:
                    bboxes.append((x, y, x + bw, y + bh))
    return bboxes

def get_inpaint_bboxes(text_bbox_abs, full_image, logger_instance=None):
    coords = adjust_text_line_coordinates(text_bbox_abs, 0, 10, full_image.shape)
    if coords is None: return []
    x1_crop, y1_crop, x2_crop, y2_crop = coords
    if y1_crop >= y2_crop or x1_crop >= x2_crop: return []
    crop_img = full_image[y1_crop:y2_crop, x1_crop:x2_crop]
    content_bboxes_relative = detect_content_in_bbox(crop_img)
    return [(x1_crop + lx1, y1_crop + ly1, x1_crop + lx2, y1_crop + ly2)
            for lx1, ly1, lx2, ly2 in content_bboxes_relative]

def create_unified_mask(input_mask: np.ndarray, method: str = 'hull') -> np.ndarray:
    if not isinstance(input_mask, np.ndarray) or input_mask.ndim != 2:
        return np.zeros_like(input_mask)
    contours, _ = cv2.findContours(input_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours: return np.zeros_like(input_mask)
    
    all_points = np.concatenate(contours, axis=0)
    unified_mask = np.zeros_like(input_mask)

    if method == 'rectangle':
        x, y, w, h = cv2.boundingRect(all_points)
        cv2.rectangle(unified_mask, (x, y), (x + w, y + h), 255, -1)
    elif method == 'hull':
        hull = cv2.convexHull(all_points)
        cv2.drawContours(unified_mask, [hull], 0, 255, -1)
    else:
        return input_mask
    return unified_mask

# === Image Slicer Class ===
class ImageSlicer:
    def __init__(self, logger, **kwargs):
        self.logger = logger
        try:
            self.height_to_width_ratio_threshold = float(kwargs.get('slice_threshold_ratio', 3.5))
            self.target_slice_ratio = float(kwargs.get('slice_target_ratio', 3.0))
            self.overlap_height_ratio = float(kwargs.get('slice_overlap_ratio', 0.2))
            self.min_slice_height_ratio = float(kwargs.get('slice_min_height_ratio', 0.7))
            self.merge_iou_threshold = float(kwargs.get('slice_merge_iou', 0.2))
            self.duplicate_iou_threshold = float(kwargs.get('slice_duplicate_iou', 0.4))
            self.merge_y_distance_threshold_ratio = float(kwargs.get('slice_merge_y_dist', 0.1))
            self.containment_threshold = float(kwargs.get('slice_containment_thresh', 0.85))
        except ValueError as e:
            self.logger.error(f"Invalid parameter value for ImageSlicer: {e}")
            self._set_default_slicer_params()
    def _set_default_slicer_params(self):
        self.height_to_width_ratio_threshold, self.target_slice_ratio = 3.5, 3.0
        self.overlap_height_ratio, self.min_slice_height_ratio = 0.2, 0.7
        self.merge_iou_threshold, self.duplicate_iou_threshold = 0.2, 0.4
        self.merge_y_distance_threshold_ratio, self.containment_threshold = 0.1, 0.85
        self.logger.warning("ImageSlicer falling back to default parameters.")
    def should_slice(self, image: np.ndarray) -> bool: h, w = image.shape[:2]; return w > 0 and (h / w) > self.height_to_width_ratio_threshold
    def calculate_slice_params(self, image_shape: tuple) -> tuple[int, int, int, int]:
        h, w = image_shape[:2]
        slice_w = w; slice_h = max(1, int(slice_w * self.target_slice_ratio)) if w > 0 else h
        eff_h = max(1, int(slice_h * (1 - self.overlap_height_ratio)))
        num_s = math.ceil(h / eff_h) if eff_h > 0 else 1
        if num_s > 1 and slice_h > 0 and (h - (num_s - 1) * eff_h) / slice_h < self.min_slice_height_ratio: num_s -= 1
        return slice_w, slice_h, eff_h, max(1, num_s)
    def get_slice(self, image: np.ndarray, idx: int, eff_h: int, slice_h: int) -> tuple[np.ndarray, int, int]:
        h, w = image.shape[:2]; start_y = idx * eff_h
        _, _, _, num_s = self.calculate_slice_params(image.shape)
        end_y = h if idx == num_s - 1 else min(start_y + slice_h, h)
        start_y = min(start_y, h - 1); end_y = max(start_y + 1, end_y)
        if start_y >= end_y: return np.array([]), start_y, end_y
        return image[start_y:end_y, 0:w].copy(), start_y, end_y
    def adjust_box_coordinates(self, boxes: np.ndarray, start_y: int) -> np.ndarray:
        if boxes is None or boxes.size == 0: return np.array([])
        adj = boxes.copy(); adj[:, [1, 3]] += start_y; return adj
    def box_contained(self, b1, b2) -> tuple[bool, float, int]:
        a1 = max(0, b1[2]-b1[0])*max(0, b1[3]-b1[1]); a2 = max(0, b2[2]-b2[0])*max(0, b2[3]-b2[1])
        if a1 <= 0 or a2 <= 0: return False, 0, 0
        xi1, yi1, xi2, yi2 = max(b1[0], b2[0]), max(b1[1], b2[1]), min(b1[2], b2[2]), min(b1[3], b2[3])
        ia = max(0, xi2-xi1)*max(0, yi2-yi1); sa = min(a1, a2)
        ratio = ia/sa if sa > 0 else 0.0; is_contained = ratio >= self.containment_threshold
        which = 1 if a1 >= a2 else 2 if is_contained else 0
        return is_contained, ratio, which
    def merge_overlapping_boxes(self, boxes: np.ndarray, img_h: int) -> np.ndarray:
        if boxes is None or boxes.size < 2: return boxes if boxes is not None else np.array([])
        bl = sorted(boxes.tolist(), key=lambda b: b[1])
        merge_y_dist_pixels = self.merge_y_distance_threshold_ratio * img_h; i = 0
        while i < len(bl):
            j = i + 1; merged_current = False
            while j < len(bl):
                b1, b2 = bl[i], bl[j]
                if b2[1] > b1[3] + merge_y_dist_pixels * 2: break
                iou = calculate_iou(b1, b2); is_cont, _, which_contains = self.box_contained(b1, b2)
                if is_cont:
                    if which_contains == 1: bl.pop(j)
                    else: bl[i] = b2; bl.pop(j); merged_current = True; break
                    continue
                if iou >= self.duplicate_iou_threshold:
                    a1, a2 = (b1[2]-b1[0])*(b1[3]-b1[1]), (b2[2]-b2[0])*(b2[3]-b2[1])
                    if a2 > a1: bl[i] = b2
                    bl.pop(j); merged_current = True; break
                y_dist = min(abs(b1[1]-b2[3]), abs(b1[3]-b2[1])); x_overlap = max(0, min(b1[2], b2[2])-max(b1[0], b2[0]))
                w1, w2 = (b1[2]-b1[0]), (b2[2]-b2[0]); min_w = min(w1, w2) if min(w1, w2) > 0 else 1.0
                x_overlap_ratio = x_overlap/min_w; a1, a2 = max(0, w1*(b1[3]-b1[1])), max(0, w2*(b2[3]-b2[1]))
                max_a = max(a1, a2); size_ratio = min(a1, a2)/max_a if max_a > 0 else 0.0
                if (y_dist < merge_y_dist_pixels and x_overlap_ratio > self.merge_iou_threshold and size_ratio > 0.3):
                    merged_box = [min(b1[0], b2[0]), min(b1[1], b2[1]), max(b1[2], b2[2]), max(b1[3], b2[3])]
                    if max_a == 0 or (merged_box[2]-merged_box[0]) * (merged_box[3]-merged_box[1]) <= 3*max_a:
                        bl[i] = merged_box; bl.pop(j); merged_current = True; break
                j += 1
            if not merged_current: i += 1
        return np.array(bl) if bl else np.array([])
    def process_slices_for_detection(self, image: np.ndarray, detect_func: Callable) -> tuple[np.ndarray, np.ndarray]:
        if not self.should_slice(image): b, t = detect_func(image); return np.array(b if b is not None else []), np.array(t if t is not None else [])
        _, slice_h, eff_h, num_s = self.calculate_slice_params(image.shape); all_b, all_t = [], []
        for i in range(num_s):
            slice_img, start_y, _ = self.get_slice(image, i, eff_h, slice_h)
            if slice_img.size == 0: continue
            b_s, t_s = detect_func(slice_img)
            if b_s.size > 0: all_b.append(self.adjust_box_coordinates(b_s, start_y))
            if t_s.size > 0: all_t.append(self.adjust_box_coordinates(t_s, start_y))
        cb = np.vstack(all_b) if all_b else np.array([]); ct = np.vstack(all_t) if all_t else np.array([])
        return self.merge_overlapping_boxes(cb, image.shape[0]), self.merge_overlapping_boxes(ct, image.shape[0])

# === RT-DETR-V2 Detector Module ===
@register_textdetectors("rtdetr_v2")
class RTDetrV2TextDetector(TextDetectorBase):
    repo_name = "ogkalu/comic-text-and-bubble-detector"
    HF_CACHE_DIR = os.path.abspath(os.path.join("data", "models", "CTBD"))
    params = {
        "confidence_threshold": {"value": "0.3", "description": "Minimum detection score (0.0-1.0)."},
        "suppress_bubble_on_fit": {"value": True, "type": "checkbox", "description": "If a text box fits perfectly inside a detected bubble, do not treat the bubble as a separate object."},
        "inpaint_mask_dilate": {"value": "4", "description": "Dilation kernel size (px) for the inpaint mask. Merges text fragments."},
        "mask_unification_method": {"value": "hull", "type": "selector", "options": ["none", "rectangle", "hull"], "description": "Method to unify fragments into one mask: 'none', 'bounding rectangle', or 'convex hull'."},
        "device": DEVICE_SELECTOR(),
        "description": f"RT-DETR-V2 (HF {repo_name}) for text/bubble detection.",
        "slice_threshold_ratio": {"value": "3.5", "description": "H/W ratio to trigger image slicing."},
        "slice_target_ratio": {"value": "3.0", "description": "Target H/W ratio for slices."},
        "slice_overlap_ratio": {"value": "0.2", "description": "Slice overlap ratio (0.0-1.0)."},
        "slice_min_height_ratio": {"value": "0.7", "description": "Minimum height ratio for the last slice."},
        "slice_merge_iou": {"value": "0.2", "description": "IoU threshold for merging text lines."},
        "slice_duplicate_iou": {"value": "0.4", "description": "IoU threshold for removing duplicate boxes."},
        "slice_merge_y_dist": {"value": "0.1", "description": "Max relative Y-distance for merging text lines."},
        "slice_containment_thresh": {"value": "0.85", "description": "Containment threshold for merging boxes."},
    }
    _load_model_keys = {"model", "processor", "image_slicer"}

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self.model: Optional[RTDetrV2ForObjectDetection] = None
        self.processor: Optional[RTDetrImageProcessor] = None
        self.image_slicer: Optional[ImageSlicer] = None
        try:
            os.makedirs(self.HF_CACHE_DIR, exist_ok=True)
            if hasattr(constants, 'HF_HUB_DISABLE_SYMLINKS_WARNINGS'):
                os.environ[constants.HF_HUB_DISABLE_SYMLINKS_WARNINGS] = "1"
        except Exception as e:
            self.logger.error(f"Failed to create cache directory {self.HF_CACHE_DIR}: {e}")

    @property
    def device(self): selected = self.get_param_value("device"); return "cpu" if selected == "cuda" and not torch.cuda.is_available() else selected
    @property
    def confidence_threshold(self):
        try: return float(self.get_param_value("confidence_threshold") or 0.3)
        except (ValueError, TypeError): return 0.3
    @property
    def suppress_bubble_on_fit(self) -> bool:
        return bool(self.get_param_value("suppress_bubble_on_fit"))
    @property
    def inpaint_mask_dilate(self) -> int:
        try: return int(self.get_param_value("inpaint_mask_dilate") or 0)
        except (ValueError, TypeError): self.logger.warning("Invalid value for 'inpaint_mask_dilate', defaulting to 0."); return 0
    @property
    def mask_unification_method(self) -> str: return self.get_param_value("mask_unification_method") or "none"

    def _initialize_slicer(self):
        slicer_params = {k: (self.get_param_value(k) or dv.get("value"))
                         for k, dv in self.params.items() if k.startswith("slice_")}
        self.image_slicer = ImageSlicer(self.logger, **slicer_params)

    def _load_model(self):
        self.logger.info(f"Loading RT-DETR-V2 model '{self.repo_name}' to device '{self.device}'.")
        try:
            self.processor = RTDetrImageProcessor.from_pretrained(self.repo_name, cache_dir=self.HF_CACHE_DIR)
            self.model = RTDetrV2ForObjectDetection.from_pretrained(self.repo_name, cache_dir=self.HF_CACHE_DIR)
            self.model.to(self.device); self.model.eval()
            self._initialize_slicer()
            self.logger.info("RT-DETR-V2 Model and processor loaded successfully.")
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except Exception as e:
            self.logger.error(f"Failed to load RT-DETR-V2 model '{self.repo_name}'. Error: {e}", exc_info=True)
            self.model, self.processor, self.image_slicer = None, None, None

    def _detect_single_slice(self, slice_img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.processor is None or self.model is None: return np.array([]), np.array([])
        try:
            pil_image = Image.fromarray(cv2.cvtColor(slice_img, cv2.COLOR_BGR2RGB))
            inputs = self.processor(images=pil_image, return_tensors="pt").to(self.device)
            with torch.no_grad(): outputs = self.model(**inputs)
            target_sizes = torch.tensor([pil_image.size[::-1]], device=self.device)
            results = self.processor.post_process_object_detection(outputs=outputs, target_sizes=target_sizes, threshold=self.confidence_threshold)[0]
            b_boxes, t_boxes = [], []
            for box, lbl in zip(results["boxes"].cpu().numpy(), results["labels"].cpu().numpy()):
                if lbl == 0: b_boxes.append(list(map(int, box)))
                elif lbl in [1, 2]: t_boxes.append(list(map(int, box)))
            return np.array(b_boxes), np.array(t_boxes)
        except Exception as e:
            self.logger.error(f"Error in _detect_single_slice: {e}", exc_info=True)
            return np.array([]), np.array([])

    def _create_text_blocks_and_mask(self, img: np.ndarray, txt_boxes: np.ndarray, bub_boxes: np.ndarray) -> Tuple[List[TextBlock], np.ndarray]:
        txt_boxes = filter_bounding_boxes(txt_boxes)
        bub_boxes = filter_bounding_boxes(bub_boxes)
        
        blocks = []
        final_inpaint_mask = np.zeros(img.shape[:2], dtype=np.uint8)

        if txt_boxes.size == 0:
            return blocks, final_inpaint_mask
        
        bub_list = bub_boxes.tolist() if bub_boxes.size > 0 else []

        for tb_rect in txt_boxes:
            local_block_mask = np.zeros(img.shape[:2], dtype=np.uint8)
            
            inpaint_regions = get_inpaint_bboxes(tb_rect.tolist(), img, self.logger)
            for r_x1, r_y1, r_x2, r_y2 in inpaint_regions:
                cv2.rectangle(local_block_mask, (r_x1, r_y1), (r_x2, r_y2), 255, -1)

            dilate_kernel_size = self.inpaint_mask_dilate
            if dilate_kernel_size > 0:
                kernel = np.ones((dilate_kernel_size, dilate_kernel_size), np.uint8)
                local_block_mask = cv2.dilate(local_block_mask, kernel, iterations=1)
            
            unification_method = self.mask_unification_method
            if unification_method != 'none':
                local_block_mask = create_unified_mask(local_block_mask, method=unification_method)
            
            final_inpaint_mask = cv2.bitwise_or(final_inpaint_mask, local_block_mask)
            
            x1, y1, x2, y2 = tb_rect
            current_tb_rect_list = tb_rect.tolist()
            best_bubble, fit_type = None, None

            for bb in bub_list:
                if does_rectangle_fit(bb, current_tb_rect_list):
                    best_bubble, fit_type = bb, "fit"
                    break 
                elif do_rectangles_overlap(bb, current_tb_rect_list):
                    if fit_type != "fit":
                        best_bubble, fit_type = bb, "overlap"
            
            bubble_coords_to_store = best_bubble
            text_class = "text_bubble" if best_bubble else "text_free"
            
            if self.suppress_bubble_on_fit and fit_type == "fit":
                self.logger.debug(f"Text box {current_tb_rect_list} fits perfectly in bubble {best_bubble}. Suppressing bubble geometry.")
                bubble_coords_to_store = None
            
            block_to_add = TextBlock(
                xyxy=current_tb_rect_list, 
                text_bbox=current_tb_rect_list,
                bubble_xyxy=bubble_coords_to_store,
                text_class=text_class, 
                lines=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]], 
                det_model=self.name
            )
            blocks.append(block_to_add)
                
        self.logger.debug(f"Processed {len(blocks)} text blocks.")
        return blocks, final_inpaint_mask

    def _detect(self, img: np.ndarray, proj: Optional[ProjImgTrans] = None) -> Tuple[np.ndarray, List[TextBlock]]:
        if not self.all_model_loaded():
            self.logger.warning("RT-DETR-V2 detector not initialized. Loading model...")
            self.load_model()
            if not self.all_model_loaded():
                self.logger.error("Failed to initialize RT-DETR-V2 detector model.")
                return np.zeros(img.shape[:2], dtype=np.uint8), []
        
        h, w = img.shape[:2]
        self.logger.info(f"Starting RT-DETR-V2 detection on {w}x{h} image...")
        bub_b, txt_b = self.image_slicer.process_slices_for_detection(img, self._detect_single_slice)
        
        blk_list, generated_mask = self._create_text_blocks_and_mask(img, txt_b, bub_b)
        self.logger.info(f"RT-DETR-V2 detection complete: Found {len(blk_list)} text blocks.")
        return generated_mask, blk_list

    def updateParam(self, param_key: str, param_content):
        super().updateParam(param_key, param_content)
        if param_key == "device":
            if self.model is not None and self.model.device.type != self.device:
                try: self.logger.info(f"Moving RT-DETR-V2 model to device: {self.device}"); self.model.to(self.device)
                except Exception as e: self.logger.error(f"Error moving RT-DETR-V2 model: {e}")
        elif param_key in ["confidence_threshold", "inpaint_mask_dilate", "mask_unification_method", "suppress_bubble_on_fit"]:
            self.logger.debug(f"RT-DETR-V2 parameter '{param_key}' updated.")
        elif param_key.startswith("slice_") and self.image_slicer:
            self.logger.debug("RT-DETR-V2 slicer parameters changed, re-initializing.")
            self._initialize_slicer()