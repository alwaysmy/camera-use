// mf.go — Media Foundation 原生封装（Go 标准库 + syscall，零外部依赖）
//
// 为什么是 MF 而不是 DirectShow/OpenCV：
//   - 实测 **MF 通道下 IAMCameraControl 完全可用**（Exposure GetRange min=-2 max=-10
//     caps=0x3，Auto/Manual 都能设）—— 这正是本项目要解决"OpenCV 打开画面发暗"的关键；
//   - 一台相机一个 IMFSourceReader，MJPG/YUY2/NV12 三种原生格式随便挑；
//   - IR 相机（Windows Hello）只能用 MF + 设备符号链接打开（DShow 枚举里根本没有它）。
//
// 线程模型：COM 有套间概念，所有对同一批接口的调用必须回到**创建它的那个线程**。
// 所以每台相机跑在 `runtime.LockOSThread()` 的 goroutine 里，外部请求通过命令队列
// 投递进去执行（见 camera.go）。
package main

import (
	"fmt"
	"runtime"
	"syscall"
	"unicode/utf16"
	"unsafe"
)

// ───────────────────────── COM 基础设施 ─────────────────────────

type GUID struct {
	D1 uint32
	D2 uint16
	D3 uint16
	D4 [8]byte
}

func NewGUID(s string) GUID {
	var g GUID
	var b [16]byte
	hex := func(c byte) byte {
		switch {
		case c >= '0' && c <= '9':
			return c - '0'
		case c >= 'a' && c <= 'f':
			return c - 'a' + 10
		case c >= 'A' && c <= 'F':
			return c - 'A' + 10
		}
		return 0
	}
	buf := make([]byte, 0, 32)
	for i := 0; i < len(s); i++ {
		if s[i] != '-' {
			buf = append(buf, s[i])
		}
	}
	for i := 0; i+1 < len(buf) && i/2 < 16; i += 2 {
		b[i/2] = hex(buf[i])<<4 | hex(buf[i+1])
	}
	g.D1 = uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
	g.D2 = uint16(b[4])<<8 | uint16(b[5])
	g.D3 = uint16(b[6])<<8 | uint16(b[7])
	copy(g.D4[:], b[8:16])
	return g
}

func (g GUID) String() string {
	return fmt.Sprintf("{%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}", g.D1, g.D2, g.D3,
		g.D4[0], g.D4[1], g.D4[2], g.D4[3], g.D4[4], g.D4[5], g.D4[6], g.D4[7])
}

var (
	ole32DLL       = syscall.NewLazyDLL("ole32.dll")
	mfplatDLL      = syscall.NewLazyDLL("mfplat.dll")
	mfDLL          = syscall.NewLazyDLL("mf.dll")
	mfreadwriteDLL = syscall.NewLazyDLL("mfreadwrite.dll")
)

func hr(r uintptr) int32 { return int32(r) }
func ok(r uintptr) bool  { return int32(r) >= 0 }

// comCall 调用 COM 接口虚函数表第 index 项（0=QueryInterface,1=AddRef,2=Release）
func comCall(this uintptr, index int, args ...uintptr) uintptr {
	if this == 0 {
		return 0x80004003 // E_POINTER
	}
	vtbl := *(*uintptr)(unsafe.Pointer(this))
	fn := *(*uintptr)(unsafe.Pointer(vtbl + uintptr(index)*unsafe.Sizeof(uintptr(0))))
	a := make([]uintptr, 0, len(args)+1)
	a = append(a, this)
	a = append(a, args...)
	r, _, _ := syscall.SyscallN(fn, a...)
	return r
}

func comAddRef(this uintptr) uintptr { return comCall(this, 1) }

func comRelease(this uintptr) {
	if this != 0 {
		comCall(this, 2)
	}
}

func comQueryInterface(this uintptr, iid GUID) uintptr {
	var out uintptr
	if !ok(comCall(this, 0, uintptr(unsafe.Pointer(&iid)), uintptr(unsafe.Pointer(&out)))) {
		return 0
	}
	return out
}

func hrErr(what string, r uintptr) error {
	return fmt.Errorf("%s 失败: 0x%08X", what, uint32(hr(r)))
}

// ───────────────────────── MF GUID 常量 ─────────────────────────

var (
	IID_IMFAttributes   = NewGUID("2CD2D921-C447-44A7-A13C-4ADABFC247E3")
	IID_IAMCameraControl = NewGUID("C6E13370-30AC-11D0-A18C-00A0C9118956")
	IID_IAMVideoProcAmp  = NewGUID("C6E13360-30AC-11D0-A18C-00A0C9118956")

	ATTRSourceType        = NewGUID("C60AC5FE-252A-478F-A0EF-BC8FA5F7CAD3")
	ATTRSourceTypeVidcap  = NewGUID("8AC3587A-4AE7-42D8-99E0-0A6013EEF90F")
	ATTRFriendlyName      = NewGUID("60D81288-76AA-4C63-8B81-D642B20D486E")
	ATTRSymbolicLink      = NewGUID("58F0AAD8-22BF-4F8A-BB3D-D2C4978C6E2F")

	MTSubtype     = NewGUID("F7E34C9A-42E8-4714-B74B-CB29D72C35E5")
	MTFrameSize   = NewGUID("1652C33D-D6B2-4012-B834-72030849A37D")
	MTFrameRate   = NewGUID("C459A2E8-3D2C-4E44-B132-FEE5156C7BB0")
	MTMajorType   = NewGUID("48EBA18E-F8C9-4687-BF11-0A74C9F96A8F")
	MTDefaultStride = NewGUID("644B4E48-1E02-4516-B0EB-C01CA9D49AC6")

	GUIDNull = NewGUID("00000000-0000-0000-0000-000000000000")
)

// fourcc → MFVideoFormat_XXXX
func fourccGUID(cc string) GUID {
	base := NewGUID("00000000-0000-0010-8000-00AA00389B71")
	base.D1 = uint32(cc[0]) | uint32(cc[1])<<8 | uint32(cc[2])<<16 | uint32(cc[3])<<24
	return base
}

func guidFourcc(g GUID) string {
	b := []byte{byte(g.D1), byte(g.D1 >> 8), byte(g.D1 >> 16), byte(g.D1 >> 24)}
	printable := true
	for _, c := range b {
		if c < 32 || c > 126 {
			printable = false
		}
	}
	if !printable {
		return g.String()
	}
	return string(b)
}

// IMFAttributes vtable: GetUINT32=7 GetUINT64=8 GetGUID=10 GetStringLength=11
//                       GetString=12 GetAllocatedString=13 SetGUID=24 SetString=25 SetUINT32=21
func attrGetU64(p uintptr, key GUID) uint64 {
	var v uint64
	comCall(p, 8, uintptr(unsafe.Pointer(&key)), uintptr(unsafe.Pointer(&v)))
	return v
}

func attrGetGUID(p uintptr, key GUID) GUID {
	var v GUID
	comCall(p, 10, uintptr(unsafe.Pointer(&key)), uintptr(unsafe.Pointer(&v)))
	return v
}

func attrSetGUID(p uintptr, key, val GUID) uintptr {
	k, v := key, val
	return comCall(p, 24, uintptr(unsafe.Pointer(&k)), uintptr(unsafe.Pointer(&v)))
}

func attrSetString(p uintptr, key GUID, s string) uintptr {
	k := key
	ws, _ := syscall.UTF16PtrFromString(s)
	return comCall(p, 25, uintptr(unsafe.Pointer(&k)), uintptr(unsafe.Pointer(ws)))
}

// attrGetAllocatedString —— 用 CoTaskMemFree 释放返回值（比对定长 GetString 稳）
func attrGetAllocatedString(p uintptr, key GUID) (string, bool) {
	var ptr uintptr
	var n uint32
	k := key
	r := comCall(p, 13, uintptr(unsafe.Pointer(&k)), uintptr(unsafe.Pointer(&ptr)), uintptr(unsafe.Pointer(&n)))
	if !ok(r) || ptr == 0 {
		return "", false
	}
	defer ole32DLL.NewProc("CoTaskMemFree").Call(ptr)
	buf := unsafe.Slice((*uint16)(unsafe.Pointer(ptr)), int(n))
	return string(utf16.Decode(buf)), true
}

// ───────────────────────── Media Foundation 初始化 ─────────────────────────

var mfRefCount int

func mfInit() error {
	if mfRefCount == 0 {
		r, _, _ := ole32DLL.NewProc("CoInitializeEx").Call(0, 2) // COINIT_MULTITHREADED
		// CoInitializeEx 可能返回 S_FALSE(1) 或 RPC_E_CHANGED_MODE，都还能用
		if int32(r) < 0 && uint32(r) != 0x80010106 {
			// CoInitializeEx 失败先尝试"清一次再重来"：
                        // 实测见过 0x8007057F(ERROR_CANNOT_FIND_WND_CLASS) ——
                        // Go 的 OS 线程会被复用，上一个 goroutine 退出时没 CoUninitialize
                        // 就会把脏套间留给下一个（这个坑真踩到了：RGB 会话 0fps）。
                        ole32DLL.NewProc("CoUninitialize").Call()
                        r2, _, _ := ole32DLL.NewProc("CoInitializeEx").Call(0, 2)
                        if int32(r2) >= 0 || uint32(r2) == 0x80010106 {
                                r = r2
                        } else {
                                return fmt.Errorf("CoInitializeEx: 0x%08X (清理后重试仍失败 0x%08X)", uint32(r), uint32(r2))
                        }
		}
		r, _, _ = mfplatDLL.NewProc("MFStartup").Call(0x00020070, 0)
		if int32(r) < 0 {
			return fmt.Errorf("MFStartup: 0x%08X", uint32(r))
		}
	}
	mfRefCount++
	return nil
}

func mfShutdown() {
	mfRefCount--
	if mfRefCount <= 0 {
		mfRefCount = 0
		mfplatDLL.NewProc("MFShutdown").Call()
	}
}

// lockCOMThread —— COM 套间是线程绑定的，做 COM 的 goroutine 必须钉住线程
// comOwner —— 本线程的 COM 是否由我们初始化（要负责 CoUninitialize）
var comOwner bool

// unlockCOMThread —— 相机 goroutine 退出时收尾：
// 不 CoUninitialize 就把线程还回 Go 的线程池，会把脏套间留给下一个 LockOSThread 的 goroutine，
// 下次 CoInitializeEx 就会失败（实测错误码 0x8007057F = ERROR_CANNOT_FIND_WND_CLASS，
// 表现是 RGB 会话直接 0fps）。
func unlockCOMThread() {
	if comOwner {
		ole32DLL.NewProc("CoUninitialize").Call()
		comOwner = false
	}
	runtime.UnlockOSThread()
}

func lockCOMThread() error {
	runtime.LockOSThread()
	comOwner = false
	defer func() { if comOwner { /* 由 unlockCOMThread 负责 */ } }()
	return mfInit()
}

func mfCreateAttributes(n int) (uintptr, error) {
	var attrs uintptr
	r, _, _ := mfplatDLL.NewProc("MFCreateAttributes").Call(
		uintptr(unsafe.Pointer(&attrs)), uintptr(n))
	if !ok(r) {
		return 0, hrErr("MFCreateAttributes", r)
	}
	return attrs, nil
}

// ───────────────────────── 设备枚举 ─────────────────────────

type DeviceInfo struct {
	Name         string
	SymbolicLink string
	Kind         string // rgb | ir
}

func EnumVidcapDevices() ([]DeviceInfo, error) {
	attrs, err := mfCreateAttributes(2)
	if err != nil {
		return nil, err
	}
	defer comRelease(attrs)
	if r := attrSetGUID(attrs, ATTRSourceType, ATTRSourceTypeVidcap); !ok(r) {
		return nil, hrErr("SetGUID(SourceType)", r)
	}
	var ppActivate uintptr
	var count uint32
	r, _, _ := mfDLL.NewProc("MFEnumDeviceSources").Call(attrs,
		uintptr(unsafe.Pointer(&ppActivate)), uintptr(unsafe.Pointer(&count)))
	if !ok(r) {
		return nil, hrErr("MFEnumDeviceSources", r)
	}
	defer ole32DLL.NewProc("CoTaskMemFree").Call(ppActivate)
	out := make([]DeviceInfo, 0, count)
	if ppActivate == 0 {
		return out, nil
	}
	items := unsafe.Slice((*uintptr)(unsafe.Pointer(ppActivate)), int(count))
	for _, act := range items {
		if act == 0 {
			continue
		}
		di := DeviceInfo{}
		di.Name, _ = attrGetAllocatedString(act, ATTRFriendlyName)
		di.SymbolicLink, _ = attrGetAllocatedString(act, ATTRSymbolicLink)
		di.Kind = classifyDevice(di.Name, di.SymbolicLink)
		comRelease(act)
		out = append(out, di)
	}
	return out, nil
}

func classifyDevice(name, link string) string {
	low := lower(name)
	if contains(low, "ir camera") || contains(low, "infrared") {
		return "ir"
	}
	return "rgb"
}

// ───────────────────────── 相机 ─────────────────────────

type MediaFormat struct {
	Subtype string
	W, H    int
	FPS     float64
}

type MFCamera struct {
	Source uintptr
	Reader uintptr
	Subtype string
	W, H    int
	FPS     float64
	Link    string
	Name    string
}

// OpenCameraByLink —— 用设备符号链接直接打开（IR 相机唯一的入口）
func OpenCameraByLink(link string) (*MFCamera, error) {
	attrs, err := mfCreateAttributes(4)
	if err != nil {
		return nil, err
	}
	defer comRelease(attrs)
	if r := attrSetGUID(attrs, ATTRSourceType, ATTRSourceTypeVidcap); !ok(r) {
		return nil, hrErr("SetGUID(SourceType)", r)
	}
	if r := attrSetString(attrs, ATTRSymbolicLink, link); !ok(r) {
		return nil, hrErr("SetString(SymbolicLink)", r)
	}
	var src uintptr
	r, _, _ := mfDLL.NewProc("MFCreateDeviceSource").Call(attrs, uintptr(unsafe.Pointer(&src)))
	if !ok(r) || src == 0 {
		return nil, hrErr("MFCreateDeviceSource", r)
	}
	var reader uintptr
	r, _, _ = mfreadwriteDLL.NewProc("MFCreateSourceReaderFromMediaSource").Call(
		src, 0, uintptr(unsafe.Pointer(&reader)))
	if !ok(r) || reader == 0 {
		comRelease(src)
		return nil, hrErr("MFCreateSourceReaderFromMediaSource", r)
	}
	return &MFCamera{Source: src, Reader: reader, Link: link}, nil
}

// NativeFormats —— 枚举该相机支持的全部原生媒体类型
func (c *MFCamera) NativeFormats() []MediaFormat {
	var out []MediaFormat
	for i := 0; i < 256; i++ {
		var mt uintptr
		r := comCall(c.Reader, 5, 0, uintptr(i), uintptr(unsafe.Pointer(&mt))) // GetNativeMediaType
		if !ok(r) || mt == 0 {
			break
		}
		sub := attrGetGUID(mt, MTSubtype)
		size := attrGetU64(mt, MTFrameSize)
		rate := attrGetU64(mt, MTFrameRate)
		fps := 0.0
		if rate != 0 && uint32(rate) != 0 {
			fps = float64(uint32(rate>>32)) / float64(uint32(rate))
		}
		out = append(out, MediaFormat{
			Subtype: guidFourcc(sub),
			W:       int(uint32(size >> 32)),
			H:       int(uint32(size)),
			FPS:     fps,
		})
		comRelease(mt)
	}
	return out
}

// SetFormat —— 挑一个匹配 (subtype, w, h) 的原生类型并应用；找不到精确匹配就报错
func (c *MFCamera) SetFormat(subtype string, w, h int) error {
	want := fourccGUID(subtype)
	for i := 0; i < 256; i++ {
		var mt uintptr
		r := comCall(c.Reader, 5, 0, uintptr(i), uintptr(unsafe.Pointer(&mt)))
		if !ok(r) || mt == 0 {
			break
		}
		sub := attrGetGUID(mt, MTSubtype)
		size := attrGetU64(mt, MTFrameSize)
		mw, mh := int(uint32(size>>32)), int(uint32(size))
		if sub == want && mw == w && mh == h {
			r2 := comCall(c.Reader, 7, 0, 0, mt) // SetCurrentMediaType
			rate := attrGetU64(mt, MTFrameRate)
			comRelease(mt)
			if !ok(r2) {
				return hrErr("SetCurrentMediaType", r2)
			}
			c.Subtype, c.W, c.H = subtype, w, h
			if rate != 0 && uint32(rate) != 0 {
				c.FPS = float64(uint32(rate>>32)) / float64(uint32(rate))
			}
			return nil
		}
		comRelease(mt)
	}
	return fmt.Errorf("相机不支持 %s %dx%d", subtype, w, h)
}

// SetBestFormat —— 按优先级挑一个可用格式
func (c *MFCamera) SetBestFormat(prefer []string, w, h int) (MediaFormat, error) {
	avail := c.NativeFormats()
	for _, st := range prefer {
		for _, f := range avail {
			if f.Subtype == st && f.W == w && f.H == h {
				if err := c.SetFormat(st, w, h); err != nil {
					return MediaFormat{}, err
				}
				return f, nil
			}
		}
	}
	return MediaFormat{}, fmt.Errorf("没有可用的格式（想要 %v %dx%d，实际支持 %v）", prefer, w, h, avail)
}

// GrabFrame —— 同步读一帧，返回 (载荷, 是否为压缩流)
//
//	MJPG → JPEG 字节（交给 image/jpeg 解）
//	YUY2/NV12 → 原始 YUV 平面
func (c *MFCamera) GrabFrame() ([]byte, error) {
	var actual, flags uint32
	var ts int64
	var sample uintptr
	r := comCall(c.Reader, 9, 0, 0,
		uintptr(unsafe.Pointer(&actual)), uintptr(unsafe.Pointer(&flags)),
		uintptr(unsafe.Pointer(&ts)), uintptr(unsafe.Pointer(&sample)))
	if !ok(r) {
		return nil, hrErr("ReadSample", r)
	}
	if sample == 0 {
		return nil, nil // STREAMTICK：还没数据
	}
	defer comRelease(sample)
	var buf uintptr
	r = comCall(sample, 41, uintptr(unsafe.Pointer(&buf))) // ConvertToContiguousBuffer
	if !ok(r) || buf == 0 {
		return nil, hrErr("ConvertToContiguousBuffer", r)
	}
	defer comRelease(buf)
	var ptr uintptr
	var maxLen, curLen uint32
	r = comCall(buf, 3, uintptr(unsafe.Pointer(&ptr)), uintptr(unsafe.Pointer(&maxLen)),
		uintptr(unsafe.Pointer(&curLen))) // Lock
	if !ok(r) {
		return nil, hrErr("IMFMediaBuffer::Lock", r)
	}
	out := make([]byte, curLen)
	copy(out, unsafe.Slice((*byte)(unsafe.Pointer(ptr)), int(curLen)))
	comCall(buf, 4) // Unlock
	return out, nil
}

// Close —— 必须先释放全部 COM 对象，MFShutdown 才不会被卡住（这个坑踩过）
func (c *MFCamera) Close() {
	comRelease(c.Reader)
	comRelease(c.Source)
	c.Reader, c.Source = 0, 0
}

// ───────────────────────── IAMCameraControl（曝光） ─────────────────────────

// CameraControl 属性号
const (
	CtrlPan = 0
	CtrlTilt = 1
	CtrlRoll = 2
	CtrlZoom = 3
	CtrlExposure = 4
	CtrlIris = 5
	CtrlFocus = 6

	CtrlFlagAuto   = 1
	CtrlFlagManual = 2
)

type ControlRange struct {
	Min, Max, Step, Default, Caps int32
}

// GetService —— 注意 streamIndex 必须是 MF_SOURCE_READER_MEDIASOURCE(0xFFFFFFFF)，
// 用 0 查会全是 E_NOINTERFACE（这个坑我踩过）
func (c *MFCamera) getService(iid GUID) uintptr {
	var obj uintptr
	r := comCall(c.Reader, 11, 0xFFFFFFFF,
		uintptr(unsafe.Pointer(&GUIDNull)), uintptr(unsafe.Pointer(&iid)),
		uintptr(unsafe.Pointer(&obj)))
	if !ok(r) {
		return 0
	}
	return obj
}

func (c *MFCamera) CameraControl() uintptr { return c.getService(IID_IAMCameraControl) }
func (c *MFCamera) VideoProcAmp() uintptr  { return c.getService(IID_IAMVideoProcAmp) }

// 返回 (min,max,step,default,caps,ok)。caps 位 0=支持Auto 1=支持Manual
func ctrlGetRange(iface uintptr, prop int32) (ControlRange, bool) {
	var lo, hi, st, df, caps int32
	r := comCall(iface, 3, uintptr(prop),
		uintptr(unsafe.Pointer(&lo)), uintptr(unsafe.Pointer(&hi)),
		uintptr(unsafe.Pointer(&st)), uintptr(unsafe.Pointer(&df)),
		uintptr(unsafe.Pointer(&caps)))
	return ControlRange{lo, hi, st, df, caps}, ok(r)
}

func ctrlGet(iface uintptr, prop int32) (int32, int32, bool) {
	var v, f int32
	r := comCall(iface, 5, uintptr(prop), uintptr(unsafe.Pointer(&v)), uintptr(unsafe.Pointer(&f)))
	return v, f, ok(r)
}

func ctrlSet(iface uintptr, prop, val, flags int32) error {
	r := comCall(iface, 4, uintptr(prop), uintptr(val), uintptr(flags))
	if !ok(r) {
		return fmt.Errorf("Set(prop=%d,val=%d,flags=%d) 失败: 0x%08X", prop, val, flags, uint32(r))
	}
	return nil
}

// ───────────────────────── 小工具 ─────────────────────────

func lower(s string) string {
	b := []byte(s)
	for i := range b {
		if b[i] >= 'A' && b[i] <= 'Z' {
			b[i] += 32
		}
	}
	return string(b)
}

func contains(s, sub string) bool {
	if len(sub) == 0 || len(sub) > len(s) {
		return false
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
