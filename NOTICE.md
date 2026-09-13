
本仓库包含或依赖以下第三方组件，**许可证各不相同**：

| 组件 | 位置 | 许可证 | 说明 |
|---|---|---|---|
| Ultralytics YOLOv8 权重（导出为 ONNX） | `backend/models/yolov8n-face.onnx`、`yolov8n-pose.onnx` | **AGPL-3.0** | ⚠️ **本仓库整体因此按 AGPL-3.0 发布**。若你要以 MIT 等宽松协议发布，请**删除这两个文件**，改用 `backend/tools/export_models.py` 让使用者自行下载导出 |
| MediaPipe Hands 模型 | `backend/models/mediapipe/hand_landmarker.task` | Apache-2.0 | 可自由分发 |
| OpenCV Haar cascade | `backend/models/haarcascade_*.xml` | Apache-2.0 (OpenCV) | 可自由分发 |
| INFERNO 色表 | `backend/src/lut.go` | CC0 / 公有领域 | 由 matplotlib 色表生成后固化 |

## 本项目自身代码

`backend/src/*.go`、`backend/tools/*.py`、`backend/*.ps1` 等为原创代码，
在 **AGPL-3.0**（因捆绑 YOLO 权重）下发布。

**想改成 MIT 只需一步**：删掉 `backend/models/yolov8n-*.onnx` 两个文件，
再把本文件与 LICENSE 换成 MIT。旁路会提示缺模型，`tools/export_models.py` 可一键重新生成。
