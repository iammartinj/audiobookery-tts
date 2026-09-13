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
import functools
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
VERSION = "1.11.0"

VYSLOVNOST_PATH = APP_DIR / "vyslovnost.json"


def nacti_vyslovnost() -> dict:
    """Načte slovníček výslovnosti. Chybějící nebo rozbitý soubor se přejde."""
    try:
        data = json.loads(VYSLOVNOST_PATH.read_text(encoding="utf-8"))
        nahrady = data.get("nahrady") or {}
        return {str(k): str(v) for k, v in nahrady.items() if k and v}
    except Exception:
        return {}


VYSLOVNOST = nacti_vyslovnost()

VYSLOVNOST_CS_PATH = APP_DIR / "vyslovnost_cs.json"
VZOR_SLOVA = re.compile(r"\w+", re.UNICODE)


@functools.lru_cache(maxsize=1)
def slovnik_cs() -> dict:
    """Měkké ti/di/ni podle Wikislovníku: tvar slova -> tvar s háčkem.

    Soubor vyrábí vyslovnost_wiki.py. Háček je jen tam, kde výslovnost ve
    Wikislovníku měkké čtení potvrzuje, takže přejatá slova jako politika
    nebo diplom zůstanou, jak jsou. Načítá se až při prvním použití -
    pracovníci ho nepotřebují.
    """
    if not VYSLOVNOST_CS_PATH.exists():
        return {}
    data = json.loads(VYSLOVNOST_CS_PATH.read_text(encoding="utf-8"))
    return data.get("nahrady") or {}


def _sestav_vzor(nahrady: dict):
    """Jeden regulární výraz pro všechna pravidla naráz.

    Delší klíče musí jít první, jinak by kratší ukousl začátek delšího.
    Hvězdička na konci znamená předponu - zbytek slova zůstane, jak byl,
    což u ohýbané češtiny šetří vypisování všech pádů.
    """
    if not nahrady:
        return None, {}

    casti, mapa = [], {}
    for i, klic in enumerate(sorted(nahrady, key=len, reverse=True)):
        jmeno = f"v{i}"
        predpona = klic.endswith("*")
        holy = klic[:-1] if predpona else klic
        if not holy:
            continue
        # \b na začátku drží náhradu na hranici slova, takže klíč 'ti'
        # nikdy nesáhne doprostřed slova 'politika'
        telo = re.escape(holy) + (r"\w*" if predpona else r"\b")
        casti.append(f"(?P<{jmeno}>\\b{telo})")
        mapa[jmeno] = (holy, nahrady[klic], predpona)

    if not casti:
        return None, {}
    return re.compile("|".join(casti), re.IGNORECASE | re.UNICODE), mapa


VZOR_VYSLOVNOSTI, MAPA_VYSLOVNOSTI = _sestav_vzor(VYSLOVNOST)


def _velikost_jako(vzor: str, novy: str) -> str:
    """Přenese velikost písmen. Celé verzálky se u nadpisů kapitol vyskytují
    běžně, tak ať se z nich nestane Ťichý."""
    if len(vzor) > 1 and vzor.isupper():
        return novy.upper()
    if vzor[:1].isupper():
        return novy[:1].upper() + novy[1:]
    return novy


def uprav_vyslovnost(text: str, jazyk: str = ""):
    """Přepíše text podle slovníčků. Vrací (text, ručních_náhrad, slov_z_Wikislovníku).

    Nejdřív ruční pravidla z vyslovnost.json, u české knihy pak háčky
    z Wikislovníku. Ruční pravidlo tak může slovník přebít - 'tichý' přepsané
    na 'tychý' ve slovníku nenajde nic, co by ho vrátilo.
    """
    if not text:
        return text, 0, 0

    rucne = 0
    if VZOR_VYSLOVNOSTI is not None:
        def nahrad(shoda):
            nonlocal rucne
            jmeno = shoda.lastgroup
            if jmeno not in MAPA_VYSLOVNOSTI:
                return shoda.group(0)
            holy, cil, predpona = MAPA_VYSLOVNOSTI[jmeno]
            nalezene = shoda.group(0)
            if predpona:
                zbytek = nalezene[len(holy):]
                novy = (cil[:-1] if cil.endswith("*") else cil) + zbytek
            else:
                novy = cil
            rucne += 1
            return _velikost_jako(nalezene, novy)

        text = VZOR_VYSLOVNOSTI.sub(nahrad, text)

    z_wiki = 0
    slovnik = slovnik_cs() if jazyk == "cs" else {}
    if slovnik:
        def po_slovech(shoda):
            nonlocal z_wiki
            slovo = shoda.group(0)
            novy = slovnik.get(slovo.lower())
            if novy is None:
                return slovo
            z_wiki += 1
            return _velikost_jako(slovo, novy)

        text = VZOR_SLOVA.sub(po_slovech, text)

    return text, rucne, z_wiki


def otisk_vyslovnosti(jazyk: str = "") -> str:
    """Otisk slovníčků - mění zvuk, takže patří do otisku rozdělané knihy.

    Wikislovník jen u české knihy. Otisk ostatních jazyků se tím nemění.
    """
    import hashlib

    polozky = "|".join(f"{k}={v}" for k, v in sorted(VYSLOVNOST.items()))
    if jazyk == "cs" and VYSLOVNOST_CS_PATH.exists():
        polozky += "|cs=" + hashlib.sha256(VYSLOVNOST_CS_PATH.read_bytes()).hexdigest()
    return hashlib.sha256(polozky.encode("utf-8")).hexdigest()[:16]


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
    "orezat_okraje": True,
    "rychly_dekoder": False,
    "kontrola_asr": False,
    # Sbalené sekce okna - na nízkém monitoru se bez toho nevejde spodek
    "sbalene_sekce": {"poslech": False, "pokrocile": True, "prubeh": False},
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


# Nastavení, bez kterých by druhá půlka knihy zněla jinak než první. Když se
# změní, navazovat nejde.
NASTAVENI_HLASU = ("jazyk_textu", "referencni_wav", "exaggeration", "cfg_weight",
                   "temperature", "min_p", "seed", "pauza_ms", "format", "bitrate")
# Opravy. Změna jen zlepší zbytek knihy, takže navázání neblokuje - do logu
# se ale vypíše. Ke slovníčkům se do stavu přidává i otisk jejich obsahu.
NASTAVENI_OPRAV = ("odstranit_lupance", "orezat_okraje", "rychly_dekoder", "kontrola_asr")


def otisk_hlasu(cesta_knihy: Path, p: dict, bloky) -> str:
    """Otisk zdroje, nastavení hlasu a rozdělení textu na bloky.

    Délky bloků jsou v otisku, aby navázání nikdy nesedlo na posunuté hranice -
    kus textu by se jinak přeskočil nebo zopakoval. Háček ze slovníčku délku
    slova nemění, takže opravy výslovnosti navázání nezablokují. Ruční
    pravidlo, které slovo prodlouží, ano.
    """
    import hashlib

    h = hashlib.sha256()
    try:
        h.update(Path(cesta_knihy).read_bytes())
    except Exception:
        h.update(str(cesta_knihy).encode("utf-8"))
    for klic in NASTAVENI_HLASU:
        h.update(f"{klic}={p.get(klic)}".encode("utf-8"))
    ref = p.get("referencni_wav")
    if ref and Path(ref).exists():
        h.update(str(Path(ref).stat().st_mtime_ns).encode("utf-8"))
    h.update(("bloky=" + ",".join(str(len(b)) for b in bloky)).encode("utf-8"))
    return h.hexdigest()[:32]


def otisk_verze_1(cesta_knihy: Path, p: dict, celkem_bloku: int) -> str:
    """Otisk, jak ho počítala verze 1.9 - jen kvůli knihám rozdělaným v ní.

    Tehdejší otisk zahrnoval i opravy, takže sedí jen při stejném slovníčku
    a filtrech. Ořez okrajů se do převodu tehdy neposílal vůbec, proto se tu
    počítá, jako by chyběl.
    """
    import hashlib

    stare = {k: v for k, v in p.items() if k != "orezat_okraje"}
    h = hashlib.sha256()
    try:
        h.update(Path(cesta_knihy).read_bytes())
    except Exception:
        h.update(str(cesta_knihy).encode("utf-8"))
    for klic in ("jazyk_textu", "referencni_wav", "exaggeration", "cfg_weight",
                 "temperature", "min_p", "odstranit_lupance", "orezat_okraje",
                 "seed", "pauza_ms",
                 "format", "bitrate"):
        h.update(f"{klic}={stare.get(klic)}".encode("utf-8"))
    if stare.get("rychly_dekoder"):
        h.update(b"rychly_dekoder=True")
    h.update(f"bloku={celkem_bloku}".encode("utf-8"))
    kod = (stare.get("jazyk_textu") or "").split("|")[0]
    h.update(otisk_vyslovnosti(kod).encode("utf-8"))
    ref = stare.get("referencni_wav")
    if ref and Path(ref).exists():
        h.update(str(Path(ref).stat().st_mtime_ns).encode("utf-8"))
    return h.hexdigest()[:32]


def posud_navazani(data: dict, zadani: dict):
    """Jde na uložený postup navázat? Vrací (jde, změněná nastavení).

    'zadani' nese otisk_hlasu, otisk_verze_1, opravy a ulozitelne parametry.
    Když navázat jde, seznam obsahuje opravy změněné od přerušení. Když ne,
    obsahuje změněná nastavení hlasu - prázdný seznam pak znamená, že se
    změnil text knihy, jeho rozdělení na bloky nebo soubor s nahrávkou hlasu.
    """
    if not data:
        return False, []
    verze = data.get("verze")
    if verze == 2:
        jde = data.get("otisk_hlasu") == zadani["otisk_hlasu"]
    elif verze == 1:
        jde = data.get("otisk") == zadani["otisk_verze_1"]
    else:
        jde = False
    if jde:
        drive = data.get("opravy") or {}
        return True, [k for k, v in zadani["opravy"].items() if k in drive and drive[k] != v]
    ulozene = data.get("parametry") or {}
    ted = zadani["ulozitelne"]
    return False, [k for k in NASTAVENI_HLASU + ("max_znaku",)
                   if k in ulozene and ulozene[k] != ted.get(k)]


def najdi_rozdelane(slozky) -> list:
    """Najde rozdělané převody podle stavových souborů ve výstupních složkách."""
    nalezene, videne = [], set()
    for slozka in slozky:
        try:
            slozka = Path(slozka)
            if not slozka.is_dir():
                continue
            for cesta in list(slozka.glob("*.progress.json")) + list(slozka.glob("*/*.progress.json")):
                if cesta in videne:
                    continue
                videne.add(cesta)
                try:
                    d = json.loads(cesta.read_text(encoding="utf-8"))
                except Exception:
                    continue
                hotovo, celkem = int(d.get("hotovo_bloku", 0)), int(d.get("celkem_bloku", 0))
                if not celkem or hotovo >= celkem or hotovo <= 0:
                    continue
                nalezene.append({
                    "cesta": cesta,
                    "nazev": d.get("nazev") or cesta.name.replace(".progress.json", ""),
                    "hotovo": hotovo, "celkem": celkem,
                    "procenta": 100.0 * hotovo / celkem,
                    "zdroj": d.get("zdroj", ""),
                    "parametry": d.get("parametry") or {},
                    "kdy": cesta.stat().st_mtime,
                })
        except Exception:
            continue
    nalezene.sort(key=lambda x: -x["kdy"])
    return nalezene


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

    @property
    def hotovo_bloku(self) -> int:
        return int(self.data.get("hotovo_bloku", 0))

    @property
    def preskocene(self) -> list:
        return list(self.data.get("preskocene") or [])

    def uloz(self, zadani: dict, hotovo: int, celkem: int, soubory: list,
             aktualni_wav: str = "", vzorku: int = 0, kapitola: int = 0,
             preskocene: list = None, nazev: str = ""):
        # Zdroj a parametry se ukládají proto, aby šlo rozdělaný převod vybrat
        # ze seznamu i po restartu, kdy má aplikace v polích něco jiného.
        self.data = {"verze": 2, "otisk_hlasu": zadani["otisk_hlasu"],
                     "opravy": zadani.get("opravy") or {}, "hotovo_bloku": hotovo,
                     "celkem_bloku": celkem, "hotove_soubory": soubory,
                     "aktualni_wav": aktualni_wav, "vzorku_v_aktualnim": vzorku,
                     "kapitola": kapitola,
                     "zdroj": zadani.get("zdroj") or self.data.get("zdroj", ""),
                     "nazev": nazev or self.data.get("nazev", ""),
                     "parametry": zadani.get("ulozitelne") or self.data.get("parametry", {}),
                     "preskocene": list(self.preskocene if preskocene is None else preskocene)}
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

# Destilovaný dekodér z Chatterbox Turbo. Tokeny na zvuk převádí ve 2 krocích
# místo 10 a bez CFG. Na 2080 Ti naměřeno 0,73 -> 0,17 s na blok, celý převod
# se ale zrychlí jen o 11 % - 88 % času zabere T3, ne dekodér. Vokodér, enkodér
# hlasu i tokenizer jsou s vícejazyčným s3gen bitově shodné, liší se jen flow.
RYCHLY_DEKODER_REPO = "ResembleAI/chatterbox-turbo"
RYCHLY_DEKODER_SOUBOR = "s3gen_meanflow.safetensors"
RYCHLY_DEKODER_GB = 1.1


def rychly_dekoder_stazeny() -> bool:
    """Je rychlý dekodér už v cache? Jen se podívá na disk, nic nestahuje."""
    slozka = CACHE_DIR / "hub" / ("models--" + RYCHLY_DEKODER_REPO.replace("/", "--"))
    return slozka.is_dir() and any(slozka.rglob(RYCHLY_DEKODER_SOUBOR))


def uvolni_hooky_t3(model) -> int:
    """Zahodí forward hooky, které po sobě chatterbox na T3 nechává.

    T3.inference() si těsně před 'if not self.compiled' nastaví
    self.compiled = False, takže při každém volání vyrobí nový
    AlignmentStreamAnalyzer. Ten si na tři sledované attention vrstvy
    zaregistruje forward hook a handle nikam neuloží - odregistrovat se tedy
    nemá čím. Hooky se hromadí po třech na blok a každý v každém kroku
    kopíruje attention. Naměřeno na 2080 Ti, 60 stejných bloků: bez úklidu
    posledních deset o 11 % pomalejších než prvních deset, s úklidem žádné
    zpomalení.

    Uklízí se před generováním, nikdy během něj - nový analyzátor si svůj
    hook zaregistruje sám.
    """
    try:
        from chatterbox.models.t3.inference.alignment_stream_analyzer import (
            LLAMA_ALIGNED_HEADS)
    except ImportError:
        # Jiná verze balíku, která analyzátor nemá - není co uklízet
        return 0

    vrstvy = getattr(getattr(getattr(model, "t3", None), "tfmr", None), "layers", None)
    if vrstvy is None:
        return 0
    uklizeno = 0
    for index, _hlava in LLAMA_ALIGNED_HEADS:
        hooky = vrstvy[index].self_attn._forward_hooks
        uklizeno += len(hooky)
        hooky.clear()
    return uklizeno


def _opakovani_az_po_doceteni(puvodni):
    """Obal AlignmentStreamAnalyzer.step, který mu token předá až po dočtení textu."""
    @functools.wraps(puvodni)
    def step(self, logits, next_token=None):
        return puvodni(self, logits, next_token=next_token if self.complete else None)

    step.audiobookery = True
    return step


def oprav_predcasny_konec() -> bool:
    """Ať chatterbox neukončí řeč uprostřed textu kvůli dvěma stejným tokenům.

    Analyzátor v chatterboxu 0.1.7 vynutí konec řeči, jakmile jsou dva
    poslední řečové tokeny stejné - a to kdykoli, podmínka "až po dočtení
    textu" je v kódu zakomentovaná. Dva stejné tokeny za sebou jsou přitom
    běžné, třeba v delší pauze, takže model uřízne zbytek bloku. Opakování
    se proto hlídá až po dočtení, jak zakomentovaná podmínka zamýšlela.

    Naměřeno na 45 blocích Ovidia se stejným seedem: původně se konec
    vynutil třikrát a pokaždé chyběla poslední věta (shoda přepisu 0,84 až
    0,93). Po opravě se všechny tři dočetly (0,98 a 0,99), jinak se nezměnil
    ani jeden blok. Úplné vypnutí hlídání dalo totéž.

    Hlavní větev chatterboxu analyzátor v květnu 2026 odstranila, opravená
    verze tedy nevyjde. Vrací, jestli je oprava na místě.
    """
    try:
        from chatterbox.models.t3.inference.alignment_stream_analyzer import AlignmentStreamAnalyzer
    except ImportError:
        return False
    if not getattr(AlignmentStreamAnalyzer.step, "audiobookery", False):
        AlignmentStreamAnalyzer.step = _opakovani_az_po_doceteni(AlignmentStreamAnalyzer.step)
    return True


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
        self.rychly_dekoder = False
        self._hlas_klic = None        # pro který referenční hlas jsou podmínky připravené
        self._vychozi_conds = None    # výchozí hlas modelu z conds.pt

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
    def nacti_model(self, volba_zarizeni: str = "auto", jazyk_klic: str = "en",
                    rychly_dekoder: bool = False):
        import torch

        pozadovane_zarizeni = self.vyber_zarizeni(volba_zarizeni)
        jazyk = jazyk_podle_klice(jazyk_klic)
        pozadovany_repo = jazyk.get("repo") if jazyk.get("zdroj") == "finetune" else None
        rychly_dekoder = bool(rychly_dekoder)

        if self.model is not None:
            # Jiné zařízení, jazykový checkpoint nebo dekodér = čistý start.
            # Nechat na T3 váhy po předchozím jazyce by bylo horší než nic.
            if (pozadovane_zarizeni != self.zarizeni or self.nacteny_repo != pozadovany_repo
                    or rychly_dekoder != self.rychly_dekoder):
                self.log(T("log_znovu"))
                self.uvolni()
            else:
                return

        self.zarizeni = pozadovane_zarizeni
        self.jazyk = jazyk
        self.rychly_dekoder = rychly_dekoder
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
        # Výchozí hlas si odložit - prepare_conditionals() ho přepíše referenčním
        self._vychozi_conds = getattr(self.model, "conds", None)
        self._hlas_klic = None
        self.log(T("log_nacten", self.sr))

        if pozadovany_repo:
            self._nacti_finetune(jazyk)
        elif jazyk.get("zdroj") == "finetune":
            self.log(T("log_bez_ft", jazyk["nazev"]))

        if rychly_dekoder:
            self._zapni_rychly_dekoder()

    # ------------------------------------------------------------------
    def stahni_rychly_dekoder(self) -> Path:
        """Vrátí cestu k vahám rychlého dekodéru. Když chybí, stáhne je.

        Rodič to volá ještě před spuštěním pracovníků - jinak by si soubor
        při prvním použití stahovalo několik procesů naráz.
        """
        from huggingface_hub import hf_hub_download

        argumenty = dict(repo_id=RYCHLY_DEKODER_REPO, filename=RYCHLY_DEKODER_SOUBOR,
                         cache_dir=str(CACHE_DIR / "hub"))
        if rychly_dekoder_stazeny():
            return Path(hf_hub_download(local_files_only=True, **argumenty))
        self.log(T("log_rd_stahuji", RYCHLY_DEKODER_GB))
        with self._hlaseni_stahovani(T("lab_rychly_dekoder")):
            return Path(hf_hub_download(token=os.environ.get("HF_TOKEN") or None, **argumenty))

    # ------------------------------------------------------------------
    def _zapni_rychly_dekoder(self):
        """Vymění dekodér zvuku za destilovanou verzi z Chatterbox Turbo.

        Uživatel si ho zapnul výslovně, takže když se nenačte, převod skončí
        chybou. Tiše pokračovat se standardním by znamenalo, že otisk knihy
        tvrdí něco jiného, než z čeho zvuk opravdu vznikl.
        """
        import gc
        import torch
        from safetensors.torch import load_file
        from chatterbox.models.s3gen import S3Gen

        try:
            cesta = self.stahni_rychly_dekoder()
            dekoder = S3Gen(meanflow=True)
            dekoder.load_state_dict(load_file(str(cesta), device="cpu"))
        except Exception as chyba:
            raise RuntimeError(T("err_rd", f"{type(chyba).__name__}: {chyba}")) from chyba

        # Starý dekodér pustit dřív, než se nový přesune na kartu. Jinak by
        # na chvíli ležely v paměti oba a pracovníkům by to ubralo místo.
        self.model.s3gen = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.model.s3gen = dekoder.to(self.zarizeni).eval()
        self.log(T("log_rd_aktivni"))

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
        # Všechno musí proběhnout před generate() - důvody jsou u funkcí
        oprav_predcasny_konec()
        uvolni_hooky_t3(self.model)
        self._priprav_hlas(referencni_wav, exaggeration)

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
    def _priprav_hlas(self, referencni_wav: str, exaggeration: float):
        """Zakóduje referenční hlas jednou, ne znovu u každého bloku.

        Chatterbox při každém generate() s audio_prompt_path znovu načte WAV
        z disku a prožene ho třemi enkodéry - a to ještě před inference_mode,
        takže si podmínky nesou i graf pro zpětný průchod. Připravené jednou
        pod no_grad ušetří tu práci a na 2080 Ti 280 MB VRAM na proces.

        Expresivitu do klíče dávat nemusíme: generate() si ji v podmínkách
        přepíše sám, když se liší.
        """
        import torch

        if not (referencni_wav and Path(referencni_wav).exists()):
            # Bez reference se čte výchozím hlasem modelu. Po bloku s referencí
            # by na modelu jinak zůstal cizí hlas.
            if self._hlas_klic is not None:
                self.model.conds = self._vychozi_conds
                self._hlas_klic = None
            return

        klic = (str(referencni_wav), Path(referencni_wav).stat().st_mtime_ns)
        if klic == self._hlas_klic:
            return
        with torch.no_grad():
            self.model.prepare_conditionals(referencni_wav, exaggeration=float(exaggeration))
        self._hlas_klic = klic

    # ------------------------------------------------------------------
    def uvolni(self):
        self.model = None
        self.finetune_nacten = False
        self.nacteny_repo = None
        self.podporuje_jazyk = True
        self.rychly_dekoder = False
        self._hlas_klic = None
        self._vychozi_conds = None
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

    # Krátké mezery mezi slovy se musely zahrnout taky - měření ukázalo, že
    # nejvýraznější lupanec seděl v mezeře 92 ms, tedy pod původní hranicí.
    # Okraj se proto škáluje s délkou mezery místo pevných 30 ms.
    min_pauza = int(sr * 0.06)
    maska = np.zeros(len(d), dtype=bool)
    for a, b in zip(zacatky, konce):
        delka = b - a
        if delka < min_pauza:
            continue
        okraj = min(int(sr * 0.03), int(delka * 0.25))
        if delka > 2 * okraj:
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

def orizni_okraje(vzorky, sr: int, zapnuto: bool = True):
    """Odřízne tiché brblání před první a za poslední skutečnou řečí v bloku.

    Model po dořečení věty občas nepřestane a několik sekund tiše hučí.
    Samotnou hlasitostí to od řeči odlišit nejde - naměřeno -33 dBFS proti
    -22 dBFS u řeči, a tichá slabika se do těch jedenácti decibelů vejde.

    Rozliší to ale spektrum: řeč má vždycky souhlásky, tedy energii nad
    4 kHz (naměřeno 10 az 70 %), kdežto to hučení je skoro čistě nízké
    (3 %). Za řeč se proto považuje rámec, který je dost hlasitý *a zároveň*
    má vysoké složky.

    Řeže se jen před prvním a za posledním takovým rámcem, a jen když je
    okrajový úsek delší než 400 ms - aby doznívající samohláska na konci
    věty přežila. Ticho pak dodá pauza, kterou vkládá aplikace sama.

    Vrací (vzorky, odriznuto_sekund).
    """
    import numpy as np

    d = np.asarray(vzorky, dtype="float32").reshape(-1)
    if not zapnuto or len(d) < int(sr * 0.5):
        return d, 0.0

    krok = max(1, int(sr * 0.01))          # 10 ms
    delka_okna = max(krok * 2, int(sr * 0.025))
    pocet = max(1, (len(d) - delka_okna) // krok + 1)
    if pocet < 10:
        return d, 0.0

    okno_fce = np.hanning(delka_okna)
    frekvence = np.fft.rfftfreq(delka_okna, 1.0 / sr)
    vysoke = frekvence > 4000.0

    energie = np.zeros(pocet)
    podil_vys = np.zeros(pocet)
    for i in range(pocet):
        kus = d[i * krok:i * krok + delka_okna].astype("float64")
        if len(kus) < delka_okna:
            break
        energie[i] = np.sqrt((kus ** 2).mean())
        spek = np.abs(np.fft.rfft(kus * okno_fce))
        celkem = spek.sum()
        podil_vys[i] = (spek[vysoke].sum() / celkem) if celkem > 0 else 0.0

    uroven = float(np.percentile(energie, 90))
    if uroven <= 0:
        return d, 0.0

    # řeč = dost hlasitá a se souhláskami
    je_rec = (energie > uroven * 0.10) & (podil_vys > 0.08)
    kde = np.flatnonzero(je_rec)
    if not len(kde):
        return d, 0.0

    prvni, posledni = int(kde[0]), int(kde[-1])
    # Řeže se až od půl sekundy okrajového ticha a doznívání se nechává
    # čtvrt vteřiny - měření ukázalo, že kratší doběh ukousne konec
    # samohlásky na konci věty.
    min_orez = int(0.5 / 0.01)             # 500 ms v rámcích
    doben = int(sr * 0.25)

    zacatek = 0
    if prvni > min_orez:
        zacatek = max(0, prvni * krok - int(sr * 0.05))
    konec = len(d)
    if (pocet - 1 - posledni) > min_orez:
        konec = min(len(d), posledni * krok + delka_okna + doben)

    if zacatek <= 0 and konec >= len(d):
        return d, 0.0
    orez = (len(d) - (konec - zacatek)) / float(sr)
    return d[zacatek:konec].copy(), orez


# Kratší kus už nemá smysl dál dělit - model by četl jednotlivá slova
MIN_CAST_ZNAKU = 40


# --------------------------------------------------------------------------
#  Kontrola zarovnání - model, který nepřestal mluvit
# --------------------------------------------------------------------------
# T3 v chatterboxu zakazuje token konce řeči, dokud pozornost nedojde na konec
# textu. Když se zarovnání cestou ztratí, model nemá jak přestat a generuje
# dál až do stropu 1000 tokenů - vznikne až dvacet sekund hučení a šumu.
# Hlasitostí ani spektrem se to od řeči oddělit nedá (naměřeno -25 až -35 dBFS
# a vysoké složky 0,1 až 0,9), analyzátor zarovnání ale ví, kdy text došel.
# Jeden snímek analyzátoru je jeden řečový token, tedy 40 ms zvuku.
SNIMKU_ZA_SEKUNDU = 25.0
# Přesah za bodem, kde text došel. Nad ním jde o halucinaci. Naměřeno na 41
# blocích: zdravé 0 až 1,48 s, blok s 28 s zvuku pro 185 znaků 1,88 s.
MAX_PRESAH_S = 1.6
# Kolik se při uříznutí nechá za bodem, kde text došel - analyzátor hlásí
# konec o tři textové tokeny dřív. Slyšitelná řeč končila 0,5 s před ním
# až 0,4 s za ním.
REZERVA_ZA_KONCEM_S = 0.8
# Slyšitelná řeč tak dlouho po bodu, kde text došel, je přídavek navíc
# ("Prosím, to siká"). U zdravých bloků nejvýš 0,38 s, u vadných 1,6 a 1,9 s.
MAX_RECI_ZA_KONCEM_S = 0.8
# Tiché brblání uvnitř bloku. Přirozené pauzy trvaly až 2,2 s (dočtené bloky
# se shodou přepisu 0,98), brblání 3,3 a 5,6 s - to se jinak ničím neprozradí.
MAX_TICHO_UVNITR_S = 3.0


def rozbor_reci(vzorky, sr: int):
    """Vrátí (kde končí slyšitelná řeč ve vzorcích, nejdelší tichý úsek uvnitř v s).

    Řeč je rámec nad 35 % hlasitosti 90. percentilu, ticho pod 10 %.
    """
    import numpy as np

    d = np.asarray(vzorky, dtype="float64").reshape(-1)
    krok, okno = max(1, int(sr * 0.02)), max(2, int(sr * 0.04))
    if len(d) < okno * 2:
        return len(d), 0.0
    kumul = np.concatenate(([0.0], np.cumsum(d * d)))
    zacatky = np.arange(0, len(d) - okno + 1, krok)
    rms = np.sqrt((kumul[zacatky + okno] - kumul[zacatky]) / okno)
    uroven = float(np.percentile(rms, 90))
    if uroven <= 0:
        return 0, 0.0
    rec = np.flatnonzero(rms > uroven * 0.35)
    if not len(rec):
        return 0, 0.0
    tiche = (rms[rec[0]:rec[-1] + 1] < uroven * 0.10).astype("int8")
    zmeny = np.diff(np.concatenate(([0], tiche, [0])))
    delky = np.flatnonzero(zmeny == -1) - np.flatnonzero(zmeny == 1)
    nejdelsi = float(delky.max()) * krok / sr if len(delky) else 0.0
    return int(zacatky[rec[-1]] + okno), nejdelsi


def stav_zarovnani(engine):
    """Kde v posledním generování došel text: (snímek, snímků celkem).

    Snímek je None, když model text nedočetl. (None, 0) znamená, že
    analyzátor není - anglický model nebo jiný build chatterboxu.
    """
    t3 = getattr(getattr(engine, "model", None), "t3", None)
    analyzator = getattr(getattr(t3, "patched_model", None), "alignment_stream_analyzer", None)
    if analyzator is None:
        return None, 0
    return analyzator.completed_at, int(analyzator.alignment.shape[0])


def posud_zarovnani(dosel, snimku: int, pocet_vzorku: int, konec_reci: int = None):
    """Vrátí (vada, kolik vzorků ponechat).

    vada: "" v pořádku, "ocas" = po dočtení pokračoval (uříznout jde),
    "nedocteno" = text nedošel do konce, takže něco chybí. konec_reci je
    vzorek, kde končí slyšitelná řeč (z rozbor_reci).
    """
    if snimku <= 0:
        return "", pocet_vzorku
    if dosel is None:
        return "nedocteno", pocet_vzorku
    presah = (snimku - dosel) / SNIMKU_ZA_SEKUNDU
    rec_za_koncem = (konec_reci * snimku / float(pocet_vzorku) - dosel) / SNIMKU_ZA_SEKUNDU \
        if konec_reci is not None and pocet_vzorku else 0.0
    if presah <= MAX_PRESAH_S and rec_za_koncem <= MAX_RECI_ZA_KONCEM_S:
        return "", pocet_vzorku
    konec = (dosel + REZERVA_ZA_KONCEM_S * SNIMKU_ZA_SEKUNDU) / float(snimku)
    return "ocas", int(pocet_vzorku * min(1.0, konec))


# --------------------------------------------------------------------------
#  Kontrola přepisem - výběr nejlepšího pokusu u podezřelého bloku
# --------------------------------------------------------------------------
# Běží jen tam, kde kontrola zarovnání něco našla, tedy asi u procenta bloků.
# Whisper má silný jazykový model a přeřek v jedné hlásce zahladí, takže se
# nehodí na hledání chyb - mezi pokusy o týž text ale pozná, který je kompletní.
ASR_REPO = "openai/whisper-large-v3-turbo"
ASR_GB = 1.6
_asr = {}          # v každém procesu se načte nejvýš jednou


def asr_stazeny() -> bool:
    slozka = CACHE_DIR / "hub" / ("models--" + ASR_REPO.replace("/", "--"))
    return slozka.is_dir() and any(slozka.rglob("*.safetensors"))


def _nacti_asr(log):
    if _asr:
        return _asr
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    if not asr_stazeny():
        log(T("log_asr_stahuji", ASR_GB))
    # Na kartu jen tehdy, když vedle modelu hlasu zbývá místo. Jinak na
    # procesor - u procenta bloků je těch pár sekund navíc jedno.
    zarizeni = "cuda" if torch.cuda.is_available() and volna_vram_gb() > ASR_GB + 1.0 else "cpu"
    typ = torch.float16 if zarizeni == "cuda" else torch.float32
    procesor = WhisperProcessor.from_pretrained(ASR_REPO)
    model = WhisperForConditionalGeneration.from_pretrained(ASR_REPO, dtype=typ).to(zarizeni).eval()
    _asr.update(procesor=procesor, model=model, zarizeni=zarizeni, typ=typ)
    log(T("log_asr_nacten", zarizeni))
    return _asr


def prepis_reci(vzorky, sr: int, jazyk: str, log) -> str:
    import numpy as np
    import torch
    import torchaudio

    a = _nacti_asr(log)
    zvuk = torch.from_numpy(np.asarray(vzorky, dtype="float32").reshape(-1))
    zvuk = torchaudio.functional.resample(zvuk, sr, 16000).numpy()
    vstup = a["procesor"](zvuk, sampling_rate=16000, return_tensors="pt")
    with torch.inference_mode():
        ids = a["model"].generate(vstup.input_features.to(a["zarizeni"], a["typ"]),
                                  language=jazyk or None, task="transcribe")
    return a["procesor"].batch_decode(ids, skip_special_tokens=True)[0]


def shoda_textu(ocekavany: str, prepsany: str) -> float:
    """0 až 1, jak moc přepis odpovídá textu. Jen písmena a číslice, bez velikosti."""
    import difflib

    def jen_slova(t):
        return " ".join(re.findall(r"\w+", t.lower()))

    return difflib.SequenceMatcher(None, jen_slova(ocekavany), jen_slova(prepsany),
                                   autojunk=False).ratio()


def _generuj_jednou(engine, text: str, p: dict, index: int, celkem: int, log):
    """Až tři pokusy o jeden kus textu. Vrátí vzorky, nebo None.

    Čistý pokus se vezme hned. Když kontrola zarovnání najde vadu, zkusí se
    to znovu s jiným seedem. Když jsou vadné všechny tři, vezme se ten
    nejlepší - s kontrolou přepisem ten nejbližší textu, jinak ten, kterému
    šel jen uříznout ocas. Blok se tak kvůli ocasu nikdy nezahodí.
    """
    kandidati = []                  # (vzorky, vada)
    kontrola = bool(p.get("kontrola_asr"))
    jazyk = (p.get("jazyk_textu") or "").split("|")[0]

    def skore(vzorky):
        try:
            return shoda_textu(text, prepis_reci(vzorky, engine.sr, jazyk, log))
        except Exception as chyba:
            log(T("log_asr_chyba", chyba))
            return 0.0

    for pokus in range(1, 4):
        try:
            if pokus > 1:
                nastav_seed(int(time.time() * 1000) % 999983)
            vzorky = engine.generuj(text, p["referencni_wav"], p["exaggeration"],
                                    p["cfg_weight"], p["temperature"],
                                    p.get("min_p", 0.05))
            delka = len(vzorky) / float(engine.sr)
            if delka < 0.05:
                log(T("log_prazdny", index, celkem))
                continue

            dosel, snimku = stav_zarovnani(engine)
            konec_reci, ticho = rozbor_reci(vzorky, engine.sr)
            if snimku:
                vada, ponechat = posud_zarovnani(dosel, snimku, len(vzorky), konec_reci)
            else:
                # Bez analyzátoru zbývá odhad z délky: čte se 13,5 znaku za sekundu
                vada = "dlouhy" if delka > len(text) / 10.0 + 3.0 else ""
                ponechat = len(vzorky)
            if not vada and ticho > MAX_TICHO_UVNITR_S:
                vada = "ticho"
            if vada == "ocas":
                log(T("log_ocas", index, celkem, (len(vzorky) - ponechat) / float(engine.sr)))
                vzorky = vzorky[:ponechat]
            elif vada == "nedocteno":
                log(T("log_nedocteno", index, celkem))
            elif vada == "ticho":
                log(T("log_ticho", index, celkem, ticho))
            elif vada == "dlouhy":
                log(T("log_dlouhy", index, celkem, delka, len(text)))

            vzorky, orez = orizni_okraje(vzorky, engine.sr,
                                         p.get("orezat_okraje", True))
            if orez > 0.3:
                log(T("log_orez", orez, index, celkem))
            vzorky, ztlumeno = odstran_lupance(vzorky, engine.sr,
                                               p.get("odstranit_lupance", True))
            if ztlumeno:
                log(T("log_lupance", ztlumeno, index, celkem))

            if not vada:
                return vzorky
            kandidati.append((vzorky, vada))
        except Exception as chyba:
            log(T("log_pokus", index, celkem, pokus, chyba))
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            time.sleep(0.5)

    if not kandidati:
        return None
    if kontrola and len(kandidati) > 1:
        body = [skore(v) for v, _ in kandidati]
        nejlepsi = max(range(len(kandidati)), key=lambda i: body[i])
        log(T("log_asr_vyber", index, celkem, nejlepsi + 1, len(kandidati), body[nejlepsi]))
        return kandidati[nejlepsi][0]
    # Bez přepisu: čistý pokus, jinak uříznutý ocas, jinak poslední
    poradi = {"": 0, "ocas": 1, "dlouhy": 2, "nedocteno": 3}
    return min(reversed(kandidati), key=lambda k: poradi[k[1]])[0]


def generuj_blok(engine, blok: str, p: dict, index: int, celkem: int, log, _hloubka: int = 0):
    """Vygeneruje blok. Vrací (vzorky nebo None, vynechané úseky textu).

    Když blok nevyjde ani na tři pokusy, rozdělí se na kratší části a zkusí se
    po nich - kratší text model rozhodí méně. Zpátky do knihy se to vloží na
    stejné místo, takže pořadí sedí. Vynechá se jen to, co nevyjde ani po
    rozdělení, a to se vrátí, ať se to dá uživateli ukázat.

    Používají to obě cesty - jednoprocesová i jednotlivý pracovník poolu.
    """
    import numpy as np

    vzorky = _generuj_jednou(engine, blok, p, index, celkem, log)
    if vzorky is not None:
        return vzorky, []

    casti = rozdel_na_bloky(blok, max(MIN_CAST_ZNAKU, len(blok) // 2)) if _hloubka < 2 else []
    if len(casti) < 2:
        log(T("log_preskocen", index, celkem, blok[:60]))
        return None, [blok]

    log(T("log_rozdeleno", index, celkem, len(casti)))
    pauza = np.zeros(int(engine.sr * min(p.get("pauza_ms", 250), 150) / 1000.0), dtype="float32")
    kusy, vynechano = [], []
    for cast in casti:
        v, chybi = generuj_blok(engine, cast, p, index, celkem, log, _hloubka + 1)
        vynechano += chybi
        if v is not None:
            if kusy:
                kusy.append(pauza)
            kusy.append(np.asarray(v, dtype="float32"))
    return (np.concatenate(kusy) if kusy else None), vynechano


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
        """Vrátí (index, (vzorky, vynechané úseky)) dalšího bloku v pořadí, nebo None při chybě."""
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

# ==========================================================================
#  Oprava jednoho úseku v hotové nahrávce
# ==========================================================================

def nazev_souboru_kapitoly(kap_i: int, nazev: str) -> str:
    """Jméno souboru kapitoly bez přípony, stejně pro převod i pro opravu."""
    nazev = re.sub(ZAKAZANE_ZNAKY, "", (nazev or "").strip())[:60].strip(" .")
    return "{:02d}".format(kap_i + 1) + (" - " + nazev if nazev else "")


class MapaBloku:
    """Kde v souborech který blok leží. Řádek JSON na blok, jen se připisuje.

    Připisování nestojí nic ani u knihy o pěti tisících blocích. Po přerušení
    a navázání se bloky generují znovu - platí pak poslední zápis.
    """

    PRIPONA = ".blocks.jsonl"

    def __init__(self, cesta: Path):
        self.cesta = Path(cesta)

    def smaz(self):
        self.cesta.unlink(missing_ok=True)

    def _pripis(self, zaznam: dict):
        with open(self.cesta, "a", encoding="utf-8") as f:
            f.write(json.dumps(zaznam, ensure_ascii=False) + "\n")

    def hlavicka(self, parametry: dict, sr: int, nazev: str):
        self._pripis({"hlavicka": {"parametry": parametry, "sr": int(sr), "nazev": nazev}})

    def pridej(self, blok: int, soubor: str, od: int, delka: int, text: str):
        self._pripis({"blok": int(blok), "soubor": soubor, "od": int(od),
                      "delka": int(delka), "text": text})

    def nacti(self):
        """Vrátí (hlavička, {soubor: [záznamy seřazené podle začátku]})."""
        hlavicka, bloky = {}, {}
        if not self.cesta.exists():
            return hlavicka, {}
        for radek in self.cesta.read_text(encoding="utf-8").splitlines():
            if not radek.strip():
                continue
            zaznam = json.loads(radek)
            if "hlavicka" in zaznam:
                hlavicka = zaznam["hlavicka"]
            else:
                bloky[zaznam["blok"]] = zaznam
        soubory = {}
        for zaznam in bloky.values():
            soubory.setdefault(zaznam["soubor"], []).append(zaznam)
        for seznam in soubory.values():
            seznam.sort(key=lambda z: z["od"])
        return hlavicka, soubory

    def prepis(self, hlavicka: dict, soubory: dict):
        docasny = self.cesta.with_suffix(".tmp")
        with open(docasny, "w", encoding="utf-8") as f:
            if hlavicka:
                f.write(json.dumps({"hlavicka": hlavicka}, ensure_ascii=False) + "\n")
            for seznam in soubory.values():
                for zaznam in seznam:
                    f.write(json.dumps(zaznam, ensure_ascii=False) + "\n")
        docasny.replace(self.cesta)


def najdi_mapu(soubor: Path):
    """Mapa, ve které je daný soubor, nebo None."""
    soubor = Path(soubor)
    for cesta in sorted(soubor.parent.glob("*" + MapaBloku.PRIPONA)):
        try:
            if soubor.stem in MapaBloku(cesta).nacti()[1]:
                return cesta
        except Exception:
            continue
    return None


def najdi_pauzy(pcm, sr: int, min_s: float) -> list:
    """Úseky digitálního ticha delší než min_s jako (začátek, konec) ve vzorcích."""
    import numpy as np

    tiche = (np.abs(np.asarray(pcm).astype("int32")) <= 2).astype("int8")
    zmeny = np.diff(np.concatenate(([0], tiche, [0])))
    zacatky, konce = np.flatnonzero(zmeny == 1), np.flatnonzero(zmeny == -1)
    nejmene = max(1, int(sr * min_s))
    return [(int(a), int(b)) for a, b in zip(zacatky, konce) if b - a >= nejmene]


def rekonstruuj_bloky(pcm, sr: int, pauza_ms: int, pocet: int):
    """Hranice bloků podle pauz, které aplikace vkládá za každý blok.

    Vrací (seznam (od, délka), kolik pauz se našlo). Seznam je None, když počet
    nesedí. Práh je 80 % pauzy: rozdělený blok má uvnitř nejvýš 150 ms a ticho
    přímo od modelu digitální nulu nemá. Na deseti kapitolách Ovidia sedělo vše.
    """
    if pauza_ms <= 0:
        return None, 0
    pauzy = [(a, b) for a, b in najdi_pauzy(pcm, sr, 0.8 * pauza_ms / 1000.0) if a > 0]
    if len(pauzy) != pocet:
        return None, len(pauzy)
    useky, od = [], 0
    for a, b in pauzy:
        useky.append((od, a - od))
        od = b
    return useky, len(pauzy)


def cas_na_sekundy(text: str):
    """'85', '1:25', '01:25.5' i '0:01:25' na sekundy. Nesmysl vrátí None."""
    casti = text.strip().replace(",", ".").split(":")
    if not casti or len(casti) > 3:
        return None
    try:
        hodnoty = [float(c) for c in casti]
    except ValueError:
        return None
    if any(h < 0 for h in hodnoty):
        return None
    sekundy = 0.0
    for h in hodnoty:
        sekundy = sekundy * 60 + h
    return sekundy


def vymen_usek(pcm, od: int, delka: int, nove):
    """Nahradí vzorky [od, od+delka) novými. Vrací (nové pole, posun dalších bloků)."""
    import numpy as np

    nove = (np.clip(np.asarray(nove, dtype="float32").reshape(-1), -1.0, 1.0) * 32767.0).astype("<i2")
    return np.concatenate([pcm[:od], nove, pcm[od + delka:]]), len(nove) - delka


def _ffprobe() -> str:
    ffmpeg = najdi_ffmpeg()
    if not ffmpeg:
        return ""
    vedle = Path(ffmpeg).with_name("ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe")
    return str(vedle) if vedle.exists() else (shutil.which("ffprobe") or "")


def _spust(prikaz) -> subprocess.CompletedProcess:
    return subprocess.run(prikaz, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def dekoduj_zvuk(cesta: Path, sr: int):
    """Celý soubor jako mono int16 v dané vzorkovací frekvenci."""
    import numpy as np

    vysledek = _spust([najdi_ffmpeg(), "-v", "error", "-i", str(cesta),
                       "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"])
    if vysledek.returncode != 0:
        raise RuntimeError(vysledek.stderr.decode("utf-8", "replace").strip()[:300])
    return np.frombuffer(vysledek.stdout, dtype="<i2").copy()


def uloz_zvuk(cesta: Path, pcm, sr: int, bitrate: str = "") -> Path:
    """Přepíše soubor novým zvukem. Předchozí verzi zazálohuje do temp, vrátí cestu k záloze.

    U MP3 se zachová ID3 i obálka. Nový soubor vzniká vedle a teprve hotový
    nahradí původní, takže nepovedený převod nic nerozbije.
    """
    cesta = Path(cesta)
    zalohy = TEMP_DIR / "zalohy_oprav"
    zalohy.mkdir(parents=True, exist_ok=True)
    zaloha = zalohy / (cesta.stem + time.strftime(" %Y%m%d-%H%M%S") + cesta.suffix)
    shutil.copy2(cesta, zaloha)

    docasny_wav = cesta.with_name(cesta.stem + ".oprava.wav")
    z = WavZapisovac(docasny_wav, sr)
    z.soubor.writeframes(pcm.astype("<i2").tobytes())
    z.zavri()

    if cesta.suffix.lower() != ".mp3":
        docasny_wav.replace(cesta)
        return zaloha

    meta, obalka = {}, None
    probe = _ffprobe()
    if probe:
        vysledek = _spust([probe, "-v", "error", "-show_entries", "format=bit_rate:format_tags",
                           "-of", "json", str(cesta)])
        if vysledek.returncode == 0:
            format_ = json.loads(vysledek.stdout.decode("utf-8", "replace")).get("format", {})
            meta = {k.lower(): v for k, v in (format_.get("tags") or {}).items()}
            if not bitrate and format_.get("bit_rate"):
                bitrate = "{}k".format(round(int(format_["bit_rate"]) / 1000))
    kandidat = cesta.with_name(cesta.stem + ".oprava.jpg")
    if _spust([najdi_ffmpeg(), "-v", "error", "-y", "-i", str(cesta), "-an",
               "-c:v", "copy", str(kandidat)]).returncode == 0 and kandidat.exists():
        obalka = kandidat

    docasny_mp3 = cesta.with_name(cesta.stem + ".oprava.mp3")
    try:
        if not prevod_na_mp3(docasny_wav, docasny_mp3, bitrate or "128k", meta, obalka):
            raise RuntimeError(T("oprava_mp3_selhal"))
        docasny_mp3.replace(cesta)
    finally:
        docasny_wav.unlink(missing_ok=True)
        docasny_mp3.unlink(missing_ok=True)
        if obalka is not None:
            obalka.unlink(missing_ok=True)
    return zaloha


class DialogRozdelane(tk.Toplevel):
    """Nabídka rozdělaných převodů. Vrací vybraný záznam, nebo None."""

    def __init__(self, rodic, zaznamy, font_rodina):
        super().__init__(rodic)
        self.vybrany = None
        self._zaznamy = zaznamy

        self.title(T("dlg_rozdelane"))
        self.configure(background=BARVY["pozadi"])
        self.transient(rodic)
        self.resizable(False, False)

        ramec = ttk.Frame(self, padding=(22, 18, 22, 16))
        ramec.pack(fill="both", expand=True)

        ttk.Label(ramec, text=T("dlg_rozdelane_popis"),
                  style="Tlumeny.TLabel").pack(anchor="w", pady=(0, 12))

        seznam = tk.Frame(ramec, background=BARVY["panel"])
        seznam.pack(fill="both", expand=True)

        self.box = tk.Listbox(
            seznam, height=min(10, max(3, len(zaznamy))), width=68,
            font=(font_rodina, 9), activestyle="none",
            background=BARVY["panel"], foreground=BARVY["text"],
            selectbackground=BARVY["akcent"], selectforeground=BARVY["pozadi"],
            relief="flat", borderwidth=0, highlightthickness=0)
        self.box.pack(side="left", fill="both", expand=True, padx=10, pady=8)
        posuv = ttk.Scrollbar(seznam, orient="vertical", command=self.box.yview,
                              style="Tenky.Vertical.TScrollbar")
        posuv.pack(side="right", fill="y")
        self.box.configure(yscrollcommand=posuv.set)

        for z in zaznamy:
            kdy = time.strftime("%d.%m. %H:%M", time.localtime(z["kdy"]))
            self.box.insert("end",
                            f"  {z['nazev'][:34]:36} {z['procenta']:5.1f} %   "
                            f"{z['hotovo']}/{z['celkem']}   {kdy}")
        if zaznamy:
            self.box.selection_set(0)
        self.box.bind("<Double-Button-1>", lambda _u: self._potvrd())

        self.popis = ttk.Label(ramec, text="", style="Tlumeny.TLabel", wraplength=520)
        self.popis.pack(anchor="w", pady=(10, 0))
        self.box.bind("<<ListboxSelect>>", lambda _u: self._obnov_popis())
        self._obnov_popis()

        tlacitka = ttk.Frame(ramec)
        tlacitka.pack(fill="x", pady=(16, 0))
        ttk.Button(tlacitka, text=T("btn_pokracovat_prevod"), style="Akce.TButton",
                   command=self._potvrd).pack(side="left")
        ttk.Button(tlacitka, text=T("btn_zrusit"), style="Tichy.TButton",
                   command=self._zrus).pack(side="left", padx=(10, 0))

        self.protocol("WM_DELETE_WINDOW", self._zrus)
        self.bind("<Escape>", lambda _u: self._zrus())
        self.bind("<Return>", lambda _u: self._potvrd())

        self.update_idletasks()
        x = rodic.winfo_rootx() + (rodic.winfo_width() - self.winfo_width()) // 2
        y = rodic.winfo_rooty() + 120
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.box.focus_set()

    def _obnov_popis(self):
        vyber = self.box.curselection()
        if not vyber:
            self.popis.config(text="")
            return
        z = self._zaznamy[vyber[0]]
        zdroj = Path(z["zdroj"]).name if z.get("zdroj") else "?"
        chybi = "" if (not z.get("zdroj") or Path(z["zdroj"]).exists()) else T("dlg_zdroj_chybi")
        self.popis.config(text=f"{T('dlg_zdroj')}: {zdroj}   ·   {z['cesta'].parent}{chybi}")

    def _potvrd(self):
        vyber = self.box.curselection()
        if vyber:
            self.vybrany = self._zaznamy[vyber[0]]
        self.destroy()

    def _zrus(self):
        self.vybrany = None
        self.destroy()


class DialogOprava(tk.Toplevel):
    """Najde blok podle času, vygeneruje ho znovu a vymění v hotovém souboru."""

    def __init__(self, app, slozka: Path):
        super().__init__(app)
        self.app = app
        self.slozka = Path(slozka)
        self.fronta = queue.Queue()
        self.prace = False
        self.soubor = None
        self.mapa = None
        self.hlavicka = {}
        self.vsechny = {}          # {soubor: [záznamy]} z celé mapy
        self.zaznamy = []          # bloky vybraného souboru
        self.aktualni = None
        self.pcm = None
        self.sr = 24000
        self.nove = None
        self.novy_text = ""

        self.title(T("dlg_oprava"))
        self.configure(background=BARVY["pozadi"])
        self.transient(app)
        self.minsize(660, 380)

        ramec = ttk.Frame(self, padding=(22, 18, 22, 16))
        ramec.pack(fill="both", expand=True)
        ttk.Label(ramec, text=T("oprava_popis"), style="Tlumeny.TLabel",
                  wraplength=660, justify="left").pack(anchor="w", pady=(0, 12))

        rada = ttk.Frame(ramec)
        rada.pack(fill="x")
        ttk.Label(rada, text=T("lab_soubor")).pack(side="left", padx=(0, 12))
        self.var_soubor = tk.StringVar()
        self.vyber = ttk.Combobox(rada, textvariable=self.var_soubor, state="readonly", width=52)
        self.vyber.pack(side="left", fill="x", expand=True)
        self.vyber.bind("<<ComboboxSelected>>",
                        lambda _u: self._nacti(self.slozka / self.var_soubor.get()))
        ttk.Button(rada, text=T("btn_vybrat"), style="Tichy.TButton",
                   command=self._vyber_soubor).pack(side="left", padx=(8, 0))

        rada = ttk.Frame(ramec)
        rada.pack(fill="x", pady=(10, 0))
        ttk.Label(rada, text=T("lab_cas")).pack(side="left", padx=(0, 12))
        self.var_cas = tk.StringVar()
        pole = ttk.Entry(rada, textvariable=self.var_cas, width=10)
        pole.pack(side="left")
        pole.bind("<Return>", lambda _u: self._najdi())
        ttk.Button(rada, text=T("btn_najit"), style="Tichy.TButton",
                   command=self._najdi).pack(side="left", padx=(8, 0))
        ttk.Button(rada, text="◀", width=3, style="Tichy.TButton",
                   command=lambda: self._posun(-1)).pack(side="left", padx=(20, 0))
        ttk.Button(rada, text="▶", width=3, style="Tichy.TButton",
                   command=lambda: self._posun(1)).pack(side="left", padx=(6, 0))
        self.var_blok = tk.StringVar()
        ttk.Label(rada, textvariable=self.var_blok,
                  style="Tlumeny.TLabel").pack(side="left", padx=(14, 0))

        self.text = tk.Text(ramec, height=6, width=80, wrap="word", font=(app.font_rodina, 10),
                            background=BARVY["panel"], foreground=BARVY["text"],
                            insertbackground=BARVY["text"], relief="flat", borderwidth=0,
                            highlightthickness=0, padx=10, pady=8)
        self.text.pack(fill="both", expand=True, pady=(12, 0))

        tlacitka = ttk.Frame(ramec)
        tlacitka.pack(fill="x", pady=(14, 0))
        self.btn_puvodni = ttk.Button(tlacitka, text=T("btn_prehrat_puvodni"), style="Tichy.TButton",
                                      command=self._prehraj_puvodni)
        self.btn_puvodni.pack(side="left")
        self.btn_generovat = ttk.Button(tlacitka, text=T("btn_generovat_znovu"), style="Akce.TButton",
                                        command=self._generuj)
        self.btn_generovat.pack(side="left", padx=(10, 0))
        self.btn_novy = ttk.Button(tlacitka, text=T("btn_prehrat_novy"), style="Tichy.TButton",
                                   command=self._prehraj_novy)
        self.btn_novy.pack(side="left", padx=(10, 0))
        self.btn_nahradit = ttk.Button(tlacitka, text=T("btn_nahradit"), style="Tichy.TButton",
                                       command=self._nahrad)
        self.btn_nahradit.pack(side="left", padx=(10, 0))
        ttk.Button(tlacitka, text=T("btn_zavrit"), style="Tichy.TButton",
                   command=self._zavri).pack(side="right")

        self.var_stav = tk.StringVar()
        ttk.Label(ramec, textvariable=self.var_stav, style="Tlumeny.TLabel",
                  wraplength=660, justify="left").pack(anchor="w", pady=(10, 0))

        self.protocol("WM_DELETE_WINDOW", self._zavri)
        self.bind("<Escape>", lambda _u: self._zavri())

        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, app.winfo_rooty() + 120)}")
        self.grab_set()
        self._nabidni_soubory()
        self.after(120, self._zpracuj_frontu)

    # ------------------------------------------------------------------
    def _nabidni_soubory(self, vybrat: str = ""):
        # Rozepsaná kapitola z přerušeného převodu se měnit nesmí - navázání
        # ji usekne na uložený počet vzorků.
        rozepsane = set()
        for kde in (self.slozka, self.slozka.parent):
            for stav in kde.glob("*.progress.json"):
                try:
                    data = json.loads(stav.read_text(encoding="utf-8"))
                    rozepsane.add(Path(data.get("aktualni_wav") or "").name)
                except Exception:
                    continue
        soubory = sorted(p.name for p in self.slozka.glob("*")
                         if p.suffix.lower() in (".mp3", ".wav") and ".oprava." not in p.name
                         and p.name not in rozepsane) if self.slozka.is_dir() else []
        self.vyber.configure(values=soubory)
        if not soubory:
            self.var_stav.set(T("oprava_zadny_soubor", self.slozka))
            self._obnov_tlacitka()
            return
        self.var_soubor.set(vybrat if vybrat in soubory else soubory[0])
        self._nacti(self.slozka / self.var_soubor.get())

    def _vyber_soubor(self):
        if self.prace:
            return
        cesta = filedialog.askopenfilename(parent=self, initialdir=str(self.slozka),
                                           filetypes=[(T("filtr_zvuk"), "*.mp3 *.wav")])
        if cesta:
            self.slozka = Path(cesta).parent
            self._nabidni_soubory(Path(cesta).name)

    def _nacti(self, cesta: Path):
        if self.prace:
            return
        cesta = Path(cesta)
        self.soubor, self.zaznamy, self.aktualni, self.nove, self.pcm = cesta, [], None, None, None
        self.text.delete("1.0", "end")
        self.var_blok.set("")
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            mapa = najdi_mapu(cesta)
            if mapa is not None:
                self.mapa = MapaBloku(mapa)
                self.hlavicka, self.vsechny = self.mapa.nacti()
                self.sr = int(self.hlavicka.get("sr") or 24000)
                self.pcm = dekoduj_zvuk(cesta, self.sr)
                self.zaznamy = [z for z in self.vsechny.get(cesta.stem, [])
                                if z["od"] + z["delka"] <= len(self.pcm)]
                pauza = int((self.hlavicka.get("parametry") or {}).get("pauza_ms", 0))
                if not self.souvisla(self.zaznamy, self.sr, pauza):
                    mapa = None
            if mapa is None:
                self.pcm, self.zaznamy = None, []
                self._dohledej(cesta)
            else:
                self.var_stav.set(T("oprava_nacteno", len(self.zaznamy)))
        except Exception as chyba:
            self.pcm, self.zaznamy = None, []
            self.var_stav.set(T("oprava_chyba", chyba))
        finally:
            self.configure(cursor="")
        self._obnov_tlacitka()

    @staticmethod
    def souvisla(zaznamy, sr: int, pauza_ms: int) -> bool:
        """Pokrývá mapa soubor od začátku bez mezer?

        Kapitola rozepsaná ve verzi bez mapy má po navázání zapsané jen bloky
        od místa navázání. Podle takové mapy by čas ukázal na špatný blok.
        """
        pauza = int(sr * pauza_ms / 1000.0) if pauza_ms > 0 else 0
        if not zaznamy or zaznamy[0]["od"] != 0:
            return False
        return all(dalsi["od"] == z["od"] + z["delka"] + pauza
                   for z, dalsi in zip(zaznamy, zaznamy[1:]))

    def _dohledej(self, cesta: Path):
        """Soubor bez mapy: hranice bloků podle pauz a texty z načtené knihy."""
        app = self.app
        if app.ma_kapitoly:
            cislo = re.match(r"(\d+)", cesta.stem)
            kap_i = int(cislo.group(1)) - 1 if cislo else -1
            texty = [(i + 1, b) for i, (k, b) in enumerate(app.bloky) if k == kap_i]
        else:
            texty = [(i + 1, b) for i, (_k, b) in enumerate(app.bloky)]
        if not texty:
            self.var_stav.set(T("oprava_bez_mapy"))
            return
        self.sr = 24000
        pcm = dekoduj_zvuk(cesta, self.sr)
        useky, nalezeno = rekonstruuj_bloky(pcm, self.sr, int(app.var_pauza.get()), len(texty))
        if useky is None:
            self.var_stav.set(T("oprava_nesedi", len(texty), nalezeno))
            return
        nazev = cesta.parent.name if app.ma_kapitoly else cesta.stem
        self.mapa = MapaBloku(cesta.parent / (nazev + MapaBloku.PRIPONA))
        self.hlavicka, self.vsechny = self.mapa.nacti()
        if not self.hlavicka:
            self.hlavicka = {"parametry": app._posbirej_parametry(), "sr": self.sr, "nazev": nazev}
        self.pcm = pcm
        self.zaznamy = [{"blok": i, "soubor": cesta.stem, "od": od, "delka": delka, "text": text}
                        for (i, text), (od, delka) in zip(texty, useky)]
        self.vsechny[cesta.stem] = self.zaznamy
        self.mapa.prepis(self.hlavicka, self.vsechny)
        self.var_stav.set(T("oprava_dohledano", len(self.zaznamy)))

    # ------------------------------------------------------------------
    def _najdi(self):
        if not self.zaznamy:
            return
        sekundy = cas_na_sekundy(self.var_cas.get())
        if sekundy is None:
            self.var_stav.set(T("oprava_spatny_cas"))
            return
        vzorek = int(sekundy * self.sr)
        if vzorek >= len(self.pcm):
            self.var_stav.set(T("oprava_mimo"))
            return
        pred = [i for i, z in enumerate(self.zaznamy) if z["od"] <= vzorek]
        self._ukaz(pred[-1] if pred else 0)

    def _posun(self, krok: int):
        if self.aktualni is not None:
            self._ukaz(max(0, min(len(self.zaznamy) - 1, self.aktualni + krok)))

    def _ukaz(self, i: int):
        if self.prace:
            return
        self.aktualni, self.nove = i, None
        z = self.zaznamy[i]
        self.var_blok.set(T("oprava_blok", z["blok"], formatuj_cas(z["od"] / self.sr),
                            formatuj_cas((z["od"] + z["delka"]) / self.sr)))
        self.text.delete("1.0", "end")
        self.text.insert("1.0", z["text"])
        self.var_stav.set("")
        self._obnov_tlacitka()

    def _obnov_tlacitka(self):
        vybrano = self.aktualni is not None and not self.prace
        for tlacitko in (self.btn_puvodni, self.btn_generovat):
            tlacitko.configure(state="normal" if vybrano else "disabled")
        for tlacitko in (self.btn_novy, self.btn_nahradit):
            tlacitko.configure(state="normal" if vybrano and self.nove is not None else "disabled")

    def _pracuje(self, stav: bool, zprava: str):
        self.prace = stav
        self.var_stav.set(zprava)
        self.configure(cursor="watch" if stav else "")
        self._obnov_tlacitka()

    # ------------------------------------------------------------------
    def _prehraj(self, vzorky, jmeno: str):
        cesta = TEMP_DIR / jmeno
        z = WavZapisovac(cesta, self.sr)
        z.zapis(vzorky)
        z.zavri()
        self.app.prehraj(cesta)

    def _prehraj_puvodni(self):
        z = self.zaznamy[self.aktualni]
        self._prehraj(self.pcm[z["od"]:z["od"] + z["delka"]].astype("float32") / 32767.0,
                      "oprava_puvodni.wav")

    def _prehraj_novy(self):
        if self.nove is not None:
            self._prehraj(self.nove, "oprava_novy.wav")

    def _parametry(self) -> dict:
        # Hlas knihy z mapy, jinak (u dohledaných souborů) to, co je v okně
        return {**self.app._posbirej_parametry(), **(self.hlavicka.get("parametry") or {})}

    def _generuj(self):
        text = self.text.get("1.0", "end").strip()
        if self.prace or self.aktualni is None or not text:
            return
        z = self.zaznamy[self.aktualni]
        p = self._parametry()
        self._pracuje(True, T("oprava_generuji"))

        def prace():
            try:
                engine = self.app.engine
                engine.nacti_model(p["zarizeni"], p["jazyk_textu"], p.get("rychly_dekoder", False))
                nastav_seed(int(time.time() * 1000) % 999983)
                vzorky, _vynechano = generuj_blok(engine, text, p, z["blok"], z["blok"],
                                                  self.app.log_z_vlakna)
                self.fronta.put(("vygenerovano", (vzorky, text)))
            except Exception as chyba:
                self.fronta.put(("chyba", chyba))

        threading.Thread(target=prace, daemon=True).start()

    def _nahrad(self):
        if self.prace or self.nove is None:
            return
        z = self.zaznamy[self.aktualni]
        cesta, pcm, nove, sr = self.soubor, self.pcm, self.nove, self.sr
        self._pracuje(True, T("oprava_nahrazuji"))

        def prace():
            try:
                nove_pcm, posun = vymen_usek(pcm, z["od"], z["delka"], nove)
                zaloha = uloz_zvuk(cesta, nove_pcm, sr)
                self.fronta.put(("nahrazeno", (nove_pcm, posun, zaloha)))
            except Exception as chyba:
                self.fronta.put(("chyba", chyba))

        threading.Thread(target=prace, daemon=True).start()

    def _zpracuj_frontu(self):
        try:
            while True:
                typ, data = self.fronta.get_nowait()
                if typ == "vygenerovano":
                    vzorky, text = data
                    self._pracuje(False, "")
                    if vzorky is None:
                        self.var_stav.set(T("oprava_nevyslo"))
                        continue
                    self.nove, self.novy_text = vzorky, text
                    z = self.zaznamy[self.aktualni]
                    self.var_stav.set(T("oprava_vygenerovano", len(vzorky) / self.sr, z["delka"] / self.sr))
                    self._obnov_tlacitka()
                    self._prehraj_novy()
                elif typ == "nahrazeno":
                    pcm, posun, zaloha = data
                    z = self.zaznamy[self.aktualni]
                    z["delka"] += posun
                    z["text"] = self.novy_text
                    for dalsi in self.zaznamy[self.aktualni + 1:]:
                        dalsi["od"] += posun
                    self.pcm = pcm
                    self.mapa.prepis(self.hlavicka, self.vsechny)
                    self.app.log(T("log_oprava", self.soubor.name, z["blok"]))
                    self.prace = False
                    self._ukaz(self.aktualni)
                    self.var_stav.set(T("oprava_nahrazeno", zaloha))
                elif typ == "chyba":
                    self._pracuje(False, T("oprava_chyba", data))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._zpracuj_frontu)

    def _zavri(self):
        if self.prace:
            return
        self.grab_release()
        self.destroy()


class Aplikace(tk.Tk):

    def __init__(self):
        super().__init__()
        # Konfigurace se musí načíst dřív než cokoli s textem - jazyk se
        # uplatní hned na titulku okna, ne až u prvního popisku.
        self.config_data = self._nacti_config()
        nastav_jazyk(self.config_data.get("jazyk", "en"))

        self.title(f"{T('app_nazev')} v{VERSION}")
        # Na nízkém monitoru se okno o pevných 980 bodech nevejde a ovládání
        # i průběh zůstanou pod hranou obrazovky. Výška se proto řídí obrazovkou;
        # sbalenými sekcemi jde okno zkrátit, takže minimum může být nízko.
        sirka = min(1000, self.winfo_screenwidth() - 40)
        vyska = min(980, self.winfo_screenheight() - 100)
        self.geometry(f"{sirka}x{vyska}")
        # Řada ovládání s opravou úseku potřebuje v češtině 904 bodů plus okraje
        self.minsize(min(960, sirka), min(480, vyska))

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
        # Kopie, ne odkaz do DEFAULT_CONFIG - ten je sdílený
        self.sbalene_sekce = dict(self.config_data.get("sbalene_sekce") or {})
        self._kapitola_v_behu = -1     # kapitola, kterou právě ukazuje druhý pruh

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
            "orezat_okraje": bool(self.var_orez.get()),
            "rychly_dekoder": bool(self.var_rychly_dekoder.get()),
            "kontrola_asr": bool(self.var_kontrola_asr.get()),
            "seed": int(self.var_seed.get() or 0),
            "jazyk_textu": self._klic_jazyka_textu(),
            "zarizeni": self.var_zarizeni.get(),
            "testovaci_veta": self.var_test_veta.get(),
            "poslouchat": bool(self.var_poslouchat.get()),
            "naskok_s": int(self.var_naskok.get()),
            "obalka": bool(self.var_obalka.get()),
            "pracovniku": int(self.var_pracovniku.get()),
            "jazyk": aktualni_jazyk(),
            "sbalene_sekce": dict(self.sbalene_sekce),
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
        self.var_orez = tk.BooleanVar(value=c["orezat_okraje"])
        self.var_rychly_dekoder = tk.BooleanVar(value=c["rychly_dekoder"])
        self.var_kontrola_asr = tk.BooleanVar(value=c["kontrola_asr"])
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
        self.var_postup_kapitola = tk.DoubleVar(value=0.0)
        self.var_kapitola_info = tk.StringVar(value="")
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
        poslech = self._sekce_sbalitelna(hlavni, T("sekce_poslech"), sbaleno=False, klic="poslech")
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
        gen = self._sekce_sbalitelna(hlavni, T("sekce_pokrocile"), klic="pokrocile")
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

        # Dva řádky - v jednom se to do okna širokého 1000 bodů nevešlo
        spodek = ttk.Frame(gen)
        spodek.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(14, 0))
        ttk.Label(spodek, text=T("lab_zarizeni")).pack(side="left", padx=(0, 12))
        cb = ttk.Combobox(spodek, textvariable=self.var_zarizeni, width=6, state="readonly",
                          values=["auto", "cuda", "cpu"])
        cb.pack(side="left")
        ttk.Label(spodek, text=T("lab_pracovniku")).pack(side="left", padx=(28, 12))
        sp_w = ttk.Spinbox(spodek, from_=0, to=4, increment=1,
                           textvariable=self.var_pracovniku, width=5)
        sp_w.pack(side="left")

        volby = ttk.Frame(gen)
        volby.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ch2 = ttk.Checkbutton(volby, text=T("lab_obalka"), variable=self.var_obalka)
        ch2.pack(side="left")
        ch3 = ttk.Checkbutton(volby, text=T("lab_lupance"), variable=self.var_lupance)
        ch3.pack(side="left", padx=(28, 0))
        ch4 = ttk.Checkbutton(volby, text=T("lab_orez"), variable=self.var_orez)
        ch4.pack(side="left", padx=(28, 0))
        self._zamknout(cb, ch2, ch3, ch4, sp_w)

        # Vlastní řádek - vedle je potřeba říct, co zapnutí stojí
        rada_rd = ttk.Frame(gen)
        rada_rd.grid(row=6, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ch5 = ttk.Checkbutton(rada_rd, text=T("lab_rychly_dekoder"),
                              variable=self.var_rychly_dekoder)
        ch5.pack(side="left")
        ttk.Label(rada_rd, text=T("hint_rd_stazeno") if rychly_dekoder_stazeny()
                  else T("hint_rd_stahne", RYCHLY_DEKODER_GB),
                  style="Tlumeny.TLabel").pack(side="left", padx=(14, 0))
        self._zamknout(ch5)

        rada_asr = ttk.Frame(gen)
        rada_asr.grid(row=7, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ch6 = ttk.Checkbutton(rada_asr, text=T("lab_kontrola_asr"),
                              variable=self.var_kontrola_asr)
        ch6.pack(side="left")
        ttk.Label(rada_asr, text=T("hint_asr_stazeno") if asr_stazeny()
                  else T("hint_asr_stahne", ASR_GB),
                  style="Tlumeny.TLabel").pack(side="left", padx=(14, 0))
        self._zamknout(ch6)

        # ---------------- Ovládání ----------------
        ovladani = ttk.Frame(hlavni)
        ovladani.pack(fill="x", pady=(6, 0))

        self.btn_start = ttk.Button(ovladani, text=T("btn_start"),
                                    command=self.spust_prevod, style="Akce.TButton")
        self.btn_start.pack(side="left")
        self.btn_navazat = ttk.Button(ovladani, text=T("btn_navazat"),
                                      command=self.pokracuj_v_rozdelanem,
                                      style="Tichy.TButton")
        self.btn_navazat.pack(side="left", padx=(10, 0))
        self.btn_pauza = ttk.Button(ovladani, text=T("btn_pauza"), command=self.prepni_pauzu,
                                    style="Tichy.TButton", state="disabled")
        self.btn_pauza.pack(side="left", padx=(10, 0))
        self.btn_stop = ttk.Button(ovladani, text=T("btn_zastavit"), command=self.zastav,
                                   style="Tichy.TButton", state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0))
        ttk.Button(ovladani, text=T("btn_otevrit"), command=self.otevri_vystup,
                   style="Tichy.TButton").pack(side="right")
        ttk.Button(ovladani, text=T("btn_oprava"), command=self.oprav_usek,
                   style="Tichy.TButton").pack(side="right", padx=(0, 10))

        # ---------------- Průběh ----------------
        postup = ttk.Frame(hlavni)
        postup.pack(fill="x", pady=(16, 0))
        postup.columnconfigure(0, weight=1)

        ttk.Progressbar(postup, variable=self.var_postup, maximum=100.0,
                        style="Tenky.Horizontal.TProgressbar").grid(
            row=0, column=0, columnspan=3, sticky="ew")
        # Hranice kapitol. Na ttk.Progressbar se kreslit nedá, takže rysky jsou
        # na vlastním plátně hned pod ním - šířka i měřítko sedí.
        self.platno_kapitol = tk.Canvas(postup, height=6, highlightthickness=0,
                                        background=BARVY["pozadi"], borderwidth=0)
        self.platno_kapitol.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self.platno_kapitol.bind("<Configure>", lambda _u: self._vykresli_rysky_kapitol())
        # Druhý pruh sleduje jen právě převáděnou kapitolu
        self.pruh_kapitoly = ttk.Progressbar(postup, variable=self.var_postup_kapitola, maximum=100.0,
                                             style="Tenky.Horizontal.TProgressbar")
        self.pruh_kapitoly.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(postup, textvariable=self.var_stav).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(postup, textvariable=self.var_bloky_info, style="Tlumeny.TLabel").grid(
            row=3, column=1, sticky="e", padx=(16, 16), pady=(8, 0))
        ttk.Label(postup, textvariable=self.var_cas_info, style="Tlumeny.TLabel").grid(
            row=3, column=2, sticky="e", pady=(8, 0))
        self.popisek_kapitoly = ttk.Label(postup, textvariable=self.var_kapitola_info,
                                          style="Tlumeny.TLabel")
        self.popisek_kapitoly.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._aktualizuj_pruh_kapitol()

        # ---------------- Log ----------------
        ramec_log = self._sekce_sbalitelna(hlavni, T("sekce_prubeh"), sbaleno=False,
                                           roztahnout=True, mezera_nahore=20, klic="prubeh")
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
                          roztahnout: bool = False, mezera_nahore: int = 0,
                          klic: str = "") -> ttk.Frame:
        """Sekce, kterou lze kliknutím na nadpis sbalit. Drží pokročilá nastavení z cesty.

        Se zadaným 'klic' si stav pamatuje v konfiguraci - kdo si okno jednou
        zkrátí, nemusí to dělat po každém spuštění znovu.
        """
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

        stav = {"sbaleno": bool(self.sbalene_sekce.get(klic, sbaleno)) if klic else sbaleno}

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
            if klic:
                self.sbalene_sekce[klic] = stav["sbaleno"]

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
            self.btn_navazat.config(state="disabled")
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

    # ------------------------------------------------------------------
    #  Kapitoly v ukazateli průběhu
    # ------------------------------------------------------------------
    def _aktualizuj_pruh_kapitol(self):
        """Pruh, rysky a popisek kapitol ukáže jen u knihy, která kapitoly má."""
        ma = len(self.kapitoly) > 1
        for w in (self.platno_kapitol, self.pruh_kapitoly, self.popisek_kapitoly):
            if ma:
                w.grid()
            else:
                w.grid_remove()
        if not ma:
            self.var_postup_kapitola.set(0.0)
            self.var_kapitola_info.set("")
        self._vykresli_rysky_kapitol()

    def _konec_kapitoly(self, kap_i: int) -> int:
        """Kolik bloků knihy je hotovo, když skončí kapitola kap_i."""
        if kap_i + 1 < len(self.kapitoly):
            return self.kapitoly[kap_i + 1]["prvni_blok"]
        return len(self.bloky)

    def _vykresli_rysky_kapitol(self):
        """Svislé rysky na hranicích kapitol, právě převáděná kapitola podtržená."""
        platno = self.platno_kapitol
        platno.delete("all")
        if len(self.kapitoly) <= 1 or not self.bloky:
            return
        sirka = max(platno.winfo_width(), 1)
        vyska = max(platno.winfo_height(), 1)
        celkem = float(len(self.bloky))
        for i, kap in enumerate(self.kapitoly):
            x = min(sirka - 1.0, kap["prvni_blok"] / celkem * sirka)
            if i == self._kapitola_v_behu:
                x2 = min(float(sirka), self._konec_kapitoly(i) / celkem * sirka)
                platno.create_rectangle(x, vyska - 3, max(x + 1.0, x2), vyska,
                                        fill=BARVY["akcent"], outline="")
            platno.create_line(x, 0, x, vyska, fill=BARVY["linka"])

    def _postup_kapitoly(self, hotovo: int, kap_i: int):
        """Druhý pruh a jeho popisek podle pozice uvnitř převáděné kapitoly."""
        if len(self.kapitoly) <= 1 or not 0 <= kap_i < len(self.kapitoly):
            return
        prvni = self.kapitoly[kap_i]["prvni_blok"]
        bloku = self._konec_kapitoly(kap_i) - prvni
        v_kapitole = hotovo - prvni
        self.var_postup_kapitola.set(100.0 * v_kapitole / bloku if bloku else 0.0)
        self.var_kapitola_info.set(T("prubeh_kapitola", kap_i + 1, len(self.kapitoly),
                                     v_kapitole, bloku))
        if kap_i != self._kapitola_v_behu:
            self._kapitola_v_behu = kap_i
            self._vykresli_rysky_kapitol()

    def _zpracuj_frontu(self):
        try:
            while True:
                typ, data = self.fronta.get_nowait()
                if typ == "log":
                    self.log(data)
                elif typ == "postup":
                    hotovo, celkem, uplynulo, zbyva, kap_i = data
                    self.var_postup.set(100.0 * hotovo / celkem if celkem else 0.0)
                    self.var_bloky_info.set(T("prubeh_bloky", hotovo, celkem))
                    self.var_cas_info.set(
                        T("prubeh_cas", formatuj_cas(uplynulo), formatuj_cas(zbyva)))
                    self._postup_kapitoly(hotovo, kap_i)
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

    def oprav_usek(self):
        if self.bezi:
            messagebox.showinfo(T("dlg_probiha"), T("dlg_pockejte"))
            return
        slozka = Path(self.var_vystup_slozka.get().strip('" ') or (APP_DIR / "vystup"))
        nazev = re.sub(ZAKAZANE_ZNAKY, "_", self.var_vystup_nazev.get().strip()
                       or self.nazev_knihy or "audiokniha")
        kniha = slozka / nazev
        self.wait_window(DialogOprava(self, kniha if kniha.is_dir() else slozka))

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
            nahrazeno = z_wiki = 0
            jazyk = self._kod_jazyka_textu()
            for i, kap in enumerate(kapitoly):
                text = normalizuj_text(kap["text"])
                text, kolik, kolik_wiki = uprav_vyslovnost(text, jazyk)
                nahrazeno += kolik
                z_wiki += kolik_wiki
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
            self._kapitola_v_behu = -1
            self.var_postup_kapitola.set(0.0)
            self.var_kapitola_info.set("")
            self._aktualizuj_pruh_kapitol()

            self.nazev_knihy = cesta.stem
            znaku = sum(len(b) for _, b in self.bloky)
            # Zhruba 14 znaků za sekundu mluveného českého textu
            odhad_audio = znaku / 14.0 + len(self.bloky) * (self.var_pauza.get() / 1000.0)

            self.var_soubor_info.set(T("info_soubor", cesta.name, f"{znaku:,}".replace(",", " "),
                                       len(self.bloky), formatuj_cas(odhad_audio)))
            self.log(T("log_nacteno", znaku, len(self.bloky), max_znaku))
            if self.ma_kapitoly:
                self.log(T("log_kapitoly", len(self.kapitoly)))
            if nahrazeno:
                self.log(T("log_vyslovnost", nahrazeno, len(VYSLOVNOST)))
            if z_wiki:
                self.log(T("log_vyslovnost_cs", z_wiki))
            self.log(T("log_ukazka_bloku", self.bloky[0][1][:120]))
        except Exception as chyba:
            self.bloky = []
            self.kapitoly = []
            self.ma_kapitoly = False
            self._aktualizuj_pruh_kapitol()
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
            self.engine.nacti_model(p["zarizeni"], p["jazyk_textu"], p.get("rychly_dekoder", False))
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
            "orezat_okraje": bool(self.var_orez.get()),
            "rychly_dekoder": bool(self.var_rychly_dekoder.get()),
            "kontrola_asr": bool(self.var_kontrola_asr.get()),
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

    # Popisky nastavení pro hlášky o tom, co se od přerušení změnilo
    NAZVY_NASTAVENI = {
        "jazyk_textu": "lab_jazyk_textu", "referencni_wav": "nazev_hlas",
        "exaggeration": "lab_expresivita", "cfg_weight": "lab_cfg", "temperature": "lab_teplota",
        "min_p": "lab_min_p", "seed": "lab_seed", "pauza_ms": "lab_pauza_ms",
        "format": "nazev_format", "bitrate": "nazev_bitrate", "max_znaku": "lab_znaku",
        "odstranit_lupance": "lab_lupance", "orezat_okraje": "lab_orez",
        "rychly_dekoder": "lab_rychly_dekoder", "kontrola_asr": "lab_kontrola_asr",
        "slovnik": "nazev_slovnik",
    }

    def _nazev_nastaveni(self, klic: str) -> str:
        return T(self.NAZVY_NASTAVENI.get(klic, klic))

    def pokracuj_v_rozdelanem(self):
        """Nabídne seznam přerušených převodů a v tom vybraném pokračuje."""
        if self.bezi:
            return

        slozky = [Path(self.var_vystup_slozka.get().strip('" ') or (APP_DIR / "vystup")),
                  APP_DIR / "vystup"]
        zaznamy = najdi_rozdelane(slozky)
        if not zaznamy:
            messagebox.showinfo(T("dlg_zadne_rozdelane"), T("dlg_zadne_rozdelane_text"))
            return

        dialog = DialogRozdelane(self, zaznamy, self.font_rodina)
        self.wait_window(dialog)
        vybrany = dialog.vybrany
        if vybrany is None:
            return

        zdroj = vybrany.get("zdroj") or ""
        if not zdroj or not Path(zdroj).exists():
            messagebox.showerror(T("dlg_chyba"), T("dlg_zdroj_pryc", zdroj or "?"))
            return

        # Obnovit nastavení z doby přerušení - jinak by otisk nesouhlasil
        # a navázat by nešlo.
        obnoveno = self._obnov_nastaveni(vybrany.get("parametry") or {})
        self.var_vstup.set(zdroj)
        self.var_vystup_slozka.set(str(vybrany["cesta"].parent))
        self.var_vystup_nazev.set(vybrany["nazev"])
        if obnoveno:
            self.log(T("log_obnoveno", ", ".join(obnoveno)))

        self.nacti_a_priprav()
        if not self.bloky:
            return
        self.spust_prevod(automaticky_navazat=True)

    def _obnov_nastaveni(self, parametry: dict) -> list:
        """Vrátí do polí hodnoty, se kterými se generovalo. Vypíše, co se změnilo."""
        mapovani = [
            ("referencni_wav", self.var_ref_wav, str),
            ("exaggeration", self.var_exag, float),
            ("cfg_weight", self.var_cfg, float),
            ("temperature", self.var_temp, float),
            ("min_p", self.var_min_p, float),
            ("seed", self.var_seed, int),
            ("pauza_ms", self.var_pauza, int),
            ("max_znaku", self.var_max_znaku, int),
            ("format", self.var_format, str),
            ("bitrate", self.var_bitrate, str),
            ("odstranit_lupance", self.var_lupance, bool),
            ("orezat_okraje", self.var_orez, bool),
            ("obalka", self.var_obalka, bool),
            ("rychly_dekoder", self.var_rychly_dekoder, bool),
            ("kontrola_asr", self.var_kontrola_asr, bool),
        ]
        # Kniha uložená dřív, než rychlý dekodér existoval, vznikla bez něj
        parametry = {"rychly_dekoder": False, **parametry}
        zmeneno = []
        for klic, promenna, typ in mapovani:
            if klic not in parametry:
                continue
            try:
                nova = typ(parametry[klic])
                if promenna.get() != nova:
                    promenna.set(nova)
                    zmeneno.append(klic)
            except Exception:
                continue

        klic_jazyka = parametry.get("jazyk_textu")
        if klic_jazyka:
            nazev = self._nazev_jazyka_textu(klic_jazyka)
            if self.var_jazyk_textu.get() != nazev:
                self.var_jazyk_textu.set(nazev)
                zmeneno.append("jazyk_textu")
            self._popis_jazyka()

        self._aktualizuj_popisky_posuvniku()
        return zmeneno


    def spust_prevod(self, automaticky_navazat: bool = False):
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
        cesta_knihy = Path(self.var_vstup.get().strip('" '))
        texty = [b for _, b in self.bloky]
        kod = (parametry.get("jazyk_textu") or "").split("|")[0]
        parametry["otisk_hlasu"] = otisk_hlasu(cesta_knihy, parametry, texty)
        parametry["otisk_verze_1"] = otisk_verze_1(cesta_knihy, parametry, len(texty))
        parametry["opravy"] = {**{k: parametry.get(k) for k in NASTAVENI_OPRAV},
                               "slovnik": otisk_vyslovnosti(kod)}
        parametry["zdroj"] = self.var_vstup.get().strip('" ')
        parametry["ulozitelne"] = {k: v for k, v in parametry.items()
                                   if k not in ("otisk_hlasu", "otisk_verze_1", "opravy", "ulozitelne")}
        parametry["ulozitelne"]["max_znaku"] = int(self.var_max_znaku.get())

        # --- navázat na přerušený běh? ---
        postup = Postup.nacti(slozka / (nazev + ".progress.json"))
        od_bloku = 0
        prepsani_potvrzeno = False
        jde, zmeny = posud_navazani(postup.data, parametry)
        if jde and 0 < postup.hotovo_bloku < len(self.bloky):
            odpoved = True if automaticky_navazat else messagebox.askyesnocancel(
                T("dlg_navazat"),
                T("dlg_navazat_text", postup.hotovo_bloku, len(self.bloky),
                  100.0 * postup.hotovo_bloku / len(self.bloky)))
            if odpoved is None:
                return                       # Zrušit
            if odpoved:
                od_bloku = postup.hotovo_bloku
                if zmeny:
                    self.log(T("log_navazuji_zmeny", ", ".join(self._nazev_nastaveni(k) for k in zmeny)))
            else:
                postup.smaz()                # začít znovu od začátku
                postup = Postup.nacti(postup.cesta)
        elif postup.data and not jde:
            # Navázat nejde. Dřív se stav tiše smazal a hotové kapitoly se
            # postupně přepsaly - teď o tom rozhodne uživatel.
            duvod = (", ".join(self._nazev_nastaveni(k) for k in zmeny) if zmeny
                     else T("duvod_neznamy"))
            if not messagebox.askyesno(T("dlg_nelze_navazat"),
                                       T("dlg_nelze_navazat_text", postup.hotovo_bloku,
                                         postup.data.get("celkem_bloku") or len(self.bloky), duvod)):
                return
            self.log(T("log_postup_neplatny"))
            postup.smaz()
            postup = Postup.nacti(postup.cesta)
            prepsani_potvrzeno = True

        # Přepsat existující výstup? Ptáme se jen když nenavazujeme.
        if od_bloku == 0 and not prepsani_potvrzeno:
            hotovy = [zaklad.with_suffix(".wav"), zaklad.with_suffix(".mp3")]
            existujici = [c for c in hotovy if c.exists()]
            # U knihy po kapitolách je výstupem složka - bez téhle kontroly
            # by se hotové kapitoly přepsaly bez ptaní
            if zaklad.is_dir() and any(f.suffix.lower() in (".mp3", ".wav") for f in zaklad.iterdir()):
                existujici.append(zaklad)
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
        self.btn_navazat.config(state="disabled")
        self.btn_test.config(state="disabled")
        self.btn_pauza.config(state="normal", text=T("btn_pauza"))
        self.btn_stop.config(state="normal")
        self._zamkni_ovladani(True)      # formát ani cesty už za běhu neměnit
        self.var_postup.set(100.0 * od_bloku / len(self.bloky) if od_bloku else 0.0)
        self.var_postup_kapitola.set(0.0)
        self._kapitola_v_behu = -1
        self._vykresli_rysky_kapitol()
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
                if p.get("rychly_dekoder"):
                    # Stáhnout teď, jinak by si ho při prvním použití
                    # stahoval každý pracovník zvlášť
                    self.engine.stahni_rychly_dekoder()
                if p.get("kontrola_asr") and not asr_stazeny():
                    self.log_z_vlakna(T("log_asr_stahuji", ASR_GB))
                    from huggingface_hub import snapshot_download
                    snapshot_download(ASR_REPO)
                pool = Pool(pocet, {"zarizeni": p["zarizeni"], "jazyk_textu": p["jazyk_textu"],
                                    "rychly_dekoder": bool(p.get("rychly_dekoder"))},
                            self.log_z_vlakna)
                if not pool.pockej_na_start():
                    self.log_z_vlakna(T("log_pool_selhal", pool.chyba or "?"))
                    pool.ukonci()
                    pool = None
                else:
                    self.log_z_vlakna(T("log_pool_pripraven", pocet))

            if pool is None:
                self.log_z_vlakna(T("log_pool_jeden"))
                self.engine.nacti_model(p["zarizeni"], p["jazyk_textu"], p.get("rychly_dekoder", False))
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

            # Kde který blok leží - podle toho jde později opravit jeden úsek
            mapa = MapaBloku(slozka / (zaklad.stem + MapaBloku.PRIPONA))
            if not od_bloku:
                mapa.smaz()
            mapa.hlavicka(p["ulozitelne"], sr, zaklad.stem)

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
            # Úseky, které se nepodařilo vygenerovat - přežijí i přerušení
            preskocene = postup.preskocene if (postup is not None and od_bloku) else []

            def cesta_kapitoly(kap_i):
                return slozka / (nazev_souboru_kapitoly(kap_i, self.kapitoly[kap_i].get("nazev")) + ".wav")

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

            for index, kap_i, blok, (vzorky, vynechano) in self._proud_bloku(bloky, p, od_bloku, pool):
                # nová kapitola = nový soubor
                if self._zapisovac is None or (po_kapitolach and kap_i != aktualni_kap):
                    if po_kapitolach and self._zapisovac is not None:
                        uzavri_a_preved(aktualni_kap)
                    aktualni_kap = kap_i
                    cil = cesta_kapitoly(kap_i) if po_kapitolach else jediny_wav
                    self._zapisovac = WavZapisovacRaw(cil, sr)

                for text in vynechano:
                    preskocene.append({"blok": index, "kapitola": kap_i + 1, "text": text})
                if vzorky is None:
                    neuspesne += 1
                else:
                    mapa.pridej(index, self._zapisovac.cesta.stem, self._zapisovac.pocet_vzorku,
                                len(vzorky), blok)
                    self._zapisovac.zapis(vzorky)
                    self._zapisovac.zapis_ticho(p["pauza_ms"])
                    if self.prehravac is not None and self.prehravac.bezi:
                        self.prehravac.pridej(vzorky, p["pauza_ms"])

                if postup is not None:
                    postup.uloz(p, index, celkem, hotove, str(self._zapisovac.cesta),
                                self._zapisovac.pocet_vzorku, aktualni_kap,
                                preskocene=preskocene, nazev=zaklad.stem)

                znaku_hotovo += len(blok)
                uplynulo = time.time() - start
                rychlost = (znaku_hotovo - znaku_pred) / uplynulo if uplynulo > 0 else 0
                zbyva = (znaku_celkem - znaku_hotovo) / rychlost if rychlost > 0 else -1
                self.fronta.put(("postup", (index, celkem, uplynulo, zbyva, kap_i)))

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
                    postup.uloz(p, postup.hotovo_bloku, celkem, hotove,
                                postup.data.get("aktualni_wav", ""),
                                postup.data.get("vzorku_v_aktualnim", 0),
                                postup.data.get("kapitola", 0), preskocene=preskocene)
                else:
                    postup.smaz()      # doběhlo celé, není na co navazovat

            self.log_z_vlakna((T("log_zastaveno_ul") if zastaveno else T("log_hotovo")) + souhrn
                              + (T("log_neuspesne", neuspesne) if neuspesne else ""))
            if preskocene:
                self.log_z_vlakna(T("log_chybejici", len(preskocene),
                                    self._zapis_chybejici(zaklad, preskocene)))
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
        """Vydává (index, kapitola, text, (vzorky, vynechané úseky)) v původním pořadí.

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
            index, data = vysledek
            pool.potvrd()
            hotovo = index
            yield index, bloky[index - 1][0], bloky[index - 1][1], data

    def _zapis_chybejici(self, zaklad: Path, preskocene: list) -> Path:
        """Seznam úseků, které se nepodařilo vygenerovat, ať je jde v knize najít."""
        cesta = zaklad.parent / (zaklad.stem + T("soubor_chybejici"))
        radky = [T("chybejici_hlavicka"), ""]
        radky += [T("chybejici_radek", u["blok"], u["kapitola"], u["text"]) for u in preskocene]
        cesta.write_text("\n".join(radky) + "\n", encoding="utf-8")
        return cesta

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
        self.btn_navazat.config(state="normal")
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
            self.var_postup_kapitola.set(100.0)

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
