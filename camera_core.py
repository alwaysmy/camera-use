"""camera_core.py — 摄像头统一控制门面（唯一真相来源）
================================================================
解决三件事：

1. **不要再依赖硬编码索引** —— OpenCV/DirectShow 的索引来自驱动枚举顺序，
   会随拔插、禁用/启用、驱动重载而变化。本模块用「PnP 友好名 ↔ DShow 枚举名
   按序遍历」建立名称到索引的映射，一律按名字解析。
2. **曝光模式** —— OpenCV 只会用 IAMCameraControl::Set(..., Manual)，
   导致图像暗。走 duvc-ctl（DirectShow COM）正确设置 Auto/Manual。
3. **黑图哨兵** —— 取到帧后校验亮度，把「静默出黑图」变成立刻报错。

典型用法::

    from camera_core import CameraController, list_devices

    with CameraController("rgb") as cam:          # 按类型/名字/索引解析
        cam.set_exposure(auto=True)
        frame = cam.grab(warmup=15)               # 预热 + 亮度校验
        cam.snapshot("photo.jpg")

命令行自检::

    python camera_core.py            # 设备表
    python camera_core.py -i rgb -o shot.jpg
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import winreg
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import cv2
import numpy as np

__all__ = [
    "CameraDevice", "CameraController",
    "CameraError", "DeviceNotFound", "BlackFrameError", "CameraOpenError",
    "list_devices", "resolve_device", "resolve_index", "dshow_names",
    "probe_index", "frame_brightness", "describe_devices",
]

CAP_API = cv2.CAP_DSHOW          # MSMF 在多数 opencv-python 构建里不可用，显式指定 DSHOW
DEFAULT_MIN_MEAN = 40.0          # 黑图哨兵阈值（0-255 均值）

# 已知虚拟摄像头关键字（不在 PnP 相机列表里、或明显是软件虚拟设备）
VIRTUAL_KEYWORDS = (
    "webcam utility", "obs virtual", "virtual camera", "manycam", "snap camera",
    "xsplit", "e2esoft", "droidcam", "iriun", "nvidia broadcast", "unity video capture",
    "mmhmm", "vive mars", "vmix", "splitcam", "youcam",
)

# 设备接口类别 GUID
IFACE_VIDEO_CAMERA = "{e5323777-f976-4f5b-9b55-b94699c46e44}"
IFACE_SENSOR_CAMERA = "{24e552d7-6523-47f7-a647-d3465bf1f5ca}"
IFACE_CAPTURE = "{65e8773d-8f56-11d0-a3b9-00a0c9223196}"


# ═══════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════
class CameraError(RuntimeError):
    """本模块所有错误的基类。"""


class DeviceNotFound(CameraError):
    """无法把选择符解析成一台设备。"""


class CameraOpenError(CameraError):
    """设备解析到了，但打不开。"""


class BlackFrameError(CameraError):
    """取到的帧过暗 —— 大概率取错了设备 / 镜头被遮挡 / 隐私开关打开。"""


# ═══════════════════════════════════════════════════════════════
# 设备模型
# ═══════════════════════════════════════════════════════════════
@dataclass
class CameraDevice:
    """一台摄像头（或虚拟摄像头）。"""

    name: str
    index: Optional[int] = None          # OpenCV / DirectShow 索引；None = 无法被 DShow 打开
    kind: str = "unknown"                # rgb | ir | virtual | unknown
    instance_id: str = ""                # PnP 实例 ID，如 USB\VID_04CA&PID_7086&MI_02\...
    status: str = ""
    mi: str = ""                         # MI_00 / MI_02
    links: dict = field(default_factory=dict)   # {video_camera|sensor_camera|capture: 符号链接}
    resolution: Optional[tuple] = None
    opens: bool = False

    # ---- 便捷属性 ----
    @property
    def is_ir(self) -> bool:
        return self.kind == "ir"

    @property
    def is_virtual(self) -> bool:
        return self.kind == "virtual"

    @property
    def dshow_capable(self) -> bool:
        return self.index is not None

    def link(self, category: str) -> Optional[str]:
        return self.links.get(category)

    @property
    def symbolic_link(self) -> Optional[str]:
        """优先给 Media Foundation 用的符号链接（IR 相机只能这样打开）。"""
        return self.links.get("video_camera") or self.links.get("sensor_camera") or self.links.get("capture")

    def to_dict(self) -> dict:
        d = {
            "name": self.name, "index": self.index, "kind": self.kind,
            "instance_id": self.instance_id, "status": self.status, "mi": self.mi,
            "opens": self.opens,
        }
        if self.resolution:
            d["resolution"] = "%dx%d" % self.resolution
        if self.links:
            d["links"] = {k: v for k, v in self.links.items()}
        return d

    def __str__(self) -> str:
        idx = "idx %s" % self.index if self.index is not None else "无 DShow 索引"
        res = "%dx%d" % self.resolution if self.resolution else "?"
        return "%-32s [%s] %s %s%s" % (
            self.name[:32], self.kind, idx, res,
            "  <- 需 Media Foundation" if self.index is None and self.links else "",
        )


# ═══════════════════════════════════════════════════════════════
# 第一层：PnP 设备表（设备名 + 实例 ID，最权威）
# ═══════════════════════════════════════════════════════════════
def _run_ps(script: str, timeout: int = 20) -> str:
    """跑一段 PowerShell，隐藏窗口（GUI 程序里不会闪黑框）。"""
    kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    exe = "powershell"
    try:
        r = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, **kwargs,
        )
    except FileNotFoundError:
        r = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, **kwargs,
        )
    return (r.stdout or "").strip()


def pnp_cameras() -> list[dict]:
    """Get-PnpDevice 枚举相机类设备（含 IR 相机，它不在 DShow 里但设备管理器里有）。"""
    script = (
        "Get-PnpDevice -Class Camera -PresentOnly -ErrorAction SilentlyContinue | "
        "Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress"
    )
    raw = _run_ps(script)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        name = (d.get("FriendlyName") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "instance_id": (d.get("InstanceId") or "").strip(),
            "status": (d.get("Status") or "").strip(),
        })
    return out


# ═══════════════════════════════════════════════════════════════
# 第二层：DShow / OpenCV 枚举顺序（名字 ↔ 索引）
# ═══════════════════════════════════════════════════════════════
def dshow_names() -> list[str]:
    """DirectShow 视频输入设备的枚举名，**顺序即 OpenCV 索引**。

    用 duvc-ctl 拿（它和 OpenCV 都走 ICreateDevEnum / CLSID_VideoInputDeviceCategory）。
    """
    try:
        import duvc_ctl as duvc
    except ImportError:
        return []
    try:
        with _quiet():                      # duvc 会往 stdout 打 "Cleaning up KsPropertySets..."
            return list(duvc.list_cameras())
    except Exception:
        return []


class _quiet:
    """屏蔽底层库往 stdout 打的噪声（duvc-ctl 的 KsPropertySets 提示）。"""

    def __enter__(self):
        import io, contextlib
        self._buf, self._ctx = io.StringIO(), contextlib.redirect_stdout(io.StringIO())
        self._ctx.__enter__()
        return self._buf

    def __exit__(self, *exc):
        self._ctx.__exit__(*exc)
        return False


# ═══════════════════════════════════════════════════════════════
# 第三层：设备接口符号链接（Media Foundation 打开 IR 相机的钥匙）
# ═══════════════════════════════════════════════════════════════
def _link_candidates(instance_id: str) -> dict[str, str]:
    """由 PnP 实例 ID 推导各类别的符号链接。"""
    base = "\\\\?\\" + instance_id.replace("\\", "#")
    return {
        "video_camera": base + "#" + IFACE_VIDEO_CAMERA + "\\GLOBAL",
        "sensor_camera": base + "#" + IFACE_SENSOR_CAMERA + "\\GLOBAL",
        "capture": base + "#" + IFACE_CAPTURE + "\\GLOBAL",
    }


def _registry_link_exists(instance_id: str, guid: str) -> bool:
    """在 HKLM\\SYSTEM\\CurrentControlSet\\Control\\DeviceClasses 里核对接口是否真的注册。

    注册表里的子键名就是设备接口路径，只是开头的 ``\\\\?\\`` 被转义成了 ``##?#``。
    """
    sub = "##?#" + instance_id.replace("\\", "#") + "#" + guid
    path = r"SYSTEM\CurrentControlSet\Control\DeviceClasses" + "\\" + guid + "\\" + sub
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path):
            return True
    except OSError:
        return False


def device_links(instance_id: str, verify: bool = True) -> dict[str, str]:
    """返回该设备真实存在的符号链接表（按类别）。"""
    if not instance_id:
        return {}
    cands = _link_candidates(instance_id)
    if not verify:
        return cands
    guids = {
        "video_camera": IFACE_VIDEO_CAMERA,
        "sensor_camera": IFACE_SENSOR_CAMERA,
        "capture": IFACE_CAPTURE,
    }
    return {k: v for k, v in cands.items() if _registry_link_exists(instance_id, guids[k])}


# ═══════════════════════════════════════════════════════════════
# 分类与匹配
# ═══════════════════════════════════════════════════════════════
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match_score(a: str, b: str) -> float:
    """两个设备名的相似度 0~1（用于把 PnP 名对上 DShow 名）。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    return 0.4 * jac + (0.4 if ta & tb else 0.0)


def classify(name: str, instance_id: str = "", in_pnp: bool = True) -> str:
    """rgb / ir / virtual。"""
    low = (name or "").lower()
    if any(k in low for k in VIRTUAL_KEYWORDS):
        return "virtual"
    if not in_pnp:
        return "virtual"          # DShow 有、PnP 没有 —— 软件虚拟设备
    if "ir camera" in low or re.search(r"\bir\b", low) or "infrared" in low:
        return "ir"
    if "MI_02" in instance_id.upper():
        return "ir"               # HP 笔记本惯例：MI_00 = RGB(FHD)，MI_02 = IR
    return "rgb"


# ═══════════════════════════════════════════════════════════════
# 设备表
# ═══════════════════════════════════════════════════════════════
def probe_index(index: int, warmup: int = 5, api: int = CAP_API) -> Optional[dict]:
    """打开某个索引读几帧，返回 {width,height,mean,backend}；打不开返回 None。"""
    cap = cv2.VideoCapture(index, api)
    if not cap.isOpened():
        cap.release()
        return None
    try:
        frame = None
        for _ in range(max(1, warmup)):
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
        if frame is None:
            return None
        return {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "mean": round(float(frame.mean()), 1),
            "backend": cap.getBackendName() if hasattr(cap, "getBackendName") else "?",
        }
    finally:
        cap.release()


def list_devices(probe: bool = False, max_index: int = 8) -> list[CameraDevice]:
    """汇总 PnP + DShow + 设备接口，得到完整设备表。

    :param probe: 为 True 时逐个索引开流，填充分辨率/亮度（慢，每个约 0.5~2s）
    """
    pnp = pnp_cameras()
    names = dshow_names()
    devices: list[CameraDevice] = []

    # 1) DShow 枚举项 —— 索引就在这个顺序里
    for i, dname in enumerate(names):
        best, score = None, 0.0
        for p in pnp:
            s = _match_score(dname, p["name"])
            if s > score:
                best, score = p, s
        in_pnp = bool(best and score >= 0.6)
        dev = CameraDevice(
            name=dname,
            index=i,
            kind=classify(dname, best["instance_id"] if in_pnp else "", in_pnp),
            instance_id=best["instance_id"] if in_pnp else "",
            status=best["status"] if in_pnp else "",
            mi=_mi(best["instance_id"]) if in_pnp else "",
        )
        if dev.instance_id:
            dev.links = device_links(dev.instance_id)
        devices.append(dev)

    # 2) PnP 里有、但没进 DShow 枚举的（典型就是 IR 相机）
    dshow_norm = [_norm(n) for n in names]
    for p in pnp:
        if any(_match_score(p["name"], n) >= 0.6 for n in names):
            continue
        dev = CameraDevice(
            name=p["name"], index=None, kind=classify(p["name"], p["instance_id"], True),
            instance_id=p["instance_id"], status=p["status"], mi=_mi(p["instance_id"]),
        )
        dev.links = device_links(dev.instance_id)
        devices.append(dev)

    # 3) 可选探活
    if probe:
        for dev in devices:
            if dev.index is None:
                continue
            info = probe_index(dev.index, api=CAP_API)
            if info:
                dev.opens = True
                dev.resolution = (info["width"], info["height"])
    return devices


def _mi(instance_id: str) -> str:
    m = re.search(r"MI_([0-9A-Fa-f]{2})", instance_id or "")
    return ("MI_" + m.group(1).upper()) if m else ""


# ═══════════════════════════════════════════════════════════════
# 选择符解析
# ═══════════════════════════════════════════════════════════════
def resolve_device(spec: Any = None, devices: Optional[Sequence[CameraDevice]] = None,
                   require_dshow: bool = True) -> CameraDevice:
    """把选择符解析成一台设备。

    spec 可以是：
      * None / "auto" / "rgb" / "ir" / "virtual" —— 按类型挑
      * 整数或数字字符串        —— 直接当 DShow 索引
      * 名字子串（不分大小写）  —— 如 "hp"、"nikon"
    """
    devs = list(devices) if devices is not None else list_devices()

    if isinstance(spec, CameraDevice):
        return spec

    if spec is None or (isinstance(spec, str) and spec.strip().lower() in ("", "auto", "default")):
        spec = "rgb"

    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        idx = int(spec)
        for d in devs:
            if d.index == idx:
                return d
        # 索引对应不到已知设备也允许（用户可能插了新设备）
        return CameraDevice(name="index-%d" % idx, index=idx, kind="unknown")

    kinds = {"rgb", "ir", "virtual"}
    key = str(spec).strip().lower()
    if key in kinds:
        # 用户明确点名了类型 —— 即使该类型只能走 Media Foundation（IR）也要给出去，
        # 只是在同类里优先挑能直接开流的。
        pool = [d for d in devs if d.kind == key]
        openable = [d for d in pool if d.index is not None]
        if openable:
            pool = openable
        if not pool:
            raise DeviceNotFound(
                "没有找到类型为 %r 的设备。当前设备表：\n%s" % (key, describe_devices(devs))
            )
        pool.sort(key=lambda d: (not d.opens, d.is_virtual, d.index is None, d.index or 0))
        return pool[0]

    # 名字子串匹配
    hits = [(d, _match_score(key, d.name)) for d in devs]
    hits = [(d, s) for d, s in hits if key in d.name.lower() or s >= 0.55]
    if require_dshow:
        openable = [(d, s) for d, s in hits if d.index is not None]
        hits = openable or hits
    if not hits:
        raise DeviceNotFound(
            "没有匹配 %r 的设备。当前设备表：\n%s" % (spec, describe_devices(devs))
        )
    hits.sort(key=lambda t: (-t[1], t[0].is_virtual, t[0].index is None))
    return hits[0][0]


def resolve_index(spec: Any = None, devices: Optional[Sequence[CameraDevice]] = None) -> int:
    """选择符 → OpenCV 索引。拿到没有 DShow 索引的设备（IR）时抛 CameraOpenError。"""
    dev = resolve_device(spec, devices, require_dshow=False)
    if dev.index is None:
        raise CameraOpenError(
            "%r 没有 DirectShow 索引（它只能走 Media Foundation，见 camera_ir.py）" % dev.name
        )
    return dev.index


def describe_devices(devices: Optional[Iterable[CameraDevice]] = None) -> str:
    devs = list(devices) if devices is not None else list_devices()
    if not devs:
        return "  (没有找到任何摄像头)"
    return "\n".join("  " + str(d) for d in devs)


# ═══════════════════════════════════════════════════════════════
# 黑图哨兵
# ═══════════════════════════════════════════════════════════════
def frame_brightness(frame: np.ndarray) -> float:
    return float(frame.mean())


def check_brightness(frame: np.ndarray, min_mean: float = DEFAULT_MIN_MEAN,
                     device: Optional[CameraDevice] = None, context: str = "") -> float:
    """帧亮度断言：太暗就抛 BlackFrameError（附带排查提示）。"""
    mean = frame_brightness(frame)
    if mean >= min_mean:
        return mean
    who = ("设备 %r" % device.name) if device else "当前设备"
    hint = [
        "%s 取到的帧 mean=%.1f < %.0f，疑似黑图。" % (who, mean, min_mean),
        "常见原因：",
        "  1) 索引/设备取错（IR 相机、虚拟摄像头常常只有个位数亮度）",
        "  2) 曝光停在 Manual 的负值档 —— 用 set_exposure(auto=True) 切自动",
        "  3) 镜头被遮挡 / 隐私开关关闭 / Windows 相机应用还占着设备",
    ]
    if device and device.is_virtual:
        hint.append("  4) 该设备是虚拟摄像头，宿主程序可能没有在推流")
    raise BlackFrameError("\n".join(hint) + ("\n  上下文: " + context if context else ""))


# ═══════════════════════════════════════════════════════════════
# 控制器
# ═══════════════════════════════════════════════════════════════
class CameraController:
    """一台摄像头的一体化控制器：解析 → 开流 → 曝光 → 采集 → 保存。

    :param spec: 见 resolve_device（缺省 "rgb"）
    :param warmup: 每次 grab 前后的预热帧数（摄像头自动曝光需要时间爬坡）
    :param min_mean: 黑图哨兵阈值，None = 关闭
    """

    def __init__(self, spec: Any = "rgb", *, warmup: int = 10,
                 min_mean: Optional[float] = DEFAULT_MIN_MEAN,
                 api: int = CAP_API, verbose: bool = False,
                 devices: Optional[Sequence[CameraDevice]] = None):
        self.devices = list(devices) if devices is not None else list_devices()
        self.device = resolve_device(spec, self.devices)
        self.warmup = int(warmup)
        self.min_mean = min_mean
        self.api = api
        self.verbose = verbose
        self._cap: Optional[cv2.VideoCapture] = None
        self._duvc = None
        self._duvc_name: Optional[str] = None
        self._exposure_mode = "unknown"

    # ---- 生命周期 ----
    @property
    def index(self) -> Optional[int]:
        return self.device.index

    @property
    def name(self) -> str:
        return self.device.name

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self, width: Optional[int] = None, height: Optional[int] = None,
             fps: Optional[float] = None) -> "CameraController":
        if self._cap is not None:
            return self
        if self.device.index is None:
            raise CameraOpenError(
                "%s 没有 DirectShow 索引，打不开。\n%s"
                % (self.device.name, _mf_hint(self.device))
            )
        cap = cv2.VideoCapture(self.device.index, self.api)
        if not cap.isOpened():
            # 有些构建里显式后端参数会被忽略，退一步用默认后端
            cap.release()
            cap = cv2.VideoCapture(self.device.index)
            if not cap.isOpened():
                cap.release()
                raise CameraOpenError(
                    "打不开索引 %d (%s)。可能被其它程序占用，或设备刚被禁用/重载。"
                    % (self.device.index, self.device.name)
                )
        for prop, val in ((cv2.CAP_PROP_FRAME_WIDTH, width),
                          (cv2.CAP_PROP_FRAME_HEIGHT, height),
                          (cv2.CAP_PROP_FPS, fps)):
            if val is not None:
                cap.set(prop, val)
        self._cap = cap
        if self.verbose:
            print("[camera_core] 打开 %s (idx %d, %s)"
                  % (self.device.name, self.device.index,
                     cap.getBackendName() if hasattr(cap, "getBackendName") else "?"),
                  file=sys.stderr)
        return self

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._duvc is not None:
            try:
                self._duvc.close()
            except Exception:
                pass
            self._duvc = None

    def __enter__(self) -> "CameraController":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 曝光（duvc-ctl / DirectShow COM）----
    def _dshow_equivalent(self) -> Optional[str]:
        """本地设备名 → duvc（=DShow 枚举）里的名字。"""
        names = dshow_names()
        if self.device.index is not None and 0 <= self.device.index < len(names):
            return names[self.device.index]
        if not names:
            return None
        best, score = None, 0.0
        for n in names:
            s = _match_score(self.device.name, n)
            if s > score:
                best, score = n, s
        return best if score >= 0.6 else None

    def duvc_camera(self):
        """拿到 duvc-ctl 的摄像头对象（用于 Auto/Manual 曝光）。"""
        if self._duvc is not None:
            return self._duvc
        name = self._dshow_equivalent()
        if name is None:
            raise CameraError("%r 不在 DirectShow 枚举里，无法用 duvc-ctl 控制" % self.device.name)
        try:
            import duvc_ctl as duvc
        except ImportError as e:
            raise CameraError("需要 duvc-ctl: pip install duvc-ctl") from e
        try:
            with _quiet():
                self._duvc = duvc.find_camera(name)
        except Exception as e:      # duvc 的异常类型随版本变化，统一转成我们的
            raise CameraError("duvc-ctl 找不到 %r: %s" % (name, e)) from e
        self._duvc_name = name
        return self._duvc

    def get_exposure(self) -> dict:
        """读当前曝光：{'value': int|None, 'mode': 'auto'|'manual'|'unknown'}。

        注意 duvc 的 ``get_exposure()`` 只回一个数值，**读不出 Auto 标志**
        （实测 Auto 模式下它回的仍是 Manual 区间的值，如 -10），
        所以 mode 取本对象最后一次设置的模式（没设过就是 unknown）。
        """
        try:
            cam = self.duvc_camera()
            val = cam.get_exposure()
        except CameraError:
            return {"value": None, "mode": "unknown"}
        return {"value": val, "mode": self._exposure_mode}

    def exposure_range(self) -> dict:
        """曝光可调范围，如 ``{'min': -10, 'max': -2, 'step': 1, 'default': -5}``。"""
        try:
            cam = self.duvc_camera()
            rng = cam.get_property_range("exposure")
        except Exception:
            return {}
        return dict(rng) if rng else {}

    def set_exposure(self, value: Optional[int] = None, auto: Optional[bool] = None) -> dict:
        """设置曝光。

        - ``set_exposure(auto=True)``      → Auto 模式（解决画面过暗的根治手段）
        - ``set_exposure(-3)``             → 手动档 -3
        - ``set_exposure(-3, auto=False)`` → 显式手动
        """
        cam = self.duvc_camera()
        if auto is None:
            auto = value is None
        if auto:
            cam.set_exposure(0, "auto")
        else:
            if value is None:
                raise CameraError("手动模式必须给 value")
            cam.set_exposure(int(value), "manual")
        self._exposure_mode = "auto" if auto else "manual"
        readback = cam.get_exposure()
        return {"mode": self._exposure_mode, "requested": value, "value": readback}

    # ---- 采集 ----
    def grab(self, warmup: Optional[int] = None, *, check: bool = True,
             stretch: bool = False) -> np.ndarray:
        """抓一帧（带预热）。check=True 时过黑图哨兵；stretch=True 时做对比度拉伸。"""
        if self._cap is None:
            self.open()
        assert self._cap is not None
        n = self.warmup if warmup is None else int(warmup)
        frame = None
        for _ in range(max(1, n)):
            ok, f = self._cap.read()
            if ok and f is not None:
                frame = f
        if frame is None:
            raise CameraError("读帧失败：%s (idx %s)" % (self.device.name, self.device.index))
        if check and self.min_mean is not None:
            check_brightness(frame, self.min_mean, self.device)
        return stretch_frame(frame) if stretch else frame

    def grab_many(self, count: int, warmup: Optional[int] = None,
                  interval: float = 0.0) -> list:
        out = []
        for i in range(count):
            out.append(self.grab(warmup=warmup if i == 0 else 1))
            if interval:
                time.sleep(interval)
        return out

    def snapshot(self, path: Optional[str] = None, out_dir: str = ".",
                 prefix: Optional[str] = None, stretch: bool = False,
                 check: bool = True) -> Path:
        """抓一帧存盘，返回路径。"""
        frame = self.grab(stretch=stretch, check=check)
        if path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            tag = prefix or _slug(self.device.name)
            path = str(Path(out_dir) / ("%s_%s.jpg" % (tag, ts)))
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(p), frame):
            raise CameraError("写文件失败: %s" % p)
        return p

    # ---- 信息 ----
    def info(self) -> dict:
        d = self.device.to_dict()
        if self.is_open:
            cap = self._cap
            assert cap is not None
            d["current"] = {
                "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "fps": round(float(cap.get(cv2.CAP_PROP_FPS)), 2),
            }
        try:
            d["exposure"] = self.get_exposure()
            d["exposure_range"] = self.exposure_range()
        except CameraError:
            pass
        return d

    def __repr__(self) -> str:
        return "<CameraController %r idx=%s %s>" % (self.device.name, self.device.index, self.device.kind)


# ═══════════════════════════════════════════════════════════════
# 小工具
# ═══════════════════════════════════════════════════════════════
def stretch_frame(frame: np.ndarray) -> np.ndarray:
    """对比度拉伸到 0-255（给 IR / 极暗图看内容用）。"""
    if frame.ndim == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame
    return cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "camera"


def _mf_hint(dev: CameraDevice) -> str:
    if dev.links:
        return ("它只注册了设备接口符号链接，可以用 Media Foundation 打开：\n"
                "  from camera_ir import IrCamera\n"
                "  IrCamera(%r).snapshot('ir.jpg')" % dev.name)
    return "该设备没有可用的采集接口。"


# ═══════════════════════════════════════════════════════════════
# 自测 CLI
# ═══════════════════════════════════════════════════════════════
def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="camera_core 自检 / 拍照")
    ap.add_argument("-i", "--index", default="rgb",
                    help='选择符：类型(rgb/ir/virtual) | 索引(0) | 名字子串("hp")，默认 rgb')
    ap.add_argument("-o", "--output", help="拍照保存路径")
    ap.add_argument("--probe", action="store_true", help="探活每个索引（慢）")
    ap.add_argument("--json", action="store_true", help="设备表输出 JSON")
    ap.add_argument("--exposure", help='曝光：auto 或整数（如 -3）')
    args = ap.parse_args(argv)

    devs = list_devices(probe=args.probe)
    if args.json:
        print(json.dumps([d.to_dict() for d in devs], indent=2, ensure_ascii=False))
    else:
        print("设备表：")
        print(describe_devices(devs))

    if args.exposure or args.output:
        with CameraController(args.index, devices=devs, verbose=True) as cam:
            if args.exposure:
                if args.exposure.lower() == "auto":
                    print("曝光 →", cam.set_exposure(auto=True))
                else:
                    print("曝光 →", cam.set_exposure(int(args.exposure)))
            if args.output:
                p = cam.snapshot(args.output)
                print("已保存:", p)
            print("信息:", json.dumps(cam.info(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
