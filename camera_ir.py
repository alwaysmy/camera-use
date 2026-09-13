"""camera_ir.py — 打开 Windows Hello 红外(IR)相机
================================================================
背景
----
笔记本的 IR 相机（Windows Hello 用）在 PnP 里是独立设备，通常和 RGB 共用
USB 复合设备：``MI_00`` = RGB，``MI_02`` = IR。

* DirectShow **枚举不到**它（被 Hello 的隐私策略从 Video Capture Sources 里过滤掉），
  所以 ``cv2.VideoCapture(i, CAP_DSHOW)`` 永远打不开，``duvc-ctl`` 也看不到。
* 但它的设备接口是真实注册的，用 **设备符号链接** 直接交给 Media Foundation
  就能打开并出图（本模块做的事）。

已验证（HP Wide Vision FHD + HP IR Camera, VID_04CA:7086）::

    原生媒体类型 : YUY2 340x340（唯一一种）
    帧率         : ~31 fps（帧间隔 32ms）
    曝光         : 相机自管，开流后 1~2 秒自动爬坡（mean 5 → 100+）
    IAMCameraControl : 不支持（E_NOINTERFACE），无法手动锁曝光
    补光灯交替   : **每 2 帧一个循环** —— 奇数帧 IR 补光灯点亮(mean≈80)，
                   偶数帧熄灭(mean≈38)。这正是 Windows Hello 做活体检测的原料，
                   本模块用 `prefer="bright"` 自动挑亮帧，另有 `read_pair()` 拿一对。

踩过的坑（保持现状的技术原因）::

    * 每帧拿到的 IMFSample 和 ConvertToContiguousBuffer 出来的 IMFMediaBuffer
      都必须 Release —— 缓冲区是池化的，泄漏十来个之后 ReadSample 会**永久阻塞**。
    * 先 Release 全部 COM 对象再 MFShutdown，否则 MFShutdown 会等在那里不返回。
    * 进程被强杀会把 IR 相机留在"占用/启动失败"态（ReadSample 报 0xC00D3704），
      这时重新插拔或重启进程即可恢复。

用法::

    from camera_ir import IrCamera

    with IrCamera() as ir:              # 自动找 IR 相机
        g = ir.read(warmup=30)          # 340x340 uint8 灰度（补光灯亮帧）
        ir.snapshot("ir.jpg", stretch=True)
        lit, dark, delta = ir.read_pair()   # 活体检测原料

命令行::

    python camera_ir.py --info
    python camera_ir.py -o ir.jpg --stretch
"""
from __future__ import annotations

import ctypes
import sys
import time
import uuid
from ctypes import (POINTER, byref, c_byte, c_long, c_longlong, c_uint32,
                    c_uint64, c_ulong, c_void_p, c_wchar_p)
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

__all__ = ["IrCamera", "IrCameraError", "list_ir_devices", "mf_available"]


# ═══════════════════════════════════════════════════════════════
# COM 地基
# ═══════════════════════════════════════════════════════════════
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", c_byte * 8)]

    def __str__(self) -> str:
        d4 = "".join("%02X" % b for b in self.Data4)
        return "{%08X-%04X-%04X-%s-%s}" % (self.Data1, self.Data2, self.Data3, d4[:4], d4[4:])


def _g(s: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


def _vtbl(ptr, index: int, restype, *argtypes):
    """取 COM 接口第 index 个虚函数并转成可调用对象。"""
    base = ctypes.cast(ptr, POINTER(c_void_p)).contents.value
    addr = ctypes.cast(base + index * ctypes.sizeof(c_void_p), POINTER(c_void_p)).contents.value
    return ctypes.CFUNCTYPE(restype, c_void_p, *argtypes)(addr)


def _release(ptr) -> None:
    if ptr:
        try:
            _vtbl(ptr, 2, c_ulong)(ptr)
        except Exception:
            pass


def _s(v: c_void_p) -> c_void_p:
    """c_void_p 非空判断用的小助手。"""
    return v is not None and v.value not in (None, 0)


def hresult_name(hr: int) -> str:
    table = {
        0x80070002: "ERROR_FILE_NOT_FOUND（符号链接无效/设备不在）",
        0x80070005: "E_ACCESSDENIED（被占用或被策略拒绝）",
        0xC00D36B9: "MF_E_NO_MORE_TYPES（媒体类型枚举结束）",
        0xC00D3E85: "MF_E_UNSUPPORTED_BYTESTREAM_TYPE",
        0xC00D36B3: "MF_E_INVALIDMEDIATYPE",
        0x80004002: "E_NOINTERFACE",
        0x80010106: "RPC_E_CHANGED_MODE（COM 套间模式冲突）",
    }
    if hr == 0:
        return "S_OK"
    return "0x%08X %s" % (hr & 0xFFFFFFFF, table.get(hr & 0xFFFFFFFF, ""))


# ---- MF 常量 -----------------------------------------------------------
_SOURCE_TYPE = _g("C60AC5FE-252A-478F-A0EF-BC8FA5F7CAD3")
_SOURCE_TYPE_VIDCAP = _g("8AC3587A-4AE7-42D8-99E0-0A6013EEF90F")
_SYMBOLIC_LINK = _g("58F0AAD8-22BF-4F8A-BB3D-D2C4978C6E2F")
_MF_MT_SUBTYPE = _g("F7E34C9A-42E8-4714-B74B-CB29D72C35E5")
_MF_MT_FRAME_SIZE = _g("1652C33D-D6B2-4012-B834-72030849A37D")
_IID_IMFMediaSource = _g("279AFA83-4981-11CE-A521-0020AF0BE560")

_MFSTARTUP_FULL = 0x0
_MF_VERSION = 0x00020070
_COINIT_MTA = 0x2

_ole32 = ctypes.windll.ole32
_mfplat = ctypes.windll.mfplat
_mf = ctypes.windll.mf
_mfreadwrite = ctypes.windll.mfreadwrite


class IrCameraError(RuntimeError):
    pass


# MF 引用计数：MFShutdown 是进程级的，最后一个用户退出时才关
_mf_refcount = 0


def _mf_startup() -> None:
    global _mf_refcount
    if _mf_refcount == 0:
        hr = _ole32.CoInitializeEx(None, _COINIT_MTA)
        # 0 = S_OK；1 = S_FALSE（本线程早就初始化过了，正常）；
        # 0x80010106 = RPC_E_CHANGED_MODE（套间模式不同，仍可继续用 MF）
        if hr not in (0, 1, 0x80010106):
            raise IrCameraError("CoInitializeEx 失败: %s" % hresult_name(hr))
        hr = _mfplat.MFStartup(_MF_VERSION, _MFSTARTUP_FULL)
        if hr != 0:
            raise IrCameraError("MFStartup 失败: %s" % hresult_name(hr))
    _mf_refcount += 1


def _mf_shutdown() -> None:
    global _mf_refcount
    if _mf_refcount > 0:
        _mf_refcount -= 1
    if _mf_refcount == 0:
        try:
            _mfplat.MFShutdown()
        except Exception:
            pass


def mf_available() -> bool:
    try:
        _mf_startup()
    except Exception:
        return False
    finally:
        _mf_shutdown()
    return True


# ═══════════════════════════════════════════════════════════════
# 找 IR 相机
# ═══════════════════════════════════════════════════════════════
def list_ir_devices() -> list:
    """返回 camera_core 设备表里 kind == 'ir' 的设备。"""
    try:
        from camera_core import list_devices
    except ImportError:
        return []
    return [d for d in list_devices() if d.kind == "ir"]


def _default_links() -> list[tuple[str, str]]:
    """(设备名, 符号链接) 候选列表，按 IR 优先。"""
    out: list[tuple[str, str]] = []
    for d in list_ir_devices():
        for cat in ("sensor_camera", "video_camera", "capture"):
            link = d.link(cat)
            if link:
                out.append(("%s [%s]" % (d.name, cat), link))
    return out


# ═══════════════════════════════════════════════════════════════
# IR 相机
# ═══════════════════════════════════════════════════════════════
class IrCamera:
    """红外相机（Media Foundation + 设备符号链接）。

    :param link: 设备符号链接；None = 自动挑第一个 IR 相机
    :param name: 按设备名子串挑（如 "HP IR"）
    :param warmup: open 后自动丢弃的帧数（IR 自动曝光爬坡需要）
    """

    def __init__(self, link: Optional[str] = None, name: Optional[str] = None,
                 warmup: int = 12, verbose: bool = False):
        self.warmup = int(warmup)
        self.verbose = verbose
        self.device_name = "?"
        self.link = link
        self.width = 0
        self.height = 0
        self.subtype = "?"
        self.fps = 0.0
        self._attrs = c_void_p()
        self._src = c_void_p()
        self._reader = c_void_p()
        self._mt = c_void_p()
        self._opened = False
        self._started = False
        self._last_ts = 0
        self._frames = 0

        if self.link is None:
            cands = _default_links()
            if name:
                low = name.lower()
                cands = [c for c in cands if low in c[0].lower()] or cands
            if not cands:
                raise IrCameraError(
                    "没有找到 IR 相机。IR 相机必须能推导出设备符号链接；"
                    "用 `python camera_core.py` 看设备表里有没有 kind=ir 的条目。"
                )
            self.device_name, self.link = cands[0]

    # ---- 打开 / 关闭 ----
    def open(self) -> "IrCamera":
        if self._opened:
            return self
        _mf_startup()
        try:
            self._attrs = self._create_attrs()
            hr = _mf.MFCreateDeviceSource(self._attrs, byref(self._src))
            if hr != 0 or not _s(self._src):
                raise IrCameraError(
                    "MFCreateDeviceSource 失败: %s\n  链接: %s" % (hresult_name(hr), self.link))
            hr = _mfreadwrite.MFCreateSourceReaderFromMediaSource(self._src, None, byref(self._reader))
            if hr != 0:
                raise IrCameraError("MFCreateSourceReaderFromMediaSource 失败: %s" % hresult_name(hr))
            self._configure()
            self._opened = True
            if self.warmup > 0:
                self.read(warmup=self.warmup, check=False)
            return self
        except Exception:
            self.close()
            raise

    def _create_attrs(self) -> c_void_p:
        attrs = c_void_p()
        hr = _mfplat.MFCreateAttributes(byref(attrs), 4)
        if hr != 0:
            raise IrCameraError("MFCreateAttributes 失败: %s" % hresult_name(hr))
        set_guid = _vtbl(attrs, 24, c_long, POINTER(GUID), POINTER(GUID))
        if set_guid(attrs, byref(_SOURCE_TYPE), byref(_SOURCE_TYPE_VIDCAP)) != 0:
            raise IrCameraError("设置 SOURCE_TYPE 失败")
        set_str = _vtbl(attrs, 25, c_long, POINTER(GUID), c_wchar_p)
        if set_str(attrs, byref(_SYMBOLIC_LINK), c_wchar_p(self.link)) != 0:
            raise IrCameraError("设置 SYMBOLIC_LINK 失败")
        return attrs

    def _configure(self) -> None:
        """挑一个原生媒体类型并应用。"""
        self._mt, self.subtype, self.width, self.height = self._pick_media_type()
        set_mt = _vtbl(self._reader, 7, c_long, c_uint32, POINTER(c_uint32), c_void_p)
        hr = set_mt(self._reader, 0, None, self._mt)
        if hr != 0:
            raise IrCameraError("SetCurrentMediaType(%s %dx%d) 失败: %s"
                                % (self.subtype, self.width, self.height, hresult_name(hr)))

    def _pick_media_type(self):
        """返回第一个可用的原生媒体类型 (mt, subtype, w, h)。

        注意：``GetGUID`` / ``GetUINT64`` 是 **IMFAttributes** 的槽位（10 / 8），
        媒体类型对象继承 IMFAttributes，所以要作用在 ``mt`` 上而不是 reader 上。
        """
        get_native = _vtbl(self._reader, 5, c_long, c_uint32, c_uint32, POINTER(c_void_p))
        usable = {"YUY2", "NV12", "MJPG", "RGB3", "L8  ", "GREY", "NV21"}
        candidates = []
        for i in range(32):
            mt = c_void_p()
            hr = get_native(self._reader, 0, i, byref(mt))
            if hr != 0 or not _s(mt):
                break
            sub = GUID()
            _vtbl(mt, 10, c_long, POINTER(GUID), POINTER(GUID))(mt, byref(_MF_MT_SUBTYPE), byref(sub))
            size = c_uint64(0)
            _vtbl(mt, 8, c_long, POINTER(GUID), POINTER(c_uint64))(mt, byref(_MF_MT_FRAME_SIZE), byref(size))
            w = (size.value >> 32) & 0xFFFFFFFF
            h = size.value & 0xFFFFFFFF
            cc = _fourcc_of(sub)
            if self.verbose:
                print("[camera_ir] 原生类型[%d] %s %dx%d" % (i, cc, w, h), file=sys.stderr)
            candidates.append((mt, cc, w, h))

        chosen = next((c for c in candidates if c[1] in usable), None)
        if chosen is None and candidates:
            chosen = candidates[0]
        for c in candidates:                 # 没选中的媒体类型必须 Release
            if c is not chosen:
                _release(c[0])
        if chosen is None:
            raise IrCameraError("设备没有可用的视频媒体类型")
        return chosen

    def close(self) -> None:
        """释放全部 COM 对象。必须先 Release 再 MFShutdown，否则 MFShutdown 会卡住。"""
        for p in (self._mt, self._reader, self._src, self._attrs):
            _release(p)
        self._mt = c_void_p()
        self._reader = c_void_p()
        self._src = c_void_p()
        self._attrs = c_void_p()
        if self._opened:
            self._opened = False
            _mf_shutdown()

    def __enter__(self) -> "IrCamera":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---- 读帧 ----
    def read(self, warmup: int = 1, check: bool = False,
             min_mean: float = 3.0, prefer: str = "bright") -> Optional[np.ndarray]:
        """读一帧，返回 HxW 的 uint8 灰度（IR 的亮度通道）。

        ``warmup`` 张之前丢弃（IR 自动曝光爬坡，第一帧往往只有个位数亮度）。

        ``prefer`` 处理 **补光灯交替帧**（实测 HP IR 相机每 2 帧一个循环：
        奇数帧 IR 补光灯点亮 mean≈80，偶数帧熄灭 mean≈38）：

        * ``"bright"``（默认）—— 多读一帧，返回亮的（补光灯点亮）那张
        * ``"dark"``  —— 返回暗的（纯环境红外）那张
        * ``"any"``   —— 直接返回最后一张，不做挑选
        """
        if not self._opened:
            self.open()
            warmup = max(0, warmup - self.warmup)
        last = None
        remaining = max(1, warmup)
        for _ in range(remaining * 3):        # 空帧（STREAMTICK）允许重试
            f = self._read_one()
            if f is None:
                continue
            last = f
            remaining -= 1
            if remaining <= 0:
                break
        if last is None:
            raise IrCameraError("读帧失败（设备没送帧）")

        if prefer in ("bright", "dark"):
            other = self._read_one()
            if other is not None:
                pick = max if prefer == "bright" else min
                last = pick((last, other), key=lambda f: float(f.mean()))

        if check and float(last.mean()) < min_mean:
            raise IrCameraError("IR 帧过暗 mean=%.1f —— 可能是 IR 补光灯没亮或场景无红外光" % last.mean())
        return last

    def read_pair(self, warmup: Optional[int] = None) -> tuple:
        """读一对连续帧：``(补光灯亮, 补光灯灭, 亮度差)``。

        这正是 Windows Hello 做活体检测的原料 —— 真脸在打光和不打光两帧上
        差异很大（皮肤反射 IR），照片/屏幕则差异很小。可用于自己写活体判据。
        """
        if warmup:
            self.read(warmup=warmup, prefer="any")
        for _ in range(6):
            a = self._read_one()
            b = self._read_one()
            if a is None or b is None:
                continue
            lit, dark = (a, b) if float(a.mean()) >= float(b.mean()) else (b, a)
            return lit, dark, float(lit.mean() - dark.mean())
        raise IrCameraError("读帧失败（拿不到一对帧）")

    def _read_one(self) -> Optional[np.ndarray]:
        read_sample = _vtbl(self._reader, 9, c_long, c_uint32, c_uint32, POINTER(c_uint32),
                            POINTER(c_uint32), POINTER(c_longlong), POINTER(c_void_p))
        actual, flags, ts, sample = c_uint32(0), c_uint32(0), c_longlong(0), c_void_p()
        hr = read_sample(self._reader, 0, 0, byref(actual), byref(flags), byref(ts), byref(sample))
        if hr != 0:
            raise IrCameraError("ReadSample 失败: %s" % hresult_name(hr))
        if not _s(sample):
            return None
        try:
            if self._last_ts:
                self.fps = 1e7 / max(1.0, ts.value - self._last_ts)
            self._last_ts = ts.value
            self._frames += 1
            buffer = c_void_p()
            hr = _vtbl(sample, 41, c_long, POINTER(c_void_p))(sample, byref(buffer))
            if hr != 0 or not _s(buffer):
                return None
            try:
                data, curlen = self._lock(buffer)
                try:
                    return self._to_gray(data, curlen)
                finally:
                    _vtbl(buffer, 4, c_long)(buffer)   # IMFMediaBuffer::Unlock
            finally:
                # 必须 Release：缓冲区是池化的，泄漏十来个之后 ReadSample 会永久阻塞
                _release(buffer)
        finally:
            _release(sample)                            # IMFSample::Release

    def _lock(self, buffer) -> tuple[bytes, int]:
        pdata, maxlen, curlen = c_void_p(), c_uint32(0), c_uint32(0)
        hr = _vtbl(buffer, 3, c_long, POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32))(
            buffer, byref(pdata), byref(maxlen), byref(curlen))
        if hr != 0:
            raise IrCameraError("IMFMediaBuffer::Lock 失败: %s" % hresult_name(hr))
        return ctypes.string_at(pdata.value, curlen.value), curlen.value

    def _to_gray(self, raw: bytes, length: int) -> Optional[np.ndarray]:
        w, h = self.width, self.height
        if w <= 0 or h <= 0:
            return None
        a = np.frombuffer(raw, dtype=np.uint8)
        cc = self.subtype
        if cc == "YUY2" and length >= h * w * 2:
            # Y0 U Y1 V ... → 只取 Y（IR 的亮度通道就是它）
            return a[: h * w * 2].reshape(h, w // 2, 4)[:, :, [0, 2]].reshape(h, w).copy()
        if cc in ("NV12", "NV21") and length >= h * w:
            return a[: h * w].reshape(h, w).copy()
        if cc in ("L8  ", "GREY") and length >= h * w:
            return a[: h * w].reshape(h, w).copy()
        if cc == "RGB3" and length >= h * w * 4:
            return a[: h * w * 4].reshape(h, w, 4)[:, :, :3].mean(axis=2).astype(np.uint8)
        if cc == "MJPG":
            import cv2
            img = cv2.imdecode(a, cv2.IMREAD_GRAYSCALE)
            return img
        return None

    def read_burst(self, count: int = 10, interval: float = 0.0,
                   prefer: str = "bright") -> list:
        """连续读 count 帧（用于测帧率或挑最亮的一帧）。"""
        out = []
        for _ in range(count):
            f = self.read(warmup=1, prefer=prefer)
            if f is not None:
                out.append(f)
            if interval:
                time.sleep(interval)
        return out

    # ---- 保存 ----
    def snapshot(self, path: Optional[str] = None, warmup: Optional[int] = None,
                 stretch: bool = True, out_dir: str = ".", prefer: str = "bright") -> Path:
        """拍一张 IR 图存盘，返回路径。

        IR 原图通常偏暗，默认 **stretch=True** 做对比度拉伸，人眼才看得清内容；
        ``prefer="bright"`` 保证拿到的是补光灯点亮的那一帧。
        """
        frame = self.read(warmup=warmup if warmup is not None else max(self.warmup, 20),
                          prefer=prefer)
        assert frame is not None
        if stretch:
            import cv2
            frame = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX)
        if path is None:
            import re
            tag = re.sub(r"[^A-Za-z0-9]+", "_", self.device_name).strip("_").lower() or "ir"
            path = str(Path(out_dir) / ("%s_ir_%s.jpg" % (tag, time.strftime("%Y%m%d_%H%M%S"))))
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        import cv2
        if not cv2.imwrite(str(p), frame):
            raise IrCameraError("写文件失败: %s" % p)
        return p

    def info(self) -> dict:
        return {
            "device": self.device_name,
            "link": self.link,
            "subtype": self.subtype,
            "resolution": "%dx%d" % (self.width, self.height),
            "fps_measured": round(self.fps, 1) if self.fps else None,
            "frames_read": self._frames,
            "opened": self._opened,
            "exposure_control": "不支持（IR 相机自管曝光，无 IAMCameraControl）",
        }

    def __repr__(self) -> str:
        return "<IrCamera %r %s %dx%d %s>" % (
            self.device_name, self.subtype, self.width, self.height,
            "open" if self._opened else "closed")


def _fourcc_of(g: GUID) -> str:
    try:
        return g.Data1.to_bytes(4, "little").decode("ascii")
    except Exception:
        return str(g)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="IR 相机拍照（Media Foundation）")
    ap.add_argument("-o", "--output", help="保存路径")
    ap.add_argument("-n", "--name", help="设备名子串，默认自动挑")
    ap.add_argument("--link", help="直接指定设备符号链接")
    ap.add_argument("--info", action="store_true", help="打印设备与流信息")
    ap.add_argument("--probe", action="store_true", help="枚举原生媒体类型")
    ap.add_argument("--frames", type=int, default=0, help="连读 N 帧（测帧率/亮度）")
    ap.add_argument("--stretch", action="store_true", help="对比度拉伸后再存")
    ap.add_argument("--no-mf", action="store_true", help="只检查 Media Foundation 是否可用")
    args = ap.parse_args(argv)

    if args.no_mf:
        print("Media Foundation 可用:", mf_available())
        return 0

    if not args.output and not args.info and not args.frames and not args.probe:
        print("IR 设备候选：")
        cands = _default_links()
        if not cands:
            print("  (无)")
        for n, l in cands:
            print("  -", n)
            print("    ", l)
        return 0

    with IrCamera(link=args.link, name=args.name, verbose=args.probe, warmup=20) as ir:
        print("设备:", ir.device_name)
        if args.probe or args.info:
            print(json.dumps(ir.info(), indent=2, ensure_ascii=False))
        if args.frames:
            t0 = time.time()
            frames = ir.read_burst(args.frames)
            dt = time.time() - t0
            means = [round(float(f.mean()), 1) for f in frames]
            print("读到 %d 帧，耗时 %.2fs → %.1f fps" % (len(frames), dt, len(frames) / dt if dt else 0))
            print("逐帧亮度:", means)
        if args.output:
            print("已保存:", ir.snapshot(args.output, stretch=args.stretch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
