# -*- coding: utf-8 -*-
"""
Audiobookery - local audiobook workshop built on Chatterbox Multilingual TTS
==============================================

Desktopová aplikace (tkinter) pro převod e-knih (TXT, EPUB, FB2, HTML)
na audioknihu pomocí Chatterbox Multilingual TTS s podporou češtiny.

Spuštění:  python audiobookery.py   (nebo run.bat)
"""

import os
import sys
import re
import json
import time
import queue
import wave
import shutil
import struct
import threading
import traceback
import subprocess
import contextlib
import collections
from pathlib import Path

from preklady import T, JAZYKY, nastav_jazyk, aktualni_jazyk

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# --------------------------------------------------------------------------
# Cesty a cache - vše relativně vedle skriptu, ať se nic neukládá do profilu
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / "model_cache"
TEMP_DIR = APP_DIR / "temp"
FONT_DIR = APP_DIR / "fonts"
HLASY_DIR = APP_DIR / "hlasy"
CONFIG_PATH = APP_DIR / "config.json"

CACHE_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)
HLASY_DIR.mkdir(exist_ok=True)

# Musí se nastavit PŘED importem huggingface_hub / chatterbox
os.environ.setdefault("HF_HOME", str(CACHE_DIR))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(CACHE_DIR / "hub"))
os.environ.setdefault("TORCH_HOME", str(CACHE_DIR / "torch"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

APP_NAME = "Audiobookery"

# Znaky, které Windows v názvu souboru nedovolí
ZAKAZANE_ZNAKY = r'[<>:"/\|?*]'
VERSION = "1.3.0"

KATALOG_PATH = APP_DIR / "modely.json"


def nacti_katalog() -> dict:
    """Načte katalog jazyků syntézy. Uživatel ho může rozšířit, aplikace ho jen čte."""
    zaloha = {"vychozi": "en",
              "jazyky": [{"kod": "en", "nazev": "English", "zdroj": "zakladni", "token": True}]}
    try:
        data = json.loads(KATALOG_PATH.read_text(encoding="utf-8"))
        if data.get("jazyky"):
            return data
    except Exception:
        pass
    return zaloha


KATALOG = nacti_katalog()


def jazyk_podle_klice(klic: str) -> dict:
    """Klíč je 'kod' nebo 'kod|repo' - jeden jazyk může mít víc variant modelu."""
    for j in KATALOG["jazyky"]:
        if klic_jazyka(j) == klic:
            return j
    for j in KATALOG["jazyky"]:
        if j["kod"] == klic:
            return j
    return KATALOG["jazyky"][0]


def klic_jazyka(j: dict) -> str:
    return f"{j['kod']}|{j['repo']}" if j.get("repo") else j["kod"]

NAPOVEDA_TOKEN = (
    "Repozitář s českým checkpointem je na Hugging Face chráněný. Postup:\n"
    "  1. přihlaste se na https://huggingface.co a na stránce modelu\n"
    "     https://huggingface.co/Thomcles/Chatterbox-TTS-Czech potvrďte přístup,\n"
    "  2. vytvořte si read token (Settings -> Access Tokens),\n"
    "  3. spusťte v prostředí aplikace 'huggingface-cli login'\n"
    "     nebo nastavte proměnnou prostředí HF_TOKEN."
)

DEFAULT_CONFIG = {
    "vstupni_soubor": "",
    "referencni_wav": "",
    "vystupni_slozka": str(APP_DIR / "vystup"),
    "vystupni_nazev": "audiokniha",
    "format": "WAV",
    "mp3_bitrate": "128k",
    "max_znaku": 200,
    "pauza_ms": 250,
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    # Ponecháno na výchozí hodnotě knihovny. Zvyšování se měřením neosvědčilo:
    # lupance v tichu neubyly a přibylo vynucené ukončování kvůli zacyklení.
    "min_p": 0.05,
    "odstranit_lupance": True,
    "seed": 0,
    # Jazyk syntézy je nezávislý na jazyku rozhraní - v českém rozhraní
    # klidně vyrábíte anglickou audioknihu.
    "jazyk_textu": "en",
    "zarizeni": "auto",
    "testovaci_veta": "Dobrý den, toto je ukázka českého hlasu pro vaši audioknihu.",
    "poslouchat": False,
    "naskok_s": 120,
    "obalka": True,
    # 0 = odvodit od volné paměti karty
    "pracovniku": 0,
    "jazyk": "en",
}


# --------------------------------------------------------------------------
#  Vzhled - tmavá minimalistická paleta
# --------------------------------------------------------------------------

BARVY = {
    "pozadi":    "#0e0e11",
    "panel":     "#16161a",
    "panel_svetlejsi": "#1f1f25",
    "linka":     "#26262c",
    "text":      "#e6e6ea",
    # Obsah polí je o stupeň tlumenější než popisky - drží to hierarchii,
    # aby cesty a názvy souborů nekřičely víc než struktura okna.
    "text_pole": "#c4c4ce",
    "tlacitko":  "#1e1e25",
    "tlacitko_aktivni": "#2a2a33",
    "tlumeny":   "#7d7d88",
    "akcent":    "#7aa2f7",
    "uspech":    "#7ee787",
    "varovani":  "#f0883e",
    "chyba":     "#f76f6f",
}

FONT_RODINA = "JetBrains Mono"
FONT_ZALOHY = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New")


def nacti_font() -> str:
    """Zaregistruje přibalený JetBrains Mono jen pro tento proces.

    Font se neinstaluje do systému - Windows umí přes AddFontResourceEx
    s příznakem FR_PRIVATE zpřístupnit soubor jen běžící aplikaci, takže
    není potřeba správce ani zásah do profilu uživatele.
    """
    import ctypes
    from tkinter import font as tkfont

    FR_PRIVATE = 0x10
    nacteno = 0
    if FONT_DIR.is_dir():
        for ttf in sorted(FONT_DIR.glob("JetBrainsMono-*.ttf")):
            try:
                if ctypes.windll.gdi32.AddFontResourceExW(str(ttf), FR_PRIVATE, 0):
                    nacteno += 1
            except Exception:
                break

    # Ověříme, že Tk font skutečně vidí - registrace sama o sobě nestačí
    try:
        dostupne = set(tkfont.families())
    except Exception:
        dostupne = set()

    if FONT_RODINA in dostupne:
        return FONT_RODINA
    for zaloha in FONT_ZALOHY:
        if zaloha in dostupne:
            return zaloha
    return "TkDefaultFont"


# ==========================================================================
#  Extrakce textu z jednotlivých formátů
# ==========================================================================

def detekuj_kodovani(cesta: Path) -> str:
    """Odhadne kódování souboru pomocí chardet, s rozumnými fallbacky."""
    import chardet

    raw = cesta.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    vysledek = chardet.detect(raw[:200_000])
    kodovani = (vysledek.get("encoding") or "utf-8").lower()
    jistota = vysledek.get("confidence") or 0.0

    # chardet u českých textů občas hádá cyrilici nebo se netrefí vůbec
    if jistota < 0.6 or kodovani in ("ascii", "windows-1251", "koi8-r", "maccyrillic"):
        for kandidat in ("utf-8", "cp1250", "iso-8859-2"):
            try:
                raw.decode(kandidat)
                return kandidat
            except UnicodeDecodeError:
                continue
    return kodovani


def nacti_txt(cesta: Path) -> str:
    kodovani = detekuj_kodovani(cesta)
    try:
        return cesta.read_text(encoding=kodovani, errors="replace")
    except LookupError:
        return cesta.read_text(encoding="utf-8", errors="replace")


def _html_na_text(html: str) -> str:
    from bs4 import BeautifulSoup

    polevka = BeautifulSoup(html, "html.parser")
    for tag in polevka(["head", "title", "script", "style", "nav", "header",
                        "footer", "noscript", "sup", "figcaption"]):
        tag.decompose()

    # Blokové elementy oddělíme prázdným řádkem, ať se věty neslepí
    for tag in polevka.find_all(["p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
        tag.append("\n")

    text = polevka.get_text("\n")
    return text


def nacti_html(cesta: Path) -> str:
    kodovani = detekuj_kodovani(cesta)
    html = cesta.read_text(encoding=kodovani, errors="replace")
    return _html_na_text(html)


def nacti_epub(cesta: Path) -> str:
    import ebooklib
    from ebooklib import epub

    kniha = epub.read_epub(str(cesta))
    casti = []

    # Pokud EPUB definuje spine, respektujeme pořadí kapitol z něj
    polozky = []
    try:
        for idref, _linear in kniha.spine:
            polozka = kniha.get_item_with_id(idref)
            if polozka is not None and polozka.get_type() == ebooklib.ITEM_DOCUMENT:
                polozky.append(polozka)
    except Exception:
        polozky = []

    if not polozky:
        polozky = list(kniha.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    for polozka in polozky:
        try:
            obsah = polozka.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        text = _html_na_text(obsah).strip()
        if len(text) > 20:
            casti.append(text)

    return "\n\n".join(casti)


def nacti_fb2(cesta: Path) -> str:
    from bs4 import BeautifulSoup

    kodovani = detekuj_kodovani(cesta)
    obsah = cesta.read_text(encoding=kodovani, errors="replace")

    try:
        polevka = BeautifulSoup(obsah, "lxml-xml")
    except Exception:
        polevka = BeautifulSoup(obsah, "html.parser")

    # Poznámky pod čarou a obrázky do audioknihy nepatří
    for tag in polevka.find_all("binary"):
        tag.decompose()

    tela = polevka.find_all("body")
    if not tela:
        return polevka.get_text("\n")

    casti = []
    for telo in tela:
        if (telo.get("name") or "").lower() in ("notes", "comments", "footnotes"):
            continue
        radky = []
        for uzel in telo.find_all(["title", "subtitle", "p", "v", "text-author"]):
            # Nadpis <title> obaluje vlastní <p> - vnořené uzly bychom četli dvakrát
            if uzel.name != "title" and uzel.find_parent(["title", "subtitle"]) is not None:
                continue
            radek = uzel.get_text(" ", strip=True)
            if radek:
                radky.append(radek)
        if radky:
            casti.append("\n".join(radky))

    return "\n\n".join(casti)


def nacti_soubor(cesta: Path) -> str:
    """Vrátí čistý text ze souboru podle přípony."""
    pripona = cesta.suffix.lower()
    if pripona == ".txt":
        return nacti_txt(cesta)
    if pripona == ".epub":
        return nacti_epub(cesta)
    if pripona == ".fb2":
        return nacti_fb2(cesta)
    if pripona in (".html", ".htm", ".xhtml"):
        return nacti_html(cesta)
    if pripona == ".md":
        return nacti_txt(cesta)
    raise ValueError(f"Nepodporovaný formát souboru: {pripona}")


def _nadpis_z_html(html: str) -> str:
    """Vytáhne první nadpis dokumentu - slouží jako název kapitoly."""
    from bs4 import BeautifulSoup

    try:
        polevka = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    for uroven in ("h1", "h2", "h3", "title"):
        uzel = polevka.find(uroven)
        if uzel is not None:
            nadpis = " ".join(uzel.get_text(" ", strip=True).split())
            if nadpis:
                return nadpis[:120]
    return ""


def nacti_epub_kapitoly(cesta: Path) -> list:
    """EPUB rozpadlý na kapitoly. Jedna položka spine = jedna kapitola."""
    import ebooklib
    from ebooklib import epub

    kniha = epub.read_epub(str(cesta))

    polozky = []
    try:
        for idref, _linear in kniha.spine:
            polozka = kniha.get_item_with_id(idref)
            if polozka is not None and polozka.get_type() == ebooklib.ITEM_DOCUMENT:
                polozky.append(polozka)
    except Exception:
        polozky = []
    if not polozky:
        polozky = list(kniha.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    kapitoly = []
    for polozka in polozky:
        try:
            html = polozka.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        text = _html_na_text(html).strip()
        # Obálky, tiráže a obsah bývají skoro prázdné - ty přeskočíme
        if len(text) < 200:
            continue
        kapitoly.append({"nazev": _nadpis_z_html(html), "text": text})
    return kapitoly


def nacti_fb2_kapitoly(cesta: Path) -> list:
    """FB2 rozpadlý na kapitoly podle sekcí nejvyšší úrovně."""
    from bs4 import BeautifulSoup

    kodovani = detekuj_kodovani(cesta)
    obsah = cesta.read_text(encoding=kodovani, errors="replace")
    try:
        polevka = BeautifulSoup(obsah, "lxml-xml")
    except Exception:
        polevka = BeautifulSoup(obsah, "html.parser")

    for tag in polevka.find_all("binary"):
        tag.decompose()

    def text_uzlu(uzel) -> str:
        radky = []
        for u in uzel.find_all(["title", "subtitle", "p", "v", "text-author"]):
            if u.name != "title" and u.find_parent(["title", "subtitle"]) is not None:
                continue
            radek = u.get_text(" ", strip=True)
            if radek:
                radky.append(radek)
        return "\n".join(radky)

    kapitoly = []
    for telo in polevka.find_all("body"):
        if (telo.get("name") or "").lower() in ("notes", "comments", "footnotes"):
            continue
        sekce = [s for s in telo.find_all("section", recursive=False)]
        if not sekce:
            sekce = telo.find_all("section")
        if not sekce:
            text = text_uzlu(telo).strip()
            if len(text) >= 200:
                kapitoly.append({"nazev": "", "text": text})
            continue
        for s in sekce:
            text = text_uzlu(s).strip()
            if len(text) < 200:
                continue
            nadpis = ""
            t = s.find("title")
            if t is not None:
                nadpis = " ".join(t.get_text(" ", strip=True).split())[:120]
            kapitoly.append({"nazev": nadpis, "text": text})
    return kapitoly


def nacti_kapitoly(cesta: Path):
    """Vrátí (kapitoly, ma_kapitoly).

    Kapitoly umí jen formáty, které je samy nesou - EPUB a FB2. U prostého
    textu by se musely hádat podle nadpisů, což je nespolehlivé, takže se
    o to ani nepokoušíme a vrátíme jednu kapitolu s celou knihou.
    """
    pripona = cesta.suffix.lower()
    if pripona == ".epub":
        kapitoly = nacti_epub_kapitoly(cesta)
        if len(kapitoly) > 1:
            return kapitoly, True
        text = "\n\n".join(k["text"] for k in kapitoly) if kapitoly else nacti_epub(cesta)
        return [{"nazev": "", "text": text}], False
    if pripona == ".fb2":
        kapitoly = nacti_fb2_kapitoly(cesta)
        if len(kapitoly) > 1:
            return kapitoly, True
        text = "\n\n".join(k["text"] for k in kapitoly) if kapitoly else nacti_fb2(cesta)
        return [{"nazev": "", "text": text}], False
    return [{"nazev": "", "text": nacti_soubor(cesta)}], False


# ==========================================================================
#  Normalizace textu a dělení na bloky
# ==========================================================================

# Zkratky, po kterých tečka NEznamená konec věty
ZKRATKY = {
    "např", "atd", "apod", "tj", "tzv", "tzn", "resp", "mj", "popř", "příp",
    "str", "č", "čís", "obr", "tab", "kap", "sv", "st", "stol", "n. l", "př",
    "ing", "mgr", "bc", "judr", "mudr", "phdr", "rndr", "doc", "prof", "csc",
    "pí", "p", "arch", "gen", "plk", "kpt", "sl", "roč", "vyd", "red", "pozn",
    "zn", "spol", "s. r. o", "a. s", "km", "kg", "hod", "min", "sek", "tis",
    "mil", "mld", "hl", "m", "cm", "mm", "j", "jr", "sen", "viz", "cca",
}

RE_MEZERY = re.compile(r"[ \t\u00a0\u2007\u202f]+")
RE_PRAZDNE_RADKY = re.compile(r"\n{3,}")
RE_ROZDELENI_SLOVA = re.compile(r"(\w)-\n(\w)")  # dělení slova na konci řádku

# Znaky, kterými může končit věta
KONCOVE_ZNAKY = '.!?:"\')»'


def spoj_zalomene_radky(text: str) -> str:
    """Spojí tvrdě zalomené řádky uvnitř odstavce zpět do jedné věty.

    TXT knihy bývají zalomené na ~70 znaků a bez tohoto kroku by se věty
    rozpadly doprostřed - model by pak četl útržky.
    """
    odstavce = []

    for odstavec in text.split("\n\n"):
        radky = [radek.strip() for radek in odstavec.split("\n") if radek.strip()]
        if not radky:
            continue

        spojene = [radky[0]]
        for radek in radky[1:]:
            predchozi = spojene[-1]
            prvni_znak = radek[:1]
            konci_vetou = predchozi[-1:] in KONCOVE_ZNAKY

            if prvni_znak.islower() or prvni_znak in ",;":
                # Věta zjevně pokračuje
                spojene[-1] = predchozi + " " + radek
            elif not konci_vetou and len(predchozi) >= 40:
                # Dlouhý řádek bez koncové interpunkce = zalomení uprostřed věty.
                # Krátký řádek necháváme být - bývá to nadpis nebo replika dialogu.
                spojene[-1] = predchozi + " " + radek
            else:
                spojene.append(radek)

        odstavce.append("\n".join(spojene))

    return "\n\n".join(odstavce)


def normalizuj_text(text: str) -> str:
    """Vyčistí text tak, aby se dal rozumně předhodit TTS modelu."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Sjednocení typografických znaků - model je jinak čte divně nebo je ignoruje
    nahrady = {
        "\u2019": "'", "\u2018": "'", "\u201a": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00ab": '"', "\u00bb": '"',
        "\u2026": "...",
        "\u2013": " - ", "\u2014": " - ", "\u2212": "-",
        "\u00ad": "",          # měkký spojovník
        "\ufeff": "",
        "\u200b": "",
        "*": "", "#": "", "_": " ", "|": " ",
    }
    for co, cim in nahrady.items():
        text = text.replace(co, cim)

    text = RE_ROZDELENI_SLOVA.sub(r"\1\2", text)
    text = RE_MEZERY.sub(" ", text)
    text = "\n".join(radek.strip() for radek in text.split("\n"))
    text = RE_PRAZDNE_RADKY.sub("\n\n", text)

    # Odstranění dekorativních oddělovačů typu "* * *" nebo "-----"
    text = re.sub(r"^[\s\-\=\~\.\*\+]{3,}$", "", text, flags=re.MULTILINE)
    text = RE_PRAZDNE_RADKY.sub("\n\n", text)

    text = spoj_zalomene_radky(text)

    return text.strip()


def rozdel_na_vety(text: str) -> list:
    """Rozdělí odstavec na věty s ohledem na české zkratky a řadové číslovky."""
    vety = []
    aktualni = []
    i = 0
    delka = len(text)

    while i < delka:
        znak = text[i]
        aktualni.append(znak)

        if znak in ".!?":
            # Načteme případné navazující interpunkční znaky (?!, ...)
            j = i + 1
            while j < delka and text[j] in ".!?\"')]»":
                aktualni.append(text[j])
                j += 1

            zbytek = text[j:]
            hotovo = "".join(aktualni)

            if not zbytek.strip():
                vety.append(hotovo)
                aktualni = []
                i = j
                continue

            # Za koncem věty musí následovat mezera
            if zbytek[:1] not in (" ", "\n"):
                i = j
                continue

            konec = True
            if znak == ".":
                # Zkratka? ("např.", "tzv." ...)
                posledni_slovo = re.split(r"[\s(\[\"']", hotovo.rstrip("."))[-1].lower()
                posledni_slovo = posledni_slovo.strip(".,;:")
                if posledni_slovo in ZKRATKY:
                    konec = False
                # Řadová číslovka nebo datum: "12. ledna", "1. kapitola"
                if re.search(r"\d\.$", hotovo):
                    konec = False
                # Jednopísmenná iniciála: "J. Novák"
                if re.search(r"(^|\s)\w\.$", hotovo):
                    konec = False
                # Následuje malé písmeno -> věta pokračuje
                dalsi = zbytek.lstrip()[:1]
                if dalsi and dalsi.islower():
                    konec = False

            if konec:
                vety.append(hotovo)
                aktualni = []

            i = j
            continue

        i += 1

    if aktualni:
        vety.append("".join(aktualni))

    return [v.strip() for v in vety if v.strip()]


def _rozsekej_dlouhou_vetu(veta: str, limit: int) -> list:
    """Rozdělí příliš dlouhou větu na čárkách, spojkách a nakonec i na mezerách."""
    if len(veta) <= limit:
        return [veta]

    kusy = []
    # Nejdřív zkusíme dělit na čárkách a střednících
    casti = re.split(r"(?<=[,;:])\s+", veta)
    buffer = ""
    for cast in casti:
        if not buffer:
            buffer = cast
        elif len(buffer) + 1 + len(cast) <= limit:
            buffer += " " + cast
        else:
            kusy.append(buffer)
            buffer = cast
    if buffer:
        kusy.append(buffer)

    # Co je pořád moc dlouhé, rozsekáme po slovech
    vysledek = []
    for kus in kusy:
        if len(kus) <= limit:
            vysledek.append(kus)
            continue
        slova = kus.split(" ")
        buffer = ""
        for slovo in slova:
            if not buffer:
                buffer = slovo
            elif len(buffer) + 1 + len(slovo) <= limit:
                buffer += " " + slovo
            else:
                vysledek.append(buffer)
                buffer = slovo
        if buffer:
            vysledek.append(buffer)

    return [k.strip() for k in vysledek if k.strip()]


def rozdel_na_bloky(text: str, max_znaku: int = 200) -> list:
    """Text -> seznam bloků do max_znaku znaků, dělených po větách."""
    bloky = []

    for odstavec in text.split("\n"):
        odstavec = odstavec.strip()
        if not odstavec:
            continue

        # Odstavce bez písmen (samá čísla / interpunkce) nemá smysl číst
        if not re.search(r"[a-zA-ZáčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", odstavec):
            continue

        aktualni = ""
        for veta in rozdel_na_vety(odstavec):
            for kus in _rozsekej_dlouhou_vetu(veta, max_znaku):
                if not aktualni:
                    aktualni = kus
                elif len(aktualni) + 1 + len(kus) <= max_znaku:
                    aktualni += " " + kus
                else:
                    bloky.append(aktualni)
                    aktualni = kus
        if aktualni:
            bloky.append(aktualni)

    return bloky


# ==========================================================================
#  Práce se zvukem (WAV zápis, ffmpeg)
# ==========================================================================

class WavZapisovac:
    """Postupně zapisuje bloky do jednoho WAV souboru - nedrží vše v paměti."""

    def __init__(self, cesta: Path, vzorkovaci_frekvence: int, kanaly: int = 1):
        self.cesta = cesta
        self.sr = vzorkovaci_frekvence
        self.soubor = wave.open(str(cesta), "wb")
        self.soubor.setnchannels(kanaly)
        self.soubor.setsampwidth(2)          # 16-bit PCM
        self.soubor.setframerate(vzorkovaci_frekvence)
        self.pocet_vzorku = 0

    def zapis(self, vzorky):
        """vzorky = numpy pole float32 v rozsahu -1..1"""
        import numpy as np

        data = np.asarray(vzorky, dtype="float32").reshape(-1)
        data = np.clip(data, -1.0, 1.0)
        pcm = (data * 32767.0).astype("<i2")
        self.soubor.writeframes(pcm.tobytes())
        self.pocet_vzorku += len(pcm)

    def zapis_ticho(self, milisekundy: int):
        if milisekundy <= 0:
            return
        pocet = int(self.sr * milisekundy / 1000.0)
        self.soubor.writeframes(b"\x00\x00" * pocet)
        self.pocet_vzorku += pocet

    @property
    def delka_s(self) -> float:
        return self.pocet_vzorku / float(self.sr) if self.sr else 0.0

    def zavri(self):
        try:
            self.soubor.close()
        except Exception:
            pass


class WavZapisovacRaw:
    """WAV zapisovač, který umí i navázat na rozepsaný soubor.

    Modul `wave` otevírá jen pro zápis od nuly a hlavičku dopisuje až při
    zavření, takže s ním pokračování po přerušení udělat nejde. Kanonická
    hlavička mono 16bit PCM má pevných 44 bajtů, takže si ji píšeme sami.
    """

    HLAVICKA = 44

    def __init__(self, cesta: Path, sr: int, pripojit_od_vzorku: int = 0):
        self.cesta = Path(cesta)
        self.sr = int(sr)
        self.pocet_vzorku = 0

        if pripojit_od_vzorku > 0 and self.cesta.exists():
            # Useknout přesně na zaznamenaný počet vzorků - případný půlblok
            # z přerušeného běhu se tím zahodí a naváže se čistě.
            self.soubor = open(self.cesta, "r+b")
            self.soubor.truncate(self.HLAVICKA + pripojit_od_vzorku * 2)
            self.soubor.seek(0, os.SEEK_END)
            self.pocet_vzorku = pripojit_od_vzorku
        else:
            self.cesta.parent.mkdir(parents=True, exist_ok=True)
            self.soubor = open(self.cesta, "wb")
            self.soubor.write(self._hlavicka(0))

    def _hlavicka(self, pocet_vzorku: int) -> bytes:
        data = pocet_vzorku * 2
        return (b"RIFF" + struct.pack("<I", 36 + data) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, 1, 1, self.sr, self.sr * 2, 2, 16) +
                b"data" + struct.pack("<I", data))

    def zapis(self, vzorky):
        import numpy as np
        data = np.clip(np.asarray(vzorky, dtype="float32").reshape(-1), -1.0, 1.0)
        pcm = (data * 32767.0).astype("<i2")
        self.soubor.write(pcm.tobytes())
        self.pocet_vzorku += len(pcm)

    def zapis_ticho(self, ms: int):
        if ms <= 0:
            return
        pocet = int(self.sr * ms / 1000.0)
        self.soubor.write(b"\x00\x00" * pocet)
        self.pocet_vzorku += pocet

    @property
    def delka_s(self) -> float:
        return self.pocet_vzorku / float(self.sr) if self.sr else 0.0

    def zavri(self):
        try:
            self.soubor.flush()
            self.soubor.seek(0)
            self.soubor.write(self._hlavicka(self.pocet_vzorku))
            self.soubor.close()
        except Exception:
            pass


def otisk_zadani(cesta_knihy: Path, p: dict, celkem_bloku: int) -> str:
    """Otisk zdroje a všeho, co ovlivňuje zvuk.

    Když se změní kterákoli položka, navazovat na starý výstup nedává smysl -
    druhá polovina knihy by zněla jinak než první.
    """
    import hashlib

    h = hashlib.sha256()
    try:
        h.update(Path(cesta_knihy).read_bytes())
    except Exception:
        h.update(str(cesta_knihy).encode("utf-8"))
    for klic in ("jazyk_textu", "referencni_wav", "exaggeration", "cfg_weight",
                 "temperature", "min_p", "odstranit_lupance", "seed", "pauza_ms",
                 "format", "bitrate"):
        h.update(f"{klic}={p.get(klic)}".encode("utf-8"))
    h.update(f"bloku={celkem_bloku}".encode("utf-8"))
    ref = p.get("referencni_wav")
    if ref and Path(ref).exists():
        h.update(str(Path(ref).stat().st_mtime_ns).encode("utf-8"))
    return h.hexdigest()[:32]


class Postup:
    """Stav rozpracovaného převodu vedle výstupu."""

    def __init__(self, cesta: Path):
        self.cesta = Path(cesta)
        self.data = {}

    @classmethod
    def nacti(cls, cesta: Path):
        p = cls(cesta)
        try:
            p.data = json.loads(p.cesta.read_text(encoding="utf-8"))
        except Exception:
            p.data = {}
        return p

    def sedi(self, otisk: str) -> bool:
        return bool(self.data) and self.data.get("otisk") == otisk and self.data.get("verze") == 1

    @property
    def hotovo_bloku(self) -> int:
        return int(self.data.get("hotovo_bloku", 0))

    def uloz(self, otisk: str, hotovo: int, celkem: int, soubory: list,
             aktualni_wav: str = "", vzorku: int = 0, kapitola: int = 0):
        self.data = {"verze": 1, "otisk": otisk, "hotovo_bloku": hotovo,
                     "celkem_bloku": celkem, "hotove_soubory": soubory,
                     "aktualni_wav": aktualni_wav, "vzorku_v_aktualnim": vzorku,
                     "kapitola": kapitola}
        try:
            docasny = self.cesta.with_suffix(".tmp")
            docasny.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            docasny.replace(self.cesta)      # atomicky, ať stav nikdy není půlka
        except Exception:
            pass

    def smaz(self):
        try:
            self.cesta.unlink(missing_ok=True)
        except Exception:
            pass


def najdi_ffmpeg() -> str:
    """Vrátí cestu k ffmpeg.exe, nebo prázdný řetězec."""
    cesta = shutil.which("ffmpeg")
    if cesta:
        return cesta
    lokalni = APP_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"
    if lokalni.exists():
        return str(lokalni)
    return ""


def prevod_na_mp3(wav_cesta: Path, mp3_cesta: Path, bitrate: str,
                  metadata: dict = None, obalka: Path = None) -> bool:
    ffmpeg = najdi_ffmpeg()
    if not ffmpeg:
        return False

    prikaz = [ffmpeg, "-y", "-i", str(wav_cesta)]
    if obalka is not None and Path(obalka).exists():
        # Obálka jako vložený obrázek stopy (ID3 APIC)
        prikaz += ["-i", str(obalka), "-map", "0:a", "-map", "1:v",
                   "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                   "-metadata:s:v", "title=Album cover",
                   "-metadata:s:v", "comment=Cover (front)"]
    prikaz += ["-codec:a", "libmp3lame", "-b:a", bitrate]
    for klic, hodnota in (metadata or {}).items():
        if hodnota:
            prikaz += ["-metadata", f"{klic}={hodnota}"]
    prikaz.append(str(mp3_cesta))

    vysledek = subprocess.run(
        prikaz,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return vysledek.returncode == 0 and mp3_cesta.exists()


def vytvor_obalku(nazev: str, cesta: Path, velikost: int = 600) -> bool:
    """Vygeneruje obálku z názvu knihy. Čistě lokálně, nic se nikam neposílá.

    Barvy se odvozují z otisku názvu, takže stejná kniha má vždy stejnou obálku.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False

    import hashlib
    import colorsys

    otisk = hashlib.sha256(nazev.encode("utf-8")).digest()
    odstin = otisk[0] / 255.0
    odstin2 = (odstin + 0.08 + otisk[1] / 255.0 * 0.12) % 1.0

    def rgb(h, s, v):
        return tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, s, v))

    horni, dolni = rgb(odstin, 0.55, 0.34), rgb(odstin2, 0.70, 0.09)

    obrazek = Image.new("RGB", (velikost, velikost), dolni)
    kresli = ImageDraw.Draw(obrazek)

    # Svislý přechod
    for y in range(velikost):
        t = y / float(velikost - 1)
        kresli.line([(0, y), (velikost, y)],
                    fill=tuple(int(horni[i] + (dolni[i] - horni[i]) * t) for i in range(3)))

    # Soustředné oblouky - jemná struktura odvozená z otisku
    svetla = rgb(odstin, 0.30, 0.95)
    for i in range(7):
        r = int(velikost * (0.22 + 0.11 * i))
        posun = (otisk[2 + i] - 128) / 128.0 * velikost * 0.18
        stred = (velikost * 0.5 + posun, velikost * 0.62)
        kresli.ellipse([stred[0] - r, stred[1] - r, stred[0] + r, stred[1] + r],
                       outline=svetla + (0,) if False else svetla, width=1)

    # Ztmavení spodku, ať je text čitelný
    zavoj = Image.new("RGBA", (velikost, velikost), (0, 0, 0, 0))
    kresli_zavoj = ImageDraw.Draw(zavoj)
    for y in range(int(velikost * 0.45), velikost):
        t = (y - velikost * 0.45) / (velikost * 0.55)
        kresli_zavoj.line([(0, y), (velikost, y)], fill=(0, 0, 0, int(190 * t)))
    obrazek = Image.alpha_composite(obrazek.convert("RGBA"), zavoj).convert("RGB")
    kresli = ImageDraw.Draw(obrazek)

    # Titulek zalomený na řádky
    ttf = FONT_DIR / "JetBrainsMono-Bold.ttf"
    velikost_pisma = int(velikost * 0.075)
    try:
        pismo = ImageFont.truetype(str(ttf), velikost_pisma)
        pismo_male = ImageFont.truetype(str(FONT_DIR / "JetBrainsMono-Regular.ttf"),
                                        int(velikost * 0.032))
    except Exception:
        pismo = pismo_male = ImageFont.load_default()

    slova, radky, radek = nazev.upper().split(), [], ""
    for slovo in slova:
        zkouska = f"{radek} {slovo}".strip()
        if kresli.textlength(zkouska, font=pismo) > velikost * 0.82 and radek:
            radky.append(radek)
            radek = slovo
        else:
            radek = zkouska
    if radek:
        radky.append(radek)
    radky = radky[:4]

    y = velikost * 0.60
    for r in radky:
        kresli.text((velikost * 0.09, y), r, font=pismo, fill=(245, 245, 248))
        y += velikost_pisma * 1.25

    kresli.text((velikost * 0.09, velikost * 0.90), "AUDIOKNIHA · CHATTERBOX TTS",
                font=pismo_male, fill=(255, 255, 255, 140))
    kresli.rectangle([velikost * 0.09, velikost * 0.545,
                      velikost * 0.09 + velikost * 0.10, velikost * 0.553],
                     fill=svetla)

    try:
        cesta.parent.mkdir(parents=True, exist_ok=True)
        obrazek.save(str(cesta), "PNG")
        return True
    except Exception:
        return False


class Prehravac:
    """Přehrává bloky během generování, s nastavitelným náskokem.

    Generování běží kolem 0,95x realtime, takže se náskok pomalu spotřebovává.
    Proto se čeká, než se nashromáždí zadaná zásoba, a teprve pak se spustí zvuk.
    Zásoba pak vydrží zhruba dvacetinásobek své délky.
    """

    def __init__(self, vzorkovaci_frekvence: int, naskok_s: float, log_fn):
        self.sr = vzorkovaci_frekvence
        self.naskok_s = float(naskok_s)
        self.log = log_fn

        self.fronta = queue.Queue()
        self.vlakno = None
        self.stop_event = threading.Event()
        self.pauza_event = threading.Event()
        self.bezi = False
        self.chyba = None

        self._zamek = threading.Lock()
        self._sekund_ve_fronte = 0.0     # kolik audia čeká na přehrání
        self._prehrano_s = 0.0
        self._spusteno = False
        self._vstup_uzavren = False
        # Historie hlasitosti pro vizualizaci - jen ke čtení z GUI vlákna
        self.hladiny = collections.deque(maxlen=160)

    # ------------------------------------------------------------------
    @staticmethod
    def dostupny() -> bool:
        try:
            import sounddevice  # noqa: F401
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    def start(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as chyba:
            self.chyba = f"sounddevice není k dispozici ({chyba})"
            self.log(T("log_poslech_vyp", self.chyba))
            return False

        try:
            zarizeni = sd.query_devices(sd.default.device[1])
            self.log(T("log_poslech_zar", zarizeni["name"].strip(), int(self.naskok_s)))
        except Exception:
            pass

        self.bezi = True
        self.vlakno = threading.Thread(target=self._smycka, daemon=True)
        self.vlakno.start()
        return True

    # ------------------------------------------------------------------
    def pridej(self, vzorky, ticho_ms: int = 0):
        """Zařadí blok do fronty k přehrání. Nikdy neblokuje generování."""
        if not self.bezi:
            return
        import numpy as np

        data = np.asarray(vzorky, dtype="float32").reshape(-1)
        if ticho_ms > 0:
            data = np.concatenate([data, np.zeros(int(self.sr * ticho_ms / 1000.0),
                                                  dtype="float32")])
        with self._zamek:
            self._sekund_ve_fronte += len(data) / float(self.sr)
        self.fronta.put(data)

    # ------------------------------------------------------------------
    @property
    def zasoba_s(self) -> float:
        with self._zamek:
            return self._sekund_ve_fronte

    @property
    def prehrano_s(self) -> float:
        with self._zamek:
            return self._prehrano_s

    @property
    def ceka_na_naskok(self) -> bool:
        return self.bezi and not self._spusteno

    @property
    def pozastaveno(self) -> bool:
        return self.pauza_event.is_set()

    def prepni_pauzu(self) -> bool:
        """Vrátí nový stav: True = pozastaveno."""
        if self.pauza_event.is_set():
            self.pauza_event.clear()
        else:
            self.pauza_event.set()
        return self.pauza_event.is_set()

    # ------------------------------------------------------------------
    def _smycka(self):
        import numpy as np
        import sounddevice as sd

        proud = None
        try:
            # Čekáme na náskok, ať přehrávání nezačne dřív, než má z čeho žít.
            # Pozor: hlídat i konec vstupu, jinak by u textu kratšího než náskok
            # čekání nikdy neskončilo - a fronta přitom plná dat.
            while not self.stop_event.is_set():
                if self.zasoba_s >= self.naskok_s or self._vstup_uzavren:
                    break
                time.sleep(0.2)

            if self.stop_event.is_set():
                return

            self._spusteno = True
            zasoba = self.zasoba_s
            if zasoba < self.naskok_s:
                self.log(T("log_poslech_kratky", round(zasoba)))
            else:
                self.log(T("log_poslech_start", round(zasoba)))

            proud = sd.OutputStream(samplerate=self.sr, channels=1, dtype="float32")
            proud.start()

            while not self.stop_event.is_set():
                try:
                    blok = self.fronta.get(timeout=0.3)
                except queue.Empty:
                    if self._konec_vstupu():
                        break
                    continue

                if blok is None:          # signál konce
                    break

                # Zapisujeme po desetinách sekundy - zastavení pak nemusí čekat
                # na dohrání celého bloku a zároveň z toho máme data na vizualizaci.
                krok = int(self.sr * 0.1)
                for zacatek in range(0, len(blok), krok):
                    if self.stop_event.is_set():
                        break

                    # Pauza poslechu: proud se zastaví, ať karta nehlásí podtečení.
                    # Generování běží dál, takže se mezitím jen zvětšuje zásoba.
                    while self.pauza_event.is_set() and not self.stop_event.is_set():
                        if proud.active:
                            proud.stop()
                        time.sleep(0.1)
                    if self.stop_event.is_set():
                        break
                    if not proud.active:
                        proud.start()

                    kousek = blok[zacatek:zacatek + krok]
                    proud.write(kousek)          # blokuje, dokud karta neodebere
                    self.hladiny.append(float(np.sqrt((kousek.astype("float64") ** 2).mean()))
                                        if kousek.size else 0.0)
                    delka = len(kousek) / float(self.sr)
                    with self._zamek:
                        self._sekund_ve_fronte = max(0.0, self._sekund_ve_fronte - delka)
                        self._prehrano_s += delka

        except Exception as chyba:
            self.chyba = str(chyba)
            self.log(T("log_poslech_chyba", chyba))
        finally:
            if proud is not None:
                try:
                    proud.stop()
                    proud.close()
                except Exception:
                    pass
            self.bezi = False

    def _konec_vstupu(self) -> bool:
        return self._vstup_uzavren and self.fronta.empty()

    # ------------------------------------------------------------------
    def uzavri_vstup(self):
        """Generování skončilo. Vlákno dohraje zbytek fronty a samo doběhne."""
        self._vstup_uzavren = True
        self.fronta.put(None)

    def zastav(self):
        """Okamžité ukončení - zbytek fronty se zahodí."""
        self.stop_event.set()
        self.pauza_event.clear()      # ať čekací smyčka nezůstane viset
        self._vstup_uzavren = True
        try:
            self.fronta.put_nowait(None)
        except Exception:
            pass
        if self.vlakno is not None:
            self.vlakno.join(timeout=3.0)
        self.bezi = False


def formatuj_cas(sekundy: float) -> str:
    if sekundy is None or sekundy < 0 or sekundy != sekundy:
        return "--:--:--"
    sekundy = int(sekundy)
    h, zbytek = divmod(sekundy, 3600)
    m, s = divmod(zbytek, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ==========================================================================
#  TTS engine - obaluje Chatterbox Multilingual
# ==========================================================================

class TtsEngine:
    def __init__(self, log_fn):
        self.log = log_fn
        self.model = None
        self.zarizeni = None
        self.sr = 24000
        self.podporuje_jazyk = True
        self.finetune_nacten = False
        self.nacteny_repo = None
        self.jazyk = None

    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _hlaseni_stahovani(self, popis: str, interval: float = 15.0):
        """Průběžně hlásí velikost cache, aby okno během stahování nevypadalo mrtvě."""
        konec = threading.Event()

        def tep():
            while not konec.wait(interval):
                try:
                    velikost = sum(f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file())
                    self.log(T("log_stahovani", popis, velikost / (1024 ** 3)))
                except Exception:
                    pass

        vlakno = threading.Thread(target=tep, daemon=True)
        vlakno.start()
        try:
            yield
        finally:
            konec.set()

    # ------------------------------------------------------------------
    def vyber_zarizeni(self, volba: str) -> str:
        import torch

        if volba == "cpu":
            return "cpu"
        if volba == "cuda":
            if not torch.cuda.is_available():
                self.log(T("log_cuda_ne"))
                return "cpu"
            return "cuda"
        # auto
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    # ------------------------------------------------------------------
    def nacti_model(self, volba_zarizeni: str = "auto", jazyk_klic: str = "en"):
        import torch

        pozadovane_zarizeni = self.vyber_zarizeni(volba_zarizeni)
        jazyk = jazyk_podle_klice(jazyk_klic)
        pozadovany_repo = jazyk.get("repo") if jazyk.get("zdroj") == "finetune" else None

        if self.model is not None:
            # Jiné zařízení nebo jiný jazykový checkpoint = čistý start.
            # Nechat na T3 váhy po předchozím jazyce by bylo horší než nic.
            if pozadovane_zarizeni != self.zarizeni or self.nacteny_repo != pozadovany_repo:
                self.log(T("log_znovu"))
                self.uvolni()
            else:
                return

        self.zarizeni = pozadovane_zarizeni
        self.jazyk = jazyk
        self.log(T("log_zarizeni", self.zarizeni))
        if self.zarizeni == "cuda":
            try:
                jmeno = torch.cuda.get_device_name(0)
                vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
                self.log(T("log_gpu", jmeno, vram))
            except Exception:
                pass

        self.log(T("log_nacitam"))

        try:
            from chatterbox import mtl_tts
        except ImportError as chyba:
            raise RuntimeError(
                "Nepodařilo se naimportovat 'chatterbox'. Nainstalujte balík:\n"
                "    pip install chatterbox-tts\n"
                f"Detail: {chyba}"
            )

        self._odemkni_jazyk(mtl_tts, jazyk["kod"])
        with self._hlaseni_stahovani(T("log_zaklad_model")):
            self.model = mtl_tts.ChatterboxMultilingualTTS.from_pretrained(device=self.zarizeni)
        self.sr = int(getattr(self.model, "sr", 24000))
        self.log(T("log_nacten", self.sr))

        if pozadovany_repo:
            self._nacti_finetune(jazyk)
        elif jazyk.get("zdroj") == "finetune":
            self.log(T("log_bez_ft", jazyk["nazev"]))

    # ------------------------------------------------------------------
    def _odemkni_jazyk(self, modul, kod: str):
        """Doplní jazyk do seznamu, proti kterému chatterbox validuje language_id.

        Balík povoluje jen 23 jazyků, na kterých se model trénoval. Tokenizer
        ale zná i další tokeny - [cs], [sk], [bg], [hu], [ro], [ta], [vi] -
        a fine-tune je právě na ně navázaný. Bez doplnění by generate() skončilo
        na ValueError dřív, než by se ke slovu dostaly nové váhy.
        """
        try:
            jazyky = modul.SUPPORTED_LANGUAGES
        except AttributeError:
            return
        if kod not in jazyky:
            jazyky[kod] = kod.upper()
            self.log(T("log_jazyk_doplnen", kod))

    # ------------------------------------------------------------------
    def _nacti_finetune(self, jazyk: dict):
        """Best-effort dotažení jazykového checkpointu přes základní model."""
        import torch

        repo = jazyk["repo"]
        try:
            from huggingface_hub import snapshot_download

            argumenty = dict(
                repo_id=repo,
                cache_dir=str(CACHE_DIR / "hub"),
                allow_patterns=["*.safetensors", "*.pt", "*.pth"],
            )

            # Nejdřív zkusíme čistě lokální cache. Když je checkpoint stažený,
            # neběží žádný síťový dotaz a hlavně se neplaší hláškou o stahování.
            slozka = None
            try:
                slozka = Path(snapshot_download(local_files_only=True, **argumenty))
                self.log(T("log_ft_cache"))
            except Exception:
                pass

            if slozka is None:
                self.log(T("log_ft_stahuji", repo, jazyk.get("velikost_gb", 2.1)))
                with self._hlaseni_stahovani(T("log_ft_popis")):
                    slozka = Path(snapshot_download(
                        # None = použije token z 'huggingface-cli login', jinak anonymně
                        token=os.environ.get("HF_TOKEN") or None, **argumenty))
        except Exception as chyba:
            text_chyby = f"{type(chyba).__name__}: {chyba}"
            self.log(T("log_ft_chyba", text_chyby[:200]))
            if any(kod in text_chyby for kod in ("401", "403", "Gated", "gated", "Unauthorized",
                                                 "restricted", "Token", "Access to model")):
                for radek in NAPOVEDA_TOKEN.split("\n"):
                    self.log(radek)
            self.log(T("log_ft_zaklad"))
            return

        # Hledáme váhy T3 (textového transformeru) - to je to, co se fine-tunuje
        kandidati = sorted(
            [p for p in slozka.rglob("*") if p.suffix in (".safetensors", ".pt", ".pth")],
            key=lambda p: (0 if "t3" in p.name.lower() else 1, -p.stat().st_size),
        )
        if not kandidati:
            self.log(T("log_ft_nenalezen"))
            return

        soubor = kandidati[0]
        self.log(T("log_ft_aplikuji", soubor.name))

        try:
            if soubor.suffix == ".safetensors":
                from safetensors.torch import load_file
                stav = load_file(str(soubor))
            else:
                stav = torch.load(str(soubor), map_location="cpu")
                for klic in ("model", "state_dict", "module"):
                    if isinstance(stav, dict) and klic in stav and isinstance(stav[klic], dict):
                        stav = stav[klic]
                        break
                if isinstance(stav, list) and stav and isinstance(stav[0], dict):
                    stav = stav[0]

            # Sjednocení případných prefixů ("t3.", "module.")
            ocisteny = {}
            for klic, hodnota in stav.items():
                novy = klic
                for prefix in ("module.", "t3."):
                    if novy.startswith(prefix):
                        novy = novy[len(prefix):]
                ocisteny[novy] = hodnota

            vysledek = self.model.t3.load_state_dict(ocisteny, strict=False)
            self.model.t3.to(self.zarizeni).eval()

            # Po prvním generování si T3 vyrobí runtime wrapper 'patched_model',
            # který sdílí paměť s 'tfmr'. Jeho klíče se hlásí jako chybějící,
            # ačkoli se do nich váhy propíšou. Skutečný problém je jedině to,
            # když checkpoint obsahuje klíč, který modul nezná.
            nesedici = list(getattr(vysledek, "unexpected_keys", []))
            chybi = [k for k in getattr(vysledek, "missing_keys", [])
                     if not k.startswith("patched_model.")]

            self.finetune_nacten = True
            self.nacteny_repo = repo
            if nesedici or chybi:
                self.log(T("log_ft_castecne", len(nesedici), len(chybi)))
            else:
                self.log(T("log_ft_hotovo", len(ocisteny), jazyk["nazev"]))
        except Exception as chyba:
            self.log(T("log_ft_nepovedlo", chyba))

    # ------------------------------------------------------------------
    def generuj(self, text: str, referencni_wav: str, exaggeration: float,
                cfg_weight: float, temperature: float, min_p: float = 0.05):
        """Vrátí numpy pole float32 (mono) s vygenerovanou řečí.

        min_p odřezává ocas rozdělení pravděpodobností. Práh je relativní k
        nejlepšímu tokenu, takže tam, kde si je model jistý, nic nezmění, a
        tam, kde váhá, nechá alternativy žít. Zmizí jen tokeny řádově méně
        pravděpodobné než ten nejlepší - a odtud pocházejí lupance v tichu.
        """
        import numpy as np

        argumenty = dict(
            exaggeration=float(exaggeration),
            cfg_weight=float(cfg_weight),
            temperature=float(temperature),
            min_p=float(min_p),
        )
        if referencni_wav and Path(referencni_wav).exists():
            argumenty["audio_prompt_path"] = referencni_wav

        kod = (self.jazyk or {}).get("kod", "en")
        if self.podporuje_jazyk:
            try:
                wav = self.model.generate(text, language_id=kod, **argumenty)
            except (TypeError, ValueError) as chyba:
                # Starší build bez multilingválního API nebo jinak řešená validace jazyka
                self.podporuje_jazyk = False
                self.log(T("log_lang_ne", kod, chyba))
                wav = self.model.generate(text, **argumenty)
        else:
            wav = self.model.generate(text, **argumenty)

        if hasattr(wav, "detach"):
            wav = wav.detach().cpu().numpy()
        wav = np.asarray(wav, dtype="float32").reshape(-1)
        return wav

    # ------------------------------------------------------------------
    def uvolni(self):
        self.model = None
        self.finetune_nacten = False
        self.nacteny_repo = None
        self.podporuje_jazyk = True
        try:
            import torch, gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


def nastav_seed(seed: int):
    if not seed:
        return
    import random
    import torch
    import numpy as np

    random.seed(seed)
    np.random.seed(seed % (2 ** 32 - 1))
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def odstran_lupance(vzorky, sr: int, zapnuto: bool = True):
    """Ztlumí krátké impulzy v pauzách. Mimo pauzy se nezmění ani jeden vzorek.

    Model občas v tichých pasážích vygeneruje submilisekundový impulz, který
    trčí kolem 20 dB nad šumovým dnem a je slyšet jako lupnutí. Měření ukázalo,
    že se od řeči odděluje spolehlivě: řečové špičky leží mimo dlouhé pauzy a
    lupance trvají zlomek milisekundy, zatímco nádech stovky.

    Postup drží tři pojistky:
      - zasahuje se jen v pauzách delších než 150 ms,
      - zúžených o 30 ms z každé strany, aby náběh a doznívání řeči zůstaly celé,
      - a jen do impulzů kratších než 15 ms, což nádech neprojde.

    Ztlumení je plynulé, ne useknutí - tvrdý řez by vyrobil vlastní lupnutí.
    Pauza si ponechá naklonovaný šum místnosti, takže nezůstane hluchá.

    Vrací (vzorky, počet_ztlumených_míst).
    """
    import numpy as np

    d = np.asarray(vzorky, dtype="float32").reshape(-1)
    if not zapnuto or len(d) < int(sr * 0.2):
        return d, 0

    okno = max(1, int(sr * 0.02))
    env = np.sqrt(np.convolve(d.astype("float64") ** 2, np.ones(okno) / okno, mode="same"))
    dno = float(np.percentile(env, 10))
    if dno <= 1e-9:
        return d, 0

    # pauzy: obálka pod trojnásobkem šumového dna, souvisle aspoň 150 ms
    tiche = env < dno * 3.0
    hran = np.diff(np.concatenate(([0], tiche.view(np.int8), [0])))
    zacatky, konce = np.flatnonzero(hran == 1), np.flatnonzero(hran == -1)

    okraj = int(sr * 0.03)
    min_pauza = int(sr * 0.15)
    maska = np.zeros(len(d), dtype=bool)
    for a, b in zip(zacatky, konce):
        if (b - a) >= min_pauza and (b - a) > 2 * okraj:
            maska[a + okraj:b - okraj] = True
    if not maska.any():
        return d, 0

    prah = dno * 8.0
    kandidati = np.flatnonzero((np.abs(d) > prah) & maska)
    if not len(kandidati):
        return d, 0

    # seskupit, co je blíž než 5 ms
    mezera = max(1, int(sr * 0.005))
    shluky, akt = [], [kandidati[0]]
    for s in kandidati[1:]:
        if s - akt[-1] <= mezera:
            akt.append(s)
        else:
            shluky.append((akt[0], akt[-1])); akt = [s]
    shluky.append((akt[0], akt[-1]))

    rampa = max(1, int(sr * 0.003))
    max_delka = int(sr * 0.015)
    zisk = np.ones(len(d), dtype="float32")
    ztlumeno = 0

    for a, b in shluky:
        if (b - a + 1) > max_delka:
            continue                      # příliš dlouhé - spíš nádech než lupanec
        od, do = max(0, a - rampa), min(len(d), b + rampa + 1)
        # ramp nesmí vylézt z pauzy ven
        if not maska[od] or not maska[do - 1]:
            od, do = max(od, a), min(do, b + 1)
        spicka = float(np.abs(d[a:b + 1]).max())
        if spicka <= 0:
            continue
        cil = min(1.0, (dno * 2.0) / spicka)

        n = do - od
        okno_zisk = np.full(n, cil, dtype="float32")
        nab = min(rampa, a - od)
        if nab > 0:
            okno_zisk[:nab] = np.linspace(1.0, cil, nab, dtype="float32")
        dob = min(rampa, do - b - 1)
        if dob > 0:
            okno_zisk[n - dob:] = np.linspace(cil, 1.0, dob, dtype="float32")
        zisk[od:do] = np.minimum(zisk[od:do], okno_zisk)
        ztlumeno += 1

    if not ztlumeno:
        return d, 0
    return (d * zisk).astype("float32"), ztlumeno

def generuj_blok(engine, blok: str, p: dict, index: int, celkem: int, log) -> object:
    """Vygeneruje blok; hlídá halucinační smyčky a při chybě to zkusí znovu.

    Používají to obě cesty - jednoprocesová i jednotlivý pracovník poolu.
    """
    # Hrubý horní odhad délky: české čtení jede kolem 12-16 znaků/s
    max_delka = len(blok) / 8.0 + 3.0

    for pokus in range(1, 4):
        try:
            if pokus > 1:
                nastav_seed(int(time.time() * 1000) % 999983)
            vzorky = engine.generuj(blok, p["referencni_wav"], p["exaggeration"],
                                    p["cfg_weight"], p["temperature"],
                                    p.get("min_p", 0.05))
            delka = len(vzorky) / float(engine.sr)

            if delka > max_delka and pokus < 3:
                log(T("log_dlouhy", index, celkem, delka, len(blok)))
                continue
            if delka < 0.05:
                log(T("log_prazdny", index, celkem))
                continue

            vzorky, ztlumeno = odstran_lupance(vzorky, engine.sr,
                                               p.get("odstranit_lupance", True))
            if ztlumeno:
                log(T("log_lupance", ztlumeno, index, celkem))
            return vzorky
        except Exception as chyba:
            log(T("log_pokus", index, celkem, pokus, chyba))
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            time.sleep(0.5)

    log(T("log_preskocen", index, celkem, blok[:60]))
    return None


def volna_vram_gb() -> float:
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        volno, _celkem = torch.cuda.mem_get_info()
        return volno / (1024 ** 3)
    except Exception:
        return 0.0


# Model sám zabírá kolem 3,3 GB, s aktivacemi při generování ~3,7 GB.
# Počítáme 4,3 GB na pracovníka - radši o jednoho méně než OOM uprostřed
# osmihodinové knihy.
VRAM_NA_PRACOVNIKA = 4.3


def doporuceny_pocet_pracovniku(strop: int = 4) -> int:
    """Kolik souběžných procesů se vejde do volné paměti karty."""
    volno = volna_vram_gb()
    if volno <= 0:
        return 1
    return max(1, min(strop, int(volno // VRAM_NA_PRACOVNIKA)))


class Pool:
    """Rozdělí bloky mezi několik procesů a vrací je zpátky v původním pořadí."""

    def __init__(self, pocet: int, nastaveni: dict, log):
        import multiprocessing as mp
        import pracovnik

        self.log = log
        self._cil = pracovnik.bezet
        self.kontext = mp.get_context("spawn")
        self.ukoly = self.kontext.Queue()
        self.vysledky = self.kontext.Queue()
        self.procesy = []
        self.sr = None
        self.chyba = None

        self._buffer = {}
        self._dalsi = 1          # index, který se má vydat jako další

        for i in range(pocet):
            n = dict(nastaveni)
            n["id"] = i + 1
            proces = self.kontext.Process(target=self._cil,
                                          args=(self.ukoly, self.vysledky, n), daemon=True)
            proces.start()
            self.procesy.append(proces)

    def pockej_na_start(self, timeout: float = 900.0) -> bool:
        """Každý pracovník si musí načíst model - běží to souběžně."""
        hotovo = 0
        konec = time.time() + timeout
        while hotovo < len(self.procesy) and time.time() < konec:
            try:
                typ, kdo, data = self.vysledky.get(timeout=1.0)
            except Exception:
                continue
            if typ == "pripraven":
                hotovo += 1
                self.sr = int(data)
            elif typ == "log":
                self.log(data)
            elif typ == "chyba":
                self.chyba = data
                return False
        return hotovo == len(self.procesy)

    def posli(self, index: int, blok: str, celkem: int, p: dict):
        self.ukoly.put((index, blok, celkem, p))

    def vezmi(self, timeout: float = 300.0):
        """Vrátí (index, vzorky) dalšího bloku v pořadí, nebo None při chybě."""
        konec = time.time() + timeout
        while time.time() < konec:
            if self._dalsi in self._buffer:
                return self._dalsi, self._buffer.pop(self._dalsi)
            try:
                typ, kdo, data = self.vysledky.get(timeout=1.0)
            except Exception:
                if not any(p.is_alive() for p in self.procesy):
                    self.chyba = "generující procesy skončily"
                    return None
                continue
            if typ == "audio":
                self._buffer[kdo] = data      # u audia je 'kdo' index bloku
            elif typ == "log":
                self.log(data)
            elif typ == "chyba":
                self.chyba = data
                return None
        return None

    def potvrd(self):
        self._dalsi += 1

    def ukonci(self):
        for _ in self.procesy:
            try:
                self.ukoly.put_nowait(None)
            except Exception:
                pass
        for proces in self.procesy:
            proces.join(timeout=5.0)
            if proces.is_alive():
                proces.terminate()


# ==========================================================================
#  GUI
# ==========================================================================

class Aplikace(tk.Tk):

    def __init__(self):
        super().__init__()
        # Konfigurace se musí načíst dřív než cokoli s textem - jazyk se
        # uplatní hned na titulku okna, ne až u prvního popisku.
        self.config_data = self._nacti_config()
        nastav_jazyk(self.config_data.get("jazyk", "en"))

        self.title(f"{T('app_nazev')} v{VERSION}")
        self.geometry("1000x980")
        self.minsize(920, 820)

        self.fronta = queue.Queue()
        self.vlakno = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.engine = TtsEngine(self.log_z_vlakna)

        self.bloky = []          # (index_kapitoly, text_bloku)
        self.kapitoly = []
        self.ma_kapitoly = False
        self.nazev_knihy = ""
        self.bezi = False
        self.prehravac = None
        self.obalka_cesta = None      # ať přežije přestavbu okna při změně jazyka

        self._vytvor_promenne()
        self._vytvor_gui()
        self._obnov_z_configu()

        self.protocol("WM_DELETE_WINDOW", self.pri_zavreni)
        self.after(100, self._zpracuj_frontu)
        self.after(500, self._aktualizuj_poslech)
        self.after(300, self._vykresli_hladinu)

        self.log(f"{T('app_nazev')} v{VERSION}")
        self.log(T("log_cache", CACHE_DIR))
        if self.font_rodina != FONT_RODINA:
            self.log(T("log_font", self.font_rodina))
        if not najdi_ffmpeg():
            self.log(T("log_ffmpeg"))
        if not Prehravac.dostupny():
            self.log(T("log_sd"))

    # ------------------------------------------------------------------
    #  Konfigurace
    # ------------------------------------------------------------------
    def _nacti_config(self) -> dict:
        data = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                ulozene = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                data.update({k: v for k, v in ulozene.items() if k in DEFAULT_CONFIG})

                # Migrace ze starší volby "český fine-tune ano/ne" na výběr jazyka,
                # ať se uživateli nastavení tiše nepřepne na angličtinu.
                if "jazyk_textu" not in ulozene and ulozene.get("cesky_finetune"):
                    data["jazyk_textu"] = "cs|" + "Thomcles/Chatterbox-TTS-Czech"
            except Exception:
                pass
        return data

    def _uloz_config(self):
        data = {
            "vstupni_soubor": self.var_vstup.get(),
            "referencni_wav": self.var_ref_wav.get(),
            "vystupni_slozka": self.var_vystup_slozka.get(),
            "vystupni_nazev": self.var_vystup_nazev.get(),
            "format": self.var_format.get(),
            "mp3_bitrate": self.var_bitrate.get(),
            "max_znaku": int(self.var_max_znaku.get()),
            "pauza_ms": int(self.var_pauza.get()),
            "exaggeration": float(self.var_exag.get()),
            "cfg_weight": float(self.var_cfg.get()),
            "temperature": float(self.var_temp.get()),
            "min_p": float(self.var_min_p.get()),
            "odstranit_lupance": bool(self.var_lupance.get()),
            "seed": int(self.var_seed.get() or 0),
            "jazyk_textu": self._klic_jazyka_textu(),
            "zarizeni": self.var_zarizeni.get(),
            "testovaci_veta": self.var_test_veta.get(),
            "poslouchat": bool(self.var_poslouchat.get()),
            "naskok_s": int(self.var_naskok.get()),
            "obalka": bool(self.var_obalka.get()),
            "pracovniku": int(self.var_pracovniku.get()),
            "jazyk": aktualni_jazyk(),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  Jazyk syntézy - katalog modelů
    # ------------------------------------------------------------------
    def _nabidka_jazyku(self) -> list:
        return [self._nazev_jazyka_textu(klic_jazyka(j)) for j in KATALOG["jazyky"]]

    def _nazev_jazyka_textu(self, klic: str) -> str:
        j = jazyk_podle_klice(klic)
        if j.get("zdroj") == "finetune":
            return f"{j['nazev']} ({j['kod']}) +{j.get('velikost_gb', 2.1):.1f} GB"
        return f"{j['nazev']} ({j['kod']})"

    def _klic_jazyka_textu(self) -> str:
        zvoleny = self.var_jazyk_textu.get()
        for j in KATALOG["jazyky"]:
            if self._nazev_jazyka_textu(klic_jazyka(j)) == zvoleny:
                return klic_jazyka(j)
        return KATALOG.get("vychozi", "en")

    def _kod_jazyka_textu(self) -> str:
        return jazyk_podle_klice(self._klic_jazyka_textu())["kod"]

    def _stazeny(self, j: dict) -> bool:
        """Je checkpoint jazyka už v cache?"""
        if j.get("zdroj") != "finetune":
            return True
        slozka = CACHE_DIR / "hub" / ("models--" + j["repo"].replace("/", "--"))
        return slozka.is_dir() and any(slozka.rglob("*.safetensors"))

    def _popis_jazyka(self):
        """Krátká věta o tom, co zvolený jazyk obnáší - stažení, kvalita, rizika."""
        j = jazyk_podle_klice(self._klic_jazyka_textu())
        if j.get("zdroj") != "finetune":
            self.var_jazyk_info.set(T("jaz_zakladni"))
            return

        stazeny = self._stazeny(j)
        casti = [T("jaz_stazeno") if stazeny else T("jaz_stahne", j.get("velikost_gb", 2.1))]
        if not j.get("overeno", False):
            casti.append(T("jaz_neovereno"))
        if not j.get("token", True):
            casti.append(T("jaz_bez_tokenu"))
        if j.get("gated") and not stazeny:
            # U staženého modelu je přihlášení už vyřešené, nemá smysl s ním strašit
            casti.append(T("jaz_gated"))
        self.var_jazyk_info.set(" · ".join(casti))

    def _vytvor_promenne(self):
        c = self.config_data
        self.var_vstup = tk.StringVar(value=c["vstupni_soubor"])
        self.var_ref_wav = tk.StringVar(value=c["referencni_wav"])
        self.var_vystup_slozka = tk.StringVar(value=c["vystupni_slozka"])
        self.var_vystup_nazev = tk.StringVar(value=c["vystupni_nazev"])
        self.var_format = tk.StringVar(value=c["format"])
        self.var_bitrate = tk.StringVar(value=c["mp3_bitrate"])
        self.var_max_znaku = tk.IntVar(value=c["max_znaku"])
        self.var_pauza = tk.IntVar(value=c["pauza_ms"])
        self.var_exag = tk.DoubleVar(value=c["exaggeration"])
        self.var_cfg = tk.DoubleVar(value=c["cfg_weight"])
        self.var_temp = tk.DoubleVar(value=c["temperature"])
        self.var_min_p = tk.DoubleVar(value=c["min_p"])
        self.var_lupance = tk.BooleanVar(value=c["odstranit_lupance"])
        self.var_seed = tk.IntVar(value=c["seed"])
        self.var_jazyk_textu = tk.StringVar(value=self._nazev_jazyka_textu(c["jazyk_textu"]))
        self.var_zarizeni = tk.StringVar(value=c["zarizeni"])
        self.var_test_veta = tk.StringVar(value=c["testovaci_veta"])
        self.var_poslouchat = tk.BooleanVar(value=c["poslouchat"])
        self.var_naskok = tk.IntVar(value=c["naskok_s"])
        self.var_obalka = tk.BooleanVar(value=c["obalka"])
        self.var_pracovniku = tk.IntVar(value=c["pracovniku"])

        self.var_stav = tk.StringVar(value=T("stav_pripraveno"))
        self.var_postup = tk.DoubleVar(value=0.0)
        self.var_bloky_info = tk.StringVar(value="—/—")
        self.var_cas_info = tk.StringVar(value="—:—:—  /  —:—:—")
        self.var_jazyk = tk.StringVar(value=JAZYKY.get(aktualni_jazyk(), "English"))
        self.var_jazyk_info = tk.StringVar(value="")
        self.var_soubor_info = tk.StringVar(value=T("info_zadny"))
        self.var_poslech_info = tk.StringVar(value="")

    def _obnov_z_configu(self):
        self._aktualizuj_popisky_posuvniku()
        self._popis_jazyka()

    # ------------------------------------------------------------------
    #  Sestavení GUI
    # ------------------------------------------------------------------
    def _vytvor_gui(self):
        self.font_rodina = nacti_font()
        self.F_BEZNY = (self.font_rodina, 10)
        self.F_MALY = (self.font_rodina, 8)
        self.F_TITULEK = (self.font_rodina, 8)

        self.configure(background=BARVY["pozadi"])
        self._nastav_pojmenovane_fonty()
        self._nastav_styl()

        # Widgety, ktere se behem prevodu zamykaji (item: nemenit format za behu)
        self.zamykatelne = []

        hlavni = ttk.Frame(self, padding=(26, 22, 26, 20))
        hlavni.pack(fill="both", expand=True)
        hlavni.columnconfigure(0, weight=1)

        # ---------------- Hlavička ----------------
        zahlavi = ttk.Frame(hlavni)
        zahlavi.pack(fill="x", pady=(0, 22))
        ttk.Label(zahlavi, text=T("znacka"), style="Nadpis.TLabel").pack(side="left")
        ttk.Label(zahlavi, text=T("podtitul", self._kod_jazyka_textu(), VERSION),
                  style="Tlumeny.TLabel").pack(side="left", padx=(12, 0))

        vyber = ttk.Combobox(zahlavi, textvariable=self.var_jazyk, width=9, state="readonly",
                             values=[JAZYKY[k] for k in ("en", "cs")])
        vyber.pack(side="right")
        vyber.bind("<<ComboboxSelected>>", lambda _u: self.zmen_jazyk())
        ttk.Label(zahlavi, text=T("lab_jazyk"), style="Tlumeny.TLabel").pack(
            side="right", padx=(0, 10))

        # ---------------- Kniha ----------------
        kniha = self._sekce(hlavni, T("sekce_kniha"))
        rada = ttk.Frame(kniha)
        rada.grid(row=0, column=0, sticky="ew")
        rada.columnconfigure(0, weight=1)
        e = ttk.Entry(rada, textvariable=self.var_vstup)
        e.grid(row=0, column=0, sticky="ew")
        b1 = ttk.Button(rada, text=T("btn_vybrat"), command=self.vyber_vstup, style="Tichy.TButton")
        b1.grid(row=0, column=1, padx=(8, 0))
        b2 = ttk.Button(rada, text=T("btn_nacist"), command=self.nacti_a_priprav, style="Tichy.TButton")
        b2.grid(row=0, column=2, padx=(6, 0))
        self._zamknout(e, b1, b2)

        rada = ttk.Frame(kniha)
        rada.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(rada, text=T("lab_jazyk_textu")).pack(side="left", padx=(0, 12))
        vyber_jaz = ttk.Combobox(rada, textvariable=self.var_jazyk_textu, width=26,
                                 state="readonly", values=self._nabidka_jazyku())
        vyber_jaz.pack(side="left")
        vyber_jaz.bind("<<ComboboxSelected>>", lambda _u: self._popis_jazyka())
        self._zamknout(vyber_jaz)
        ttk.Label(rada, textvariable=self.var_jazyk_info,
                  style="Tlumeny.TLabel").pack(side="left", padx=(14, 0))

        ttk.Label(kniha, textvariable=self.var_soubor_info,
                  style="Tlumeny.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))

        # ---------------- Hlas ----------------
        hlas = self._sekce(hlavni, T("sekce_hlas"))
        rada = ttk.Frame(hlas)
        rada.grid(row=0, column=0, sticky="ew")
        rada.columnconfigure(0, weight=1)
        e = ttk.Entry(rada, textvariable=self.var_ref_wav)
        e.grid(row=0, column=0, sticky="ew")
        b1 = ttk.Button(rada, text=T("btn_vybrat"), command=self.vyber_ref_wav, style="Tichy.TButton")
        b1.grid(row=0, column=1, padx=(8, 0))
        b2 = ttk.Button(rada, text="×", command=lambda: self.var_ref_wav.set(""),
                        style="Tichy.TButton", width=3)
        b2.grid(row=0, column=2, padx=(6, 0))
        self._zamknout(e, b1, b2)

        rada = ttk.Frame(hlas)
        rada.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        rada.columnconfigure(0, weight=1)
        e = ttk.Entry(rada, textvariable=self.var_test_veta)
        e.grid(row=0, column=0, sticky="ew")
        self.btn_test = ttk.Button(rada, text=T("btn_test"), command=self.test_hlasu,
                                   style="Tichy.TButton")
        self.btn_test.grid(row=0, column=1, padx=(8, 0))
        self._zamknout(e)

        # ---------------- Výstup ----------------
        vystup = self._sekce(hlavni, T("sekce_vystup"))
        rada = ttk.Frame(vystup)
        rada.grid(row=0, column=0, sticky="ew")
        rada.columnconfigure(0, weight=1)
        e = ttk.Entry(rada, textvariable=self.var_vystup_slozka)
        e.grid(row=0, column=0, sticky="ew")
        b1 = ttk.Button(rada, text=T("btn_vybrat"), command=self.vyber_vystup, style="Tichy.TButton")
        b1.grid(row=0, column=1, padx=(8, 0))
        self._zamknout(e, b1)

        rada = ttk.Frame(vystup)
        rada.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        rada.columnconfigure(0, weight=1)
        e = ttk.Entry(rada, textvariable=self.var_vystup_nazev)
        e.grid(row=0, column=0, sticky="ew")
        r1 = ttk.Radiobutton(rada, text="wav", variable=self.var_format, value="WAV")
        r1.grid(row=0, column=1, padx=(14, 0))
        r2 = ttk.Radiobutton(rada, text="mp3", variable=self.var_format, value="MP3")
        r2.grid(row=0, column=2, padx=(8, 0))
        c1 = ttk.Combobox(rada, textvariable=self.var_bitrate, width=5, state="readonly",
                          values=["96k", "128k", "160k", "192k"])
        c1.grid(row=0, column=3, padx=(8, 0))
        self._zamknout(e, r1, r2, c1)

        # ---------------- Poslech ----------------
        poslech = self._sekce(hlavni, T("sekce_poslech"))
        rada = ttk.Frame(poslech)
        rada.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(rada, text=T("lab_prehravat"), variable=self.var_poslouchat,
                        command=self.prepni_poslech).pack(side="left")
        ttk.Label(rada, text=T("lab_naskok")).pack(side="left", padx=(28, 12))
        sp = ttk.Spinbox(rada, from_=15, to=1800, increment=15,
                         textvariable=self.var_naskok, width=7)
        sp.pack(side="left")
        self._zamknout(sp)

        self.btn_poslech_pauza = ttk.Button(rada, text=T("btn_poslech_pauza"),
                                            command=self.prepni_pauzu_poslechu,
                                            style="Tichy.TButton", state="disabled")
        self.btn_poslech_pauza.pack(side="left", padx=(28, 0))

        # Vizualizace: obálka vlevo, hladina vpravo
        vizu = ttk.Frame(poslech)
        vizu.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        vizu.columnconfigure(1, weight=1)

        self.platno_obalka = tk.Canvas(vizu, width=104, height=104, highlightthickness=0,
                                       background=BARVY["panel"], borderwidth=0)
        self.platno_obalka.grid(row=0, column=0, sticky="w")
        self.platno_obalka.create_text(52, 52, text="—", fill=BARVY["linka"],
                                       font=(self.font_rodina, 20))
        # Po přestavbě okna (změna jazyka) je plátno nové - obálku vrátíme zpět
        drive = getattr(self, "obalka_cesta", None)
        if drive is not None and Path(drive).exists():
            self.after(0, lambda: self.zobraz_obalku(Path(drive)))

        self.platno_hladina = tk.Canvas(vizu, height=104, highlightthickness=0,
                                        background=BARVY["panel"], borderwidth=0)
        self.platno_hladina.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        ttk.Label(poslech, textvariable=self.var_poslech_info,
                  style="Tlumeny.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))

        # ---------------- Pokročilé (sbaleno) ----------------
        gen = self._sekce_sbalitelna(hlavni, T("sekce_pokrocile"))
        gen.columnconfigure(1, weight=1)

        self.popisky_posuvniku = {}

        def posuvnik(radek, popis, promenna, od, do, klic):
            ttk.Label(gen, text=popis).grid(row=radek, column=0, sticky="w", padx=(0, 14), pady=3)
            sk = ttk.Scale(gen, from_=od, to=do, variable=promenna,
                           command=lambda _e: self._aktualizuj_popisky_posuvniku())
            sk.grid(row=radek, column=1, sticky="ew", padx=(0, 12))
            popisek = ttk.Label(gen, text="", width=5, style="Hodnota.TLabel")
            popisek.grid(row=radek, column=2, sticky="w", padx=(0, 28))
            self.popisky_posuvniku[klic] = (popisek, promenna)
            self._zamknout(sk)

        def cislo(radek, popis, promenna, od, do, krok):
            ttk.Label(gen, text=popis).grid(row=radek, column=3, sticky="w", padx=(0, 12), pady=3)
            sp = ttk.Spinbox(gen, from_=od, to=do, increment=krok, textvariable=promenna, width=7)
            sp.grid(row=radek, column=4, sticky="w")
            self._zamknout(sp)

        posuvnik(0, T("lab_expresivita"), self.var_exag, 0.25, 1.0, "exag")
        posuvnik(1, T("lab_cfg"), self.var_cfg, 0.0, 1.0, "cfg")
        posuvnik(2, T("lab_teplota"), self.var_temp, 0.05, 1.5, "temp")
        posuvnik(3, T("lab_min_p"), self.var_min_p, 0.0, 0.30, "minp")
        cislo(0, T("lab_znaku"), self.var_max_znaku, 80, 400, 10)
        cislo(1, T("lab_pauza_ms"), self.var_pauza, 0, 2000, 50)
        cislo(2, T("lab_seed"), self.var_seed, 0, 999999, 1)

        spodek = ttk.Frame(gen)
        spodek.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(14, 0))
        ttk.Label(spodek, text=T("lab_zarizeni")).pack(side="left", padx=(0, 12))
        cb = ttk.Combobox(spodek, textvariable=self.var_zarizeni, width=6, state="readonly",
                          values=["auto", "cuda", "cpu"])
        cb.pack(side="left")
        ch2 = ttk.Checkbutton(spodek, text=T("lab_obalka"), variable=self.var_obalka)
        ch2.pack(side="left", padx=(28, 0))
        ch3 = ttk.Checkbutton(spodek, text=T("lab_lupance"), variable=self.var_lupance)
        ch3.pack(side="left", padx=(28, 0))
        ttk.Label(spodek, text=T("lab_pracovniku")).pack(side="left", padx=(28, 12))
        sp_w = ttk.Spinbox(spodek, from_=0, to=4, increment=1,
                           textvariable=self.var_pracovniku, width=5)
        sp_w.pack(side="left")
        self._zamknout(cb, ch2, ch3, sp_w)

        # ---------------- Ovládání ----------------
        ovladani = ttk.Frame(hlavni)
        ovladani.pack(fill="x", pady=(6, 0))

        self.btn_start = ttk.Button(ovladani, text=T("btn_start"),
                                    command=self.spust_prevod, style="Akce.TButton")
        self.btn_start.pack(side="left")
        self.btn_pauza = ttk.Button(ovladani, text=T("btn_pauza"), command=self.prepni_pauzu,
                                    style="Tichy.TButton", state="disabled")
        self.btn_pauza.pack(side="left", padx=(10, 0))
        self.btn_stop = ttk.Button(ovladani, text=T("btn_zastavit"), command=self.zastav,
                                   style="Tichy.TButton", state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))
        ttk.Button(ovladani, text=T("btn_otevrit"), command=self.otevri_vystup,
                   style="Tichy.TButton").pack(side="right")

        # ---------------- Průběh ----------------
        postup = ttk.Frame(hlavni)
        postup.pack(fill="x", pady=(16, 0))
        postup.columnconfigure(0, weight=1)

        ttk.Progressbar(postup, variable=self.var_postup, maximum=100.0,
                        style="Tenky.Horizontal.TProgressbar").grid(
            row=0, column=0, columnspan=3, sticky="ew")
        ttk.Label(postup, textvariable=self.var_stav).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(postup, textvariable=self.var_bloky_info, style="Tlumeny.TLabel").grid(
            row=1, column=1, sticky="e", padx=(16, 16), pady=(8, 0))
        ttk.Label(postup, textvariable=self.var_cas_info, style="Tlumeny.TLabel").grid(
            row=1, column=2, sticky="e", pady=(8, 0))

        # ---------------- Log ----------------
        ramec_log = self._sekce_sbalitelna(hlavni, T("sekce_prubeh"), sbaleno=False,
                                           roztahnout=True, mezera_nahore=20)
        ramec_log.columnconfigure(0, weight=1)
        ramec_log.rowconfigure(0, weight=1)

        self.log_box = tk.Text(
            ramec_log, height=8, wrap="word", state="disabled",
            font=(self.font_rodina, 9),
            background=BARVY["panel"], foreground=BARVY["tlumeny"],
            insertbackground=BARVY["text"], selectbackground=BARVY["panel_svetlejsi"],
            selectforeground=BARVY["text"],
            relief="flat", borderwidth=0, highlightthickness=0,
            padx=14, pady=12, spacing1=1,
        )
        self.log_box.grid(row=0, column=0, sticky="nsew")
        posuv = ttk.Scrollbar(ramec_log, orient="vertical", command=self.log_box.yview,
                              style="Tenky.Vertical.TScrollbar")
        posuv.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=posuv.set)

        self.log_box.tag_configure("cas", foreground="#3d3d47")
        self.log_box.tag_configure("bezny", foreground=BARVY["tlumeny"])
        self.log_box.tag_configure("chyba", foreground=BARVY["chyba"])
        self.log_box.tag_configure("varovani", foreground=BARVY["varovani"])
        self.log_box.tag_configure("uspech", foreground=BARVY["uspech"])

    # ------------------------------------------------------------------
    def _zamknout(self, *widgety):
        """Zapamatuje si widget i jeho normální stav, ať ho jde za běhu vypnout."""
        for w in widgety:
            try:
                normalni = "readonly" if str(w.cget("state")) == "readonly" else "normal"
            except tk.TclError:
                normalni = "normal"
            self.zamykatelne.append((w, normalni))

    def _zamkni_ovladani(self, zamknout: bool):
        for w, normalni in self.zamykatelne:
            try:
                w.configure(state="disabled" if zamknout else normalni)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    def _sekce(self, rodic, nadpis: str) -> ttk.Frame:
        """Nadpis sekce + tenká linka + prostor na obsah. Žádné rámečky."""
        obal = ttk.Frame(rodic)
        obal.pack(fill="x", pady=(0, 20))
        obal.columnconfigure(0, weight=1)

        zahlavi = ttk.Frame(obal)
        zahlavi.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        zahlavi.columnconfigure(1, weight=1)
        ttk.Label(zahlavi, text=nadpis, style="Titulek.TLabel").grid(row=0, column=0, sticky="w")
        tk.Frame(zahlavi, height=1, background=BARVY["linka"]).grid(
            row=0, column=1, sticky="ew", padx=(12, 0), pady=(6, 0))

        obsah = ttk.Frame(obal)
        obsah.grid(row=1, column=0, sticky="ew")
        obsah.columnconfigure(0, weight=1)
        return obsah

    def _sekce_sbalitelna(self, rodic, nadpis: str, sbaleno: bool = True,
                          roztahnout: bool = False, mezera_nahore: int = 0) -> ttk.Frame:
        """Sekce, kterou lze kliknutím na nadpis sbalit. Drží pokročilá nastavení z cesty."""
        obal = ttk.Frame(rodic)
        obal.pack(fill="both" if roztahnout else "x",
                  expand=roztahnout, pady=(mezera_nahore, 20))
        obal.columnconfigure(0, weight=1)
        if roztahnout:
            obal.rowconfigure(1, weight=1)

        zahlavi = ttk.Frame(obal)
        zahlavi.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        zahlavi.columnconfigure(1, weight=1)

        znacka = ttk.Label(zahlavi, text="", style="Titulek.TLabel")
        znacka.grid(row=0, column=0, sticky="w")
        linka = tk.Frame(zahlavi, height=1, background=BARVY["linka"])
        linka.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=(6, 0))

        obsah = ttk.Frame(obal)
        obsah.columnconfigure(0, weight=1)

        stav = {"sbaleno": sbaleno}

        def vykresli():
            sipka = "+" if stav["sbaleno"] else "−"
            znacka.configure(text=f"{nadpis}  {sipka}")
            if stav["sbaleno"]:
                obsah.grid_forget()
            else:
                obsah.grid(row=1, column=0, sticky="nsew" if roztahnout else "ew")

        def prepni(_udalost=None):
            stav["sbaleno"] = not stav["sbaleno"]
            vykresli()

        for w in (znacka, linka, zahlavi):
            w.bind("<Button-1>", prepni)
            w.configure(cursor="hand2")
        vykresli()
        return obsah

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _nastav_pojmenovane_fonty(self):
        """Přepíše pojmenované fonty Tk.

        Entry, Spinbox i Combobox mají vlastní výchozí '-font TkTextFont', které
        přebije font ze stylu - nastavovat ho přes ttk.Style je proto k ničemu.
        Jediné místo, kde se to dá srovnat naráz, jsou tyhle pojmenované fonty.
        """
        from tkinter import font as tkfont

        velikosti = {
            "TkDefaultFont": 10, "TkTextFont": 10, "TkFixedFont": 10,
            "TkMenuFont": 10, "TkHeadingFont": 10, "TkTooltipFont": 9,
            "TkIconFont": 10, "TkCaptionFont": 10, "TkSmallCaptionFont": 9,
        }
        for jmeno, velikost in velikosti.items():
            try:
                tkfont.nametofont(jmeno).configure(family=self.font_rodina, size=velikost)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    def _nastav_styl(self):
        """Tmavé ladění ttk. Základem je 'clam' - jediné téma, které se dá plně přebarvit."""
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        poz, panel, svetlejsi = BARVY["pozadi"], BARVY["panel"], BARVY["panel_svetlejsi"]
        text, tlumeny, akcent, linka = (BARVY["text"], BARVY["tlumeny"],
                                        BARVY["akcent"], BARVY["linka"])

        # clam kresli 3D okraje pres bordercolor/lightcolor/darkcolor. Dokud
        # nejsou srovnane s pozadim, svitI kolem kazdeho pole svetly ramecek.
        s.configure(".", background=poz, foreground=text, font=self.F_BEZNY,
                    borderwidth=0, focuscolor=poz, relief="flat",
                    bordercolor=poz, lightcolor=poz, darkcolor=poz,
                    troughcolor=panel)
        s.configure("TFrame", background=poz)
        s.configure("TLabel", background=poz, foreground=text, font=self.F_BEZNY)
        s.configure("Titulek.TLabel", foreground=tlumeny, font=self.F_TITULEK)
        s.configure("Tlumeny.TLabel", foreground=tlumeny, font=self.F_MALY)
        s.configure("Hodnota.TLabel", foreground=akcent, font=self.F_MALY)
        s.configure("Nadpis.TLabel", foreground=text, font=(self.font_rodina, 13, "bold"))

        # Tlačítka - plochá, bez rámečků, o odstín světlejší než pole
        s.configure("Tichy.TButton", background=BARVY["tlacitko"], foreground=text,
                    borderwidth=0, relief="flat", padding=(14, 7), font=self.F_BEZNY)
        s.map("Tichy.TButton",
              background=[("pressed", linka), ("active", BARVY["tlacitko_aktivni"]),
                          ("disabled", poz)],
              foreground=[("disabled", linka)])

        s.configure("Akce.TButton", background=akcent, foreground=poz,
                    borderwidth=0, relief="flat", padding=(18, 7),
                    font=(self.font_rodina, 10, "bold"))
        s.map("Akce.TButton",
              background=[("pressed", "#5f86d8"), ("active", "#93b4ff"), ("disabled", panel)],
              foreground=[("disabled", linka)])

        # Vstupní pole
        for jmeno in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(jmeno, fieldbackground=panel, background=panel,
                        foreground=BARVY["text_pole"],
                        insertcolor=text, arrowcolor=tlumeny, borderwidth=0,
                        relief="flat", padding=(10, 7), selectbackground=svetlejsi,
                        selectforeground=text, arrowsize=9,
                        bordercolor=panel, lightcolor=panel, darkcolor=panel,
                        troughcolor=panel)
            s.map(jmeno,
                  fieldbackground=[("readonly", panel), ("disabled", poz)],
                  foreground=[("disabled", linka)],
                  bordercolor=[("focus", linka)],
                  lightcolor=[("focus", linka)],
                  arrowcolor=[("active", text)])

        # Rozbalovací seznam comboboxu je klasický tk widget, styl na nej neplati
        self.option_add("*TCombobox*Listbox.background", panel)
        self.option_add("*TCombobox*Listbox.foreground", text)
        self.option_add("*TCombobox*Listbox.selectBackground", akcent)
        self.option_add("*TCombobox*Listbox.selectForeground", poz)
        self.option_add("*TCombobox*Listbox.borderWidth", 0)
        self.option_add("*TCombobox*Listbox.font", self.F_BEZNY)

        # Přepínače. clam pro indikátor používá 'indicatorbackground' - ne
        # 'indicatorcolor', ten se tiše ignoruje a políčko zůstane bílé.
        for jmeno in ("TCheckbutton", "TRadiobutton"):
            s.configure(jmeno, background=poz, foreground=text, font=self.F_BEZNY,
                        indicatorbackground=panel, indicatorforeground=poz,
                        indicatorsize=11, indicatormargin=(0, 0, 9, 0),
                        upperbordercolor=linka, lowerbordercolor=linka,
                        borderwidth=0, focusthickness=0)
            s.map(jmeno,
                  background=[("active", poz)],
                  indicatorbackground=[("selected", akcent), ("active", svetlejsi),
                                       ("!selected", panel)],
                  upperbordercolor=[("selected", akcent), ("active", svetlejsi)],
                  lowerbordercolor=[("selected", akcent), ("active", svetlejsi)],
                  foreground=[("disabled", linka)])

        # Posuvníky hodnot - gripcount=0 odstrani ryhovani na jezdci
        s.configure("Horizontal.TScale", background=akcent, troughcolor=panel,
                    borderwidth=0, sliderthickness=14, sliderrelief="flat", gripcount=0,
                    bordercolor=panel, lightcolor=akcent, darkcolor=akcent)
        s.map("Horizontal.TScale",
              background=[("active", "#93b4ff"), ("disabled", linka)])

        # Ukazatel průběhu - tenká linka, žádný 3D rám
        s.configure("Tenky.Horizontal.TProgressbar", troughcolor=panel, background=akcent,
                    borderwidth=0, thickness=3, lightcolor=akcent, darkcolor=akcent,
                    bordercolor=panel)

        # Posuvník logu
        s.configure("Tenky.Vertical.TScrollbar", background=panel, troughcolor=poz,
                    bordercolor=poz, arrowcolor=poz, borderwidth=0, arrowsize=1, width=6)
        s.map("Tenky.Vertical.TScrollbar", background=[("active", svetlejsi)])

    def _aktualizuj_popisky_posuvniku(self):
        for popisek, promenna in self.popisky_posuvniku.values():
            popisek.config(text=f"{promenna.get():.2f}")

    # ------------------------------------------------------------------
    #  Log a fronta zpráv z pracovního vlákna
    # ------------------------------------------------------------------
    def log(self, zprava: str):
        if zprava.startswith("CHYBA"):
            znacka = "chyba"
        elif zprava.startswith("VAROVÁNÍ"):
            znacka = "varovani"
        elif zprava.startswith(("HOTOVO", "Český fine-tune aplikován", "Spouštím přehrávání")):
            znacka = "uspech"
        else:
            znacka = "bezny"

        self.log_box.config(state="normal")
        self.log_box.insert("end", time.strftime("%H:%M:%S  "), "cas")
        self.log_box.insert("end", f"{zprava}\n", znacka)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def log_z_vlakna(self, zprava: str):
        self.fronta.put(("log", zprava))

    # ------------------------------------------------------------------
    def prepni_poslech(self):
        """Zapnutí/vypnutí poslechu i uprostřed běžícího převodu."""
        chce = bool(self.var_poslouchat.get())

        if not self.bezi:
            return                       # projeví se při spuštění převodu

        if chce and (self.prehravac is None or not self.prehravac.bezi):
            self.prehravac = Prehravac(self.engine.sr, max(5, int(self.var_naskok.get())),
                                       self.log_z_vlakna)
            if self.prehravac.start():
                self.log(T("log_poslech_zap"))
            else:
                self.prehravac = None
                self.var_poslouchat.set(False)
        elif not chce and self.prehravac is not None and self.prehravac.bezi:
            self.prehravac.zastav()
            self.log(T("log_poslech_off"))

    # ------------------------------------------------------------------
    def zmen_jazyk(self):
        """Přepne jazyk rozhraní a postaví okno znovu.

        Widgety si texty drží v sobě, takže překreslit jednotlivě by znamenalo
        držet odkaz na každý popisek. Postavit okno znovu je jednodušší i
        spolehlivější - proměnné i běžící převod to přežijí.
        """
        nazev = self.var_jazyk.get()
        kod = next((k for k, v in JAZYKY.items() if v == nazev), "en")
        if kod == aktualni_jazyk():
            return

        stary_log = self.log_box.get("1.0", "end").rstrip("\n")
        nastav_jazyk(kod)
        self._uloz_config()

        for potomek in self.winfo_children():
            potomek.destroy()
        self._vytvor_gui()
        self._obnov_z_configu()
        self.title(f"{T('app_nazev')} v{VERSION}")

        # Texty, které nejsou svázané s widgetem, je nutné přeložit ručně
        if not self.bezi:
            self.var_stav.set(T("stav_pripraveno"))
        if not self.bloky:
            self.var_soubor_info.set(T("info_zadny"))

        # Běžící převod musí po přestavbě zůstat v odpovídajícím stavu
        if self.bezi:
            self.btn_start.config(state="disabled")
            self.btn_test.config(state="disabled")
            self.btn_pauza.config(state="normal")
            self.btn_stop.config(state="normal")
            self._zamkni_ovladani(True)

        if stary_log:
            self.log_box.config(state="normal")
            self.log_box.insert("end", stary_log + "\n", "bezny")
            self.log_box.config(state="disabled")
        self.log(T("log_jazyk", JAZYKY[kod]))
        self.log_box.see("end")

    # ------------------------------------------------------------------
    def prepni_pauzu_poslechu(self):
        """Pauza a pokračování samotného přehrávání. Generování běží dál."""
        p = self.prehravac
        if p is None or not p.bezi:
            return
        pozastaveno = p.prepni_pauzu()
        self.btn_poslech_pauza.config(
            text=T("btn_poslech_hrat") if pozastaveno else T("btn_poslech_pauza"))
        self.log(T("log_poslech_pauza") if pozastaveno else T("log_poslech_hraj"))

    # ------------------------------------------------------------------
    def _aktualizuj_poslech(self):
        """Ukazuje, kolik náskoku zbývá - to je jediné, co může poslech shodit."""
        p = self.prehravac
        bezici = p is not None and p.bezi
        self.btn_poslech_pauza.config(state="normal" if bezici else "disabled")
        if not bezici and self.btn_poslech_pauza.cget("text") != T("btn_poslech_pauza"):
            self.btn_poslech_pauza.config(text=T("btn_poslech_pauza"))

        if bezici:
            zasoba = p.zasoba_s
            if p.pozastaveno:
                self.var_poslech_info.set(T("poslech_pauza", formatuj_cas(zasoba),
                                            formatuj_cas(p.prehrano_s)))
            elif p.ceka_na_naskok:
                self.var_poslech_info.set(T("poslech_naskok", formatuj_cas(zasoba),
                                            formatuj_cas(p.naskok_s)))
            else:
                # Při ~0,95x realtime se zásoba tenčí zhruba dvacetkrát pomaleji, než roste
                vydrzi = zasoba * 20
                self.var_poslech_info.set(T("poslech_hraje", formatuj_cas(zasoba),
                                            formatuj_cas(p.prehrano_s), formatuj_cas(vydrzi)))
        elif p is not None and not p.bezi and self.var_poslech_info.get():
            self.var_poslech_info.set(T("poslech_konec"))
        self.after(500, self._aktualizuj_poslech)

    # ------------------------------------------------------------------
    def _vykresli_hladinu(self):
        """Sloupcová vizualizace hlasitosti právě přehrávaného zvuku."""
        platno = self.platno_hladina
        platno.delete("all")
        sirka = max(platno.winfo_width(), 1)
        vyska = max(platno.winfo_height(), 1)

        p = self.prehravac
        hladiny = list(p.hladiny) if (p is not None and p.bezi) else []

        if not hladiny:
            platno.create_text(sirka // 2, vyska // 2,
                               text=T("vizu_ticho") if p is None or not p.bezi else "…",
                               fill=BARVY["linka"], font=(self.font_rodina, 9))
        else:
            sirka_sloupce = 3
            mezera = 2
            pocet = min(len(hladiny), max(1, sirka // (sirka_sloupce + mezera)))
            vzorek = hladiny[-pocet:]
            stred = vyska / 2.0
            for i, h in enumerate(vzorek):
                # RMS řeči se drží nízko, proto odmocnina a strop na 0.35
                podil = min(1.0, (h / 0.35) ** 0.5)
                v = max(1.0, podil * (vyska * 0.42))
                x = sirka - (pocet - i) * (sirka_sloupce + mezera)
                cerstvost = i / float(pocet)
                barva = BARVY["akcent"] if cerstvost > 0.75 else "#3f5a91"
                platno.create_rectangle(x, stred - v, x + sirka_sloupce, stred + v,
                                        fill=barva, outline="")

        self.after(80, self._vykresli_hladinu)

    # ------------------------------------------------------------------
    def zobraz_obalku(self, cesta: Path):
        """Vykreslí vygenerovanou obálku do malého náhledu."""
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return
        try:
            obr = Image.open(str(cesta)).resize((104, 104), Image.LANCZOS)
            self._obalka_foto = ImageTk.PhotoImage(obr)   # nesmí ji sebrat GC
            self.platno_obalka.delete("all")
            self.platno_obalka.create_image(0, 0, anchor="nw", image=self._obalka_foto)
            self.obalka_cesta = Path(cesta)
        except Exception as chyba:
            self.log(T("log_obalka_nahled", chyba))

    def _zpracuj_frontu(self):
        try:
            while True:
                typ, data = self.fronta.get_nowait()
                if typ == "log":
                    self.log(data)
                elif typ == "postup":
                    hotovo, celkem, uplynulo, zbyva = data
                    self.var_postup.set(100.0 * hotovo / celkem if celkem else 0.0)
                    self.var_bloky_info.set(T("prubeh_bloky", hotovo, celkem))
                    self.var_cas_info.set(
                        T("prubeh_cas", formatuj_cas(uplynulo), formatuj_cas(zbyva)))
                elif typ == "stav":
                    self.var_stav.set(data)
                elif typ == "hotovo":
                    self._prevod_dokoncen(data)
                elif typ == "chyba":
                    self._prevod_dokoncen(None, chyba=data)
                elif typ == "obalka":
                    self.zobraz_obalku(Path(data))
                elif typ == "test_hotovo":
                    self.btn_test.config(state="normal")
                    self.var_stav.set(T("stav_pripraveno"))
                    if data:
                        self.prehraj(Path(data))
        except queue.Empty:
            pass
        self.after(120, self._zpracuj_frontu)

    # ------------------------------------------------------------------
    #  Výběr souborů
    # ------------------------------------------------------------------
    def vyber_vstup(self):
        cesta = filedialog.askopenfilename(
            title=T("dlg_vyber_knihu"),
            filetypes=[
                (T("filtr_vse"), "*.txt *.epub *.fb2 *.html *.htm *.xhtml *.md"),
                (T("filtr_text"), "*.txt *.md"),
                ("EPUB", "*.epub"),
                ("FictionBook", "*.fb2"),
                (T("filtr_html"), "*.html *.htm *.xhtml"),
                (T("filtr_vsechny"), "*.*"),
            ])
        if cesta:
            self.var_vstup.set(cesta)
            if not self.var_vystup_nazev.get() or self.var_vystup_nazev.get() == "audiokniha":
                self.var_vystup_nazev.set(Path(cesta).stem)
            self.nacti_a_priprav()

    def vyber_ref_wav(self):
        cesta = filedialog.askopenfilename(
            title=T("dlg_vyber_hlas"),
            filetypes=[(T("filtr_zvuk"), "*.wav *.mp3 *.flac *.ogg *.m4a"),
                       (T("filtr_vsechny"), "*.*")])
        if cesta:
            self.var_ref_wav.set(cesta)

    def vyber_vystup(self):
        slozka = filedialog.askdirectory(title=T("dlg_vyber_slozku"))
        if slozka:
            self.var_vystup_slozka.set(slozka)

    def otevri_vystup(self):
        slozka = Path(self.var_vystup_slozka.get())
        slozka.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(slozka))
        except Exception as chyba:
            messagebox.showerror(T("dlg_chyba"), T("dlg_slozka", chyba))

    def prehraj(self, cesta: Path):
        try:
            os.startfile(str(cesta))
        except Exception:
            self.log(T("log_ulozen", cesta))

    # ------------------------------------------------------------------
    #  Načtení a příprava textu
    # ------------------------------------------------------------------
    def nacti_a_priprav(self):
        cesta_txt = self.var_vstup.get().strip('" ')
        if not cesta_txt:
            messagebox.showwarning(T("dlg_chybi_soubor"), T("dlg_vyberte"))
            return
        cesta = Path(cesta_txt)
        if not cesta.exists():
            messagebox.showerror("Chyba", f"Soubor neexistuje:\n{cesta}")
            return

        try:
            self.log(T("log_nacitam_sbor", cesta.name))
            kapitoly, ma_kapitoly = nacti_kapitoly(cesta)
            max_znaku = max(50, int(self.var_max_znaku.get()))

            # Každý blok si nese index kapitoly, ze které pochází - podle toho
            # se pak výstup rozpadne na soubory.
            self.kapitoly = []
            self.bloky = []
            for i, kap in enumerate(kapitoly):
                text = normalizuj_text(kap["text"])
                if not text.strip():
                    continue
                bloky = rozdel_na_bloky(text, max_znaku)
                if not bloky:
                    continue
                self.kapitoly.append({"nazev": kap.get("nazev") or "", "prvni_blok": len(self.bloky)})
                self.bloky.extend((len(self.kapitoly) - 1, b) for b in bloky)

            if not self.bloky:
                raise ValueError("Text se nepodařilo rozdělit na bloky.")
            self.ma_kapitoly = ma_kapitoly and len(self.kapitoly) > 1

            self.nazev_knihy = cesta.stem
            znaku = sum(len(b) for _, b in self.bloky)
            # Zhruba 14 znaků za sekundu mluveného českého textu
            odhad_audio = znaku / 14.0 + len(self.bloky) * (self.var_pauza.get() / 1000.0)

            self.var_soubor_info.set(T("info_soubor", cesta.name, f"{znaku:,}".replace(",", " "),
                                       len(self.bloky), formatuj_cas(odhad_audio)))
            self.log(T("log_nacteno", znaku, len(self.bloky), max_znaku))
            if self.ma_kapitoly:
                self.log(T("log_kapitoly", len(self.kapitoly)))
            self.log(T("log_ukazka_bloku", self.bloky[0][1][:120]))
        except Exception as chyba:
            self.bloky = []
            self.kapitoly = []
            self.ma_kapitoly = False
            self.var_soubor_info.set(T("info_nezdarilo"))
            self.log(T("log_chyba", chyba))
            messagebox.showerror(T("dlg_chyba_nacteni"), str(chyba))

    # ------------------------------------------------------------------
    #  Test hlasu
    # ------------------------------------------------------------------
    def test_hlasu(self):
        if self.bezi:
            messagebox.showinfo(T("dlg_probiha"), T("dlg_pockejte"))
            return

        veta = self.var_test_veta.get().strip()
        if not veta:
            messagebox.showwarning(T("dlg_chybi_text"), T("dlg_zadejte"))
            return

        self.btn_test.config(state="disabled")
        self.var_stav.set(T("stav_ukazka"))
        self._uloz_config()

        parametry = self._posbirej_parametry()
        vlakno = threading.Thread(target=self._worker_test, args=(veta, parametry), daemon=True)
        vlakno.start()

    def _worker_test(self, veta: str, p: dict):
        vystup = None
        try:
            self.engine.nacti_model(p["zarizeni"], p["jazyk_textu"])
            nastav_seed(p["seed"])

            self.log_z_vlakna(T("log_generuji_uk"))
            start = time.time()
            vzorky = self.engine.generuj(veta, p["referencni_wav"], p["exaggeration"],
                                         p["cfg_weight"], p["temperature"],
                                         p.get("min_p", 0.05))

            vystup = TEMP_DIR / "test_hlasu.wav"
            zapisovac = WavZapisovac(vystup, self.engine.sr)
            zapisovac.zapis(vzorky)
            zapisovac.zavri()

            self.log_z_vlakna(T("log_ukazka_hotova", time.time() - start,
                                len(vzorky) / self.engine.sr, vystup))
        except Exception as chyba:
            vystup = None
            self.log_z_vlakna(T("log_chyba_test", chyba))
            self.log_z_vlakna(traceback.format_exc(limit=3))
        finally:
            self.fronta.put(("test_hotovo", str(vystup) if vystup else ""))

    # ------------------------------------------------------------------
    #  Hlavní převod
    # ------------------------------------------------------------------
    def _posbirej_parametry(self) -> dict:
        return {
            "referencni_wav": self.var_ref_wav.get().strip('" '),
            "exaggeration": float(self.var_exag.get()),
            "cfg_weight": float(self.var_cfg.get()),
            "temperature": float(self.var_temp.get()),
            "min_p": float(self.var_min_p.get()),
            "odstranit_lupance": bool(self.var_lupance.get()),
            "seed": int(self.var_seed.get() or 0),
            "zarizeni": self.var_zarizeni.get(),
            "jazyk_textu": self._klic_jazyka_textu(),
            "pauza_ms": int(self.var_pauza.get()),
            "format": self.var_format.get(),
            "bitrate": self.var_bitrate.get(),
            "poslouchat": bool(self.var_poslouchat.get()),
            "naskok_s": max(5, int(self.var_naskok.get())),
            "obalka": bool(self.var_obalka.get()),
            "pracovniku": int(self.var_pracovniku.get()),
        }

    def spust_prevod(self):
        if self.bezi:
            return
        if not self.bloky:
            self.nacti_a_priprav()
            if not self.bloky:
                return

        slozka = Path(self.var_vystup_slozka.get().strip('" ') or (APP_DIR / "vystup"))
        nazev = (self.var_vystup_nazev.get().strip() or self.nazev_knihy or "audiokniha")
        nazev = re.sub(ZAKAZANE_ZNAKY, "_", nazev)
        slozka.mkdir(parents=True, exist_ok=True)
        zaklad = slozka / nazev

        parametry = self._posbirej_parametry()
        if parametry["format"] == "MP3" and not najdi_ffmpeg():
            messagebox.showwarning(T("dlg_ffmpeg"), T("dlg_ffmpeg_text"))
            parametry["format"] = "WAV"
        parametry["otisk"] = otisk_zadani(Path(self.var_vstup.get().strip('" ')),
                                          parametry, len(self.bloky))

        # --- navázat na přerušený běh? ---
        postup = Postup.nacti(slozka / (nazev + ".progress.json"))
        od_bloku = 0
        if postup.sedi(parametry["otisk"]) and 0 < postup.hotovo_bloku < len(self.bloky):
            odpoved = messagebox.askyesnocancel(
                T("dlg_navazat"),
                T("dlg_navazat_text", postup.hotovo_bloku, len(self.bloky),
                  100.0 * postup.hotovo_bloku / len(self.bloky)))
            if odpoved is None:
                return                       # Zrušit
            if odpoved:
                od_bloku = postup.hotovo_bloku
            else:
                postup.smaz()                # začít znovu od začátku
                postup = Postup.nacti(postup.cesta)
        elif postup.data and not postup.sedi(parametry["otisk"]):
            # Stav existuje, ale kniha nebo parametry se změnily - navazovat nelze
            self.log(T("log_postup_neplatny"))
            postup.smaz()
            postup = Postup.nacti(postup.cesta)

        # Přepsat existující výstup? Ptáme se jen když nenavazujeme.
        if od_bloku == 0:
            hotovy = [zaklad.with_suffix(".wav"), zaklad.with_suffix(".mp3")]
            existujici = [c for c in hotovy if c.exists()]
            if existujici and not messagebox.askyesno(T("dlg_existuje"),
                                                      T("dlg_prepsat", existujici[0])):
                return

        self._uloz_config()

        # Předchozí přehrávač může ještě dobírat zásobu z minulého převodu
        if self.prehravac is not None:
            self.prehravac.zastav()
            self.prehravac = None

        self.bezi = True
        self.stop_event.clear()
        self.pause_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_test.config(state="disabled")
        self.btn_pauza.config(state="normal", text=T("btn_pauza"))
        self.btn_stop.config(state="normal")
        self._zamkni_ovladani(True)      # formát ani cesty už za běhu neměnit
        self.var_postup.set(100.0 * od_bloku / len(self.bloky) if od_bloku else 0.0)
        self.var_poslech_info.set("")

        self.vlakno = threading.Thread(
            target=self._worker_prevod,
            args=(list(self.bloky), zaklad, parametry, od_bloku, postup),
            daemon=True)
        self.vlakno.start()

    def prepni_pauzu(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_pauza.config(text=T("btn_pauza"))
            self.log(T("log_pokracuji"))
        else:
            self.pause_event.set()
            self.btn_pauza.config(text=T("btn_pokracovat"))
            self.log(T("log_pozastaveno"))

    def zastav(self):
        if not self.bezi:
            return
        if messagebox.askyesno(T("dlg_zastavit"), T("dlg_zastavit_text")):
            self.stop_event.set()
            self.pause_event.clear()
            self.var_stav.set(T("stav_zastavuji"))

    # ------------------------------------------------------------------
    def _worker_prevod(self, bloky, zaklad: Path, p: dict, od_bloku: int = 0, postup=None):
        zapisovac = None
        pool = None
        try:
            # O souběhu se rozhoduje dřív, než se cokoli načte. Při dávkování
            # si model drží jen pracovníci - rodič by jím zbytečně blokoval
            # paměť, kterou by jinak dostal další proces.
            pocet = int(p.get("pracovniku") or 0) or doporuceny_pocet_pracovniku()
            if pocet > 1:
                self.log_z_vlakna(T("log_pool_start", pocet, volna_vram_gb()))
                pool = Pool(pocet, {"zarizeni": p["zarizeni"], "jazyk_textu": p["jazyk_textu"]},
                            self.log_z_vlakna)
                if not pool.pockej_na_start():
                    self.log_z_vlakna(T("log_pool_selhal", pool.chyba or "?"))
                    pool.ukonci()
                    pool = None
                else:
                    self.log_z_vlakna(T("log_pool_pripraven", pocet))

            if pool is None:
                self.log_z_vlakna(T("log_pool_jeden"))
                self.engine.nacti_model(p["zarizeni"], p["jazyk_textu"])
                sr = self.engine.sr
            else:
                sr = pool.sr
            nastav_seed(p["seed"])

            celkem = len(bloky)
            znaku_celkem = sum(len(b) for _, b in bloky)
            znaku_pred = sum(len(b) for _, b in bloky[:od_bloku])
            znaku_hotovo = znaku_pred
            neuspesne = 0

            # Rozpad na soubory dává smysl jen tam, kde kapitoly zná sám formát
            # knihy a kde je z čeho udělat MP3.
            po_kapitolach = bool(p["format"] == "MP3" and self.ma_kapitoly and najdi_ffmpeg())
            slozka = (zaklad.parent / zaklad.stem) if po_kapitolach else zaklad.parent
            slozka.mkdir(parents=True, exist_ok=True)

            self.fronta.put(("stav", T("stav_generuji")))
            if od_bloku:
                self.log_z_vlakna(T("log_navazuji", od_bloku + 1, celkem))
            self.log_z_vlakna(T("log_start", celkem, slozka))
            if po_kapitolach:
                self.log_z_vlakna(T("log_po_kapitolach", len(self.kapitoly)))

            obalka_cesta = None
            if p["obalka"]:
                kandidat = slozka / (zaklad.stem + ".png")
                if kandidat.exists() or vytvor_obalku(self.nazev_knihy or zaklad.stem, kandidat):
                    obalka_cesta = kandidat
                    self.log_z_vlakna(T("log_obalka", kandidat.name))
                    self.fronta.put(("obalka", str(kandidat)))
                else:
                    self.log_z_vlakna(T("log_obalka_ne"))

            if p["poslouchat"]:
                self.prehravac = Prehravac(sr, p["naskok_s"], self.log_z_vlakna)
                if not self.prehravac.start():
                    self.prehravac = None

            hotove = list(postup.data.get("hotove_soubory") or []) if postup else []
            otisk = p["otisk"]

            def cesta_kapitoly(kap_i):
                nazev = (self.kapitoly[kap_i].get("nazev") or "").strip()
                nazev = re.sub(ZAKAZANE_ZNAKY, "", nazev)[:60].strip(" .")
                jmeno = "{:02d}".format(kap_i + 1) + (" - " + nazev if nazev else "")
                return slozka / (jmeno + ".wav")

            def uzavri_a_preved(kap_i, dokoncena=True):
                """Kapitolu dopsat a případně převést na MP3.

                Nedokončenou kapitolu na MP3 převádět nesmíme - do MP3 už se
                nedá dopisovat, takže by se na ni po přerušení nedalo navázat.
                Zůstane jako WAV a doplní se při dalším běhu.
                """
                if self._zapisovac is None:
                    return
                z = self._zapisovac
                delka = z.delka_s
                wav = z.cesta
                z.zavri()
                self._zapisovac = None
                if delka <= 0:
                    wav.unlink(missing_ok=True)
                    return
                if not dokoncena:
                    self.log_z_vlakna(T("log_kapitola_rozdelana", wav.name))
                    return
                mp3 = wav.with_suffix(".mp3")
                popis = self.kapitoly[kap_i].get("nazev") or wav.stem
                meta = {"title": popis, "album": self.nazev_knihy or zaklad.stem,
                        "track": str(kap_i + 1), "genre": "Audiobook"}
                if prevod_na_mp3(wav, mp3, p["bitrate"], meta, obalka_cesta):
                    wav.unlink(missing_ok=True)       # WAV už není k ničemu
                    hotove.append(mp3.name)
                    self.log_z_vlakna(T("log_kapitola_hotova", kap_i + 1, mp3.name))
                else:
                    self.log_z_vlakna(T("log_mp3_selhal"))
                    hotove.append(wav.name)

            # --- navázání na rozepsaný soubor ---
            self._zapisovac = None
            aktualni_kap = -1
            if od_bloku and postup is not None:
                vzorku = int(postup.data.get("vzorku_v_aktualnim", 0))
                rozepsany = postup.data.get("aktualni_wav") or ""
                if rozepsany and vzorku > 0 and Path(rozepsany).exists():
                    aktualni_kap = int(postup.data.get("kapitola", bloky[od_bloku][0]))
                    self._zapisovac = WavZapisovacRaw(Path(rozepsany), sr, vzorku)
                    self.log_z_vlakna(T("log_navazuji_soubor", Path(rozepsany).name,
                                        formatuj_cas(self._zapisovac.delka_s)))

            jediny_wav = slozka / (zaklad.stem + ".wav")

            if pool is not None:
                pool._dalsi = od_bloku + 1

            start = time.time()

            for index, kap_i, blok, vzorky in self._proud_bloku(bloky, p, od_bloku, pool):
                # nová kapitola = nový soubor
                if self._zapisovac is None or (po_kapitolach and kap_i != aktualni_kap):
                    if po_kapitolach and self._zapisovac is not None:
                        uzavri_a_preved(aktualni_kap)
                    aktualni_kap = kap_i
                    cil = cesta_kapitoly(kap_i) if po_kapitolach else jediny_wav
                    self._zapisovac = WavZapisovacRaw(cil, sr)

                if vzorky is None:
                    neuspesne += 1
                else:
                    self._zapisovac.zapis(vzorky)
                    self._zapisovac.zapis_ticho(p["pauza_ms"])
                    if self.prehravac is not None and self.prehravac.bezi:
                        self.prehravac.pridej(vzorky, p["pauza_ms"])

                if postup is not None:
                    postup.uloz(otisk, index, celkem, hotove, str(self._zapisovac.cesta),
                                self._zapisovac.pocet_vzorku, aktualni_kap)

                znaku_hotovo += len(blok)
                uplynulo = time.time() - start
                rychlost = (znaku_hotovo - znaku_pred) / uplynulo if uplynulo > 0 else 0
                zbyva = (znaku_celkem - znaku_hotovo) / rychlost if rychlost > 0 else -1
                self.fronta.put(("postup", (index, celkem, uplynulo, zbyva)))

                if index % 25 == 0:
                    self.log_z_vlakna(T("log_prubeh", index, celkem,
                                        formatuj_cas(self._zapisovac.delka_s),
                                        formatuj_cas(uplynulo), formatuj_cas(zbyva)))
                    self._vycisti_vram()

            if pool is not None:
                pool.ukonci()
                pool = None

            zastaveno = self.stop_event.is_set()

            if po_kapitolach:
                if self._zapisovac is not None:
                    uzavri_a_preved(aktualni_kap, dokoncena=not zastaveno)
                vysledek = slozka
                souhrn = T("log_souhrn_kapitoly", len(hotove))
            else:
                delka = self._zapisovac.delka_s if self._zapisovac else 0.0
                if self._zapisovac is not None:
                    self._zapisovac.zavri()
                    self._zapisovac = None
                if delka <= 0:
                    raise RuntimeError("Nevygenerovalo se žádné audio.")
                vysledek = jediny_wav
                # Při zastavení se na MP3 nepřevádí. Do MP3 se nedá dopisovat,
                # takže by převod rozdělanou knihu uzavřel a navázat by nešlo.
                if p["format"] == "MP3" and zastaveno:
                    self.log_z_vlakna(T("log_wav_ponechan", jediny_wav.name))
                elif p["format"] == "MP3":
                    self.fronta.put(("stav", T("stav_mp3")))
                    self.log_z_vlakna(T("log_mp3"))
                    mp3 = jediny_wav.with_suffix(".mp3")
                    meta = {"title": self.nazev_knihy or zaklad.stem, "genre": "Audiobook",
                            "comment": "Vytvořeno pomocí Chatterbox TTS"}
                    if prevod_na_mp3(jediny_wav, mp3, p["bitrate"], meta, obalka_cesta):
                        jediny_wav.unlink(missing_ok=True)   # při MP3 WAV neuchováváme
                        vysledek = mp3
                        self.log_z_vlakna(T("log_mp3_hotovo", mp3))
                    else:
                        self.log_z_vlakna(T("log_mp3_selhal"))
                velikost = vysledek.stat().st_size / (1024 * 1024) if vysledek.exists() else 0.0
                souhrn = T("log_souhrn", formatuj_cas(delka), velikost)

            if self.prehravac is not None and self.prehravac.bezi:
                zbyva_s = self.prehravac.zasoba_s
                if zbyva_s > 1:
                    self.log_z_vlakna(T("log_dobira", formatuj_cas(zbyva_s)))
                self.prehravac.uzavri_vstup()

            if postup is not None:
                if zastaveno:
                    # Doplnit seznam hotových kapitol - v cyklu se ukládá
                    # ještě před jejich převodem na MP3.
                    postup.uloz(otisk, postup.hotovo_bloku, celkem, hotove,
                                postup.data.get("aktualni_wav", ""),
                                postup.data.get("vzorku_v_aktualnim", 0),
                                postup.data.get("kapitola", 0))
                else:
                    postup.smaz()      # doběhlo celé, není na co navazovat

            self.log_z_vlakna((T("log_zastaveno_ul") if zastaveno else T("log_hotovo")) + souhrn
                              + (T("log_neuspesne", neuspesne) if neuspesne else ""))
            if zastaveno and postup is not None:
                self.log_z_vlakna(T("log_lze_navazat"))
            self.fronta.put(("hotovo", str(vysledek)))

        except Exception as chyba:
            if getattr(self, "_zapisovac", None) is not None:
                self._zapisovac.zavri()
                self._zapisovac = None
            if self.prehravac is not None:
                self.prehravac.zastav()
            self.log_z_vlakna(T("log_chyba", chyba))
            self.log_z_vlakna(traceback.format_exc(limit=5))
            self.fronta.put(("chyba", str(chyba)))

    def _proud_bloku(self, bloky, p, od_bloku, pool):
        """Vydává (index, kapitola, text, vzorky) v původním pořadí.

        Jedna cesta pro obě varianty - buď se generuje rovnou, nebo se bloky
        rozešlou pracovníkům a tady se počká, až dojde ten, který je na řadě.
        """
        celkem = len(bloky)

        def cekej_na_pauzu():
            while self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.2)

        if pool is None:
            for index, (kap_i, blok) in enumerate(bloky, start=1):
                if index <= od_bloku:
                    continue
                cekej_na_pauzu()
                if self.stop_event.is_set():
                    self.log_z_vlakna(T("log_zastaveno_na", index, celkem))
                    return
                yield index, kap_i, blok, self._generuj_s_opakovanim(blok, p, index, celkem)
            return

        odeslano = od_bloku
        okno = max(2, len(pool.procesy) * 2)     # kolik bloků držet rozpracovaných
        hotovo = od_bloku

        while hotovo < celkem:
            while (odeslano < celkem and odeslano - hotovo < okno
                   and not self.stop_event.is_set() and not self.pause_event.is_set()):
                pool.posli(odeslano + 1, bloky[odeslano][1], celkem, p)
                odeslano += 1

            cekej_na_pauzu()
            if self.stop_event.is_set():
                self.log_z_vlakna(T("log_zastaveno_na", hotovo + 1, celkem))
                return

            if odeslano == hotovo:            # po pauze nemusí být co odebírat
                continue

            vysledek = pool.vezmi()
            if vysledek is None:
                raise RuntimeError(pool.chyba or "generující proces selhal")
            index, vzorky = vysledek
            pool.potvrd()
            hotovo = index
            yield index, bloky[index - 1][0], bloky[index - 1][1], vzorky

    def _generuj_s_opakovanim(self, blok: str, p: dict, index: int, celkem: int):
        return generuj_blok(self.engine, blok, p, index, celkem, self.log_z_vlakna)

    def _vycisti_vram(self):
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _prevod_dokoncen(self, cesta, chyba=None):
        self.bezi = False
        self.btn_start.config(state="normal")
        self.btn_test.config(state="normal")
        self.btn_pauza.config(state="disabled", text=T("btn_pauza"))
        self.btn_stop.config(state="disabled")
        self._zamkni_ovladani(False)

        if chyba:
            self.var_stav.set(T("stav_chyba"))
            if self.prehravac is not None:
                self.prehravac.zastav()
            messagebox.showerror(T("dlg_selhal"), str(chyba))
            return

        # Zastavení uživatelem ukončí i poslech; po normálním dokončení
        # necháváme přehrávač dobrat zásobu na pozadí.
        if self.stop_event.is_set() and self.prehravac is not None:
            self.prehravac.zastav()

        zastaveno = self.stop_event.is_set()
        self.var_stav.set(T("stav_zastaveno") if zastaveno else T("stav_hotovo"))
        if not zastaveno:
            self.var_postup.set(100.0)

        nadpis = T("stav_zastaveno") if zastaveno else T("dlg_hotovo")
        popis = T("dlg_zastaveno_text") if zastaveno else T("dlg_hotovo_text")
        if cesta and messagebox.askyesno(nadpis, f"{popis}\n{cesta}\n\n{T('dlg_otevrit')}"):
            self.otevri_vystup()

    # ------------------------------------------------------------------
    def pri_zavreni(self):
        if self.bezi:
            if not messagebox.askyesno(T("dlg_ukoncit"), T("dlg_ukoncit_text")):
                return
            self.stop_event.set()
            self.pause_event.clear()
            if self.prehravac is not None:
                self.prehravac.zastav()
            self.var_stav.set(T("stav_ukoncuji"))
            self._uloz_config()
            # Nesmíme zavřít okno dřív, než vlákno dopíše WAV hlavičku,
            # jinak by zůstal poškozený soubor.
            self._pockej_na_vlakno()
            return

        if self.prehravac is not None:
            self.prehravac.zastav()
        self._uloz_config()
        self.destroy()

    def _pockej_na_vlakno(self, zbyva_pokusu: int = 300):
        if self.vlakno is not None and self.vlakno.is_alive() and zbyva_pokusu > 0:
            self.after(200, lambda: self._pockej_na_vlakno(zbyva_pokusu - 1))
        else:
            self.destroy()


def main():
    import multiprocessing
    multiprocessing.freeze_support()

    try:
        # Ostřejší vykreslení GUI na HiDPI monitorech
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    aplikace = Aplikace()
    aplikace.mainloop()


if __name__ == "__main__":
    main()
