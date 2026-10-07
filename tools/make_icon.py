"""Генератор иконки приложения — красный бумажный фонарь (как 🏮).

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


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    t = clamp01(t)
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


GOLD = (246, 196, 83)
GOLD_CAP = (252, 211, 120)
PAPER_TOP = (192, 54, 46)
PAPER_BOT = (110, 22, 18)


def red_lantern(size: int) -> bytearray:
    """RGBA-буфер `size`x`size` с красным бумажным фонарём и тёплым ореолом.

    Корпус — вертикальный эллипс из тёмно-красной бумаги, по нему золотые
    меридианы-рёбра и горизонтальное кольцо; сверху золотой колпачок, снизу
    кольцо с тёмным устьем и маленькая кисточка.
    """
    n = size
    buf = bytearray(n * n * 4)

    xc = n / 2
    ytop = 0.16 * n
    h = 0.64 * n
    ybot = ytop + h
    w = 0.315 * n
    gc = ytop + h / 2

    def p_of(y: float) -> float:
        return clamp01((y - ytop) / h)

    def r_of(p: float) -> float:
        q = 2.0 * p - 1.0
        return w * math.sqrt(1.0 - q * q)

    band = max(1.0, 0.045 * n)
    outline = max(1.0, int(0.012 * n))
    aa = 1.1
    ribs = (0.0, 0.48, 0.88)          # меридианы по нормализованной |u|
    ring_p = 0.82                     # горизонтальное кольцо корпуса

    tmp = [0, 0, 0, 0]

    def over(idx, scol) -> None:
        sr, sg, sb, sa = scol
        if sa <= 0:
            return
        dr, dg, db, da = (buf[idx], buf[idx + 1],
                          buf[idx + 2], buf[idx + 3])
        if da <= 0:
            buf[idx], buf[idx + 1], buf[idx + 2], buf[idx + 3] = sr, sg, sb, sa
            return
        ia = da / 255.0
        oa = sa / 255.0
        a = oa + ia * (1.0 - oa)
        if a <= 0:
            buf[idx] = buf[idx + 1] = buf[idx + 2] = buf[idx + 3] = 0
            return
        buf[idx] = int((sr * oa + dr * ia * (1.0 - oa)) / a + 0.5)
        buf[idx + 1] = int((sg * oa + dg * ia * (1.0 - oa)) / a + 0.5)
        buf[idx + 2] = int((sb * oa + db * ia * (1.0 - oa)) / a + 0.5)
        buf[idx + 3] = int(a * 255.0 + 0.5)

    # слой ореола — мягкий тёплый градиент вокруг фонаря
    rg = 0.60 * n
    for ay in range(n):
        for ax in range(n):
            d = math.hypot((ax - xc) * (xc / max(rg, 1)), ay - gc) / rg
            a = int(64.0 * clamp01(1.0 - d) ** 1.7)
            if a <= 0:
                continue
            buf[(ay * n + ax) * 4] = 248
            buf[(ay * n + ax) * 4 + 1] = 176
            buf[(ay * n + ax) * 4 + 2] = 74
            buf[(ay * n + ax) * 4 + 3] = a

    # корпус: бумага + золотые рёбра и кольца
    for ay in range(n):
        p = p_of(ay)
        rh = r_of(p)
        if rh <= 0:
            continue
        ring = abs(p - ring_p) * h <= band
        for ax in range(n):
            ddx = abs(ax - xc)
            idx = (ay * n + ax) * 4
            if ddx <= rh:
                al = 255 if ddx <= rh - aa else int(255 * clamp01(
                    (rh - ddx) / aa))
                u = ddx / rh
                is_rib = any(abs(u - k) <= band / rh for k in ribs)
                # 3D: края корпуса темнее
                sh = 1.0 - 0.34 * clamp01(ddx / rh)
                pur = min(1.0, sh + 0.10 * clamp01((0.98 - p) / 0.25))
                if ring:
                    col = lerp(GOLD, GOLD_CAP, pur)
                    col = tuple(int(c * (1.0 - 0.18 * clamp01(ddx / rh)))
                                for c in col)
                elif is_rib:
                    col = tuple(int(c * (1.0 - 0.12 * clamp01(ddx / rh)))
                                for c in GOLD)
                else:
                    col = lerp(PAPER_TOP, PAPER_BOT, p)
                    col = tuple(int(c * sh) for c in col)
                tmp[0], tmp[1], tmp[2], tmp[3] = col[0], col[1], col[2], al
                over(idx, tmp)
            elif ddx <= rh + outline:
                al = int(255 * clamp01(1.0 - (ddx - rh) / outline))
                if al > 0:
                    tmp[0], tmp[1], tmp[2], tmp[3] = 84, 18, 14, al
                    over(idx, tmp)

    # тёплый свет изнутри у нижнего среза
    for ay in range(int(ybot - 0.16 * n), int(ybot + 0.02 * n)):
        for ax in range(int(xc - 0.11 * n), int(xc + 0.11 * n)):
            d = math.hypot((ay - (ybot - 0.06 * n)) / (0.02 * n),
                           (ax - xc) / (0.13 * n))
            gl = int(70.0 * clamp01(1.0 - d))
            if gl > 0:
                tmp[0], tmp[1], tmp[2], tmp[3] = 255, 196, 120, gl
                over((ay * n + ax) * 4, tmp)

    # золотой колпачок сверху + шарик-петелька
    ycap = ytop - 0.02 * n
    hcap = 0.058 * n
    wcap = 0.20 * n
    for ay in range(int(ycap - hcap), int(ycap + hcap + 1)):
        for ax in range(int(xc - wcap), int(xc + wcap + 1)):
            dx = (ax - xc) / wcap
            dy = (ay - ycap) / hcap
            e = dx * dx + dy * dy
            idx = (ay * n + ax) * 4
            if e <= 1.0:
                sh = 1.0 - 0.15 * clamp01(abs(dy))
                col = tuple(int(c * sh) for c in GOLD_CAP)
                tmp[0], tmp[1], tmp[2], tmp[3] = col[0], col[1], col[2], 255
                over(idx, tmp)
            elif e <= 1.12:
                al = int(255 * clamp01(1.0 - (e - 1.0) / 0.12))
                if al > 0:
                    tmp[0], tmp[1], tmp[2], tmp[3] = 150, 106, 30, al
                    over(idx, tmp)

    # кольцо устья + тёмное отверстие
    yring = ybot - 0.004 * n
    wring = 0.155 * n
    hring = 0.042 * n
    for ay in range(int(yring - hring), int(yring + hring + 1)):
        for ax in range(int(xc - wring), int(xc + wring + 1)):
            dx = (ax - xc) / wring
            dy = (ay - yring) / hring
            e = dx * dx + dy * dy
            idx = (ay * n + ax) * 4
            if e <= 1.0:
                if e <= 0.82:
                    tmp[0], tmp[1], tmp[2], tmp[3] = 34, 10, 8, 255
                else:
                    sh = 1.0 - 0.20 * clamp01(abs(dy) / 0.8)
                    col = tuple(int(c * sh) for c in GOLD)
                    tmp[0], tmp[1], tmp[2], tmp[3] = col[0], col[1], col[2], 255
                over(idx, tmp)

    # кисточка под кольцом (на мелких размерах ими не жертвуя шириной)
    if n >= 24:
        ty0 = yring + 0.032 * n
        th = 0.13 * n
        tyb = ty0 + th
        for ay in range(int(ty0 - 1), int(tyb + 1)):
            t = (ay - ty0) / th
            hw = int((0.026 * n) * (1.0 - 0.45 * t) + 0.5)
            aal = max(1.0, 0.045 * n)
            for ax in range(int(xc - hw) - 1, int(xc + hw) + 2):
                ddx = abs(ax - xc)
                if ddx <= hw:
                    al = 255
                elif ddx <= hw + aal:
                    al = int(255 * clamp01(1.0 - (ddx - hw) / aal))
                else:
                    continue
                col = lerp((170, 40, 34), (120, 24, 20), t)
                idx = (ay * n + ax) * 4
                tmp[0], tmp[1], tmp[2], tmp[3] = col[0], col[1], col[2], al
                over(idx, tmp)
        # золотой наконечник
        for ay in range(int(tyb), int(tyb + 0.05 * n) + 1):
            for ax in range(int(xc - 0.035 * n), int(xc + 0.035 * n) + 1):
                d = math.hypot((ay - tyb) / max(0.028 * n, 1e-6),
                               (ax - xc) / max(0.034 * n, 1e-6))
                if d <= 1.0:
                    al = 255 if d <= 0.85 else int(255 * clamp01(
                        1.0 - (d - 0.85) / 0.15))
                    sh = 1.0 - 0.2 * clamp01(abs(ax - xc) / (0.034 * n))
                    col = tuple(int(c * sh) for c in GOLD)
                    tmp[0], tmp[1], tmp[2], tmp[3] = col[0], col[1], col[2], al
                    over((ay * n + ax) * 4, tmp)

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
    blobs = {s: png_blob(red_lantern(s), s) for s in SIZES}
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