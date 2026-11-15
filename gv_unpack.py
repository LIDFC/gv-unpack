"""Распаковка контейнера GamesVoice GVARC (Definitive.gv) без установщика.

Формат (выяснен по заголовку и проверен по таблице смещений):
  0x00  'GVARC'
  0x10  u32 число файлов
  0x14  u32 начало таблицы файлов (36)
  0x18  u32 размер таблицы; данные начинаются с 0x14+0x18
  0x23  u8  ключ XOR для данных
  далее записи с 0x24:
    u32 длина имени, имя (путь через '\\'),
    u32 0, u32 число чанков, u64 полный размер,
    N * (u32 размер распакованный, u32 размер упакованный, u64 смещение)
  чанк = XOR(ключ) поверх блока LZ4 (без frame-заголовка)

Запуск (папка с Definitive.gv):  python gv_unpack.py [файл.gv] [--list]
Нужен модуль lz4:  pip install lz4  (иначе работает медленный запасной декодер)
Ничего, кроме папки extracted, не трогает.
"""
import struct
import sys
import time
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
GV = Path(args[0] if args else "Definitive.gv")
OUT = GV.parent / "extracted"
LIST_ONLY = "--list" in sys.argv

try:
    import lz4.block as _lz4

    def lz4_decode(data, usize):
        return _lz4.decompress(data, uncompressed_size=usize)

    FAST = True
except ImportError:
    FAST = False

    def lz4_decode(src, usize):
        """Чистый Python, медленно (единицы МБ/с). Лучше pip install lz4."""
        out = bytearray()
        i, n = 0, len(src)
        while i < n:
            tok = src[i]
            i += 1
            ll = tok >> 4
            if ll == 15:
                while True:
                    b = src[i]
                    i += 1
                    ll += b
                    if b != 255:
                        break
            out += src[i:i + ll]
            i += ll
            if i >= n:
                break
            off = src[i] | (src[i + 1] << 8)
            i += 2
            ml = tok & 15
            if ml == 15:
                while True:
                    b = src[i]
                    i += 1
                    ml += b
                    if b != 255:
                        break
            ml += 4
            if off == 0 or off > len(out):
                raise ValueError("битый LZ4: неверное смещение")
            if off >= ml:
                s = len(out) - off
                out += out[s:s + ml]
            else:
                pat = bytes(out[-off:])
                out += (pat * (ml // off + 1))[:ml]
        if len(out) != usize:
            raise ValueError(f"размер {len(out)} != ожидаемого {usize}")
        return bytes(out)


def parse(f):
    h = f.read(0x28)
    if h[:5] != b"GVARC":
        sys.exit("Это не GVARC")
    count, tbl, tblsize = struct.unpack_from("<III", h, 0x10)
    key = h[0x23]
    data_start = tbl + tblsize
    f.seek(tbl)
    table = f.read(tblsize)
    entries, p = [], 0
    for _ in range(count):
        (nlen,) = struct.unpack_from("<I", table, p)
        p += 4
        name = table[p:p + nlen].decode("utf-8", "replace")
        p += nlen
        _, nch, total = struct.unpack_from("<IIQ", table, p)
        p += 16
        chunks = [struct.unpack_from("<IIQ", table, p + 16 * k) for k in range(nch)]
        p += 16 * nch
        entries.append((name, total, chunks))
    if p != tblsize:
        print(f"! предупреждение: разобрано {p} байт таблицы из {tblsize}")
    return key, data_start, entries


def safe_path(name):
    parts = [x for x in name.replace("\\", "/").split("/") if x]
    if not parts or any(x in ("..", ".") or ":" in x for x in parts):
        raise ValueError(f"подозрительный путь: {name}")
    return OUT.joinpath(*parts)


def main():
    if not GV.exists():
        sys.exit(f"Нет файла {GV}")
    if not FAST:
        print("lz4 не установлен: работаю медленным декодером. Лучше: pip install lz4")
    with GV.open("rb") as f:
        key, data_start, entries = parse(f)
        print(f"Ключ XOR: {key:#04x}, данные с {data_start}, файлов: {len(entries)}")
        for name, total, chunks in entries:
            print(f"  {name}  {total:,} байт, чанков {len(chunks)}")
        if LIST_ONLY:
            return
        table = bytes((b ^ key) for b in range(256))
        table = bytes.maketrans(bytes(range(256)), table)
        for name, total, chunks in entries:
            dst = safe_path(name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            print(f"\n-> {dst}")
            written, t0 = 0, time.time()
            with dst.open("wb") as o:
                for k, (us, cs, off) in enumerate(chunks, 1):
                    f.seek(off)
                    raw = f.read(cs)
                    if len(raw) != cs:
                        sys.exit("файл .gv обрезан")
                    data = lz4_decode(raw.translate(table), us)
                    if len(data) != us:
                        sys.exit(f"чанк {k}: размер {len(data)} != {us}")
                    o.write(data)
                    written += us
                    print(f"\r  чанк {k}/{len(chunks)}  {written/1048576:,.0f} МБ  "
                          f"{written/1048576/max(time.time()-t0,.001):.1f} МБ/с", end="", flush=True)
            print()
            if written != total:
                print(f"  ! размер {written} не равен заявленному {total}")
            low = name.lower()
            with dst.open("rb") as r:
                if low.endswith(".mp4"):
                    r.seek(4)
                    print("  проверка mp4:", "ok" if r.read(4) == b"ftyp" else "НЕ ftyp, ключ/формат не тот")
                elif low.endswith(".pak"):
                    r.seek(max(0, written - 300))
                    tail = r.read()
                    print("  проверка pak:", "ok, футер UE найден" if bytes.fromhex("E1126F5A") in tail
                          else "футер не найден (проверь вручную)")
    print(f"\nГотово. Результат в {OUT}")


main()
