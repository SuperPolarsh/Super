"""Local subject detection and crop-path planning for reframed clips."""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Iterable

import cv2
import numpy as np


@dataclass(frozen=True)
class CropPoint:
    time: float
    x: float
    y: float
    scene_cut: bool = False


@dataclass(frozen=True)
class TrackingPlan:
    source_width: int
    source_height: int
    crop_width: int
    crop_height: int
    points: list[CropPoint]
    detected_subjects: int


def compute_crop_dimensions(width: int, height: int, aspect_ratio: str) -> tuple[int, int]:
    """Return the largest in-frame crop that matches ``aspect_ratio``."""
    if width <= 0 or height <= 0:
        raise ValueError("Video dimensions must be positive.")
    if aspect_ratio == "Original":
        return width, height
    target = {"9:16": 9 / 16, "16:9": 16 / 9}.get(aspect_ratio)
    if target is None:
        raise ValueError(f"Unsupported aspect ratio: {aspect_ratio}")
    source_ratio = width / height
    if source_ratio > target:
        return int(round(height * target)), height
    return width, int(round(width / target))


def choose_primary_subject(boxes: Iterable[tuple[int, int, int, int]], width: int, height: int):
    """Prefer a large face near centre when no active-speaker signal exists."""
    boxes = list(boxes)
    if not boxes:
        return None
    frame_diagonal = hypot(width, height)

    def score(box):
        x, y, box_width, box_height = box
        area = box_width * box_height
        distance = hypot((x + box_width / 2) - width / 2, (y + box_height / 2) - height / 2)
        return area - (distance / frame_diagonal) * area * 0.35

    return max(boxes, key=score)


def subject_crop_target(subject, source_width, source_height, crop_width, crop_height) -> tuple[float, float]:
    """Place a detected face in the upper third, leaving comfortable headroom."""
    if subject is None:
        return (source_width - crop_width) / 2, (source_height - crop_height) / 2
    x, y, width, height = subject
    face_center_x = x + width / 2
    face_center_y = y + height / 2
    # Keeping eyes/forehead around 28% down the crop gives natural headroom.
    target_x = face_center_x - crop_width / 2
    target_y = face_center_y - crop_height * 0.28
    return clamp_crop(target_x, target_y, source_width, source_height, crop_width, crop_height)


def clamp_crop(x, y, source_width, source_height, crop_width, crop_height) -> tuple[float, float]:
    return (
        max(0, min(float(x), source_width - crop_width)),
        max(0, min(float(y), source_height - crop_height)),
    )


def smooth_crop_path(points: Iterable[CropPoint], max_pan_speed=420.0, smoothing=0.45) -> list[CropPoint]:
    """Limit camera speed while allowing a clean snap after a scene cut."""
    raw_points = sorted(points, key=lambda point: point.time)
    if not raw_points:
        return []
    output = [raw_points[0]]
    for point in raw_points[1:]:
        previous = output[-1]
        elapsed = max(0.001, point.time - previous.time)
        if point.scene_cut:
            output.append(point)
            continue
        desired_x = previous.x + (point.x - previous.x) * smoothing
        desired_y = previous.y + (point.y - previous.y) * smoothing
        max_distance = max_pan_speed * elapsed
        delta_x, delta_y = desired_x - previous.x, desired_y - previous.y
        distance = hypot(delta_x, delta_y)
        if distance > max_distance:
            scale = max_distance / distance
            desired_x, desired_y = previous.x + delta_x * scale, previous.y + delta_y * scale
        output.append(CropPoint(point.time, desired_x, desired_y, False))
    return output


def _histogram(frame):
    small = cv2.resize(frame, (96, 54))
    return cv2.calcHist([small], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])


def plan_subject_tracking(video_path, start_time, end_time, sample_interval=0.35, progress_callback=None) -> TrackingPlan:
    """Create a local face/upper-body-guided crop plan for a clip interval."""
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError("OpenCV could not open the input video for local tracking.")
    try:
        source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        crop_width, crop_height = compute_crop_dimensions(source_width, source_height, "9:16")
        face_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        body_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_upperbody.xml")
        if face_detector.empty():
            face_detector = None
        if body_detector.empty():
            body_detector = None
        if face_detector is None and body_detector is None:
            if progress_callback:
                progress_callback("Face detector unavailable; using a centered vertical crop.")
            x, y = subject_crop_target(None, source_width, source_height, crop_width, crop_height)
            return TrackingPlan(source_width, source_height, crop_width, crop_height,
                                [CropPoint(0, x, y)], 0)

        duration = max(0.01, end_time - start_time)
        samples = max(1, int(duration / sample_interval) + 1)
        previous_histogram = None
        points = []
        detected_subjects = 0
        for index in range(samples):
            relative_time = min(duration, index * sample_interval)
            capture.set(cv2.CAP_PROP_POS_MSEC, (start_time + relative_time) * 1000)
            success, frame = capture.read()
            if not success:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = (face_detector.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(36, 36))
                     if face_detector is not None else ())
            subject = choose_primary_subject(faces, source_width, source_height)
            if subject is None and body_detector is not None:
                bodies = body_detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(70, 70))
                subject = choose_primary_subject(bodies, source_width, source_height)
            if subject is not None:
                detected_subjects += 1
            current_histogram = _histogram(frame)
            scene_cut = previous_histogram is not None and cv2.compareHist(
                previous_histogram, current_histogram, cv2.HISTCMP_BHATTACHARYYA
            ) > 0.45
            previous_histogram = current_histogram
            x, y = subject_crop_target(subject, source_width, source_height, crop_width, crop_height)
            points.append(CropPoint(relative_time, x, y, scene_cut))
            if progress_callback:
                progress_callback(f"Tracking subject: {index + 1}/{samples} frames sampled")

        if not points:
            x, y = subject_crop_target(None, source_width, source_height, crop_width, crop_height)
            points = [CropPoint(0, x, y)]
        return TrackingPlan(source_width, source_height, crop_width, crop_height,
                            smooth_crop_path(points), detected_subjects)
    finally:
        capture.release()


def crop_expression(points: Iterable[CropPoint], axis: str) -> str:
    """Create an FFmpeg crop x/y expression with linear motion between samples."""
    points = list(points)
    if not points:
        return "0"
    values = [round(getattr(point, axis), 3) for point in points]
    expression = str(values[-1])
    for index in range(len(points) - 2, -1, -1):
        current, following = points[index], points[index + 1]
        current_value, following_value = values[index], values[index + 1]
        interval = max(0.001, following.time - current.time)
        linear = f"{current_value}+({following_value}-{current_value})*(t-{current.time:.3f})/{interval:.3f}"
        expression = f"if(lt(t,{following.time:.3f}),{linear},{expression})"
    return expression
