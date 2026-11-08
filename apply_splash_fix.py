#!/usr/bin/env python3
"""
apply_splash_fix.py - lascia visibile SOLO lo splash di DigiVol all'avvio.

Cosa si vede all'avvio su Android:
  1. la schermata iniziale del SISTEMA (da Android 12): resa invisibile (sfondo
     nero, icona trasparente);
  2. il PRESPLASH di python-for-android, mentre Python parte. Python impiega
     qualche secondo a partire e questo tempo non si puo' eliminare: si puo'
     pero' riempirlo. Il presplash e' un'immagine nera con il LOGO al centro,
     nello stesso punto e della stessa dimensione del logo dello splash di
     Kivy (le misure sono le costanti PRESPLASH_* di main.py): cosi' il logo
     compare dopo una frazione di secondo, resta fermo, e il passaggio a Kivy
     non si vede; poi sfumano le scritte;
  3. lo splash di DigiVol, disegnato da Kivy.

Uso (nella cartella del progetto, accanto a buildozer.spec e main.py):
    python3 apply_splash_fix.py            presplash con il logo (consigliato)
    python3 apply_splash_fix.py --black    presplash tutto nero (nessun logo)
    python3 apply_splash_fix.py --dry-run  mostra cosa farebbe, senza scrivere

Il logo viene da icon.png; per l'immagine serve Pillow (python3 -m pip install
pillow). Senza Pillow o senza icon.png si usa il presplash nero.

E' sicuro rilanciarlo: non duplica nulla. Prima di modificare crea una copia
di sicurezza, buildozer.spec.bak.
"""

import argparse
import configparser
import io
import re
import struct
import sys
import zlib
from pathlib import Path

THEME = "DigiVolTheme"
RES_DIR = "res_overlay"
PRESPLASH_BLACK = "presplash_black.png"
PRESPLASH_LOGO = "presplash_logo.png"

# (file di origine nella cartella del progetto, destinazione nella cartella res di Android)
RESOURCE_MAP = (
    (f"{RES_DIR}/digivol_theme.xml", "values/digivol_theme.xml"),
    (f"{RES_DIR}/digivol_theme_v31.xml", "values-v31/digivol_theme.xml"),
    (f"{RES_DIR}/digivol_splash_empty.xml", "drawable/digivol_splash_empty.xml"),
)


def black_png(size=64):
    """PNG nero pieno generato qui (nessuna libreria esterna)."""
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * size for _ in range(size))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def read_presplash_constants(base):
    """Dimensione dell'immagine e del logo: la fonte e' main.py (PRESPLASH_*),
    cosi' lo splash di Kivy e questa immagine non possono andare fuori sincrono."""
    names = (("PRESPLASH_CANVAS_W", 1080), ("PRESPLASH_CANVAS_H", 2400), ("PRESPLASH_LOGO_PX", 750))
    try:
        text = (base / "main.py").read_text(encoding="utf-8")
    except OSError:
        return tuple(default for _, default in names)
    values = []
    for name, default in names:
        match = re.search(rf"^{name}\s*=\s*(\d+)", text, re.M)
        values.append(int(match.group(1)) if match else default)
    return tuple(values)


def logo_png(icon_path, canvas_w, canvas_h, logo_px):
    """Immagine nera canvas_w x canvas_h con il logo (lato maggiore = logo_px)
    esattamente al centro. Richiede Pillow."""
    from PIL import Image  # ImportError gestito da chi chiama

    icon = Image.open(icon_path).convert("RGBA")
    scale = logo_px / max(icon.size)
    icon = icon.resize((max(1, round(icon.width * scale)), max(1, round(icon.height * scale))),
                       Image.LANCZOS)
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 255))
    canvas.alpha_composite(icon, ((canvas_w - icon.width) // 2, (canvas_h - icon.height) // 2))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def theme_xml(parent, android12):
    items = ['        <item name="android:windowBackground">@android:color/black</item>']
    if android12:
        items += [
            '        <item name="android:windowSplashScreenBackground">@android:color/black</item>',
            '        <item name="android:windowSplashScreenAnimatedIcon">@drawable/digivol_splash_empty</item>',
        ]
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        f'    <style name="{THEME}" parent="{parent}">\n' + "\n".join(items) + "\n    </style>\n</resources>\n"
    )


EMPTY_ICON_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
    '    android:width="108dp" android:height="108dp"\n'
    '    android:viewportWidth="108" android:viewportHeight="108">\n'
    '    <path android:fillColor="#00000000" android:pathData="M0,0h1v1h-1z"/>\n'
    '</vector>\n'
)


# ---------------------------------------------------------------------------
# Modifica di buildozer.spec (testo, senza toccare commenti e ordine)
# ---------------------------------------------------------------------------

def app_bounds(lines):
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == "[app]"), None)
    if start is None:
        sys.exit("Nel file non c'e' la sezione [app]: e' davvero un buildozer.spec?")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[.+\]\s*$", lines[i])), len(lines))
    return start + 1, end


def find_active(lines, key):
    lo, hi = app_bounds(lines)
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*(.*?)\s*$")
    for i in range(lo, hi):
        m = pattern.match(lines[i].rstrip("\r\n"))
        if m:
            return i, m.group(1)
    return None, None


def set_key(lines, key, value, eol):
    """Imposta key = value nella sezione [app]. Restituisce (azione, valore precedente)."""
    index, old = find_active(lines, key)
    new_line = f"{key} = {value}{eol}"
    if index is not None:
        if old == value:
            return "gia' a posto", old
        lines[index] = new_line
        return "modificata", old
    lo, hi = app_bounds(lines)
    commented = [i for i in range(lo, hi)
                 if re.match(rf"^\s*#\s*{re.escape(key)}\s*=", lines[i])]
    if commented:
        # Sostituisce l'esempio vuoto se c'e', altrimenti l'ultimo esempio.
        empty = [i for i in commented if re.match(rf"^\s*#\s*{re.escape(key)}\s*=\s*$", lines[i].rstrip("\r\n"))]
        lines[(empty or commented)[-1]] = new_line
        return "attivata", None
    insert_at = hi
    while insert_at > lo and not lines[insert_at - 1].strip():
        insert_at -= 1          # prima delle righe vuote che chiudono la sezione
    if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r")):
        lines[insert_at - 1] += eol
    lines.insert(insert_at, new_line)
    return "aggiunta", None


def add_to_list(lines, key, items, eol):
    index, old = find_active(lines, key)
    current = [part.strip() for part in (old or "").split(",") if part.strip()]
    added = [item for item in items if item not in current]
    if not added and index is not None:
        return "gia' a posto", old
    action, _ = set_key(lines, key, ",".join(current + added), eol)
    return action, old


def read_spec(path):
    raw = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    return raw.splitlines(keepends=True), eol


def main():
    ap = argparse.ArgumentParser(description="Nasconde presplash e schermata di sistema all'avvio.")
    ap.add_argument("--spec", default="buildozer.spec")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--black", action="store_true", help="presplash tutto nero, senza logo")
    ap.add_argument("--icon", default="icon.png", help="immagine del logo (default: icon.png)")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        sys.exit(f"Non trovo {spec_path}. Lancialo dalla cartella che contiene buildozer.spec.")
    base = spec_path.resolve().parent
    lines, eol = read_spec(spec_path)

    # --- verifiche sulle impostazioni attuali ---
    _, api_text = find_active(lines, "android.api")
    api = int(api_text) if api_text and api_text.isdigit() else None
    _, fullscreen = find_active(lines, "fullscreen")
    parent = ("@android:style/Theme.NoTitleBar.Fullscreen" if (fullscreen or "").strip() == "1"
              else "@android:style/Theme.NoTitleBar")
    use_v31 = api is None or api >= 31
    report = []

    if not use_v31:
        report.append(f"ATTENZIONE: android.api = {api}. Per nascondere la schermata di sistema "
                      "di Android 12+ serve android.api = 33 (o piu'). Imposta quel valore e rilancia "
                      "lo script; per ora applico solo il presplash nero.")

    # --- presplash: logo al centro (o tutto nero) ---
    presplash_name, presplash_bytes = PRESPLASH_BLACK, black_png()
    if not args.black:
        icon_path = base / args.icon
        canvas_w, canvas_h, logo_px = read_presplash_constants(base)
        try:
            presplash_bytes = logo_png(icon_path, canvas_w, canvas_h, logo_px)
            presplash_name = PRESPLASH_LOGO
            report.append(f"presplash con il logo: {canvas_w}x{canvas_h}, logo {logo_px} px, da {args.icon}")
        except ImportError:
            report.append("ATTENZIONE: Pillow non e' installato, uso il presplash nero. "
                          "Per il logo: python3 -m pip install pillow, poi rilancia.")
        except OSError as exc:
            report.append(f"ATTENZIONE: non riesco a leggere {args.icon} ({exc}): uso il presplash nero.")

    # --- file di risorse ---
    files = {
        f"{RES_DIR}/digivol_theme.xml": theme_xml(parent, android12=False).encode(),
        f"{RES_DIR}/digivol_theme_v31.xml": theme_xml(parent, android12=True).encode(),
        f"{RES_DIR}/digivol_splash_empty.xml": EMPTY_ICON_XML.encode(),
        presplash_name: presplash_bytes,
    }
    if not use_v31:  # senza tema non servono nemmeno i file di risorse
        for name in (f"{RES_DIR}/digivol_theme.xml", f"{RES_DIR}/digivol_theme_v31.xml",
                     f"{RES_DIR}/digivol_splash_empty.xml"):
            files.pop(name)
    for name, content in files.items():
        target = base / name
        state = "gia' presente" if target.exists() and target.read_bytes() == content else "creato"
        if not args.dry_run and state == "creato":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        report.append(f"  file {name}: {state}")

    # --- buildozer.spec ---
    changes = []
    changes.append(("presplash.filename", *set_key(lines, "presplash.filename", f"%(source.dir)s/{presplash_name}", eol)))
    changes.append(("android.presplash_color", *set_key(lines, "android.presplash_color", "black", eol)))
    if use_v31:
        changes.append(("android.apptheme", *set_key(lines, "android.apptheme", f"@style/{THEME}", eol)))
        res_items = [f"{src}:{dst}" for src, dst in RESOURCE_MAP]
        changes.append(("android.add_resources", *add_to_list(lines, "android.add_resources", res_items, eol)))
    if find_active(lines, "icon.filename")[0] is None:
        if (base / "icon.png").exists():
            changes.append(("icon.filename", *set_key(lines, "icon.filename", "%(source.dir)s/icon.png", eol)))
        else:
            report.append("ATTENZIONE: icon.filename non e' impostato e non trovo icon.png.")

    report.append("buildozer.spec:")
    for key, action, old in changes:
        extra = f" (prima: {old})" if old not in (None, "") and action == "modificata" else ""
        report.append(f"  {key}: {action}{extra}")

    if not args.dry_run:
        backup = spec_path.with_suffix(spec_path.suffix + ".bak")
        if not backup.exists():
            backup.write_bytes(spec_path.read_bytes())
        spec_path.write_bytes("".join(lines).encode("utf-8"))
        # Controllo finale: il file deve restare leggibile da Buildozer.
        parser = configparser.RawConfigParser(strict=False)
        parser.read(spec_path, encoding="utf-8")
        assert parser.has_option("app", "presplash.filename"), "buildozer.spec non e' piu' valido"

    print("\n".join(report))
    print("\n(nessuna modifica scritta: era --dry-run)" if args.dry_run else
          f"\nFatto. Ora: git add buildozer.spec {presplash_name} res_overlay main.py userdata.py")


if __name__ == "__main__":
    main()
