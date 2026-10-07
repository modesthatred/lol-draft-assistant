"""Генератор иконки приложения — бумажный воздушный фонарь (sky lantern).

Ноль зависимостей: рисуем пиксели чистой математикой, пакуем в PNG через
zlib/struct, оборачиваем в ICO (PNG-записи поддерживаются Windows с Vista).
Запуск:  python tools/make_icon.py  ->  иконка icon.ico в корне проекта.

Иконку используем тремя способами:
  * вшиваем в .exe (DraftAssistant.spec: EXE(icon='icon.ico', ...));
  * кладём рядом с .exe через datas=('icon.ico', '.') и грузим в трей через
    LoadImageW(LR_LOADFROMFILE) — раньше в трее была системная иконка
    IDI_APPLICATION, которую и не разобрать в трее;
  * файл живёт в репозитории как логотип.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICO_PATH = ROOT / "icon.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def skylamp(size: int) -> bytearray:
    """RGBA-буфер `size`x`size` с воздушным фонарём и тёплым ореолом."""
    n = size
    buf = bytearray(n * n * 4)

    xc = n / 2
    ytop = 0.09 * n
    h = 0.84 * n
    w = 0.315 * n
    gc = ytop + h / 2                       # центр ореола

    def p_of(y: float) -> float:
        return max(0.0, min(1.0, (y - ytop) / h))

    def r_of(p: float) -> float:
        cap = 0.20
        if p <= cap:
            return w * math.sin(math.pi / 2 * (p / cap if cap else 1.0))
        if p <= 0.80:
            t = (p - cap) / 0.60
            return w * (1.0 + 0.10 * math.sin(math.pi * t))
        t = (p - 0.80) / 0.13
        return w * 1.05 * (1.0 - t) + 0.50 * w * t

    ribs = (0.08, 0.26, 0.44, 0.62, 0.80, 0.905)
    band = max(1.0, 0.038 * n)
    outline = max(1.0, int(0.015 * n))
    aa = 1.15

    # звёзды на небе (для крупных размеров, иначе каша)
    stars = []
    if n >= 48:
        cnt = int(5 * (n / 64.0) ** 2)
        for k in range(cnt):
            sx = (k * 97 + 11) % n
            sy = (k * 53 + 3) % int(0.62 * n)
            if math.hypot(sx - xc, sy - gc) < 0.42 * n:
                continue
            stars.append((sx, sy, (k * 31) % 3))
    if n >= 96:
        for k in range(cnt):
            sx = (k * 193 + 7) % n
            sy = (k * 71 + 5) % int(0.55 * n)
            if math.hypot(sx - xc, sy - gc) < 0.50 * n:
                continue
            stars.append((sx, sy, 2))

    px = pw = ph = 0.0                      # тёплые тона
    def over(out_rgb, dst_index, scol):
        sr, sg, sb, sa = scol
        if sa <= 0:
            return
        dr, dg, db, da = (buf[dst_index], buf[dst_index + 1],
                          buf[dst_index + 2], buf[dst_index + 3])
        if da <= 0:
            out_rgb[0], out_rgb[1], out_rgb[2], out_rgb[3] = sr, sg, sb, sa
            return
        ia = da / 255.0
        oa = sa / 255.0
        a = oa + ia * (1.0 - oa)
        if a <= 0:
            out_rgb[0] = out_rgb[1] = out_rgb[2] = out_rgb[3] = 0
            return
        out_rgb[0] = int((sr * oa + dr * ia * (1.0 - oa)) / a + 0.5)
        out_rgb[1] = int((sg * oa + dg * ia * (1.0 - oa)) / a + 0.5)
        out_rgb[2] = int((sb * oa + db * ia * (1.0 - oa)) / a + 0.5)
        out_rgb[3] = int(a * 255.0 + 0.5)

    # слой ореола (мягкий тёплый градиент вокруг фонаря)
    rg = 0.62 * n
    for ay in range(n):
        for ax in range(n):
            d = math.hypot((ax - xc) * (xc / max(rg, 1)), ay - gc) / rg
            a = int(70.0 * clamp01(1.0 - d) ** 1.7)
            if a <= 0:
                continue
            buf[(ay * n + ax) * 4] = 247
            buf[(ay * n + ax) * 4 + 1] = 200
            buf[(ay * n + ax) * 4 + 2] = 138
            buf[(ay * n + ax) * 4 + 3] = a

    tmp = [0, 0, 0, 0]
    for ay in range(n):
        p = p_of(ay)
        rh = r_of(p)
        is_rib = any(abs(p - rp) * h <= band for rp in ribs)
        for ax in range(n):
            ddx = abs(ax - xc)
            base = int(ax)
            # бумага 2D: источник и цвет по вертикали
            top = (248, 206, 128)
            low = (214, 132, 58)
            pcol = tuple(int(top[c] + (low[c] - top[c]) * clamp01(p)) for c in range(3))
            fill = False
            if ddx <= rh - aa + 0.0:
                fill = True
                al = 255
            elif ddx <= rh:
                fill = True
                al = int(255 * clamp01((rh - ddx) / aa))
            if fill:
                if is_rib:
                    pcol = (int(pcol[0] * 0.52), int(pcol[1] * 0.42),
                            int(pcol[2] * 0.34))
                # 3D-подсветка: края темнее, низ чуть ярче у огня
                sh = 1.0 - 0.30 * clamp01(ddx / max(rh, 1e-6))
                sh = min(1.0, sh + 0.08 * clamp01((0.95 - p) / 0.30))
                tmp[0] = int(pcol[0] * sh)
                tmp[1] = int(pcol[1] * sh)
                tmp[2] = int(pcol[2] * sh)
                tmp[3] = al
                over(tmp, (ay * n + ax) * 4, tmp)
            elif ddx <= rh + outline:
                al = int(255 * clamp01(1.0 - (ddx - rh) / outline))
                if al > 0:
                    tmp[0], tmp[1], tmp[2], tmp[3] = 64, 34, 18, al
                    over(tmp, (ay * n + ax) * 4, tmp)

    # пламя в устье фонаря + его блик
    yf = ytop + 0.95 * h
    for ay in range(int(yf - 0.05 * n), int(yf + 0.055 * n)):
        for ax in range(int(xc - 0.12 * n), int(xc + 0.12 * n)):
            ddx = abs(ax - xc)
            t = (ay - yf) / (0.055 * n)
            fw = 0.085 * n * (1.0 - abs(t))
            if ddx > fw + 1.0:
                continue
            glu = (ay - yf) / (0.16 * n)
            gl = int(80.0 * clamp01(1.0 - math.hypot((glu) * 0.9,
                                                     ddx / (0.16 * n))))
            if gl > 0:
                tmp[0], tmp[1], tmp[2], tmp[3] = 255, 170, 80, gl
                over(tmp, (ay * n + ax) * 4, tmp)
            if ddx <= fw:
                core = clamp01(1.0 - ddx / max(fw, 1e-6))
                row = clamp01(1.0 - abs(t))
                inside = row * (1.0 - 0.35 * (1.0 - core))
                if inside > 0.15:
                    if core > 0.72:
                        fl = (255, 248, 214)
                    else:
                        fl = (255, 176, 70)
                    tmp[0], tmp[1], tmp[2], tmp[3] = (fl[0], fl[1], fl[2],
                                                      int(255 * inside))
                    over(tmp, (ay * n + ax) * 4, tmp)

    # звёзды
    for (sx, sy, kind) in stars:
        for oy in range(2 if kind >= 2 else 1):
            for ox in range(2 if kind >= 2 else 1):
                i = ((sy + oy) * n + (sx + ox)) * 4
                if sy + oy < n and sx + ox < n:
                    tmp[0], tmp[1], tmp[2], tmp[3] = 203, 216, 255, 150
                    over(tmp, i, tmp)

    return buf


def png_blob(buf: bytearray, size: int) -> bytes:
    """Упаковать RGBA-буфер в 8-битный PNG (color type 6)."""
    n = size
    raw = bytearray()
    for y in range(n):
        raw.append(0)
        start = y * n * 4
        raw += buf[start:start + n * 4]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            chunk(b"IEND", b""))


def build_ico() -> bytes:
    blobs = {s: png_blob(skylamp(s), s) for s in SIZES}
    header = struct.pack("<HHH", 0, 1, len(blobs))
    entries = []
    data = b""
    offset = 6 + 16 * len(blobs)
    for s in SIZES:
        b = blobs[s]
        entries.append(struct.pack("<BBBBHHII", s & 0xFF or 0, s & 0xFF or 0,
                                   0, 0, 1, 32, len(b), offset))
        data += b
        offset += len(b)
    return header + b"".join(entries) + data


def main() -> None:
    ico = build_ico()
    ICO_PATH.write_bytes(ico)
    print(f"icon.ico: {len(ico)} bytes, sizes {SIZES}")


if __name__ == "__main__":
    main()