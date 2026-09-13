"""camera_core / camera_ir 的回归测试。

默认只跑**纯逻辑**部分（不需要摄像头，秒级完成）：

    python -m unittest discover -s tests -v

带硬件（会真的开摄像头，较慢）：

    python tests/test_camera_core.py --hw
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera_core import (  # noqa: E402
    BlackFrameError, CameraDevice, CameraOpenError, _link_candidates, _match_score,
    check_brightness, classify, describe_devices, frame_brightness, resolve_device,
    resolve_index, stretch_frame,
)

HW = "--hw" in sys.argv          # 是否连硬件一起测


def synthetic_devices() -> list[CameraDevice]:
    """模拟一台典型笔记本：RGB(MI_00, idx0) + 虚拟摄像头(idx1) + IR(MI_02, 无索引)。"""
    return [
        CameraDevice(name="HP Wide Vision FHD Camera", index=0, kind="rgb",
                     instance_id=r"USB\VID_04CA&PID_7086&MI_00\9&24b3143e&0&0000",
                     status="OK", mi="MI_00", opens=True, resolution=(640, 480)),
        CameraDevice(name="Nikon Webcam Utility", index=1, kind="virtual", opens=True,
                     resolution=(1024, 768)),
        CameraDevice(name="HP IR Camera", index=None, kind="ir",
                     instance_id=r"USB\VID_04CA&PID_7086&MI_02\9&24b3143e&0&0002",
                     status="OK", mi="MI_02",
                     links={"sensor_camera": r"\\?\USB#...&MI_02#...#{24e552d7}\GLOBAL"}),
    ]


class TestClassify(unittest.TestCase):
    def test_ir_by_name(self):
        self.assertEqual(classify("HP IR Camera"), "ir")
        self.assertEqual(classify("Integrated Infrared Camera"), "ir")

    def test_ir_by_mi02_when_pnp(self):
        self.assertEqual(classify("HP Camera Module", r"USB\VID_x&PID_y&MI_02\z"), "ir")

    def test_rgb(self):
        self.assertEqual(classify("HP Wide Vision FHD Camera", r"USB\VID_x&PID_y&MI_00\z"), "rgb")

    def test_virtual_keywords(self):
        for n in ("Nikon Webcam Utility", "OBS Virtual Camera", "DroidCam Source 3",
                  "ManyCam Virtual Webcam", "Snap Camera"):
            self.assertEqual(classify(n), "virtual", n)

    def test_dshow_only_is_virtual(self):
        # DShow 里有、PnP 里没有 → 软件虚拟设备
        self.assertEqual(classify("Some Softcam", "", in_pnp=False), "virtual")


class TestMatchScore(unittest.TestCase):
    def test_identical(self):
        self.assertAlmostEqual(_match_score("HP Camera", "HP Camera"), 1.0)

    def test_substring(self):
        self.assertGreaterEqual(_match_score("HP Wide Vision FHD Camera", "HP Wide Vision"), 0.9)

    def test_unrelated(self):
        self.assertLess(_match_score("Nikon Webcam Utility", "HP Wide Vision FHD Camera"), 0.5)

    def test_punctuation_insensitive(self):
        self.assertAlmostEqual(_match_score("HP-123 (Camera)", "hp 123 camera"), 1.0)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.devs = synthetic_devices()

    def test_by_kind_rgb(self):
        self.assertEqual(resolve_device("rgb", self.devs).index, 0)

    def test_auto_defaults_to_rgb(self):
        self.assertEqual(resolve_device(None, self.devs).index, 0)
        self.assertEqual(resolve_device("auto", self.devs).index, 0)

    def test_by_kind_ir_returns_indexless(self):
        dev = resolve_device("ir", self.devs)
        self.assertIsNone(dev.index)
        self.assertTrue(dev.is_ir)

    def test_by_index_int_and_str(self):
        self.assertEqual(resolve_device(1, self.devs).name, "Nikon Webcam Utility")
        self.assertEqual(resolve_device("1", self.devs).name, "Nikon Webcam Utility")

    def test_by_name_substring(self):
        self.assertEqual(resolve_device("nikon", self.devs).index, 1)
        self.assertEqual(resolve_device("HP WIDE", self.devs).index, 0)

    def test_name_matching_skips_indexless_by_default(self):
        # "hp" 既匹配 FHD 也匹配 IR，但默认要能开流的那台
        self.assertEqual(resolve_index("hp", self.devs), 0)

    def test_ir_cannot_resolve_to_index(self):
        with self.assertRaises(CameraOpenError):
            resolve_index("ir", self.devs)

    def test_unknown_name_raises(self):
        from camera_core import DeviceNotFound
        with self.assertRaises(DeviceNotFound):
            resolve_device("sony", self.devs)

    def test_missing_kind_raises(self):
        from camera_core import DeviceNotFound
        with self.assertRaises(DeviceNotFound):
            resolve_device("ir", [d for d in self.devs if d.kind != "ir"])

    def test_unknown_index_still_usable(self):
        dev = resolve_device(7, self.devs)
        self.assertEqual(dev.index, 7)


class TestSentinel(unittest.TestCase):
    def test_bright_frame_passes(self):
        frame = np.full((10, 10, 3), 128, np.uint8)
        self.assertAlmostEqual(check_brightness(frame, 40), 128.0)

    def test_dark_frame_raises(self):
        frame = np.full((10, 10, 3), 8, np.uint8)
        with self.assertRaises(BlackFrameError) as cm:
            check_brightness(frame, 40, self._dev())
        msg = str(cm.exception)
        self.assertIn("mean=8.0", msg)
        self.assertIn("曝光", msg)

    def test_virtual_device_gets_extra_hint(self):
        dev = CameraDevice(name="Nikon Webcam Utility", index=1, kind="virtual")
        with self.assertRaises(BlackFrameError) as cm:
            check_brightness(np.zeros((4, 4, 3), np.uint8), 40, dev)
        self.assertIn("虚拟摄像头", str(cm.exception))

    def test_threshold_boundary(self):
        frame = np.full((4, 4), 40, np.uint8)
        check_brightness(frame, 40)                 # 等于阈值应通过
        with self.assertRaises(BlackFrameError):
            check_brightness(frame, 40.1)

    @staticmethod
    def _dev():
        return CameraDevice(name="HP IR Camera", index=None, kind="ir")


class TestHelpers(unittest.TestCase):
    def test_stretch_frame(self):
        frame = np.array([[10, 20], [30, 40]], np.uint8)
        out = stretch_frame(frame)
        self.assertEqual(out.min(), 0)
        self.assertEqual(out.max(), 255)

    def test_stretch_on_color(self):
        frame = np.zeros((4, 4, 3), np.uint8)
        frame[0, 0] = 200
        self.assertEqual(stretch_frame(frame).shape, (4, 4))

    def test_brightness(self):
        self.assertAlmostEqual(frame_brightness(np.full((2, 2), 50, np.uint8)), 50.0)

    def test_link_candidates_format(self):
        links = _link_candidates(r"USB\VID_04CA&PID_7086&MI_02\9&24b3143e&0&0002")
        self.assertIn("video_camera", links)
        self.assertIn("sensor_camera", links)
        for link in links.values():
            self.assertTrue(link.startswith("\\\\?\\USB#"), link)
            self.assertIn("&MI_02#", link)
            self.assertTrue(link.endswith("\\GLOBAL"))

    def test_device_dict_and_str(self):
        d = CameraDevice(name="HP Wide Vision FHD Camera", index=0, kind="rgb",
                         resolution=(640, 480))
        self.assertEqual(d.to_dict()["resolution"], "640x480")
        self.assertIn("[rgb]", str(d))
        self.assertIn("idx 0", str(d))

    def test_describe_empty(self):
        self.assertIn("没有找到", describe_devices([]))


@unittest.skipUnless(HW, "需要真实摄像头（加 --hw 启用）")
class TestHardware(unittest.TestCase):
    def test_enumerate(self):
        from camera_core import list_devices
        devs = list_devices()
        self.assertTrue(devs, "至少应该枚举到一台设备")

    def test_open_rgb_and_shot(self):
        from camera_core import CameraController
        with CameraController("rgb", warmup=12) as cam:
            frame = cam.grab()
            self.assertEqual(frame.ndim, 3)
            self.assertGreater(float(frame.mean()), 40.0)


if __name__ == "__main__":
    unittest.main(argv=[a for a in sys.argv if a != "--hw"] or None, verbosity=2)
