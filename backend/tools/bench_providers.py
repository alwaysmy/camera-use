# -*- coding: utf-8 -*-
"""各后端速度 + 精度：每个后端在独立子进程里测（避免 onnxruntime 模块缓存冲突）。"""
import json
import os
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))  # backend/
import subprocess
import sys

# 子进程模式：按指定路径加载 onnxruntime 并跑基准
if len(sys.argv) > 1 and sys.argv[1] == "--child":
    mode = sys.argv[2]
    if mode == "ortgpu":
        sys.path.insert(0, r"<REPO>\backend\.ortgpu")
    import glob
    import time

    import numpy as np
    import onnxruntime as ort

    SP = r"<PYTHON>\Lib\site-packages\nvidia"
    for d in glob.glob(os.path.join(SP, "*", "bin")):
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]

    out = {"mode": mode, "version": ort.__version__, "providers": ort.get_available_providers(), "bench": {}}
    M = r"<REPO>\backend\models"
    for name, shape in (("yolov8n-face.onnx", [1, 3, 640, 640]), ("yolov8n-pose.onnx", [1, 3, 640, 640])):
        p = os.path.join(M, name)
        for prov in ort.get_available_providers():
            if prov == "AzureExecutionProvider":
                continue
            try:
                so = ort.SessionOptions()
                so.intra_op_num_threads = 4
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                so.log_severity_level = 3
                s = ort.InferenceSession(p, so, providers=[prov])
                x = np.random.rand(*shape).astype(np.float32)
                n = s.get_inputs()[0].name
                for _ in range(3):
                    s.run(None, {n: x})
                ts = []
                for _ in range(15):
                    t = time.perf_counter()
                    s.run(None, {n: x})
                    ts.append((time.perf_counter() - t) * 1000)
                ts.sort()
                o = s.run(None, {n: x})[0]
                out["bench"][f"{name}|{prov}"] = {"ms": round(ts[len(ts) // 2], 2),
                                                 "sum": float(np.abs(o).sum()), "max": float(np.abs(o).max())}
            except Exception as e:  # noqa: BLE001
                out["bench"][f"{name}|{prov}"] = {"err": str(e)[:150]}
    print("###JSON###" + json.dumps(out))
    sys.exit(0)

# 父进程：分别跑两个子进程
here = os.path.abspath(__file__)
res = {}
for mode in ("normal", "ortgpu"):
    r = subprocess.run([sys.executable, here, "--child", mode], capture_output=True, text=True, timeout=600)
    for line in r.stdout.splitlines():
        if line.startswith("###JSON###"):
            res[mode] = json.loads(line[len("###JSON###"):])
    if mode not in res:
        print(f"[{mode}] 失败:", (r.stderr or r.stdout)[-300:])

for mode, d in res.items():
    print(f"\n=== {mode} ===")
    print("  onnxruntime", d["version"], "| providers:", d["providers"])
    for k, v in d["bench"].items():
        name, prov = k.split("|")
        if "err" in v:
            print(f"  {name.replace('.onnx',''):<16s} {prov:<24s} 失败: {v['err'][:70]}")
        else:
            print(f"  {name.replace('.onnx',''):<16s} {prov:<24s} {v['ms']:>7.2f} ms   |sum|={v['sum']:.1f}")

# 精度对比：CPU(参考) vs 各加速后端（同一输入、同一模型）
print("\n=== 精度对比（CPU fp32 作参考；DirectML 已知走 fp16）===")
for mode, d in res.items():
    for name in ("yolov8n-face.onnx", "yolov8n-pose.onnx"):
        cpu = d["bench"].get(f"{name}|CPUExecutionProvider", {}).get("sum")
        for prov in ("DmlExecutionProvider", "CUDAExecutionProvider"):
            b = d["bench"].get(f"{name}|{prov}")
            if b and "sum" in b and cpu:
                print(f"  {mode:<8s} {name.replace('.onnx',''):<16s} {prov:<22s} "
                      f"输出幅度差 {abs(b['sum']-cpu)/cpu*100:6.3f}%")
