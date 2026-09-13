// image.go — 图像算子（全部在 YCbCr 4:2:0 上工作）
package main

import (
	"image"
	"math"
	"runtime"
	"sync"
)

// ───────────────────────── 并行小工具 ─────────────────────────
//
// 单帧算子都是"逐像素、行间无依赖"的，而 Go 这边是纯标量实现
// （OpenCV 那些是 SIMD 优化过的 C++）。所以这里按行分带并行，
// 把 16~32 个核用起来 —— 这是标量实现唯一能追平 SIMD 的办法。

var numWorkers = func() int {
	n := runtime.NumCPU()
	if n > 8 {
		n = 8
	}
	if n < 1 {
		n = 1
	}
	return n
}()

// parallelRows —— 把 [0,h) 按行分给 n 个 goroutine 跑。
// 阈值只要求"每带至少 4 行"：设太大了（比如 32 行/带）在 480 行时反而会退回串行，
// 这个坑实测踩过（并行完全没生效）。
func parallelRows(h int, fn func(y0, y1 int)) {
	w := numWorkers
	// 按行数自适应：640×480 这种小图起 16 个 goroutine，调度与内存带宽争用的
	// 开销会反超收益 —— 而且 **CPU 时间（统计所有线程）会明显变大**。
	// 让每个 goroutine 至少干 96 行：640×480 这种小图，起十几个 goroutine 的
	// 调度与内存带宽争用开销会远超收益 —— **CPU 时间（统计所有线程）反而成倍变大**。
	if lim := h / 96; lim < w {
		w = lim
	}
	if w < 1 {
		w = 1
	}
	if w <= 1 || h < 4*w {
		fn(0, h)
		return
	}
	var wg sync.WaitGroup
	band := (h + w - 1) / w
	for y0 := 0; y0 < h; y0 += band {
		y1 := y0 + band
		if y1 > h {
			y1 = h
		}
		wg.Add(1)
		go func(a, b int) {
			defer wg.Done()
			fn(a, b)
		}(y0, y1)
	}
	wg.Wait()
}

// parallelCols —— 按列分带（纵向模糊用）
func parallelCols(w0 int, fn func(x0, x1 int)) {
	n := numWorkers
	if lim := w0 / 96; lim < n {
		n = lim
	}
	if n < 1 {
		n = 1
	}
	w := w0
	if n <= 1 || w < 4*n {
		fn(0, w)
		return
	}
	var wg sync.WaitGroup
	band := (w + n - 1) / n
	for x0 := 0; x0 < w; x0 += band {
		x1 := x0 + band
		if x1 > w {
			x1 = w
		}
		wg.Add(1)
		go func(a, b int) {
			defer wg.Done()
			fn(a, b)
		}(x0, x1)
	}
	wg.Wait()
}

// flipYCbCr —— 镜像翻转（水平/垂直/两者）。
// 放在**输出端**做：镜像只是"给你看的画面"，不该影响配准变换与融合数学
// （如果在输入端翻，tx 之类的配准参数就得跟着换算，徒增出错面）。
func flipYCbCr(src *image.YCbCr, horiz, vert bool) *image.YCbCr {
	if !horiz && !vert {
		return src
	}
	w, h := src.Rect.Dx(), src.Rect.Dy()
	cw, ch := (w+1)/2, (h+1)/2
	dst := image.NewYCbCr(src.Rect, image.YCbCrSubsampleRatio420)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			sy := y
			if vert {
				sy = h - 1 - y
			}
			so := sy * src.YStride
			do := y * dst.YStride
			if horiz {
				for x := 0; x < w; x++ {
					dst.Y[do+x] = src.Y[so+w-1-x]
				}
			} else {
				copy(dst.Y[do:do+w], src.Y[so:so+w])
			}
		}
	})
	for y := 0; y < ch; y++ {
		sy := y
		if vert {
			sy = ch - 1 - y
		}
		so := sy * src.CStride
		do := y * dst.CStride
		if horiz {
			for x := 0; x < cw; x++ {
				dst.Cb[do+x] = src.Cb[so+cw-1-x]
				dst.Cr[do+x] = src.Cr[so+cw-1-x]
			}
		} else {
			copy(dst.Cb[do:do+cw], src.Cb[so:so+cw])
			copy(dst.Cr[do:do+cw], src.Cr[so:so+cw])
		}
	}
	return dst
}

// planePercentile —— 灰度平面（可能带 stride）的百分位
func planePercentile(p []byte, stride, w, h int, pct float64) float64 {
	var hist [256]int
	n := 0
	for y := 0; y < h; y++ {
		row := y * stride
		for x := 0; x < w; x++ {
			hist[p[row+x]]++
			n++
		}
	}
	if n == 0 {
		return 0
	}
	lim := int(pct / 100.0 * float64(n))
	acc := 0
	for i := 0; i < 256; i++ {
		acc += hist[i]
		if acc >= lim {
			return float64(i)
		}
	}
	return 255
}

// transferIR —— 把 IR 的色阶**对齐到 RGB 的亮度分布**。
//
// 踩坑之后换的做法：原来是"百分位拉伸到满量程 → 再乘 gain"，
// 结果 IR 均值被抬到 ~128，62% 权重一混就整片削顶（人脸发白、饱和度掉）。
// 现在：把 IR 的 [p2,p98] 线性映射到 RGB 亮度的 [p2,p98]，
// 融合图影调与 RGB 一致、只借 IR 的纹理和暗部；gain 变成围绕均值的微调。
func transferIR(ir []byte, irStride, w, h int,
	irP2, irP98, yP2, yP98, yMean, gain, blend float64) {
	if irP98-irP2 < 1 {
		return
	}
	scale := (yP98 - yP2) / (irP98 - irP2)
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			row := y * irStride
			for x := 0; x < w; x++ {
				raw := float64(ir[row+x])
				v := (raw-irP2)*scale + yP2
				v = (v-yMean)*gain + yMean
				if blend < 1.0 {
					v = raw*(1-blend) + v*blend
				}
				ir[row+x] = clamp8(v)
			}
		}
	})
}

func clamp8(v float64) uint8 {
	if v <= 0 {
		return 0
	}
	if v >= 255 {
		return 255
	}
	return uint8(v + 0.5)
}

func clampInt(v int) uint8 {
	if v < 0 {
		return 0
	}
	if v > 255 {
		return 255
	}
	return uint8(v)
}

func cloneYCbCr(src *image.YCbCr) *image.YCbCr {
	dst := image.NewYCbCr(src.Rect, image.YCbCrSubsampleRatio420)
	copy(dst.Y, src.Y)
	copy(dst.Cb, src.Cb)
	copy(dst.Cr, src.Cr)
	return dst
}

func newGrayYCbCr(w, h int) *image.YCbCr {
	img := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	for i := range img.Cb {
		img.Cb[i] = 128
	}
	for i := range img.Cr {
		img.Cr[i] = 128
	}
	return img
}

// blurPlane —— 可分离盒式模糊跑 3 遍 ≈ 高斯（色度降噪与掩码羽化都用它）。
// 每趟内部按行/列并行 —— 羽化半径 12×3 趟在 640×480 上是数百万次运算，
// 串行会把 warpGray 拖成瓶颈。
func blurPlane(p []byte, stride, w, h, radius int) {
	if radius < 1 || w < 2 || h < 2 {
		return
	}
	tmp := make([]byte, w*h)
	r := radius
	for pass := 0; pass < 3; pass++ {
		parallelRows(h, func(y0, y1 int) {
			for y := y0; y < y1; y++ {
				row := y * stride
				var sum, cnt int
				for x := -r; x <= r; x++ {
					if x >= 0 && x < w {
						sum += int(p[row+x])
						cnt++
					}
				}
				for x := 0; x < w; x++ {
					tmp[y*w+x] = uint8(sum / cnt)
					if x-r >= 0 {
						sum -= int(p[row+x-r])
						cnt--
					}
					if x+r+1 < w {
						sum += int(p[row+x+r+1])
						cnt++
					}
				}
			}
		})
		parallelCols(w, func(x0, x1 int) {
			for x := x0; x < x1; x++ {
				var sum, cnt int
				for y := -r; y <= r; y++ {
					if y >= 0 && y < h {
						sum += int(tmp[y*w+x])
						cnt++
					}
				}
				for y := 0; y < h; y++ {
					p[y*stride+x] = uint8(sum / cnt)
					if y-r >= 0 {
						sum -= int(tmp[(y-r)*w+x])
						cnt--
					}
					if y+r+1 < h {
						sum += int(tmp[(y+r+1)*w+x])
						cnt++
					}
				}
			}
		})
	}
}

// stretchPlane —— 百分位拉伸（直方图法，比 min-max 抗噪）
func stretchPlane(p []byte, stride, w, h int, loPct, hiPct, amount float64) {
	if amount <= 0 {
		return
	}
	var hist [256]int
	n := w * h
	for y := 0; y < h; y++ {
		row := y * stride
		for x := 0; x < w; x++ {
			hist[p[row+x]]++
		}
	}
	loLim, hiLim := int(loPct*float64(n)), int(hiPct*float64(n))
	acc, lo, hi := 0, 0, 255
	for i := 0; i < 256; i++ {
		acc += hist[i]
		if acc >= loLim {
			lo = i
			break
		}
	}
	acc = 0
	for i := 0; i < 256; i++ {
		acc += hist[i]
		if acc >= hiLim {
			hi = i
			break
		}
	}
	if hi-lo < 2 {
		return
	}
	scale := 255.0 / float64(hi-lo)
	base := float64(lo)
	for y := 0; y < h; y++ {
		row := y * stride
		for x := 0; x < w; x++ {
			cur := float64(p[row+x])
			v := (cur - base) * scale
			p[row+x] = clamp8(cur + (v-cur)*amount)
		}
	}
}

// warpGray —— IR 灰图 → RGB 视角（双线性），返回图与羽化掩码。
// 并行：按输出行分带（行间完全独立）。每行的源坐标随 x 线性推进，
// 所以用增量步进而不是每像素 6 次乘法 + 取整。
func warpGray(src []byte, sw, sh int, scale, rot, tx, ty float64, dw, dh int) ([]byte, []byte) {
	dst := make([]byte, dw*dh)
	mask := make([]byte, dw*dh)
	c, s := math.Cos(rot), math.Sin(rot)
	a11, a12 := scale*c, -scale*s
	a21, a22 := scale*s, scale*c
	det := a11*a22 - a12*a21
	if math.Abs(det) < 1e-9 {
		return dst, mask
	}
	i11, i12 := a22/det, -a12/det
	i21, i22 := -a21/det, a11/det
	swf, shf := float64(sw), float64(sh)

	parallelRows(dh, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			dy := float64(y) + 0.5 - ty
			// 该行起点的源坐标，以及每前进 1 像素的增量
			fx := i11*(0.5-tx) + i12*dy - 0.5
			fy := i21*(0.5-tx) + i22*dy - 0.5
			rowOut := y * dw
			for x := 0; x < dw; x++ {
				idx := rowOut + x
				if fx < -1 || fy < -1 || fx > swf || fy > shf {
					fx += i11
					fy += i21
					continue
				}
				x0, y0i := int(math.Floor(fx)), int(math.Floor(fy))
				ax, ay := fx-float64(x0), fy-float64(y0i)
				mask[idx] = 255
				if x0 < 0 || y0i < 0 || x0 >= sw-1 || y0i >= sh-1 {
					cx, cy := x0, y0i
					if cx < 0 {
						cx = 0
					}
					if cy < 0 {
						cy = 0
					}
					if cx > sw-1 {
						cx = sw - 1
					}
					if cy > sh-1 {
						cy = sh - 1
					}
					dst[idx] = src[cy*sw+cx]
				} else {
					r0 := y0i * sw
					r1 := r0 + sw
					p00 := float64(src[r0+x0])
					p10 := float64(src[r0+x0+1])
					p01 := float64(src[r1+x0])
					p11 := float64(src[r1+x0+1])
					dst[idx] = clamp8(p00*(1-ax)*(1-ay) + p10*ax*(1-ay) +
						p01*(1-ax)*ay + p11*ax*ay)
				}
				fx += i11
				fy += i21
			}
		}
	})
	// 掩码羽化：半径给小了会在融合区边界留下可见接缝
	blurPlane(mask, dw, dw, dh, 12)
	return dst, mask
}

func applyGain(p []byte, gain float64) {
	if gain == 1.0 {
		return
	}
	for i := range p {
		p[i] = clamp8(float64(p[i]) * gain)
	}
}

// fuseYCbCr —— IR 出亮度、RGB 出色度（只动 Y 平面）
//
// 色度处理踩过的坑（对比 Python 参考实现后修正）：
//   - 之前按增益自动给"半径 = chroma*8"的大模糊 → 色度被抹平，饱和度掉、还出大片色块；
//     Python 参考用的是 GaussianBlur ksize=7（全分辨率）≈ 色度平面上 σ0.7，几乎不糊。
//   - 之前加了"软限幅 46" → 直接把饱和度压下去。Python 版没有这个，实测融合后
//     平均饱和度比原始 RGB 高 43%（chroma=1.6 的正常效果）。
// 现在：模糊半径交给独立的"色度平滑"参数（默认很小），只做截断不做软限幅。
func fuseYCbCr(base *image.YCbCr, irW, mask []byte, weight, chroma float64, chromaBlur int) *image.YCbCr {
	out := cloneYCbCr(base)
	w, h := out.Rect.Dx(), out.Rect.Dy()
	cw, ch := (w+1)/2, (h+1)/2
	if chroma != 1.0 {
		if chromaBlur > 0 {
			blurPlane(out.Cb, out.CStride, cw, ch, chromaBlur)
			blurPlane(out.Cr, out.CStride, cw, ch, chromaBlur)
		}
		for i := range out.Cb {
			out.Cb[i] = clamp8((float64(out.Cb[i])-128)*chroma + 128)
			out.Cr[i] = clamp8((float64(out.Cr[i])-128)*chroma + 128)
		}
	}
	// 融合：行间独立，按行分带并行
	parallelRows(h, func(y0, y1 int) {
		for y := y0; y < y1; y++ {
			yo := y * out.YStride
			io := y * w
			for x := 0; x < w; x++ {
				m := 1.0
				if mask != nil {
					m = float64(mask[io+x]) / 255.0
				}
				ww := weight * m
				if ww <= 0 {
					continue
				}
				out.Y[yo+x] = clamp8(float64(out.Y[yo+x])*(1-ww) + float64(irW[io+x])*ww)
			}
		}
	})
	return out
}

func detailYCbCr(base *image.YCbCr, irW, mask []byte, amount float64) *image.YCbCr {
	out := cloneYCbCr(base)
	w, h := out.Rect.Dx(), out.Rect.Dy()
	blur := make([]byte, len(irW))
	copy(blur, irW)
	blurPlane(blur, w, w, h, 4)
	for y := 0; y < h; y++ {
		yo := y * out.YStride
		io := y * w
		for x := 0; x < w; x++ {
			m := 1.0
			if mask != nil {
				m = float64(mask[io+x]) / 255.0
			}
			d := (float64(irW[io+x]) - float64(blur[io+x])) * amount * m
			out.Y[yo+x] = clamp8(float64(out.Y[yo+x]) + d)
		}
	}
	return out
}

func grayToYCbCr(g []byte, w, h int) *image.YCbCr {
	img := newGrayYCbCr(w, h)
	for y := 0; y < h; y++ {
		copy(img.Y[y*img.YStride:y*img.YStride+w], g[y*w:y*w+w])
	}
	return img
}

func colorMapYCbCr(g []byte, w, h int) *image.YCbCr {
	img := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	for y := 0; y < h; y++ {
		yo := y * img.YStride
		co := (y / 2) * img.CStride
		for x := 0; x < w; x++ {
			v := g[y*w+x]
			img.Y[yo+x] = infernoY[v]
			if y%2 == 0 && x%2 == 0 {
				img.Cb[co+x/2] = infernoCb[v]
				img.Cr[co+x/2] = infernoCr[v]
			}
		}
	}
	return img
}

// illumDiff —— 补光照明分量 |亮帧−灭帧|，拉伸到 0-255（结果是一张正常图，不是边缘图）
func illumDiff(bright, dark []byte) []byte {
	out := make([]byte, len(bright))
	lo, hi := 255, 0
	for i := range bright {
		d := int(bright[i]) - int(dark[i])
		if d < 0 {
			d = -d
		}
		out[i] = uint8(d)
		if d < lo {
			lo = d
		}
		if d > hi {
			hi = d
		}
	}
	if hi-lo < 2 {
		return out
	}
	scale := 255.0 / float64(hi-lo)
	for i := range out {
		out[i] = clamp8((float64(out[i]) - float64(lo)) * scale)
	}
	return out
}

func meanOf(p []byte) float64 {
	if len(p) == 0 {
		return 0
	}
	var s int64
	for _, v := range p {
		s += int64(v)
	}
	return float64(s) / float64(len(p))
}

// ───────────────────────── 画布与拼图 ─────────────────────────

func blitYCbCr(dst, src *image.YCbCr, dx, dy int) {
	if src == nil {
		return
	}
	sw, sh := src.Rect.Dx(), src.Rect.Dy()
	_, dh := dst.Rect.Dx(), dst.Rect.Dy()
	for y := 0; y < sh; y++ {
		ty := dy + y
		if ty < 0 || ty >= dh {
			continue
		}
		copy(dst.Y[ty*dst.YStride+dx:ty*dst.YStride+dx+sw], src.Y[y*src.YStride:y*src.YStride+sw])
	}
	for y := 0; y < sh/2; y++ {
		ty := dy/2 + y
		if ty < 0 || ty >= (dh+1)/2 {
			continue
		}
		copy(dst.Cb[ty*dst.CStride+dx/2:ty*dst.CStride+dx/2+sw/2],
			src.Cb[y*src.CStride:y*src.CStride+sw/2])
		copy(dst.Cr[ty*dst.CStride+dx/2:ty*dst.CStride+dx/2+sw/2],
			src.Cr[y*src.CStride:y*src.CStride+sw/2])
	}
}

// resizeYCbCrCover —— 等比缩放后**居中裁切**填满目标框（cover 语义）。
// 缩略图用这个：正方形源（IR 340×340）放进 4:3 格子时不会留黑边，视觉上"填满"。
func resizeYCbCrCover(src *image.YCbCr, dw, dh int) *image.YCbCr {
sw, sh := src.Rect.Dx(), src.Rect.Dy()
if sw == 0 || sh == 0 || dw == 0 || dh == 0 {
return src
}
// 取较大的缩放比 → 覆盖整个目标框
rs := float64(dw) / float64(sw)
rh := float64(dh) / float64(sh)
if rh > rs {
rs = rh
}
cw := int(float64(sw) * rs)
ch := int(float64(sh) * rs)
if cw < dw {
cw = dw
}
if ch < dh {
ch = dh
}
big := resizeYCbCr(src, cw, ch)
ox := (cw - dw) / 2
oy := (ch - dh) / 2
out := image.NewYCbCr(image.Rect(0, 0, dw, dh), image.YCbCrSubsampleRatio420)
for y := 0; y < dh; y++ {
sy := y + oy
if sy < 0 || sy >= ch {
continue
}
copy(out.Y[y*out.YStride:y*out.YStride+dw], big.Y[sy*big.YStride+ox:sy*big.YStride+ox+dw])
}
cw2, ch2 := (dw+1)/2, (dh+1)/2
ox2, oy2 := ox/2, oy/2
for y := 0; y < ch2; y++ {
sy := y + oy2
if sy < 0 || sy >= (ch+1)/2 {
continue
}
copy(out.Cb[y*out.CStride:y*out.CStride+cw2], big.Cb[sy*big.CStride+ox2:sy*big.CStride+ox2+cw2])
copy(out.Cr[y*out.CStride:y*out.CStride+cw2], big.Cr[sy*big.CStride+ox2:sy*big.CStride+ox2+cw2])
}
return out
}

func resizeYCbCr(src *image.YCbCr, dw, dh int) *image.YCbCr {
	sw, sh := src.Rect.Dx(), src.Rect.Dy()
	out := image.NewYCbCr(image.Rect(0, 0, dw, dh), image.YCbCrSubsampleRatio420)
	xr := float64(sw) / float64(dw)
	yr := float64(sh) / float64(dh)
	for y := 0; y < dh; y++ {
		sy := int(float64(y)*yr) % sh
		so := sy * src.YStride
		to := y * out.YStride
		for x := 0; x < dw; x++ {
			sx := int(float64(x)*xr) % sw
			out.Y[to+x] = src.Y[so+sx]
		}
	}
	scw, sch := (sw+1)/2, (sh+1)/2
	dcw, dch := (dw+1)/2, (dh+1)/2
	for y := 0; y < dch; y++ {
		sy := y * sch / dch
		so := sy * src.CStride
		to := y * out.CStride
		for x := 0; x < dcw; x++ {
			sx := x * scw / dcw
			out.Cb[to+x] = src.Cb[so+sx]
			out.Cr[to+x] = src.Cr[so+sx]
		}
	}
	return out
}

// byteWriter —— 避免 bytes.Buffer 的额外拷贝
type byteWriter struct {
	buf []byte
}

func newByteWriter(cap int) *byteWriter { return &byteWriter{buf: make([]byte, 0, cap)} }
func (w *byteWriter) Write(p []byte) (int, error) {
	w.buf = append(w.buf, p...)
	return len(p), nil
}
func (w *byteWriter) Bytes() []byte { return w.buf }
