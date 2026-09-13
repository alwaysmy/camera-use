"""camera_tool.py — 摄像头命令行工具（CLI）

设备的选取一律走 camera_core 的名称解析，**不要再用裸索引**：
``-i`` 接受 类型(rgb/ir/virtual) | 索引(0) | 名字子串("hp")，缺省自动挑 RGB 相机。

    python camera_tool.py detect                # 设备表（含 IR / 虚拟摄像头）
    python camera_tool.py capture -i rgb -o a.jpg
    python camera_tool.py auto-exposure auto    # 根治画面过暗
    python camera_tool.py ir -o ir.jpg          # 拍 IR 红外照片
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from camera_core import (
    CAP_API, DEFAULT_MIN_MEAN, BlackFrameError, CameraController, CameraError,
    describe_devices, list_devices, resolve_device,
)

try:
    import duvc_ctl as duvc
    HAS_DUVC = True
except ImportError:
    HAS_DUVC = False

# ── OpenCV 参数常量映射 ──────────────────────────────────────────────
PROP_MAP = {
    "FRAME_WIDTH":       cv2.CAP_PROP_FRAME_WIDTH,
    "FRAME_HEIGHT":      cv2.CAP_PROP_FRAME_HEIGHT,
    "FPS":               cv2.CAP_PROP_FPS,
    "BRIGHTNESS":        cv2.CAP_PROP_BRIGHTNESS,
    "CONTRAST":          cv2.CAP_PROP_CONTRAST,
    "SATURATION":        cv2.CAP_PROP_SATURATION,
    "HUE":               cv2.CAP_PROP_HUE,
    "GAIN":              cv2.CAP_PROP_GAIN,
    "EXPOSURE":          cv2.CAP_PROP_EXPOSURE,
    "AUTO_EXPOSURE":     cv2.CAP_PROP_AUTO_EXPOSURE,
    "AUTO_WB":           cv2.CAP_PROP_AUTO_WB,
    "FOCUS":             cv2.CAP_PROP_FOCUS,
    "ZOOM":              cv2.CAP_PROP_ZOOM,
    "PAN":               cv2.CAP_PROP_PAN,
    "TILT":              cv2.CAP_PROP_TILT,
    "ROLL":              cv2.CAP_PROP_ROLL,
    "BACKLIGHT":         cv2.CAP_PROP_BACKLIGHT,
    "SHARPNESS":         cv2.CAP_PROP_SHARPNESS,
    "TRIGGER":           cv2.CAP_PROP_TRIGGER,
    "TRIGGER_DELAY":     cv2.CAP_PROP_TRIGGER_DELAY,
    "WB_TEMPERATURE":    cv2.CAP_PROP_WB_TEMPERATURE,
    "RECTIFICATION":     cv2.CAP_PROP_RECTIFICATION,
    "TEMPERATURE":       cv2.CAP_PROP_TEMPERATURE,
}


class CameraManager:
    """通用摄像头管理器 — 检测 / 查询 / 拍照 / 录像 / 参数调节

    设备选取全部委托给 ``camera_core``（名称解析 + 曝光 + 黑图哨兵），
    本类只负责编排和格式化输出。
    """

    def __init__(self, save_dir: str = None, spec=None):
        self.save_dir = Path(save_dir or os.getcwd())
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.spec = spec                      # None = 自动挑 rgb
        self._devices = None
        self._cc = None

    # ── 设备表（懒加载 + 缓存）──────────────────────────────────
    @property
    def devices(self):
        if self._devices is None:
            self._devices = list_devices()
        return self._devices

    def refresh_devices(self):
        self._devices = list_devices(probe=True)
        return self._devices

    def clear_device_cache(self):
        self._devices = None

    # ── 控制器 ──────────────────────────────────────────────────
    def controller(self, index=None, warmup: int = 10,
                   min_mean=DEFAULT_MIN_MEAN) -> CameraController:
        """给某个选择符建一个 CameraController（不打开设备）。"""
        spec = index if index not in (None, "") else (self.spec or "rgb")
        return CameraController(spec, devices=self.devices, warmup=warmup, min_mean=min_mean)

    def close_duvc(self):
        if self._cc is not None:
            self._cc.close()
            self._cc = None

    # ── 打开底层 VideoCapture（统一解析 + 显式 DSHOW 后端）──────
    def _resolve(self, spec):
        s = spec if spec not in (None, "") else (self.spec or "rgb")
        return resolve_device(s, self.devices)

    def _open(self, spec):
        """返回 (cv2.VideoCapture, CameraDevice)；失败抛 CameraError。"""
        dev = self._resolve(spec)
        if dev.index is None:
            raise CameraError(
                f"{dev.name} 没有 DirectShow 索引，只能用 Media Foundation 打开："
                f" python camera_ir.py")
        cap = cv2.VideoCapture(dev.index, CAP_API)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"无法打开索引 {dev.index} ({dev.name})，可能被占用或被禁用")
        return cap, dev

    # ── 曝光（走 duvc-ctl，OpenCV 设不了 Auto 标志）──────────────
    def set_auto_exposure(self, camera_index=None) -> bool:
        """切到 Auto 曝光模式（解决 OpenCV 画面过暗的根治手段）。"""
        try:
            cc = self.controller(camera_index)
            cc.set_exposure(auto=True)
            self._cc = cc
            return True
        except (CameraError, Exception) as e:
            print(f"设置自动曝光失败: {e}", file=sys.stderr)
            return False

    def set_manual_exposure(self, value: int, camera_index=None) -> bool:
        try:
            cc = self.controller(camera_index)
            cc.set_exposure(int(value))
            self._cc = cc
            return True
        except Exception as e:
            print(f"设置手动曝光失败: {e}", file=sys.stderr)
            return False

    def get_exposure_value(self, camera_index=None):
        try:
            if self._cc is not None:
                return self._cc.get_exposure().get("value")
            cc = self.controller(camera_index)
            self._cc = cc
            return cc.get_exposure().get("value")
        except Exception:
            return None

    # ── 1. 设备发现 ──────────────────────────────────────────────
    def detect(self, max_index: int = 20, probe: bool = True) -> list[dict]:
        """设备表：DShow 索引 + 类型(rgb/ir/virtual) + 实测分辨率/亮度。

        和旧版不同：这里**同时列出只能走 Media Foundation 的 IR 相机**
        （它没有 DShow 索引，但设备确实存在）。
        """
        from camera_core import probe_index
        devs = list_devices(probe=False, max_index=max_index)
        self._devices = devs
        rows = []
        for d in devs:
            info = probe_index(d.index, api=CAP_API) if (probe and d.index is not None) else None
            if info:
                d.opens = True
                d.resolution = (info["width"], info["height"])
            rows.append({
                "index": d.index,
                "name": d.name,
                "kind": d.kind,
                "width": info["width"] if info else 0,
                "height": info["height"] if info else 0,
                "fps": 0.0,
                "backend": info["backend"] if info else "-",
                "mean": info["mean"] if info else None,
                "note": "" if d.index is not None else "只能用 Media Foundation 打开",
            })
        return rows

    # ── 2. Windows 硬件信息 (PowerShell) ─────────────────────────
    @staticmethod
    def get_hw_info() -> list[dict]:
        """通过 PowerShell 获取 Windows 摄像头硬件信息"""
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-PnpDevice -Class Camera -ErrorAction SilentlyContinue | "
                    "Select-Object FriendlyName, InstanceId, Status | "
                    "ConvertTo-Json -Compress"
                ],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
            )
            raw = result.stdout.strip()
            if not raw:
                return []
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            devices = []
            for d in data:
                devices.append({
                    "name": d.get("FriendlyName", ""),
                    "instance_id": d.get("InstanceId", ""),
                    "status": d.get("Status", ""),
                })
            return devices
        except Exception as e:
            return [{"error": str(e)}]

    # ── 3. 参数全量查询 ──────────────────────────────────────────
    def query_params(self, camera_index=None) -> dict:
        """查询指定摄像头的所有可读参数"""
        try:
            cap, dev = self._open(camera_index)
        except CameraError as e:
            return {"error": str(e)}

        result = {"index": dev.index, "name": dev.name, "kind": dev.kind, "params": {}}
        for name, prop_id in PROP_MAP.items():
            try:
                val = cap.get(prop_id)
                result["params"][name] = round(val, 4) if isinstance(val, float) else val
            except Exception:
                result["params"][name] = None
        cap.release()
        return result

    # ── 4. 参数可调性测试 ────────────────────────────────────────
    def test_params(self, camera_index=None) -> list[dict]:
        """逐个测试参数是否可写"""
        try:
            cap, dev = self._open(camera_index)
        except CameraError as e:
            return [{"error": str(e)}]

        results = []
        for name, prop_id in PROP_MAP.items():
            try:
                original = cap.get(prop_id)
                if original is None or original == -1:
                    results.append({"name": name, "readable": True, "writable": False, "value": None})
                    continue
                # 尝试写入不同值
                test_val = original + 5 if original < 100 else original - 5
                cap.set(prop_id, test_val)
                after = cap.get(prop_id)
                writable = abs(after - original) > 0.01
                # 恢复原值
                cap.set(prop_id, original)
                results.append({
                    "name": name,
                    "value": round(original, 4) if isinstance(original, float) else original,
                    "writable": writable,
                    "after_test": round(after, 4) if isinstance(after, float) else after,
                })
            except Exception as e:
                results.append({"name": name, "error": str(e)})
        cap.release()
        return results

    # ── 5. 拍照 ──────────────────────────────────────────────────
    def capture(self, camera_index=None, filename: str = None,
                warmup: int = 10, check: bool = True) -> str:
        """拍照并保存，返回文件路径。

        相比旧版：先预热 warmup 帧（等自动曝光爬坡），再过黑图哨兵 ——
        取错设备/曝光停在 Manual 负档时会**立刻报错**，而不是静默存一张黑图。
        """
        cc = self.controller(camera_index, warmup=warmup,
                             min_mean=DEFAULT_MIN_MEAN if check else None)
        with cc:
            name = cc.device.name
            idx = cc.device.index
            if filename is None:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"capture_{idx}_{ts}.jpg"
            path = self.save_dir / filename
            frame = cc.grab()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), frame):
            raise RuntimeError(f"写文件失败: {path}")
        mean = float(frame.mean())
        print(f"[{name}] idx={idx} {frame.shape[1]}x{frame.shape[0]} 亮度={mean:.1f}")
        return str(path)

    # ── 6. 录像 ──────────────────────────────────────────────────
    def record(
        self,
        camera_index=None,
        duration: float = 5.0,
        fps: float = 30.0,
        resolution: tuple = None,
        filename: str = None,
    ) -> tuple:
        """录制视频并保存，返回 (路径, 实际帧数, 耗时秒)"""
        cap, dev = self._open(camera_index)

        if resolution:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, resolution[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, resolution[1])

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if filename is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"record_{dev.index}_{ts}.avi"
        path = self.save_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)

        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        out = cv2.VideoWriter(str(path), fourcc, fps, (w, h))

        frames_to_collect = max(1, int(duration * fps))
        collected = 0
        t_start = time.time()
        try:
            while collected < frames_to_collect:
                ret, frame = cap.read()
                if not ret:
                    break
                out.write(frame)
                collected += 1
        finally:
            cap.release()
            out.release()
        return str(path), collected, time.time() - t_start

    # ── 7. 设置参数 ──────────────────────────────────────────────
    def set_param(self, camera_index, param_name: str, value: float) -> dict:
        """设置指定参数"""
        prop_id = PROP_MAP.get(param_name.upper())
        if prop_id is None:
            return {"error": f"未知参数: {param_name}", "available": list(PROP_MAP.keys())}

        try:
            cap, dev = self._open(camera_index)
        except CameraError as e:
            return {"error": str(e)}

        original = cap.get(prop_id)
        cap.set(prop_id, value)
        after = cap.get(prop_id)
        cap.release()
        return {
            "param": param_name.upper(),
            "index": dev.index,
            "original": original,
            "requested": value,
            "actual": after,
            "changed": abs(after - original) > 0.01,
        }

    # ── 8. 连续预览 ──────────────────────────────────────────────
    def preview(self, camera_index=None, width: int = 640, height: int = 480):
        """实时预览窗口，按 q 退出，按 s 拍照"""
        try:
            cap, dev = self._open(camera_index)
        except CameraError as e:
            print(e)
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        print(f"预览 {dev.name} (idx {dev.index}) — 按 q 退出 | 按 s 拍照")
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                cv2.imshow(f"Camera {dev.index} - {dev.name}", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord("s"):
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = self.save_dir / f"preview_{dev.index}_{ts}.jpg"
                    cv2.imwrite(str(path), frame)
                    print(f"已保存: {path}")
        finally:
            cap.release()
            cv2.destroyAllWindows()

    # ── 9. 格式化输出 ────────────────────────────────────────────
    @staticmethod
    def print_cameras(cameras: list[dict]):
        if not cameras:
            print("未检测到摄像头")
            return
        print(f"{'索引':>4}  {'类型':>7}  {'分辨率':>11}  {'亮度':>6}  {'设备名':<34} 备注")
        print("-" * 104)
        for c in cameras:
            idx = "-" if c.get("index") is None else str(c["index"])
            res = f"{c.get('width', 0)}x{c.get('height', 0)}" if c.get("width") else "-"
            mean = f"{c['mean']:.1f}" if c.get("mean") is not None else "-"
            print(f"{idx:>4}  {c.get('kind', '?'):>7}  {res:>11}  {mean:>6}  "
                  f"{c.get('name', '')[:34]:<34} {c.get('note', '')}")
        print("\n提示：索引会随拔插/禁用变化，建议用名字或类型选取 —— "
              "capture -i rgb / -i ir / -i nikon")

    @staticmethod
    def print_hw_info(devices: list[dict]):
        if not devices:
            print("未检测到硬件设备信息")
            return
        for d in devices:
            if "error" in d:
                print(f"  错误: {d['error']}")
                continue
            print(f"  {d['name']}")
            print(f"    ID: {d['instance_id']}")
            print(f"    状态: {d['status']}")

    @staticmethod
    def print_params(params: dict):
        if "error" in params:
            print(params["error"])
            return
        who = f"摄像头 {params.get('index')}"
        if params.get("name"):
            who += f" ({params['name']}, {params.get('kind', '?')})"
        print(f"{who} 参数:")
        print(f"{'参数名':>30}  {'当前值':>12}")
        print("-" * 46)
        for k, v in params["params"].items():
            print(f"{k:>30}  {str(v):>12}")

    @staticmethod
    def print_param_tests(results: list[dict]):
        if not results:
            return
        print(f"{'参数名':>30}  {'当前值':>10}  {'可写':>4}  {'测试后值':>10}")
        print("-" * 60)
        for r in results:
            if "error" in r:
                print(f"{r['name']:>30}  错误: {r['error']}")
                continue
            val = str(r.get("value", ""))
            wr = "✓" if r.get("writable") else "✗"
            after = str(r.get("after_test", ""))
            print(f"{r['name']:>30}  {val:>10}  {wr:>4}  {after:>10}")


# ── CLI 入口 ──────────────────────────────────────────────────────
def _add_index_arg(p, default=None):
    p.add_argument("-i", "--index", "--device", dest="index", default=default,
                   help='设备选择符：类型(rgb/ir/virtual) | 索引(0) | 名字子串("hp")；缺省自动挑 RGB')


def main():
    parser = argparse.ArgumentParser(
        description="摄像头通用工具 — 设备选取走名称解析，不要再依赖索引",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python camera_tool.py detect\n"
               "  python camera_tool.py capture -i rgb -o a.jpg\n"
               "  python camera_tool.py auto-exposure auto\n"
               "  python camera_tool.py ir -o ir.jpg\n")
    sub = parser.add_subparsers(dest="cmd", help="子命令")

    # detect
    p_det = sub.add_parser("detect", help="设备表（含 IR / 虚拟摄像头）")
    p_det.add_argument("--no-probe", action="store_true", help="不逐个开流探测（快）")
    # hwinfo
    sub.add_parser("hwinfo", help="Windows PnP 硬件信息")
    # params
    _add_index_arg(sub.add_parser("params", help="查询参数"))
    # test
    _add_index_arg(sub.add_parser("test", help="测试参数可调性"))
    # capture
    p_cap = sub.add_parser("capture", help="拍照（预热 + 黑图哨兵）")
    _add_index_arg(p_cap)
    p_cap.add_argument("-o", "--output", type=str, default=None)
    p_cap.add_argument("-d", "--dir", type=str, default=None)
    p_cap.add_argument("--warmup", type=int, default=10, help="预热帧数（默认 10）")
    p_cap.add_argument("--no-check", action="store_true", help="关闭黑图哨兵")
    # record
    p_rec = sub.add_parser("record", help="录像")
    _add_index_arg(p_rec)
    p_rec.add_argument("-t", "--time", type=float, default=5.0, help="时长(秒)")
    p_rec.add_argument("--fps", type=float, default=30.0)
    p_rec.add_argument("-o", "--output", type=str, default=None)
    p_rec.add_argument("-d", "--dir", type=str, default=None)
    # set
    p_set = sub.add_parser("set", help="设置参数")
    _add_index_arg(p_set)
    p_set.add_argument("param", type=str, help="参数名")
    p_set.add_argument("value", type=float, help="目标值")
    # preview
    p_prev = sub.add_parser("preview", help="实时预览")
    _add_index_arg(p_prev)
    p_prev.add_argument("--width", type=int, default=640)
    p_prev.add_argument("--height", type=int, default=480)
    # info (综合)
    _add_index_arg(sub.add_parser("info", help="综合信息(检测+硬件+参数+可调性)"))
    # list-params
    sub.add_parser("list-params", help="列出所有支持的参数名")
    # auto-exposure
    p_ae = sub.add_parser("auto-exposure", help="设置自动/手动曝光（根治画面过暗）")
    _add_index_arg(p_ae)
    p_ae.add_argument("mode", choices=["auto", "manual"], help="曝光模式")
    p_ae.add_argument("-v", "--value", type=int, default=-2, help="手动模式下的曝光值")
    p_ae.add_argument("--range", action="store_true", help="顺带打印曝光可调范围")
    # ir
    p_ir = sub.add_parser("ir", help="拍 IR 红外照片（走 Media Foundation）")
    p_ir.add_argument("-o", "--output", type=str, default=None)
    p_ir.add_argument("-n", "--name", type=str, default=None, help="IR 设备名子串")
    p_ir.add_argument("--info", action="store_true", help="打印 IR 流信息")
    p_ir.add_argument("--raw", action="store_true", help="不做对比度拉伸")
    p_ir.add_argument("--pair", action="store_true", help="同时保存补光灯亮/灭两帧")
    p_ir.add_argument("-d", "--dir", type=str, default=None)

    args = parser.parse_args()
    mgr = CameraManager(spec=args.index if getattr(args, "index", None) else None)

    if args.cmd == "detect":
        cams = mgr.detect(probe=not args.no_probe)
        mgr.print_cameras(cams)

    elif args.cmd == "hwinfo":
        devs = mgr.get_hw_info()
        mgr.print_hw_info(devs)

    elif args.cmd == "params":
        p = mgr.query_params(args.index)
        mgr.print_params(p)

    elif args.cmd == "test":
        r = mgr.test_params(args.index)
        mgr.print_param_tests(r)

    elif args.cmd == "capture":
        if args.dir:
            mgr.save_dir = Path(args.dir)
        try:
            path = mgr.capture(args.index, args.output, warmup=args.warmup,
                               check=not args.no_check)
            print(f"已保存: {path}")
        except BlackFrameError as e:
            print("拍照被黑图哨兵拦下：\n" + str(e))
            return 2
        except CameraError as e:
            print(f"拍照失败: {e}")
            return 1

    elif args.cmd == "record":
        if args.dir:
            mgr.save_dir = Path(args.dir)
        path, frames, elapsed = mgr.record(
            args.index, args.time, args.fps, filename=args.output
        )
        print(f"已保存: {path}  帧数: {frames}  耗时: {elapsed:.2f}s")

    elif args.cmd == "set":
        r = mgr.set_param(args.index, args.param, args.value)
        if "error" in r:
            print(r["error"])
            if "available" in r:
                print("可用参数:", ", ".join(r["available"]))
        else:
            print(f"{r['param']}: {r['original']} -> {r['actual']} ({'已生效' if r['changed'] else '未变化'})")

    elif args.cmd == "preview":
        mgr.preview(args.index, args.width, args.height)

    elif args.cmd == "info":
        print("=" * 50)
        print("1. 可用摄像头")
        print("=" * 50)
        cams = mgr.detect()
        mgr.print_cameras(cams)
        print()
        print("=" * 50)
        print("2. Windows 硬件信息")
        print("=" * 50)
        devs = mgr.get_hw_info()
        mgr.print_hw_info(devs)
        print()
        print("=" * 50)
        print(f"3. 摄像头 {args.index} 参数")
        print("=" * 50)
        p = mgr.query_params(args.index)
        mgr.print_params(p)
        print()
        print("=" * 50)
        print(f"4. 摄像头 {args.index} 参数可调性")
        print("=" * 50)
        r = mgr.test_params(args.index)
        mgr.print_param_tests(r)

    elif args.cmd == "list-params":
        print("支持的参数名:")
        for name in sorted(PROP_MAP.keys()):
            print(f"  {name}")

    elif args.cmd == "auto-exposure":
        if not HAS_DUVC:
            print("错误: duvc-ctl未安装，请运行: pip install duvc-ctl")
        else:
            if args.mode == "auto":
                ok = mgr.set_auto_exposure(args.index)
                print(f"自动曝光: {'成功' if ok else '失败'}")
            else:
                ok = mgr.set_manual_exposure(args.value, args.index)
                print(f"手动曝光({args.value}): {'成功' if ok else '失败'}")
            val = mgr.get_exposure_value(args.index)
            print(f"当前曝光值: {val}")
            if args.range:
                try:
                    cc = mgr.controller(args.index)
                    print(f"曝光可调范围: {cc.exposure_range()}")
                    print(f"设备: {cc.device}")
                except CameraError as e:
                    print(f"读范围失败: {e}")

    elif args.cmd == "ir":
        try:
            from camera_ir import IrCamera
        except ImportError as e:
            print(f"IR 功能需要 camera_ir.py（同目录）: {e}")
            return 1
        out_dir = Path(args.dir) if args.dir else mgr.save_dir
        try:
            with IrCamera(name=args.name, warmup=20) as ir:
                print(f"IR 设备: {ir.device_name}")
                if args.info:
                    print(json.dumps(ir.info(), indent=2, ensure_ascii=False))
                if args.output:
                    # 给了带目录的路径就用它；只给文件名才落到 save_dir
                    outp = Path(args.output)
                    path = outp if (outp.is_absolute() or outp.parent != Path(".")) \
                        else out_dir / outp.name
                    print("已保存:", ir.snapshot(str(path), stretch=not args.raw))
                if args.pair:
                    lit, dark, delta = ir.read_pair()
                    base = out_dir / ("ir_pair_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
                    for tag, img in (("lit", lit), ("dark", dark)):
                        cv2.imwrite(str(base) + f"_{tag}.jpg",
                                    cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX))
                    print(f"补光灯亮/灭两帧已保存: {base}_lit.jpg / _dark.jpg  亮度差={delta:.1f}")
                if not (args.info or args.output or args.pair):
                    print("（加 -o 保存照片 / --info 看流信息 / --pair 取补光灯亮灭对帧）")
        except Exception as e:
            print(f"IR 失败: {e}")
            return 1

    else:
        parser.print_help()


if __name__ == "__main__":
    main()