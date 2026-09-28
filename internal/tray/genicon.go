//go:build ignore

// genicon renders internal/tray/icon.ico (16x16 + 32x32, 32-bit BGRA): a
// rounded accent-blue square with a white bridge glyph, matching the
// console's accent color. It exists so the icon stays reproducible from
// source; the .ico itself is committed and embedded at build time.
//
// Run from the repo root:
//
//	go run ./internal/tray/genicon.go
package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"image/color"
	"math"
	"os"
)

const (
	accentR, accentG, accentB = 0x2f, 0x5e, 0xe8 // console --accent
)

func main() {
	var buf bytes.Buffer
	sizes := []int{16, 32}

	// ICONDIR
	binary.Write(&buf, binary.LittleEndian, uint16(0)) // reserved
	binary.Write(&buf, binary.LittleEndian, uint16(1)) // type: icon
	binary.Write(&buf, binary.LittleEndian, uint16(len(sizes)))

	// Image payloads are written after the directory; compute offsets.
	headerSize := 6 + 16*len(sizes)
	payloads := make([][]byte, len(sizes))
	offset := headerSize
	for i, s := range sizes {
		p := render(s)
		payloads[i] = encodeDIB(s, p)
		w := byte(s) // 16 and 32 both fit; 256 would encode as 0
		binary.Write(&buf, binary.LittleEndian, w)                       // width
		binary.Write(&buf, binary.LittleEndian, w)                       // height
		binary.Write(&buf, binary.LittleEndian, byte(0))                 // palette
		binary.Write(&buf, binary.LittleEndian, byte(0))                 // reserved
		binary.Write(&buf, binary.LittleEndian, uint16(1))               // planes
		binary.Write(&buf, binary.LittleEndian, uint16(32))              // bpp
		binary.Write(&buf, binary.LittleEndian, uint32(len(payloads[i]))) // image size
		binary.Write(&buf, binary.LittleEndian, uint32(offset))          // offset
		offset += len(payloads[i])
	}
	for _, p := range payloads {
		buf.Write(p)
	}

	if err := os.WriteFile("internal/tray/icon.ico", buf.Bytes(), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Printf("wrote internal/tray/icon.ico (%d bytes)\n", buf.Len())
}

// render draws the icon at size s as BGRA pixels (top-down).
func render(s int) []byte {
	px := make([]byte, s*s*4)
	corner := float64(s) * 0.19 // rounded-corner radius
	for y := 0; y < s; y++ {
		for x := 0; x < s; x++ {
			fx, fy := float64(x)+0.5, float64(y)+0.5
			if !insideRoundedSquare(fx, fy, float64(s), corner) {
				continue // transparent
			}
			c := color.RGBA{accentR, accentG, accentB, 255}
			if glyph(fx, fy, s) {
				c = color.RGBA{255, 255, 255, 255}
			}
			i := (y*s + x) * 4
			px[i+0], px[i+1], px[i+2], px[i+3] = c.B, c.G, c.R, c.A
		}
	}
	return px
}

func insideRoundedSquare(x, y, size, cr float64) bool {
	if x < 0 || y < 0 || x > size || y > size {
		return false
	}
	// Check the four corner circles.
	cx, cy := cr, cr
	if x < cr && y < cr {
		return dist(x, y, cx, cy) <= cr
	}
	if x > size-cr && y < cr {
		return dist(x, y, size-cr, cy) <= cr
	}
	if x < cr && y > size-cr {
		return dist(x, y, cx, size-cr) <= cr
	}
	if x > size-cr && y > size-cr {
		return dist(x, y, size-cr, size-cr) <= cr
	}
	return true
}

func dist(x1, y1, x2, y2 float64) float64 {
	return math.Hypot(x1-x2, y1-y2)
}

// glyph is a bridge silhouette: a deck line, an arch above it, two piers.
// Coordinates are expressed relative to the icon size so 16 and 32 match.
func glyph(x, y float64, s int) bool {
	f := float64(s) / 16 // scale factor vs the 16px design
	// Deck: horizontal bar.
	if y >= 10*f && y < 11.5*f && x >= 2*f && x < 14*f {
		return true
	}
	// Arch: ring segment centered on the deck midpoint.
	d := dist(x, y, 8*f, 10.5*f)
	if d >= 4.2*f && d <= 5.6*f && y <= 10.5*f {
		return true
	}
	// Piers.
	if y >= 11.5*f && y < 14*f {
		if (x >= 3*f && x < 4.5*f) || (x >= 11.5*f && x < 13*f) {
			return true
		}
	}
	return false
}

// encodeDIB wraps top-down BGRA pixels into the ICO payload form:
// BITMAPINFOHEADER + bottom-up XOR data + 1-bit AND mask.
func encodeDIB(s int, topDown []byte) []byte {
	var buf bytes.Buffer
	rowBytes := s * 4
	andRow := ((s + 31) / 32) * 4

	binary.Write(&buf, binary.LittleEndian, uint32(40))          // biSize
	binary.Write(&buf, binary.LittleEndian, int32(s))            // biWidth
	binary.Write(&buf, binary.LittleEndian, int32(s*2))          // biHeight (XOR+AND)
	binary.Write(&buf, binary.LittleEndian, uint16(1))           // biPlanes
	binary.Write(&buf, binary.LittleEndian, uint16(32))          // biBitCount
	binary.Write(&buf, binary.LittleEndian, uint32(0))           // biCompression
	binary.Write(&buf, binary.LittleEndian, uint32(rowBytes*s+andRow*s)) // biSizeImage
	binary.Write(&buf, binary.LittleEndian, int32(0))            // biXPelsPerMeter
	binary.Write(&buf, binary.LittleEndian, int32(0))            // biYPelsPerMeter
	binary.Write(&buf, binary.LittleEndian, uint32(0))           // biClrUsed
	binary.Write(&buf, binary.LittleEndian, uint32(0))           // biClrImportant

	// XOR data, bottom-up.
	for y := s - 1; y >= 0; y-- {
		buf.Write(topDown[y*rowBytes : (y+1)*rowBytes])
	}
	// AND mask: all zero (alpha channel governs transparency).
	buf.Write(make([]byte, andRow*s))
	return buf.Bytes()
}
