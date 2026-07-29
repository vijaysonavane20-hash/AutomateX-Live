#!/usr/bin/env python3
r"""
================================================================================
  SAMPLE COLLECTOR — har shop se N random documents copy karo
  ------------------------------------------------------------------------------
  MAQSAD (purpose):
    Project ke liye document FORMAT samajhna hai. Isliye har shop se
    kuch random documents nikal ke ek alag folder mein rakh dete hain,
    taaki ek jagah baith ke saare formats dekhe ja sakein.

  LOGIC (simple):
    1. STORE_MASTER_NEW.xlsx se shop list lo   (FlowAI jaisa hi)
    2. Har shop ka download folder banao:
         SOURCE \ SEGMENT \ "ProfitCode StoreName"
    3. Us folder ko recursively scan karo (MM-MONTH \ DD-MM-YYYY \ files)
    4. N documents chuno — VARIETY pehle:
         alag file type (PDF / Excel / Image) + alag date folder
    5. Copy karo:  DEST \ SEGMENT \ CODE - StoreName \ CODE_date_filename
    6. Excel index banao — kaunsa sample kahan se aaya

  IMPORTANT:
    • Ye script SIRF COPY karta hai. Source ko haath nahi lagata.
    • Outlook ki zaroorat NAHI — sirf already-downloaded folder padhta hai.
    • Pehle SCAN hota hai, count dikhta hai, TAB copy confirm poochta hai.

  INSTALL:  pip install openpyxl
================================================================================
"""

import os
import re
import sys
import random
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from collections import defaultdict

os.chdir(Path(__file__).parent)

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
except ImportError as e:
    print(f"\n  Missing package: {e}")
    print("  Run:  pip install openpyxl\n")
    input("  Press Enter to exit ..."); sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG  — yahan apne raste badal sakte ho
# ══════════════════════════════════════════════════════════════════════════════

# NOTE: asli raste (network IP / username) is file mein NAHI likhte —
# ye repo public hai. Wo sample_paths.txt mein rehte hain (gitignored).
# Pehli baar chalane pe script khud poochega aur save kar dega.
PATHS_FILE = "sample_paths.txt"

SOURCE_BASE = ""     # sample_paths.txt se aayega
DEST_BASE   = ""     # sample_paths.txt se aayega

PER_SHOP_DEFAULT = 3

MONTH_FOLDER = {1:"01-JANUARY",2:"02-FEBRUARY",3:"03-MARCH",4:"04-APRIL",
                5:"05-MAY",6:"06-JUNE",7:"07-JULY",8:"08-AUGUST",
                9:"09-SEPTEMBER",10:"10-OCTOBER",11:"11-NOVEMBER",12:"12-DECEMBER"}

# File classification — variety ke liye
KIND_EXT = {
    "PDF":     {".pdf"},
    "EXCEL":   {".xlsx", ".xls", ".xlsm", ".xlsb", ".csv"},
    "IMAGE":   {".png", ".jpg", ".jpeg", ".jfif", ".tiff", ".bmp", ".webp", ".gif"},
    "WORD":    {".doc", ".docx"},
    "ARCHIVE": {".zip", ".rar"},
}
KIND_RANK = {"PDF": 0, "EXCEL": 1, "IMAGE": 2, "WORD": 3, "ARCHIVE": 4, "JUNK": 8}

# Signature / inline junk images — inhe SABSE LAST priority (ya skip)
JUNK_IMG = re.compile(
    r"^(Outlook-[A-Za-z0-9]+"
    r"|image\d*"
    r"|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\.",
    re.IGNORECASE)
MIN_IMG_BYTES = 25_000          # isse chhoti image = logo/signature, kaam ki nahi
SYS_SKIP = re.compile(r"\.(p7s|dat|ini|tmp|db)$", re.IGNORECASE)


def load_paths():
    """
    Raste sample_paths.txt se padho. Nahi mila to poochho aur save karo.
    Ye file .gitignore mein hai — GitHub pe kabhi nahi jayegi.
    Format (2 lines):
        SOURCE=\\\\server\\share\\...
        DEST=C:\\Users\\<you>\\Desktop\\CASH SITUATION SAMPLE
    """
    global SOURCE_BASE, DEST_BASE
    vals = {}
    if os.path.exists(PATHS_FILE):
        with open(PATHS_FILE, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    vals[k.strip().upper()] = v.strip()

    SOURCE_BASE = vals.get("SOURCE", "")
    DEST_BASE   = vals.get("DEST", "")

    if not SOURCE_BASE:
        print("\n  ── PEHLI BAAR SETUP ──")
        print("  SOURCE = wo folder jahan FlowAI ne documents download kiye")
        SOURCE_BASE = input("  SOURCE : ").strip().strip('"')
    if not DEST_BASE:
        print("  DEST   = jahan samples copy karne hain")
        DEST_BASE = input("  DEST   : ").strip().strip('"')

    if not (SOURCE_BASE and DEST_BASE):
        return False

    if not os.path.exists(PATHS_FILE):
        try:
            with open(PATHS_FILE, "w", encoding="utf-8") as fh:
                fh.write("# Local only — GitHub pe nahi jata (.gitignore)\n")
                fh.write(f"SOURCE={SOURCE_BASE}\n")
                fh.write(f"DEST={DEST_BASE}\n")
            print(f"  → Raste {PATHS_FILE} mein save ho gaye "
                  f"(agli baar nahi poochega)")
        except Exception as e:
            print(f"  ! {PATHS_FILE} save nahi hua: {e}")
    return True


def find_master():
    for p in (os.path.join("master", "STORE_MASTER_NEW.xlsx"),
              "STORE_MASTER_NEW.xlsx",
              os.path.join("master", "STORE_MASTER_NEW - Copy.xlsx"),
              "STORE_MASTER_NEW - Copy.xlsx"):
        if os.path.exists(p):
            return p
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  WINDOWS LONG PATH  (260 char limit bypass — deep folders mein zaroori hai)
# ══════════════════════════════════════════════════════════════════════════════

def lp(path):
    r"""Long path prefix lagao: C:\x -> \\?\C:\x  |  \\srv\x -> \\?\UNC\srv\x"""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def unlp(path):
    r"""Report ke liye \\?\ prefix hatao — warna Excel mein ganda dikhta hai."""
    p = str(path)
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*]', "", str(s)).strip()


# ══════════════════════════════════════════════════════════════════════════════
#  STORE MASTER  (FlowAI wali same logic — columns match karte hain)
# ══════════════════════════════════════════════════════════════════════════════

def load_master(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["STORE MASTER"] if "STORE MASTER" in wb.sheetnames else wb.active
    stores = []
    first = True
    for row in ws.iter_rows(values_only=True):
        if first:
            first = False; continue
        if not row or not row[1]:
            continue
        code = str(row[1]).strip().upper()
        if not code or code == "SHORT CODE":
            continue
        if str(row[9] or "ACTIVE").strip().upper() == "INACTIVE":
            continue
        stores.append(dict(
            profit  = str(row[0] or "").strip(),
            code    = code,
            name    = str(row[2] or "").strip(),
            segment = str(row[3] or "").strip().upper(),
            subj_kw = str(row[5] or "").strip().upper().replace("\xa0", " "),
            fmt     = str(row[7] or "DATE-MONTH-YEAR").strip().upper(),
            loc     = str(row[8] or "FILENAME").strip().upper(),
        ))
    wb.close()
    return stores


def shop_folder(store):
    """FlowAI ne jis folder mein download kiya tha, wahi banao."""
    return os.path.join(
        SOURCE_BASE,
        safe_name(store["segment"]) or "UNKNOWN",
        safe_name(f"{store['profit']} {store['name']}"))


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN  — ek shop ke saare documents dhoondo
# ══════════════════════════════════════════════════════════════════════════════

def classify(fname, size):
    ext = Path(fname).suffix.lower()
    for kind, exts in KIND_EXT.items():
        if ext in exts:
            if kind == "IMAGE" and (JUNK_IMG.match(fname) or size < MIN_IMG_BYTES):
                return "JUNK"
            return kind
    return "OTHER"


DATE_FOLDER = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")


def scan_shop(folder, months=None):
    """
    Shop folder ko recursively scan karo.
    months = {"07-JULY", ...}  ya None (= sab mahine)
    Return: list of dicts
    """
    out = []
    if not os.path.isdir(lp(folder)):
        return out, "FOLDER NOT FOUND"

    try:
        for root, dirs, files in os.walk(lp(folder)):
            rel = os.path.relpath(root, lp(folder))
            parts = [p for p in rel.split(os.sep) if p and p != "."]

            # month filter — pehla level MM-MONTH hota hai
            if months and parts:
                if parts[0].upper() not in months:
                    dirs[:] = []
                    continue

            # date folder (DD-MM-YYYY) dhoondo path mein
            datefolder = ""
            for p in parts:
                if DATE_FOLDER.match(p):
                    datefolder = p
                    break

            for fn in files:
                if SYS_SKIP.search(fn):
                    continue
                full = os.path.join(root, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                if size == 0:
                    continue
                out.append(dict(
                    path       = full,
                    name       = fn,
                    size       = size,
                    kind       = classify(fn, size),
                    datefolder = datefolder,
                    month      = parts[0] if parts else "",
                ))
    except Exception as e:
        return out, f"SCAN ERROR: {e}"

    return out, ("EMPTY" if not out else "OK")


# ══════════════════════════════════════════════════════════════════════════════
#  PICK  — variety pehle: alag type + alag date
# ══════════════════════════════════════════════════════════════════════════════

def pick_samples(files, n):
    """
    N documents chuno taaki MAXIMUM variety mile:
      • alag-alag file type (PDF, Excel, Image)
      • alag-alag date folder
      • JUNK (signature images) sabse last
    """
    if len(files) <= n:
        return list(files)

    # JUNK (signature/logo images) ko variety round se BAAHAR rakho —
    # ek aur asli PDF hamesha ek signature image se behtar hai.
    good = [f for f in files if f["kind"] != "JUNK"]
    junk = [f for f in files if f["kind"] == "JUNK"]

    picked, taken, used_dates = [], set(), set()

    def take(pool, respect_date):
        for f in pool:
            if id(f) in taken:
                continue
            if respect_date and f["datefolder"] and f["datefolder"] in used_dates:
                continue
            picked.append(f); taken.add(id(f))
            if f["datefolder"]:
                used_dates.add(f["datefolder"])
            return True
        return False

    by_kind = defaultdict(list)
    for f in good:
        by_kind[f["kind"]].append(f)
    for lst in by_kind.values():
        random.shuffle(lst)
    kinds = sorted(by_kind, key=lambda k: (KIND_RANK.get(k, 5), -len(by_kind[k])))

    # Round-robin: har kind se baari-baari.
    # Pehle naye date folder, phir date repeat allowed.
    for respect_date in (True, False):
        progress = True
        while len(picked) < n and progress:
            progress = False
            for k in kinds:
                if len(picked) >= n:
                    break
                if take(by_kind[k], respect_date):
                    progress = True

    # Sabse aakhir mein junk — sirf jab aur kuch bacha hi nahi
    if len(picked) < n:
        random.shuffle(junk)
        for f in junk:
            if len(picked) >= n:
                break
            if id(f) not in taken:
                picked.append(f); taken.add(id(f))

    return picked[:n]


# ══════════════════════════════════════════════════════════════════════════════
#  COPY
# ══════════════════════════════════════════════════════════════════════════════

def copy_sample(src, store, f, idx):
    """
    DEST \ SEGMENT \ CODE - StoreName \ CODE_DDMMYYYY_originalname.ext
    Naam mein code+date isliye taaki file dekh ke pata chale kiski hai.
    """
    dest_dir = os.path.join(
        DEST_BASE,
        safe_name(store["segment"]) or "UNKNOWN",
        safe_name(f"{store['code']} - {store['name']}"))
    os.makedirs(lp(dest_dir), exist_ok=True)

    tag = f["datefolder"].replace("-", "") if f["datefolder"] else "NODATE"
    base = safe_name(f["name"]) or f["name"]
    fname = f"{store['code']}_{tag}_{idx}_{base}"

    # bahut lamba naam chhota karo (Windows)
    if len(fname) > 150:
        stem, ext = os.path.splitext(fname)
        fname = stem[:150 - len(ext)] + ext

    dest = os.path.join(dest_dir, fname)
    if os.path.exists(lp(dest)):
        return dest, "ALREADY EXISTS"
    shutil.copy2(lp(src), lp(dest))
    return dest, "COPIED"


# ══════════════════════════════════════════════════════════════════════════════
#  REPORT
# ══════════════════════════════════════════════════════════════════════════════

BLUE = PatternFill("solid", fgColor="1F4E79")
WBF  = Font(color="FFFFFF", bold=True)
GRN  = PatternFill("solid", fgColor="C8E6C9")
REDF = PatternFill("solid", fgColor="FFCDD2")
ORG  = PatternFill("solid", fgColor="FFE0B2")
YEL  = PatternFill("solid", fgColor="FFF9C4")


def _hdr(ws, cols):
    ws.append(cols)
    for c in ws[1]:
        c.fill = BLUE; c.font = WBF
        c.alignment = Alignment(horizontal="center", vertical="center",
                                wrap_text=True)
    ws.freeze_panes = "A2"


def _aw(ws, mx=55):
    for col in ws.columns:
        w = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(w + 2, mx)


def report(copied, missing, stats, per_shop, months):
    wb = openpyxl.Workbook()

    ws = wb.active; ws.title = "SAMPLES"
    _hdr(ws, ["Segment", "Code", "Store Name", "Doc Date", "Kind",
              "File Name", "Size KB", "Subject Keyword", "Date Format",
              "Date Location", "Source Path", "Sample Path", "Status"])
    for r in copied:
        ws.append([r["segment"], r["code"], r["name"], r["docdate"], r["kind"],
                   r["file"], round(r["size"] / 1024, 1), r["subj_kw"],
                   r["fmt"], r["loc"], unlp(r["src"]), unlp(r["dest"]),
                   r["status"]])
        st = r["status"]
        f = (GRN if st == "COPIED" else YEL if st == "ALREADY EXISTS" else REDF)
        ws.cell(row=ws.max_row, column=13).fill = f
    _aw(ws)

    ws2 = wb.create_sheet("NO DOCUMENTS")
    _hdr(ws2, ["Segment", "Code", "Store Name", "Reason", "Expected Folder"])
    for m in missing:
        ws2.append([m["segment"], m["code"], m["name"], m["reason"],
                    unlp(m["folder"])])
        for c in ws2[ws2.max_row]:
            c.fill = ORG
    _aw(ws2)

    ws3 = wb.create_sheet("SUMMARY")
    for row in [("SAMPLE COLLECTOR REPORT", ""),
                ("Generated", datetime.now().strftime("%d-%m-%Y %H:%M")),
                ("Source", SOURCE_BASE),
                ("Destination", DEST_BASE),
                ("Per shop", per_shop),
                ("Months scanned", ", ".join(sorted(months)) if months else "ALL"),
                ("", ""),
                ("Shops in master (active)", stats["total"]),
                ("Shops with documents", stats["with_docs"]),
                ("Shops with NO documents", stats["no_docs"]),
                ("Files copied", stats["copied"]),
                ("Files already existed", stats["existed"]),
                ("Copy errors", stats["errors"]),
                ("", ""),
                ("--- FILE KIND BREAKDOWN ---", "")]:
        ws3.append(list(row))
    for k, v in sorted(stats["kinds"].items(), key=lambda x: -x[1]):
        ws3.append([k, v])
    _aw(ws3)

    name = f"SAMPLE_INDEX_{datetime.now().strftime('%d%m%Y_%H%M')}.xlsx"
    wb.save(name)
    return name


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "=" * 70)
    print("  SAMPLE COLLECTOR — har shop se random documents")
    print("=" * 70)

    if not load_paths():
        print("\n  ERROR: SOURCE / DEST raste chahiye.")
        input("\n  Press Enter ..."); return

    master = find_master()
    if not master:
        print("\n  ERROR: STORE_MASTER_NEW.xlsx nahi mila!")
        print("  Is script ke saath same folder mein rakho (ya master\\ ke andar).")
        input("\n  Press Enter ..."); return

    stores = load_master(master)
    print(f"\n  Master      : {master}  ({len(stores)} active shops)")
    print(f"  Source      : {SOURCE_BASE}")
    print(f"  Destination : {DEST_BASE}")

    if not os.path.isdir(lp(SOURCE_BASE)):
        print("\n  ⚠ SOURCE folder reachable nahi hai!")
        print("  Network drive connected hai? VPN on hai?")
        input("\n  Press Enter ..."); return

    # ── kitne documents per shop ──────────────────────────────────────────────
    v = input(f"\n  Har shop se kitne documents? [{PER_SHOP_DEFAULT}] : ").strip()
    per_shop = int(v) if v.isdigit() and int(v) > 0 else PER_SHOP_DEFAULT

    # ── month filter (network share pe speed ke liye) ─────────────────────────
    print("\n  ── MONTH FILTER (network scan tez karne ke liye) ──")
    print("  Example: 06,07     |  [Enter] = saare mahine")
    mv = input("  Month numbers : ").strip()
    months = None
    if mv:
        months = {MONTH_FOLDER[int(x)] for x in re.split(r"[,\s]+", mv)
                  if x.strip().isdigit() and 1 <= int(x) <= 12}
        months = months or None
    print(f"  → {', '.join(sorted(months)) if months else 'ALL MONTHS'}")

    # ── segment filter ────────────────────────────────────────────────────────
    segs = sorted({s["segment"] for s in stores})
    print("\n  ── SEGMENT FILTER ──")
    for i, sg in enumerate(segs, 1):
        cnt = sum(1 for s in stores if s["segment"] == sg)
        print(f"    {i}. {sg}  ({cnt} shops)")
    sv = input("  Numbers (e.g. 1,3) or [Enter] = ALL : ").strip()
    if sv:
        want = {segs[int(x) - 1] for x in re.split(r"[,\s]+", sv)
                if x.strip().isdigit() and 1 <= int(x) <= len(segs)}
        if want:
            stores = [s for s in stores if s["segment"] in want]
            print(f"  → {len(stores)} shops selected")

    # ── SCAN ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  SCANNING {len(stores)} shops ... (network share — thoda time lagega)")
    print("=" * 70 + "\n")

    plan, missing = [], []
    kinds = defaultdict(int)

    for i, s in enumerate(stores, 1):
        folder = shop_folder(s)
        files, status = scan_shop(folder, months)

        if status != "OK":
            missing.append(dict(segment=s["segment"], code=s["code"],
                                name=s["name"], reason=status, folder=folder))
            print(f"  [{i}/{len(stores)}] {s['code']:<8} ✗ {status}")
            continue

        chosen = pick_samples(files, per_shop)
        for f in chosen:
            kinds[f["kind"]] += 1
        plan.append((s, chosen))
        kd = ", ".join(sorted({f["kind"] for f in chosen}))
        print(f"  [{i}/{len(stores)}] {s['code']:<8} ✓ {len(files):>4} files "
              f"→ {len(chosen)} chosen  [{kd}]")

    total_files = sum(len(c) for _, c in plan)
    print("\n" + "=" * 70)
    print(f"  SCAN DONE")
    print(f"    Shops with documents : {len(plan)}")
    print(f"    Shops with NOTHING   : {len(missing)}")
    print(f"    Files to copy        : {total_files}")
    print("=" * 70)

    if total_files == 0:
        print("\n  Kuch copy karne ko nahi mila.")
        input("\n  Press Enter ..."); return

    ok = input(f"\n  {total_files} files copy karein? (y/n) : ").strip().lower()
    if ok not in ("y", "yes", ""):
        print("  Cancelled — kuch copy nahi hua.")
        input("\n  Press Enter ..."); return

    # ── COPY ──────────────────────────────────────────────────────────────────
    print(f"\n  Copying to: {DEST_BASE}\n")
    os.makedirs(lp(DEST_BASE), exist_ok=True)

    copied = []
    n_copy = n_exist = n_err = 0

    for s, chosen in plan:
        for idx, f in enumerate(chosen, 1):
            rec = dict(segment=s["segment"], code=s["code"], name=s["name"],
                       docdate=f["datefolder"], kind=f["kind"], file=f["name"],
                       size=f["size"], subj_kw=s["subj_kw"], fmt=s["fmt"],
                       loc=s["loc"], src=f["path"], dest="", status="")
            try:
                dest, st = copy_sample(f["path"], s, f, idx)
                rec["dest"], rec["status"] = dest, st
                if st == "COPIED":
                    n_copy += 1
                else:
                    n_exist += 1
            except Exception as e:
                rec["status"] = f"ERROR: {e}"
                n_err += 1
                print(f"    ✗ {s['code']} {f['name'][:40]} — {e}")
            copied.append(rec)
        print(f"    ✓ {s['code']:<8} {len(chosen)} files")

    stats = dict(total=len(stores), with_docs=len(plan), no_docs=len(missing),
                 copied=n_copy, existed=n_exist, errors=n_err, kinds=dict(kinds))

    print("\n  Report bana raha hoon ...")
    rpt = report(copied, missing, stats, per_shop, months)

    print("\n" + "=" * 70)
    print(f"  DONE")
    print(f"    Copied          : {n_copy}")
    print(f"    Already existed : {n_exist}")
    print(f"    Errors          : {n_err}")
    print(f"    Sample folder   : {DEST_BASE}")
    print(f"    Index file      : {rpt}")
    print("=" * 70)
    input("\n  Press Enter to exit ...")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("\n  Error — Press Enter to exit ...")
