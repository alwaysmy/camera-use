"""
camera_native.py — 摄像头原生接口工具
======================================
多层获取设备信息: WMI → PowerShell → OpenCV(DShow) → USB VID/PID

用法:
  python camera_native.py                  # 全量综合报告
  python camera_native.py wmi              # 仅WMI查询
  python camera_native.py ps               # 仅PowerShell查询
  python camera_native.py opencv           # 仅OpenCV查询
  python camera_native.py json             # JSON格式输出
  python camera_native.py json -o out.json # JSON保存到文件
  python camera_native.py snapshot -i 1    # 指定摄像头拍照
"""
import ctypes
import ctypes.wintypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes, Structure, POINTER, byref
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════
# USB VID 数据库
# ═══════════════════════════════════════════════════════════════
KNOWN_USB_VID = {
    "04CA": "Lite-On Technology (HP Camera OEM)",
    "046D": "Logitech",
    "04F2": "Chicony Electronics",
    "05A9": "OmniVision",
    "0AC8": "Vimicro (Z-Star)",
    "0BDA": "Realtek",
    "1903": "Suyin",
    "045E": "Microsoft",
    "05E1": "Vimicro",
    "2BC5": "Sunplus IT",
    "05C8": "Foxconn",
    "06F4": "Vimicro",
    "041E": "Creative Technology",
    "03F0": "HP Inc.",
}


def parse_vid_pid(hw_id: str) -> dict:
    """从硬件ID中解析 USB VID / PID"""
    m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", hw_id)
    if not m:
        return {}
    vid, pid = m.group(1).upper(), m.group(2).upper()
    return {
        "vid": vid,
        "pid": pid,
        "vendor": KNOWN_USB_VID.get(vid, "Unknown"),
        "usb_id": f"VID_{vid}&PID_{pid}",
    }


# ═══════════════════════════════════════════════════════════════
# 第一层: WMI 查询 (最可靠的设备信息源)
# ═══════════════════════════════════════════════════════════════
def wmi_get_cameras() -> list[dict]:
    """WMI Win32_PnPEntity 枚举摄像头设备"""
    import wmi
    c = wmi.WMI()
    results = []
    for item in c.Win32_PnPEntity():
        name = item.Name or ""
        if "camera" not in name.lower() and "webcam" not in name.lower():
            continue
        hw_ids = list(item.HardwareID or ())
        usb_info = []
        for hw in hw_ids:
            parsed = parse_vid_pid(hw)
            if parsed:
                usb_info.append(parsed)

        results.append({
            "name": item.Name,
            "device_id": item.DeviceID,
            "status": item.Status,
            "pnp_class": item.PNPClass,
            "manufacturer": item.Manufacturer,
            "description": item.Description,
            "service": item.Service,
            "system_name": item.SystemName,
            "hardware_ids": hw_ids,
            "usb": usb_info[0] if usb_info else None,
        })
    return results


# ═══════════════════════════════════════════════════════════════
# 第二层: PowerShell Get-PnpDevice
# ═══════════════════════════════════════════════════════════════
def ps_get_cameras() -> list[dict]:
    """PowerShell 查询摄像头详细信息"""
    ps_script = r"""
$json = Get-PnpDevice -Class Camera -ErrorAction SilentlyContinue |
  Select-Object FriendlyName, InstanceId, Status, Class, Service, Manufacturer |
  ConvertTo-Json -Compress -Depth 3
if ($json) { $json } else { "[]" }
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        raw = r.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception as e:
        return [{"error": str(e)}]


# ═══════════════════════════════════════════════════════════════
# 第三层: OpenCV + DirectShow 后端
# ═══════════════════════════════════════════════════════════════
def opencv_get_cameras(max_idx: int = 8) -> list[dict]:
    """OpenCV 遍历索引，读取参数并测试可调性。

    注意：**索引不可靠**（随拔插/禁用变化），且这里只列得出 DirectShow 设备。
    要看完整设备表（含 IR / 虚拟摄像头）用 ``camera_core.list_devices()``。
    """
    import cv2

    results = []
    for i in range(max_idx):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if not cap.isOpened():
            continue
        ret, frame = cap.read()
        if not ret:
            cap.release()
            continue

        backend = str(cap.getBackendName()) if hasattr(cap, "getBackendName") else "unknown"
        info = {
            "index": i,
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": cap.get(cv2.CAP_PROP_FPS),
            "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
            "backend": backend,
            "params": {},
        }

        # 读取 + 测试可调性
        PROP_LIST = [
            "BRIGHTNESS", "CONTRAST", "SATURATION", "HUE",
            "GAIN", "EXPOSURE", "FOCUS", "ZOOM",
            "PAN", "TILT", "ROLL", "BACKLIGHT",
            "SHARPNESS", "WHITE_BALANCE_TEMPERATURE",
            "AUTO_EXPOSURE", "AUTO_WB",
        ]
        for pname in PROP_LIST:
            prop_id = getattr(cv2, f"CAP_PROP_{pname}", None)
            if prop_id is None:
                continue
            val = cap.get(prop_id)
            if val == -1:
                info["params"][pname] = {"value": None, "writable": False}
                continue
            # 测试写入
            test_val = val + 5 if val < 100 else val - 5
            cap.set(prop_id, test_val)
            after = cap.get(prop_id)
            writable = abs(after - val) > 0.01
            cap.set(prop_id, val)  # 恢复
            info["params"][pname] = {
                "value": round(val, 4),
                "writable": writable,
                "test_after": round(after, 4) if writable else None,
            }

        cap.release()
        results.append(info)
    return results


# ═══════════════════════════════════════════════════════════════
# 第四层: OpenCV 拍照
# ═══════════════════════════════════════════════════════════════
def opencv_capture(camera_index=0, save_dir: str = ".", warmup: int = 8) -> dict:
    """拍照并返回文件路径 + 帧信息。

    ``camera_index`` 可以是数字索引，也可以是名字子串/类型
    （"rgb" / "ir" / "hp"），后者走 camera_core 解析，避免索引错位。

    更推荐直接用 ``camera_core.CameraController``（带黑图哨兵和曝光控制）。
    """
    import cv2
    try:
        from camera_core import CameraController
        with CameraController(camera_index, warmup=warmup,
                              min_mean=None) as cam:
            frame = cam.grab(check=False)
            idx = cam.index
            name = cam.device.name
    except ImportError:            # 退回到原始实现
        frame, idx, name = None, camera_index, str(camera_index)
        cap = cv2.VideoCapture(int(camera_index), cv2.CAP_DSHOW)
        if not cap.isOpened():
            return {"error": f"无法打开摄像头 {camera_index}"}
        for _ in range(max(1, warmup)):
            ret, f = cap.read()
            if ret:
                frame = f
        cap.release()
        if frame is None:
            return {"error": f"摄像头 {camera_index} 读取帧失败"}

    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"snapshot_{idx}_{ts}.jpg"
    path = Path(save_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), frame)

    h, w = frame.shape[:2]
    return {
        "file": str(path.resolve()),
        "device": name,
        "width": w,
        "height": h,
        "channels": frame.shape[2] if len(frame.shape) > 2 else 1,
        "mean": round(float(frame.mean()), 1),
        "camera_index": idx,
    }


# ═══════════════════════════════════════════════════════════════
# 综合报告
# ═══════════════════════════════════════════════════════════════
def full_report() -> dict:
    """全量信息采集"""
    print("  [1/3] WMI...", file=sys.stderr, end=" ")
    wmi_data = wmi_get_cameras()
    print("OK", file=sys.stderr)

    print("  [2/3] PowerShell...", file=sys.stderr, end=" ")
    ps_data = ps_get_cameras()
    print("OK", file=sys.stderr)

    print("  [3/3] OpenCV (含参数可调性)...", file=sys.stderr, end=" ")
    cv_data = opencv_get_cameras()
    print("OK", file=sys.stderr)

    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wmi": wmi_data,
        "powershell": ps_data,
        "opencv": cv_data,
    }


def print_report(report: dict):
    """格式化打印综合报告"""
    print()
    print("=" * 64)
    print("  Camera Native Report")
    print(f"  {report['timestamp']}")
    print("=" * 64)

    # ── WMI ──
    print("\n[WMI] 设备信息")
    print("-" * 64)
    if not report["wmi"]:
        print("  (无)")
    for cam in report["wmi"]:
        print(f"  名称:     {cam['name']}")
        print(f"  设备ID:   {cam['device_id']}")
        print(f"  状态:     {cam['status']}")
        print(f"  制造商:   {cam['manufacturer']}")
        print(f"  驱动:     {cam['service']}")
        usb = cam.get("usb")
        if usb:
            print(f"  USB:      {usb['usb_id']}  ({usb['vendor']})")
        print()

    # ── PowerShell ──
    print("[PowerShell] PnP 详情")
    print("-" * 64)
    if not report["powershell"]:
        print("  (无)")
    for cam in report["powershell"]:
        if "error" in cam:
            print(f"  错误: {cam['error']}")
            continue
        for key in ["FriendlyName", "InstanceId", "Status", "Class", "Service", "Manufacturer"]:
            val = cam.get(key, "")
            if val:
                print(f"  {key:20s}: {val}")
        print()

    # ── OpenCV ──
    print("[OpenCV] DirectShow 摄像头参数")
    print("-" * 64)
    if not report["opencv"]:
        print("  (无)")
    for cam in report["opencv"]:
        writable_params = {k: v for k, v in cam.get("params", {}).items() if v.get("writable")}
        readonly_params = {k: v for k, v in cam.get("params", {}).items() if not v.get("writable") and v.get("value") is not None}
        unsupported = {k: v for k, v in cam.get("params", {}).items() if v.get("value") is None}

        print(f"  索引 {cam['index']}: {cam['width']}x{cam['height']}  FPS={cam['fps']:.1f}  后端={cam['backend']}")

        if writable_params:
            print(f"    可调参数:")
            for k, v in writable_params.items():
                print(f"      {k:35s} = {v['value']}")
        if readonly_params:
            print(f"    只读参数:")
            for k, v in readonly_params.items():
                print(f"      {k:35s} = {v['value']}")
        if unsupported:
            print(f"    不支持:")
            names = ", ".join(unsupported.keys())
            print(f"      {names}")
        print()


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="摄像头原生接口工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("command", nargs="?", default="full",
                        choices=["wmi", "ps", "opencv", "full", "json", "snapshot"],
                        help="查询方式 (默认: full)")
    parser.add_argument("-i", "--index", type=str, default="rgb",
                        help='设备选择符：类型(rgb/ir/virtual) | 索引(0) | 名字子串("hp")')
    parser.add_argument("-o", "--output", help="输出文件 (JSON)")
    parser.add_argument("-d", "--dir", default=".", help="拍照保存目录")
    parser.add_argument("--max-index", type=int, default=20, help="OpenCV扫描最大索引")
    args = parser.parse_args()

    if args.command == "wmi":
        print(json.dumps(wmi_get_cameras(), indent=2, ensure_ascii=False))

    elif args.command == "ps":
        print(json.dumps(ps_get_cameras(), indent=2, ensure_ascii=False))

    elif args.command == "opencv":
        print(json.dumps(opencv_get_cameras(args.max_index), indent=2, ensure_ascii=False))

    elif args.command == "snapshot":
        result = opencv_capture(args.index, args.dir)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command in ("full", "json"):
        report = full_report()
        if args.command == "json" or args.output:
            out = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(out, encoding="utf-8")
                print(f"已保存: {args.output}", file=sys.stderr)
            else:
                print(out)
        else:
            print_report(report)


if __name__ == "__main__":
    main()