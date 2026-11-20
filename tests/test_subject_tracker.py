from subject_tracker import CropPoint, compute_crop_dimensions, smooth_crop_path


def test_ratio_math_uses_the_largest_vertical_crop_inside_widescreen_video():
    assert compute_crop_dimensions(1920, 1080, "9:16") == (608, 1080)


def test_ratio_math_uses_the_largest_widescreen_crop_inside_vertical_video():
    assert compute_crop_dimensions(1080, 1920, "16:9") == (1080, 608)


def test_smoothing_limits_pan_speed_between_samples():
    path = smooth_crop_path([CropPoint(0, 0, 0), CropPoint(1, 1000, 0)], max_pan_speed=100, smoothing=1)
    assert path[-1].x == 100


def test_scene_cut_snaps_to_new_crop_position():
    path = smooth_crop_path([CropPoint(0, 0, 0), CropPoint(1, 1000, 0, scene_cut=True)], max_pan_speed=100)
    assert path[-1].x == 1000
