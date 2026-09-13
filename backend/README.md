# camera_backend — 原生 Go 后端

**零第三方依赖、离线可构建、不依赖外网。** 只用 Go 标准库 + Windows syscall
直接调 Media Foundation / SetupAPI。

## 构建

```powershell
cd backend
.\build.ps1            # 等价于 GOFLAGS=-mod=mod GOPROXY=off go build -o camera_backend.exe .
```

`GOPROXY=off` 是刻意的：这个模块没有任何外部依赖，把代理关掉可以证明
**构建过程完全不需要网络**。

## 运行

```powershell
.\camera_backend.exe                    # 控制台 http://127.0.0.1:8770/
.\camera_backend.exe -open              # 顺手打开浏览器
.\camera_backend.exe -list              # 只列设备（原生枚举）
.\camera_backend.exe -probe             # 采集链路自检：设备/格式/抓帧/曝光
.\camera_backend.exe -selftest          # 算子 + 自动标定自检（不用相机）

.\camera_backend.exe -codec yuy2 -no-ir # 单路高质量（未压缩码流只能开一路）
.\camera_backend.exe -w 1280 -h 720     # 720p（必须 MJPG）
```

## 实测性能

### 算子层（单帧 640×480，不含相机 I/O）

| | Python + OpenCV | Go 串行版 | **Go 并行版（16 线程）** |
|---|---|---|---|
| 配准 warp | 1.69 ms | 4.20 ms | **2.08 ms** |
| 融合 fuse | 6.97 ms（LAB） | 2.10 ms（YCbCr） | **1.57 ms** |
| JPEG 编码 | **0.80 ms**（libjpeg-turbo） | 4.20 ms | 4.16 ms |
| **合计** | 9.47 ms | 10.50 ms | **7.80 ms** |

**说实话**：Go 这边是**纯标量**实现，单看算子并不占便宜 ——
OpenCV 的 `warpAffine` 是 SIMD 优化的 C++，`imencode` 用的 libjpeg-turbo
比 Go 的 `image/jpeg` 快 **5 倍**。Go 只在"融合"这步赢，因为它走 YCbCr、
避开了 LAB 往返。把 16 个核用起来（逐像素、行间无依赖，天然可并行）之后
才反超，而且**瓶颈已经从算子变成 JPEG 编码**（占 53%）。

> 并行度这里踩过一个坑：`parallelRows` 的阈值一开始写成"每带至少 32 行"，
> 480 行时直接退回串行，并行完全没生效。阈值应该是"每带至少 4 行"。

### 整机

| | Python 版 WebUI | **Go 版** |
|---|---|---|
| 拼图 MJPEG 流 | ~12 fps（3 条独立流） | **25.0 fps**（单流拼图，实测客户端 24.7） |
| 单帧渲染 | — | 12.0 ms |
| 空闲 CPU | — | **0%**（没人看流就不渲染） |
| 帧率上限 | 无（能跑多快跑多快） | 精确受控（60fps 目标实测 34.3） |

### 并发能力（USB 总线实测）

| 组合 | RGB | IR | 结论 |
|---|---|---|---|
| RGB **YUY2** + IR YUY2 | **0.0 fps** | 29.8 | ❌ 总线到顶，RGB 一帧都拿不到 |
| RGB **NV12** + IR YUY2 | **14.9 fps** | 29.8 | ✅ **推荐**：未压缩 + 带 IR |
| RGB MJPG + IR YUY2 | 14.9 fps | 29.8 | ✅ 最省带宽，可上 1080p |
| RGB NV12 720p + IR | — | — | ❌ 需 48.4 MB/s，超预算 |

推算依据：YUY2 = 16 bit/px（640×480@30 → 18.4 MB/s），NV12 = 12 bit/px（13.8 MB/s），
IR 固定 6.9 MB/s。预算是 25.3 超、20.7 稳定 → 取 **21 MB/s**。
所以 `normalize()` 是按**位宽算预算**判断，而不是简单禁止"未压缩 + IR"。

> 想自己验：`POST /api/diag-dual {"codec":"yuy2","seconds":8}` 会临时绕过规则、
> 实测后自动恢复 MJPG 并给出结论。

## 运动伪影与延迟（实测踩坑记录）

用户反馈"融合模式下移动有伪影、感觉比 Python 版还卡"，定位到三处，都不是浏览器的问题：

### ① IR 亮帧太旧 → 重影（主因）

IR 补光灯逐帧交替，要把它分成"亮/灭"两组。旧实现是从**最近 8 帧里挑均值最大**的当亮帧 ——
最坏会拿到 **266ms 前**的帧；人一动，IR 纹理和 RGB 就对不上，融合图出现重影。

改成**滑动中位数判相位 + 就地更新最近亮帧**，最坏延迟只有 2 帧：

| | 旧实现 | 现在 |
|---|---|---|
| IR 亮帧龄 | 最坏 266 ms | **52 ms** |
| RGB/IR 帧错配 | ~260 ms | **43 ms** |

（43ms 的剩余错配来自补光灯固有交替周期：亮帧每 66ms 才来一次。
想再压只能改用"当帧+亮度补偿"，但灭帧噪声大，与"效果优先"冲突，故不做。）

### ② 色阶参数逐帧重算 → 整幅亮度抽动

`transferIR` 的百分位统计量原先每帧都从当前画面重算 —— 人一动直方图就变，
**整幅画面的亮度跟着抽动**，看起来就像画面在"呼吸/漂移"。
现在对 IR/RGB 的 `p2/p98/mean` 做一阶 IIR（EMA，α=0.12）平滑。

### ③ 固定节拍渲染 → 白等最多 40ms

之前为了把帧率钉在目标值，用的是"固定节拍等定时器"；
帧到了也要等到下一个节拍才渲染，延迟白堆在那里。
现在是**帧到就渲染、只受帧率上限约束**（等待期间有新帧不会提前，但也不会多等一轮）。

| | 固定节拍 | 现在 |
|---|---|---|
| 目标帧率上限 | 25 fps | **60 fps**（帧到就出图，实测 44.6）|
| 客户端实测 | 24.7 fps | **44.6 fps**（= 输入帧率，每帧都渲染）|
| 单帧渲染 | 12.0 ms | 12.1 ms |
| 默认 JPEG 质量 | 80 | **88** |

> `fps` 滑杆是硬上限（默认 60 ≈ 输入帧率，等于每帧都渲染）。CPU 约 53% 单核。；`quality` 默认提到了 88 —— 后端现在算得动，
> 按"效果优先"就该把码率花在画质上。


## 色彩：为什么必须原生实现 LAB（而不是直接用 OpenCV）

用户反馈"颜色不对、偏黄发脏"。用**同一帧输入**（Go 后端自己存的 rgb_*/ir_*）跑两套实现对比：

| | 亮度 | LAB彩度 | 色偏 b（黄-蓝） | 与 Python 参考的 PSNR |
|---|---|---|---|---|
| 原始 RGB | 111.7 | 8.88 | +4.27 | — |
| Go **YCbCr** 融合 | 95.0 | 12.79 | **+8.43**（旧） | 19.4 dB |
| Go **LAB** 融合（现在） | 95.0 | 12.79 | **+6.47** | **32.8 dB** |
| Python LAB 融合 | 95.4 | 12.82 | +5.64 | — |

根因是**数学层面的**：YCbCr 的 Cb/Cr 是**伽马编码之后**的色差，LAB 的 a/b 作用在**线性光**上。
同样"乘 1.6"放大色度，在伽马空间放大 1.6 倍折算到线性空间会放大得**远超 1.6** ——
实测把原始色偏 +4.27 放大成 +8.43（2.9x），而 LAB 只放大到 +5.64（1.3x）。
表现就是"融合之后整体偏黄、发脏"。

修法：在 `lab.go` 里**原生实现 OpenCV 那套 sRGB/D65 LAB**（8bit 约定 L8=L*·255/100、a8=a*+128），
融合改成 `blur(a,b) → (x-128)*chroma+128 → L 与 IR 混合`，色度模糊用 OpenCV 同款
σ=0.3*((ksize-1)*0.5-1)+0.8 的可分离高斯（ksize=2*ChromaBlur+1，默认 7）。

代价与收益：单帧 12.0 → 18.1 ms（+6ms），但**客户端仍是 44.6 fps**（输入帧率上限），
而 Go 与 Python 参考实现的 PSNR 从 **19.4 dB 提升到 32.8 dB**、平均色差 21.6 → 3.3。

> **为什么不直接 cgo 调 OpenCV**：opencv-python 装机时确实带了 opencv_world DLL，技术上能链。
> 但那样就破坏了"零依赖、离线可构建"（需要 cgo+gcc+40MB DLL），而我真正需要的只有 LAB 换算
> —— 那是 `lab.go` 里约 150 行纯 Go。结论：不引依赖，自己实现，效果对齐（32.8dB）。

### 融合后偏色的进一步校准（白平衡）

修完 LAB 之后还剩一个**实现缺陷**：色度是"围绕中性灰(128)"放大的，
于是画面**本身的色偏也被一起放大**（实测原始 a=+2.24/b=+5.60 被放大到 +7.37/+6.47）。

改成 **围绕"本帧平均色度"放大**，再加一个白平衡把整体色偏按需拉回中性：

```
a_out = (a - refA) * chroma + refA * (1 - wb)
```

* `refA` = 场景色偏参考（**截尾均值**：去掉两端各 10% 再平均；纯中位数对偏斜分布不够、
  纯均值容易被高饱和物体拽跑），并做一阶 IIR 平滑避免色温跳动
* `wb=0` → 保留现场色偏，但不再放大它
* `wb=1` → 完全中性化（灰世界白平衡）
* 默认 `0.6`（保留四成气氛）

实测（同一场景，UI 里有「白平衡」滑杆，并实时显示测到的色偏）：

| | 亮度 | LAB彩度 | 色偏 a | 色偏 b |
|---|---|---|---|---|
| 原始 RGB | 118.1 | 9.21 | +2.24 | +5.60 |
| 融合 wb=0 | 105.2 | 10.77 | +1.51 | +6.64 |
| 融合 wb=0.6（默认） | 105.3 | 9.53 | +0.77 | +3.26 |
| 融合 wb=1.0 | 107.7 | 9.17 | -0.02 | +1.08 |




## 目录结构

```
backend/
├── camera_backend.exe          产物（build.ps1 生成，已 gitignore）
├── go.mod
├── main.go                     入口：参数解析 / 自检 / 旁路拉起
├── mf.go          Media Foundation（COM 走 syscall）：枚举/打开/格式/曝光/白平衡/画质控制
├── devices.go     SetupAPI 设备发现（IR 相机只能这样找到）
├── camera.go      相机会话：一台相机一个 LockOSThread goroutine + 命令队列
├── yuv.go         相机原生格式（MJPG/YUY2/NV12）→ YCbCr 4:2:0
├── lab.go         LAB 色彩空间（对齐 OpenCV）＋融合/白平衡/gamma/暗部去彩
├── image.go       算子：warp / 融合 / 差分 / 拉伸 / 羽化 / 镜像
├── align.go       自动配准（梯度图 + 粗搜 + 细搜）
├── face.go        原生 Viola-Jones 人脸检测（cascade XML 当数据文件用）
├── person.go      人物存在/位置（IR 差分 + 运动，无模型）
├── vision.go      旁路推理结果的接收与叠加绘制（人脸/骨架/手部）
├── engine.go      会话状态 / 带宽规则 / 渲染循环 / 各视图合成
├── web.go         HTTP 服务 + 嵌入式前端（单条 MJPEG 拼图流）
├── probe.go       采集链路自检（-probe / -wb / -facetest）
├── lut.go         INFERNO 色表（由 OpenCV 生成后固化）
├── build.ps1      离线构建（GOPROXY=off）
├── start.ps1      一键启动（检依赖 → 停旧实例 → 起后端+旁路 → 开浏览器）
├── models/        ONNX 模型 + MediaPipe task（运行时用，已入库）
├── tools/         Python 脚本：export_models.py / vision_sidecar.py / bench_providers.py
├── captures/      运行期存图（gitignore，可整目录删）
├── calib/         暗场校准 / 波段测量状态（gitignore）
└── var/           运行期产物说明
```

**拼图布局**：主视图 + 一排 4 个缩略图（**RGB / IR / 补光差分 / 边缘图**），
按画布宽度**均分 4 列**填满整行（原来 3×160 只占 496px，右边空 144px）。
边缘图 = 对亮度做 Sobel 梯度幅值并归一化 —— 一眼看对焦/配准/细节，且不需要额外相机通道。

## 怎么启动

```powershell
cd D:\MyProjects\AI\camera_use\backend
.\start.ps1
```

`start.ps1` 会依次：检查可执行文件（没有就构建）→ 检查旁路模型与 python 包 →
**停掉旧实例并清理残留的旁路进程** → 按推荐配置启动（NV12 640×480 + IR + 旁路 8fps）→
等真正出帧了再报"就绪"→ 打开浏览器。

### 参数

| 命令 | 说明 |
|---|---|
| `.\start.ps1` | 推荐：NV12 640×480 + IR + YOLO 旁路 |
| `.\start.ps1 -NoVision` | 只跑原生功能（人脸回退到内置 Viola-Jones，不启 Python） |
| `.\start.ps1 -Hires` | 640×480 **YUY2** 单路最高画质（IR 自动关：未压缩+IR 带宽不够） |
| `.\start.ps1 -Codec mjpg -W 1280 -H 720` | 720p / 1080p（未压缩码流上不去） |
| `.\start.ps1 -NoBrowser` | 不开浏览器 |
| `.\start.ps1 -VisionFPS 15` | 旁路推理频率（实测整周期 52~75ms，15fps 有点紧） |

停止：`Get-Process camera_backend | Stop-Process -Force`

### 直接跑 exe（不用脚本）

```powershell
.\camera_backend.exe -open                       # 最简：MJPG + IR，不开旁路
.\camera_backend.exe -codec nv12 -vision -open   # 推荐：未压缩 + YOLO 旁路
.\camera_backend.exe -help                       # 看全部参数（注意 -h 是"高度"不是 help）
```

诊断（不需要相机）：`-list` 列设备 / `-probe` 采集链路自检 / `-selftest` 算子与标定回归 /
`-wb` 探白平衡 / `-facetest 图片` 跑一次人脸检测。

### 首次使用建议

1. **暗场校准**：遮住镜头（RGB 和 IR 两个窗口都要遮）→ 点「暗场校准」。校完自动生效并保存，
   下次启动自动加载（状态栏显示 `暗场 RGB✓/IR✓`）
2. **相机白平衡**：点「自动校准」扫两轮取最中性档。**灯光一变就要重跑**（或直接用自动档）
3. 想让 YOLO/手部跑得最快：`pip install onnxruntime-directml mediapipe`
   （实测本机 DirectML 比 CUDA 还快 3.7 倍，见下）

## 人脸检测（原生 Viola-Jones，零依赖）

`face.go` 自己实现了 Viola-Jones：把 OpenCV 装的 `haarcascade_*.xml` **当数据文件**用
（拷到 `models/`，908KB），用 `encoding/xml` 解析 + Go 自己算积分图与级联判定。
主程序依然零外部依赖、离线可构建。

**用途**：让"人脸区域"参与成像优化 —— 肤色比整幅场景更接近中性参考，
所以**人脸区域测白平衡比全帧灰世界准得多**（实测色偏 a/b 明显更稳）。

三个开关（UI 里勾）：`face_detect` 启检测 / `face_wb` 用人脸区域测色偏 / `face_box` 画框。

后台 1/2 分辨率 ~5fps 跑（约 19ms/次），**不占渲染帧率**；不在渲染循环里做全分辨率检测
（一次 36ms 会把 45fps 拖垮）。

### 踩的两个坑（都很有代表性）

**① 归一化因子少乘了 √N**
训练时特征是按 `σ_pixel × N` 归一的，我原来只乘了 `sqrt(Σx²-(Σx)²/N)`，
阈值小了约 115 倍（窗口 115×115），于是每个弱分类器的判定退化成常数 ——
表现是**合成图乱检出 2720 张、真脸 0 张**。改成 `sqrt(variance*N)` 后与 OpenCV 结果一致。

**② 尺度步长 1.15 会整段跳过正确尺度**
实测同一张图（脸 72px）：步长 1.15 → **任何 minNeighbors 都是 0 张**；
1.08 → 稳定 1 张；1.03 → 1 张。Haar 级联对尺度错配很敏感
（25 级里任何一级不过就整体否决），所以步长必须细。现在用 **1.08 + 3 票投票分组**
（`groupFaces` 等价于 OpenCV 的 minNeighbors，顺带干掉单次误检）。

结果：本机实测与 OpenCV 的 `detectMultiScale` 检出位置一致（差几像素），速度更快
（1/2 图 7~19ms vs OpenCV ~60ms）。

## 暗场校准：会保存、会默认启用、有明确提示

- **保存**：`calib/dark_<rgb|ir>.gob`（约 2MB：每像素的暗场均值 + 热点图）
- **默认启用**：`Start()` 里 `loadDark("rgb"/"ir")`，**每次启动自动加载**，
  状态栏显示 `暗场 RGB✓/IR✓`；`/api/state` 的 `dark_rgb`/`dark_ir` 可直接查
- **完成提示**：UI 里给一个高对比度横幅，写明保存路径、采集帧数、均值、
  "已保存并即刻生效 / 下次启动自动加载"，以及撤销方式（「清暗场」）
- 采集时会校验画面亮度（RGB>32 / IR>45 直接拒），避免没遮住镜头就存了一坨垃圾


## 直接命令相机白平衡（IAMVideoProcAmp）

`IAMCameraControl` 只管曝光/对焦/光圈；**白平衡、增益、饱和度、背光补偿**在
`IAMVideoProcAmp` 上（两者 vtable 布局相同：3=GetRange 4=Set 5=Get，辅助函数可复用）。

探测结果（本机 HP Wide Vision FHD）：

```
[0] 亮度      -64..64    caps=Manual        当前 0
[1] 对比度      0..100    caps=Manual        当前 50
[2] 色相     -180..180   caps=Manual        当前 0
[3] 饱和度      0..100    caps=Manual        当前 64
[4] 锐度        0..100    caps=Manual        当前 0
[5] Gamma     100..500   caps=Manual        当前 300
[7] 白平衡   2800..6500  caps=Auto+Manual   当前 4600（自动）   ← 关键
[8] 背光补偿    0..2      caps=Manual        当前 1
[9] 增益        0..128    caps=Manual        当前 0
```

**实测色温对色偏的影响**（关掉软件白平衡，纯看相机；值为相机"认为"的场景色温）：

| 设置 | 亮度 | LAB彩度 | 色偏 a | 色偏 b |
|---|---|---|---|---|
| 自动 | 98.5 | 9.25 | +4.40 | +4.08 |
| 2800K | 82.5 | 23.03 | -3.95 | -19.27 |
| 3540K | — | — | -2.09 | -5.82 |
| **3910K** | — | — | **-1.52** | **+0.36** |
| 4280K | — | — | -1.62 | +5.90 |
| 6500K | 90.5 | 40.90 | -8.06 | +39.10 |

规律：**值越低画面越蓝、越高越黄**；而且拉得越远**饱和度指数级放大**
（2800K 时饱和度 152），所以它是粗调，细调仍交给软件白平衡。

**自动校准**（`POST /api/wb/auto-tune`，UI 里「自动校准」按钮）：
两阶段搜索 —— 先粗搜 6 档，再在最优点附近精搜 7 档；每档都**等 3 帧真正的新画面**
再测（不 sleep 赌时间），最小化 |a|+|b|。

实测：相机自动 8.48 → **校准到 3910K 后 1.88**（好 4.5 倍），
即**光靠相机自己就把暖光色偏修掉大半**，软件白平衡只需做最后一点微调。

> 一维旋钮、二维目标（a 与 b 不能同时归零），所以只能最小化 |a|+|b|。
> 剩下的残差交给软件白平衡滑块，两者叠加使用。


## 旁路视觉推理：YOLO 人脸 + YOLOv8n-pose 骨架

**定位**：这类 DNN 重活不塞进 Go 主程序（保持零依赖、离线可构建）。
旁路进程只做三件事：从后端拿**干净帧** → onnxruntime 推理 → 把结构化结果 POST 回去。
后端负责画框与状态；**旁路不跑也完全不影响其它功能**（人脸自动回退到原生 Viola-Jones）。

```powershell
python tools/export_models.py            # 一次性：.pt → ONNX（需 ultralytics，仅此时联网）
python tools/vision_sidecar.py           # 常驻：默认 5fps，取融合图做推理
python tools/vision_sidecar.py --image x.jpg   # 单张图联调
```

- 模型：`models/yolov8n-face.onnx`(12MB) + `models/yolov8n-pose.onnx`(13MB)，运行时零联网
- 帧源：`/frame.jpg?view=fuse`（**干净帧，不叠加任何框**，免得把框当人；融合图更亮，暗光下更稳）
- 回传：`POST /api/vision`，后端存 3 秒（过期不画，旁路挂了不会留旧框）
- 绘制：人脸绿框 / 人形青框 / 骨架黄线 / 关节点品红（YCbCr 里也能画彩色：Y 逐像素、Cb/Cr 每 2×2）
- 实测（CPU，4 线程）：face 22~31ms + pose 24~36ms ≈ 60ms/帧；后端收到的延迟约 110ms
- UI 开关：人脸分组里的「YOLO 人脸」「YOLO 骨架」；状态栏显示旁路是否在跑

## 人物存在/位置（IR 差分 + 运动，无模型）

跟人脸检测共用一次下采样：**IR 差分**（亮−灭，皮肤/近物反射强 → 静止的人也能测到）
\+ **运动检测**（相邻帧灰度差 → 动的人能定位）合成到 8×6 网格。
输出 `present / coverage / box / zone(左中右) / motion / ir_diff`，
去抖 = 连续 2 帧判有、连续 8 帧没动静才判无。

### 运维提醒：相机白平衡是**跟灯光绑定**的

实测踩过：把相机 WB 校准到 3910K 手动档后，**灯光一变（比如关灯/换灯）画面立刻偏蓝**
（色偏 b 从 +2.2 掉到 −8.3）。所以：
- 灯光稳定的场景 → 手动档 + 「自动校准」很准
- 灯光会变 → **用自动档**，或者变了就重跑一次「自动校准」（3~8 秒）


### 推理后端选型（本机实测：Ryzen 9 7945HX + RTX 5070 Ti 16GB）

| 模型（640×640） | CPU(4T) | **DirectML** | CUDA 12 | TensorRT |
|---|---|---|---|---|
| yolov8n-face | 27.5 ms | **1.39 ms** | 5.14 ms | 27.1 ms |
| yolov8n-pose | 34.7 ms | **2.37 ms** | 5.11 ms | 29.2 ms |

**结论：本机用 DirectML 最快**（比 CUDA 还快 3.7 倍，比 CPU 快 20 倍）。
CUDA 慢在小模型上**每次调用的 kernel launch + 显存往返开销**占比过高；
TensorRT 这里更慢（每次都在建引擎，没吃到缓存）。

**精度**（CPU fp32 作参考，同输入同模型，输出幅度相对差）：
DirectML **0.048% / 0.060%**，CUDA **0.165% / 0.177%** —— 都在 0.2% 以内，
折算到 640 像素坐标是亚像素级，对检测结果无实际影响。

复现：`python tools/bench_providers.py`（每个后端在**独立子进程**里测 ——
onnxruntime 的模块缓存会让 `sys.path.insert` 失效，同一个进程里换不了后端，这个坑踩过）。

> 环境说明：系统装的是 CUDA Toolkit **13.2**，但 ORT 的 CUDA EP 要的是 **CUDA 12** 运行时，
> 靠 pip 的 `nvidia-*-cu12`（cudart 12.9 / cuDNN 9.9 / cuBLAS 12.9）提供。
> `onnxruntime-gpu` 用 `pip install --target .ortgpu` 独立安装，不破坏 DirectML 环境。

## 手部关键点与手势（MediaPipe Hands）

`models/mediapipe/hand_landmarker.task`（7.46MB，float16），在**同一张帧**上跑，走既有旁路链路。

- **21 点关键点** → **五指伸展判定**：判据是「指尖到腕关节的距离 vs 中间关节到腕关节的距离」，
  与手的朝向无关（比"指尖 y 坐标"那种判据稳得多）；拇指另用「指尖离小指根」判断
- **手势命名**：五指 0/1 组合映射（五指张开 / 握拳 / 剪刀手 / 竖大拇指 / 六 / Shaka / 三 / 四 / OK …）
- 实测 CPU 16~26ms/帧；`--no-hands` 可关
- 实测（举着手机那张）：`Left 五指=[1,1,1,1,1] → 五指张开`、`Right 五指=[1,0,0,0,0] → 竖大拇指` ✓

### 踩坑：/api/vision 的 json tag 冲突

处理器原本把 `VisionResult` 嵌进匿名结构**又重复声明 frame_w/frame_h**，
json tag 冲突 → 解码报 `cannot unmarshal object into Go struct field .frame_h`，
**旁路回传全部静默失败**。现在直接解码 `VisionResult`。
另一个相关现象：`/api/state` 的统计**每秒才刷一次**，回传后立刻查会看到旧值（不是没收到）。

## 事件驱动（不轮询、不 sleep 循环）

| 位置 | 做法 |
|---|---|
| 相机 → 渲染 | 每出一帧往 `wake`（容量 1）投一个信号；满则丢弃（丢的是重复通知，不是帧） |
| 渲染节拍 | **帧到就渲染**，只受帧率上限约束（不再固定等定时器，那是白加延迟） |
| 帧率上限 | 硬上限：不让新帧信号打断节流（否则 15+30fps 输入把流推到 35fps，滑杆失效） |
| MJPEG 客户端 | `sync.Cond` 广播：`publish()` 后 `Broadcast()`，N 个客户端只等一次、空闲零唤醒 |
| 客户端断开 | 看门狗 goroutine 在 `ctx.Done()` 时唤醒 `Cond`（`Cond` 本身不可取消） |
| 暗场 / 波段采样 | `WaitFrame(ctx, lastSeq)` 阻塞等新帧，不 sleep |
| 相机命令 | 带缓冲 channel 投递闭包到相机线程执行（COM 套间绑线程） |

## 文件

| 文件 | 作用 |
|---|---|
| `mf.go` | Media Foundation 封装（COM 走 syscall）：枚举 / MFCreateDeviceSource / IMFSourceReader / IAMCameraControl |
| `devices.go` | SetupAPI 设备发现 —— **IR 相机只能这样找出来**（MF 枚举看不到它） |
| `camera.go` | 相机会话：一台相机一个 `LockOSThread` 的 goroutine + 命令队列（COM 套间绑线程） |
| `yuv.go` | 相机原生格式（MJPG / YUY2 / NV12）→ `image.YCbCr` 4:2:0 |
| `image.go` | 算子：warp / 融合 / 差分 / 拉伸 / 羽化 |
| `align.go` | 自动配准：梯度图 + 1/4 分辨率粗搜 + 全分辨率精搜 |
| `engine.go` | 会话状态、带宽规则、渲染循环 |
| `web.go` | HTTP + 嵌入式前端（单条 MJPEG 拼图流） |
| `probe.go` | 采集链路自检 |
| `lut.go` | INFERNO 色表（由 OpenCV 生成后固化，避免运行时依赖） |

## 为什么这么设计

**为什么用 MF 而不是 DirectShow/OpenCV**
实测 FHD 相机在 MF 通道下 `IAMCameraControl` 完全可用
（`Exposure GetRange` → `min=-10 max=-2 default=-5 caps=0x3`，Auto/Manual 都能设）
—— 这正是"OpenCV 打开画面发暗"的根因所在。同时 IR 相机（Windows Hello）
**只能**用 MF + 设备符号链接打开，它压根不在 DirectShow 枚举里。

**为什么全程走 YCbCr 4:2:0**
MJPG 解出来就是 `*image.YCbCr`，YUY2/NV12 也能零成本转成它；
融合只需要改 Y 平面，色度天然减半，最后直接命中 `image/jpeg` 的快速路径 ——
全程不做彩色空间往返。

**为什么一台相机一个绑线程的 goroutine**
COM 有套间（apartment）概念，接口对象只能在创建它的线程上调用。
用 `Do(func(cam *MFCamera) error)` 把命令投递进相机线程执行，
采集与设置共用同一个循环，就不会出现并发访问 `IMFSourceReader` 的问题。

**实测性能**（640×480 RGB + 340×340 IR，32 核）

```
warpGray      4.2 ms
fuseYCbCr     2.1 ms
jpeg encode   4.2 ms
合计         10.5 ms → 单核 95 fps
拼图流        24.7 fps（含 3 个缩略图的 640×604 拼图）
RGB 14.9 fps(MJPG) + IR 29.8 fps(YUY2) 并发
```

## 码流选择（实测）

| 格式 | 类型 | 最高分辨率 | 说明 |
|---|---|---|---|
| **MJPG** | 每帧独立 JPEG，有损 | 1920×1080 | 与 IR 并发**必须**用这个 |
| **NV12** | YUV 4:2:0 未压缩（半平面） | 1920×1080 | 画质好，但带宽重 |
| **YUY2** | YUV 4:2:2 未压缩（打包） | **只有 640×480** | 画质最好 |

> "不是 YUV 吗" —— 是。YUV 是色彩模型，YUY2 / NV12 / I420 是**内存布局**
> （打包 vs 平面、色度怎么抽样）。FOURCC 里只是没有叫 "YUV" 的名字而已。

单路同场景对比（锐度/色度细节/块噪声）：

| | 梯度能量(锐度) | 色度细节 | 高频块噪声 |
|---|---|---|---|
| YUY2 | **192.4** | **3.35** | **0.497** |
| MJPG | 165.0 | 3.12 | 0.565 |

**但两路同开时 USB 带宽会打架**：RGB 用未压缩格式会从 30fps 崩到 **1.3fps**。
所以后端硬性规定：**未压缩码流只能开一路** —— 想同时开 IR，就切 MJPG。
（API 会自动关掉 IR 并返回说明，前端也会提示。）

## HTTP 接口（也能给 agent 直接调）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 控制台页面 |
| GET | `/mjpg` | MJPEG 拼图流（主视图 + RGB/IR/差分缩略图，单流） |
| GET | `/snapshot.jpg` | 当前画面单帧 JPEG（高质量） |
| GET | `/api/state` | 参数 + 统计 JSON |
| POST | `/api/set` | `{"mode":"fuse","weight":0.62,"gain":1.6,"chroma":1.6,"scale":1.3,"tx":98,"ty":18,"rot":0,"stretch":0.5,"fps":25,"thumbs":true}` |
| POST | `/api/config` | `{"codec":"mjpg","w":1280,"h":720,"ir":true}`（带带宽规则） |
| GET/POST | `/api/exposure` | 读/设曝光：`{"value":-3,"auto":false}` |
| POST | `/api/align` | 自动配准，返回 `{scale,tx,ty,score}` |
| POST | `/api/dark` | 暗场校准 `{"target":"both","frames":30}`（没遮住会报错） |
| POST | `/api/dark/clear` | 清除暗场 |
| POST | `/api/snapshot` | 存图，返回文件路径 |
| POST | `/api/wavelength` | 水吸收测波段 `{"step":"empty|filled|status|reset"}` |
