#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""camctl.py — 给 agent 用的摄像头命令行接口（机器可读）

设计原则
--------
1. **默认输出 JSON**，形状固定：成功 ``{"ok":true,"command":...,"data":{...}}``，
   失败 ``{"ok":false,"command":...,"error":{"code","message","hint"}}``。
2. **退出码有语义**：0 成功 / 1 一般错误 / 2 黑图或未遮住 / 3 找不到设备 / 4 硬件不可用。
3. 每个子命令自包含（自己开会话、自己收尾），可以直接被 agent 当工具调用。
4. 人类可读输出用 ``--human``。

常用::

    python camctl.py devices
    python camctl.py capture --out a.jpg --codec yuy2
    python camctl.py ir --out ir.jpg --diff
    python camctl.py fuse  --out fused.jpg --weight 0.65
    python camctl.py align
    python camctl.py dark-cal --target both --frames 30
    python camctl.py wavelength --step empty
    python camctl.py serve --port 8765
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np

EXIT_OK, EXIT_ERROR, EXIT_DARK, EXIT_NODEV, EXIT_HW = 0, 1, 2, 3, 4


class CliError(Exception):
    def __init__(self, message: str, code: str = "ERROR", hint: str = "",
                 exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.message, self.code, self.hint, self.exit_code = message, code, hint, exit_code


def emit(ok: bool, command: str, data=None, error: CliError | None = None,
         human: bool = False, exit_code: int = EXIT_OK) -> int:
    if human:
        if ok:
            print(_human(data, command))
        else:
            print(f"❌ {command}: {error.message}")
            if error.hint:
                print(f"   提示: {error.hint}")
        return exit_code
    obj = {"ok": ok, "command": command, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if ok:
        obj["data"] = data
    else:
        obj["error"] = {"code": error.code, "message": error.message, "hint": error.hint}
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return exit_code


def _human(data, command) -> str:
    if data is None:
        return f"✅ {command}"
    if isinstance(data, list):
        return "\n".join("  " + json.dumps(d, ensure_ascii=False) for d in data)
    return "✅ " + json.dumps(data, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════
# 会话小工具
# ═══════════════════════════════════════════════════════════════
def _session(args, use_ir=True, codec="auto"):
    from camera_pipeline import CameraSession
    s = CameraSession(rgb_spec=getattr(args, "device", "rgb"),
                      codec=codec, use_ir=use_ir,
                      out_dir=getattr(args, "out_dir", "captures"),
                      load_dark=not getattr(args, "no_dark", False))
    return s


def _need_ready(s: "object", need_ir=True, timeout=20.0):
    if not s.wait_ready(timeout=timeout, need_ir=need_ir):
        errs = []
        if s.rgb and s.rgb.error:
            errs.append(s.rgb.error)
        if s.ir and s.ir.error:
            errs.append(s.ir.error)
        raise CliError("；".join(errs) or "等待画面超时（相机可能被占用）",
                       "NOT_READY",
                       "关掉正在用摄像头的程序（包括本项目的 GUI/WebUI），或检查设备是否被禁用",
                       EXIT_NODEV)


def _save(img, path: Path, gray_stretch=False) -> str:
    from camera_pipeline import stretch
    path.parent.mkdir(parents=True, exist_ok=True)
    if gray_stretch and img.ndim == 2:
        img = stretch(img)
    if not cv2.imwrite(str(path), img):
        raise CliError(f"写文件失败: {path}", "IO_ERROR", "检查目录是否存在/可写")
    return str(path)


def _check_dark(frame, name, min_mean=40.0):
    m = float(frame.mean())
    if m < min_mean:
        raise CliError(
            f"{name} 取到的画面 mean={m:.1f} < {min_mean}，疑似黑图",
            "BLACK_FRAME",
            "确认不是取错设备（IR/虚拟相机经常只有个位数亮度）；"
            "或镜头被遮挡、隐私开关关闭",
            EXIT_DARK)
    return m


# ═══════════════════════════════════════════════════════════════
# 子命令
# ═══════════════════════════════════════════════════════════════
def cmd_devices(args):
    from camera_core import list_devices
    devs = list_devices(probe=args.probe)
    data = [d.to_dict() for d in devs]
    if args.human:
        for d in data:
            print(f"  idx={d.get('index')} [{d.get('kind')}] {d['name']}"
                  + (f"  {d.get('resolution')}" if d.get("resolution") else ""))
        return EXIT_OK
    return emit(True, "devices", data, human=args.human)


def cmd_capture(args):
    s = _session(args, use_ir=False, codec=args.codec if args.codec != "auto" else "yuy2")
    try:
        _need_ready(s, need_ir=False)
        from camera_core import BlackFrameError
        frame = None
        for _ in range(max(1, args.warmup)):
            frame = s.rgb.frame
            time.sleep(1 / 60.0)
        if frame is None:
            raise CliError("没有取到帧", "NO_FRAME")
        mean = _check_dark(frame, "RGB") if not args.no_check else float(frame.mean())
        path = _save(frame, Path(args.out) if args.out else
                     Path(args.out_dir) / f"rgb_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
        return emit(True, "capture", {
            "file": path, "device": str(s.rgb.device), "codec": s.rgb.fourcc,
            "width": int(frame.shape[1]), "height": int(frame.shape[0]),
            "mean": round(mean, 1), "exposure": s.rgb.cam.get_exposure() if s.rgb.cam else None,
        }, human=args.human)
    finally:
        s.stop()


def cmd_ir(args):
    s = _session(args, use_ir=True, codec="mjpg")
    try:
        _need_ready(s, need_ir=True)
        time.sleep(0.4)
        if args.diff:
            img = s.ir.diff()
            if img is None:
                raise CliError("拿不到补光差分（两帧同相位？）", "NO_LED_PAIR",
                               "IR 相机是逐帧亮灭补光的，等一两拍再试")
            name = "diff"
        else:
            img = s.ir.bright
            name = "ir"
        path = _save(img, Path(args.out) if args.out else
                     Path(args.out_dir) / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.jpg",
                     gray_stretch=not args.raw)
        return emit(True, "ir", {
            "file": path, "device": s.ir.device_name, "kind": name,
            "resolution": f"{img.shape[1]}x{img.shape[0]}",
            "raw": bool(args.raw), **s.ir.led_stats(),
        }, human=args.human)
    finally:
        s.stop()


def cmd_diff(args):
    args.diff = True
    return cmd_ir(args)


def cmd_fuse(args):
    s = _session(args, use_ir=True, codec=args.codec if args.codec != "auto" else "mjpg")
    try:
        _need_ready(s)
        if args.align == "auto":
            info = s.do_align()
        else:
            info = {"scale": s.params["scale"], "tx": s.params["tx"], "ty": s.params["ty"]}
        s.set_params(weight=args.weight, gain=args.gain, chroma=args.chroma, mode="fuse")
        res = s.compose("fuse")
        if res["view"] is None:
            raise CliError("融合失败（缺少 RGB 或 IR）", "FUSE_FAILED")
        path = _save(res["view"], Path(args.out) if args.out else
                     Path(args.out_dir) / f"fuse_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
        if args.save_parts:
            for k in ("rgb", "ir"):
                if res[k] is not None:
                    _save(res[k], Path(path).with_name(Path(path).stem + f"_{k}.jpg"),
                          gray_stretch=(k == "ir"))
        return emit(True, "fuse", {
            "file": path, "align": info, "weight": args.weight,
            "gain": args.gain, "chroma": args.chroma, "stats": res["stats"],
        }, human=args.human)
    finally:
        s.stop()


def cmd_align(args):
    s = _session(args, use_ir=True, codec="mjpg")
    try:
        _need_ready(s)
        info = s.do_align()
        if args.save:
            from camera_pipeline import CALIB_DIR
            CALIB_DIR.mkdir(parents=True, exist_ok=True)
            (CALIB_DIR / "align.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        return emit(True, "align", info, human=args.human)
    finally:
        s.stop()


def cmd_exposure(args):
    from camera_core import CameraController
    cc = CameraController(args.device, warmup=6, min_mean=None)
    try:
        with cc:
            if args.action == "get":
                data = {"device": str(cc.device), "exposure": cc.get_exposure(),
                        "range": cc.exposure_range()}
            elif args.action == "auto":
                data = {"device": str(cc.device), "set": cc.set_exposure(auto=True)}
            else:
                if args.value is None:
                    raise CliError("manual 模式需要 --value", "BAD_ARGS")
                data = {"device": str(cc.device), "set": cc.set_exposure(int(args.value))}
        return emit(True, "exposure", data, human=args.human)
    except Exception as e:
        if isinstance(e, CliError):
            raise
        raise CliError(str(e), "CAMERA_ERROR", "确认设备名（camctl.py devices）", EXIT_NODEV)


def cmd_dark_cal(args):
    from camera_pipeline import DarkNotCovered, DarkField
    s = _session(args, use_ir=True, codec="mjpg")
    try:
        _need_ready(s, need_ir=(args.target in ("ir", "both")), timeout=25)
        try:
            out = s.calibrate_dark(target=args.target, frames=args.frames)
        except DarkNotCovered as e:
            raise CliError(str(e), "NOT_COVERED",
                           "用不透光的物体完全遮住镜头（IR 窗口也要遮），再重试", EXIT_DARK)
        return emit(True, "dark-cal", out, human=args.human)
    finally:
        s.stop()


def cmd_dark_status(args):
    from camera_pipeline import DarkField
    data = {}
    for tag in ("rgb", "ir"):
        d = DarkField.load(tag)
        data[tag] = ({"ready": True, **d.meta} if d and d.ready else {"ready": False})
    return emit(True, "dark-status", data, human=args.human)


def cmd_dark_clear(args):
    from camera_pipeline import CALIB_DIR
    removed = []
    for p in CALIB_DIR.glob("dark_*.npz"):
        p.unlink()
        removed.append(str(p))
    return emit(True, "dark-clear", {"removed": removed}, human=args.human)


# ---- 水吸收测波段 -------------------------------------------------
WAVE_FILE = lambda: Path(__file__).resolve().parent / "calib" / "wavelength.json"   # noqa: E731


def _measure_illum(s, roi=None, frames=10) -> dict:
    from camera_pipeline import measure_illuminator
    try:
        return measure_illuminator(s.ir, roi=roi, frames=frames)
    except Exception as e:
        raise CliError(str(e), "NO_FRAME", "确认 IR 相机可用且未被占用")


def cmd_wavelength(args):
    import numpy as _np
    from camera_pipeline import wavelength_verdict
    path = WAVE_FILE()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    if args.step == "reset":
        path.unlink(missing_ok=True)
        return emit(True, "wavelength", {"reset": True}, human=args.human)

    if args.step == "status":
        return emit(True, "wavelength", wavelength_verdict(state), human=args.human)

    s = _session(args, use_ir=True, codec="mjpg")
    try:
        _need_ready(s)
        m = _measure_illum(s, roi=args.roi, frames=args.frames)
    finally:
        s.stop()

    state[args.step] = m
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    out = {"step": args.step, **m, "roi": args.roi or "中心 20% 方框"}
    if "empty" in state and "filled" in state:
        out.update(wavelength_verdict(state))
    else:
        out["next"] = ("把同一个容器装满水，位置保持不变，再跑 --step filled"
                       if args.step == "empty" else "先跑 --step empty 做参照")
    return emit(True, "wavelength", out, human=args.human)


def cmd_serve(args):
    from camera_webui import serve
    return serve(host=args.host, port=args.port, use_ir=not args.no_ir,
                 device=args.device, codec=args.codec, open_browser=args.open)


def cmd_shot_compare(args):
    """同一场景下 YUY2 / MJPG 各拍一张，附锐度/色度指标，用于码流选型。"""
    from camera_pipeline import CameraSession
    from camera_core import CameraController
    out = {}
    for cc in ("yuy2", "mjpg"):
        s = _session(args, use_ir=False, codec=cc)
        try:
            _need_ready(s, need_ir=False)
            time.sleep(0.6)
            frames = []
            for _ in range(6):
                if s.rgb.raw is not None:
                    frames.append(s.rgb.raw.copy())
                time.sleep(1 / 30.0)
            img = np.clip(np.mean(np.stack(frames), axis=0), 0, 255).astype(np.uint8)
            p = _save(img, Path(args.out_dir) / f"codec_{cc}.jpg")
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            out[cc] = {"file": p, "actual_fourcc": s.rgb.fourcc,
                       "sharpness": round(float(cv2.Laplacian(g, cv2.CV_32F).var()), 1),
                       "mean": round(float(img.mean()), 1)}
        finally:
            s.stop()
    return emit(True, "codec-compare", out, human=args.human)


# ═══════════════════════════════════════════════════════════════
# 参数解析
# ═══════════════════════════════════════════════════════════════
def build_parser():
    # 全局选项用 SUPPRESS 做默认，这样放在子命令前后都能生效
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--human", action="store_true", default=argparse.SUPPRESS,
                        help="人类可读输出（默认 JSON）")
    common.add_argument("--out-dir", default=argparse.SUPPRESS, help="输出目录（默认 captures）")
    common.add_argument("--device", default=argparse.SUPPRESS, help="RGB 选择符：rgb/名字子串/索引")
    common.add_argument("--no-dark", action="store_true", default=argparse.SUPPRESS,
                        help="不加载已存的暗场")

    p = argparse.ArgumentParser(
        prog="camctl", description="摄像头命令接口（agent 友好，默认 JSON 输出）",
        parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.set_defaults(human=False, out_dir="captures", device="rgb", no_dark=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    S = lambda name, **kw: sub.add_parser(name, parents=[common], **kw)   # noqa: E731

    d = S("devices", help="列出设备")
    d.add_argument("--probe", action="store_true", help="逐个开流探活（慢）")
    d.set_defaults(func=cmd_devices)

    d = S("capture", help="RGB 拍照")
    d.add_argument("-o", "--out"); d.add_argument("--warmup", type=int, default=10)
    d.add_argument("--codec", default="auto", choices=["auto", "yuy2", "mjpg"])
    d.add_argument("--no-check", action="store_true", help="关掉黑图哨兵")
    d.set_defaults(func=cmd_capture)

    d = S("ir", help="红外拍照")
    d.add_argument("-o", "--out"); d.add_argument("--raw", action="store_true", help="不做对比度拉伸")
    d.add_argument("--diff", action="store_true", help="输出补光差分（纯补光照明分量）")
    d.set_defaults(func=cmd_ir)

    d = S("diff", help="补光差分成像")
    d.add_argument("-o", "--out"); d.add_argument("--raw", action="store_true")
    d.set_defaults(func=cmd_diff)

    d = S("fuse", help="RGB+IR 融合出一张图")
    d.add_argument("-o", "--out"); d.add_argument("--weight", type=float, default=0.62)
    d.add_argument("--gain", type=float, default=1.6); d.add_argument("--chroma", type=float, default=2.0)
    d.add_argument("--align", default="keep", choices=["keep", "auto"])
    d.add_argument("--codec", default="auto", choices=["auto", "yuy2", "mjpg"])
    d.add_argument("--save-parts", action="store_true", help="同时存 rgb/ir 原图")
    d.set_defaults(func=cmd_fuse)

    d = S("align", help="自动标定配准")
    d.add_argument("--save", action="store_true", help="写入 calib/align.json")
    d.set_defaults(func=cmd_align)

    d = S("exposure", help="读/设曝光")
    d.add_argument("action", choices=["get", "auto", "manual"])
    d.add_argument("-v", "--value", type=int)
    d.set_defaults(func=cmd_exposure)

    d = S("dark-cal", help="暗场校准（需先遮住镜头）")
    d.add_argument("--target", default="both", choices=["rgb", "ir", "both"])
    d.add_argument("--frames", type=int, default=30)
    d.set_defaults(func=cmd_dark_cal)

    d = S("dark-status", help="查看暗场状态")
    d.set_defaults(func=cmd_dark_status)

    d = S("dark-clear", help="清除暗场")
    d.set_defaults(func=cmd_dark_clear)

    d = S("wavelength", help="水吸收法测 IR 波段")
    d.add_argument("--step", required=True, choices=["empty", "filled", "status", "reset"])
    d.add_argument("--roi", type=lambda s: tuple(int(x) for x in s.split(",")),
                   help="测量区域 x,y,w,h（IR 坐标，默认中心 20%%）")
    d.add_argument("--frames", type=int, default=10)
    d.set_defaults(func=cmd_wavelength)

    d = S("codec-compare", help="同场景对比 YUY2 与 MJPG 画质")
    d.set_defaults(func=cmd_shot_compare)

    d = S("serve", help="启动 WebUI")
    d.add_argument("--host", default="127.0.0.1"); d.add_argument("--port", type=int, default=8765)
    d.add_argument("--codec", default="auto"); d.add_argument("--no-ir", action="store_true")
    d.add_argument("--open", action="store_true", help="自动打开浏览器")
    d.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        return emit(False, args.cmd, error=e, human=args.human, exit_code=e.exit_code)
    except KeyboardInterrupt:
        return emit(False, args.cmd, error=CliError("被中断", "INTERRUPTED"), human=args.human)
    except Exception as e:
        if args.cmd != "serve":
            traceback.print_exc(file=sys.stderr)
        err = CliError("%s: %s" % (type(e).__name__, e), "EXCEPTION",
                       "加 --human 看细节；硬件类问题见 docs/06")
        return emit(False, args.cmd, error=err, human=args.human, exit_code=EXIT_ERROR)


if __name__ == "__main__":
    import os
    _rc = main()
    # 摄像头栈（Media Foundation / DirectShow）在解释器退出阶段可能卡住 ——
    # 实测有命令已经打印完 JSON 却迟迟不退出。这里强制收尾。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(_rc)
