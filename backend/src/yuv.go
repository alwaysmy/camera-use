// yuv.go — 相机原生格式 → image.YCbCr 4:2:0
//
// 整条流水线**只在 YCbCr 里走**：不过 RGB、不做色彩空间往返，
// 融合只改 Y 平面，最后直接喂给 image/jpeg 的快速路径。
package main

import (
	"bytes"
	"fmt"
	"image"
	"image/jpeg"
)

// decodePayload —— 把相机吐的一帧变成 4:2:0 的 *image.YCbCr
func decodePayload(data []byte, subtype string, w, h int) (*image.YCbCr, error) {
	switch subtype {
	case "MJPG":
		img, err := jpeg.Decode(bytes.NewReader(data))
		if err != nil {
			return nil, fmt.Errorf("JPEG 解码失败: %w", err)
		}
		switch v := img.(type) {
		case *image.YCbCr:
			if v.SubsampleRatio == image.YCbCrSubsampleRatio420 {
				return v, nil
			}
			return to420(v), nil
		case *image.Gray:
			return grayToYCbCr(v.Pix, v.Bounds().Dx(), v.Bounds().Dy()), nil
		default:
			return rgbaTo420(img), nil
		}
	case "YUY2":
		return yuy2To420(data, w, h), nil
	case "NV12":
		return nv12To420(data, w, h), nil
	case "GREY":
		return grayToYCbCr(data, w, h), nil
	}
	return nil, fmt.Errorf("不支持的格式 %s", subtype)
}

// decodeGray —— YUV 格式取 Y 平面（IR 相机用；比转 RGB 省一个数量级）
func decodeGray(data []byte, subtype string, w, h int) ([]byte, error) {
	switch subtype {
	case "YUY2":
		out := make([]byte, w*h)
		if len(data) < w*h*2 {
			return nil, fmt.Errorf("YUY2 数据长度不足")
		}
		for y := 0; y < h; y++ {
			src := y * w * 2
			dst := y * w
			for x := 0; x < w; x++ {
				out[dst+x] = data[src+x*2] // Y0 U Y1 V：只取偶数位
			}
		}
		return out, nil
	case "NV12":
		if len(data) < w*h {
			return nil, fmt.Errorf("NV12 数据长度不足")
		}
		out := make([]byte, w*h)
		copy(out, data[:w*h])
		return out, nil
	case "GREY":
		out := make([]byte, w*h)
		copy(out, data[:min(len(data), w*h)])
		return out, nil
	case "MJPG":
		img, err := jpeg.Decode(bytes.NewReader(data))
		if err != nil {
			return nil, err
		}
		yc := to420Of(img)
		out := make([]byte, yc.Rect.Dx()*yc.Rect.Dy())
		for y := 0; y < yc.Rect.Dy(); y++ {
			copy(out[y*yc.Rect.Dx():], yc.Y[y*yc.YStride:y*yc.YStride+yc.Rect.Dx()])
		}
		return out, nil
	}
	return nil, fmt.Errorf("不支持的格式 %s", subtype)
}

// bgrBytesTo420 —— BGR 交错字节 → YCbCr 4:2:0（回到 JPEG 编码器的快速路径）
func bgrBytesTo420(bgr []byte, w, h int) *image.YCbCr {
	img := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	for y := 0; y < h; y++ {
		yo := y * img.YStride
		ro := y * w * 3
		for x := 0; x < w; x++ {
			b := int(bgr[ro+x*3])
			g := int(bgr[ro+x*3+1])
			r := int(bgr[ro+x*3+2])
			img.Y[yo+x] = uint8((19595*r + 38470*g + 7471*b + 32768) >> 16)
		}
	}
	for y := 0; y < h; y += 2 {
		co := (y / 2) * img.CStride
		for x := 0; x < w; x += 2 {
			var sr, sg, sb, n int
			for dy := 0; dy < 2 && y+dy < h; dy++ {
				ro := (y+dy)*w*3 + x*3
				for dx := 0; dx < 2 && x+dx < w; dx++ {
					sb += int(bgr[ro+dx*3])
					sg += int(bgr[ro+dx*3+1])
					sr += int(bgr[ro+dx*3+2])
					n++
				}
			}
			r, g, b := sr/n, sg/n, sb/n
			img.Cb[co+x/2] = clampInt(128 + ((-11059*r-21710*g+32768*b)>>16))
			img.Cr[co+x/2] = clampInt(128 + ((32768*r-27443*g-5329*b)>>16))
		}
	}
	return img
}

func yuy2To420(data []byte, w, h int) *image.YCbCr {
	img := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	cw := (w + 1) / 2
	for y := 0; y < h; y++ {
		src := y * w * 2
		dstY := y * img.YStride
		for x := 0; x < w; x++ {
			img.Y[dstY+x] = data[src+x*2]
		}
		// YUY2: [Y0 U Y1 V] —— 每个 4 字节组覆盖 2 个横向像素
		co := (y / 2) * img.CStride
		for x := 0; x < cw; x++ {
			i := src + x*4
			if i+3 >= len(data) {
				break
			}
			img.Cb[co+x] = data[i+1]
			img.Cr[co+x] = data[i+3]
		}
	}
	return img
}

func nv12To420(data []byte, w, h int) *image.YCbCr {
	img := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	for y := 0; y < h; y++ {
		copy(img.Y[y*img.YStride:y*img.YStride+w], data[y*w:y*w+w])
	}
	base := w * h
	cw := (w + 1) / 2
	for y := 0; y < (h+1)/2; y++ {
		co := y * img.CStride
		for x := 0; x < cw; x++ {
			i := base + (y*cw+x)*2
			if i+1 >= len(data) {
				break
			}
			img.Cb[co+x] = data[i]
			img.Cr[co+x] = data[i+1]
		}
	}
	return img
}

// to420 —— 任意子采样 → 4:2:0。
//
// 之前这里写错过：对 4:2:2 源把色度列索引乘了 2，读到 w/2 宽的色度平面之外，
// 结果色度被打散、饱和度腰斩（实测 MJPG 的 LAB 彩度 10.23 → 6.36）。
// 现在按"每个目标色度像素覆盖的 2×2 luma 区域"求平均，任意子采样都正确。
func to420(src *image.YCbCr) *image.YCbCr {
	if src.SubsampleRatio == image.YCbCrSubsampleRatio420 {
		return src
	}
	sw, sh := src.Rect.Dx(), src.Rect.Dy()
	out := image.NewYCbCr(image.Rect(0, 0, sw, sh), image.YCbCrSubsampleRatio420)
	for y := 0; y < sh; y++ {
		copy(out.Y[y*out.YStride:y*out.YStride+sw], src.Y[y*src.YStride:y*src.YStride+sw])
	}
	dstCW, dstCH := (sw+1)/2, (sh+1)/2
	parallelRows(dstCH, func(y0, y1 int) {
		for dy := y0; dy < y1; dy++ {
			do := dy * out.CStride
			for dx := 0; dx < dstCW; dx++ {
				var scb, scr, n int
				for ly := 2 * dy; ly <= 2*dy+1 && ly < sh; ly++ {
					for lx := 2 * dx; lx <= 2*dx+1 && lx < sw; lx++ {
						sx, sy := lx, ly
						switch src.SubsampleRatio {
						case image.YCbCrSubsampleRatio422:
							sx, sy = lx/2, ly
						case image.YCbCrSubsampleRatio440:
							sx, sy = lx, ly/2
						case image.YCbCrSubsampleRatio444:
							sx, sy = lx, ly
						default: // 420
							sx, sy = lx/2, ly/2
						}
						si := sy*src.CStride + sx
						if si < len(src.Cb) && si < len(src.Cr) {
							scb += int(src.Cb[si])
							scr += int(src.Cr[si])
							n++
						}
					}
				}
				if n > 0 {
					out.Cb[do+dx] = uint8(scb / n)
					out.Cr[do+dx] = uint8(scr / n)
				} else {
					out.Cb[do+dx] = 128
					out.Cr[do+dx] = 128
				}
			}
		}
	})
	return out
}

func to420Of(img image.Image) *image.YCbCr {
	if yc, ok := img.(*image.YCbCr); ok {
		return to420(yc)
	}
	return rgbaTo420(img)
}

func rgbaTo420(img image.Image) *image.YCbCr {
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	out := image.NewYCbCr(image.Rect(0, 0, w, h), image.YCbCrSubsampleRatio420)
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			r, g, bb, _ := img.At(b.Min.X+x, b.Min.Y+y).RGBA()
			R, G, B := int(r>>8), int(g>>8), int(bb>>8)
			out.Y[y*out.YStride+x] = uint8((19595*R + 38470*G + 7471*B + 32768) >> 16)
		}
	}
	for y := 0; y < (h+1)/2; y++ {
		co := y * out.CStride
		for x := 0; x < (w+1)/2; x++ {
			var sr, sg, sb, n int
			for dy := 0; dy < 2; dy++ {
				for dx := 0; dx < 2; dx++ {
					px, py := x*2+dx, y*2+dy
					if px >= w || py >= h {
						continue
					}
					r, g, bb, _ := img.At(b.Min.X+px, b.Min.Y+py).RGBA()
					sr += int(r >> 8)
					sg += int(g >> 8)
					sb += int(bb >> 8)
					n++
				}
			}
			if n == 0 {
				continue
			}
			R, G, B := sr/n, sg/n, sb/n
			out.Cb[co+x] = clampInt(128 + ((-11059*R-21710*G+32768*B)>>16))
			out.Cr[co+x] = clampInt(128 + ((32768*R-27443*G-5329*B)>>16))
		}
	}
	return out
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
