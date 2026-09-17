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
import platform
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
VERSION = "1.14.0"

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


def otevri_v_systemu(cesta):
    """Otevře soubor nebo složku tím, čím je systém otevírá.

    os.startfile() je jen na Windows - na macOS a Linuxu ta funkce vůbec
    neexistuje, takže volání spadne na AttributeError. Jinde se to musí
    předat systémovému spouštěči: 'open' na macOS, 'xdg-open' na Linuxu.

    Spouštěč se nečeká - 'open' se vrátí hned, ale některé implementace
    'xdg-open' drží proces, dokud aplikace neskončí. Když spouštěč není
    v PATH, vyhodíme výjimku, ať to volající umí ohlásit; tichý neúspěch
    by vypadal jako že se nestalo nic.
    """
    cesta = str(cesta)
    if sys.platform == "win32":
        os.startfile(cesta)
        return

    spoustec = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(spoustec) is None:
        raise RuntimeError(T("err_spoustec", spoustec))
    subprocess.Popen([spoustec, cesta],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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

# T3 v MLX - jen na Apple Siliconu. Vypnuto znamená torchovou cestu jako dosud;
# ostatní hodnoty jsou kvantizace backbonu. Měřeno na M4, 20 běhů po ~200
# řečových tokenech: float32 58 tok/s a 2,14 GB parametrů, 8 bitů 158 tok/s
# a 0,70 GB, 4 bity 0,45 GB, ale rychlejší už ne - smyčka je vázaná režií,
# ne pamětí, takže 4 bity jen ubírají na kvalitě (KL 3e-2 proti 8e-5 u osmi).
# Torchová cesta na MPS zvládá 27 tok/s a s každým během zpomaluje.
MLX_VYPNUTO = "off"
MLX_PRESNOSTI = {MLX_VYPNUTO: None, "8-bit": 8, "4-bit": 4, "float32": None}
MLX_VOLBY = (MLX_VYPNUTO, "8-bit", "4-bit", "float32")
# Základní T3 pro jazyky bez fine-tunu - MLX si checkpoint načítá sám.
MLX_ZAKLADNI_T3 = "t3_mtl23ls_v2.safetensors"
# MLX si drží vyrovnávací paměť bufferů podle velikosti. Bloky mají různou
# délku, takže bez stropu cache roste s nejdelším blokem a už se nevrátí.
MLX_STROP_CACHE_MB = 512


def mlx_mozny() -> bool:
    """Může tady MLX vůbec běžet? Jen platforma, nic se neimportuje."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


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
    # T3 v MLX - uplatní se jen na Apple Siliconu, jinde se ignoruje
    "mlx_presnost": MLX_VYPNUTO,
    # Podrobný průběh je sbalený - okno ukazuje převod a poslech, ne log
    "sbalene_sekce": {"log": True},
    # Barevné schéma: "tmave" nebo "svetle"
    "tema": "tmave",
    "seed": 0,
    # Jazyk syntézy je nezávislý na jazyku rozhraní - v českém rozhraní
    # klidně vyrábíte anglickou audioknihu.
    "jazyk_textu": "en",
    "zarizeni": "auto",
    "testovaci_veta": "Dobrý den, toto je ukázka českého hlasu pro vaši audioknihu.",
    "obalka": True,
    # 0 = odvodit od volné paměti karty
    "pracovniku": 0,
    "jazyk": "en",
}


# --------------------------------------------------------------------------
#  Vzhled - tmavá minimalistická paleta
# --------------------------------------------------------------------------

# Dvě schémata podle návrhu. BARVY je jeden sdílený slovník - dialogy i log
# si z něj berou barvy při stavbě. Přepnutí schématu ho přepíše a okno se
# postaví znovu, stejně jako při změně jazyka.
PALETY = {
    "tmave": {
        "pozadi": "#0e0e11", "panel": "#16161a", "pole": "#1a1a1f",
        "tlacitko": "#1e1e25", "tlacitko_aktivni": "#2a2a33", "linka": "#22222a",
        "text": "#f4f4f8", "text2": "#d2d2da", "tlumeny": "#a6a6b2",
        "stopa": "#23232b", "stopa2": "#33333f", "akcent": "#7aa2f7",
        "plocha": "#7aa2f7", "plocha_aktivni": "#93b4ff", "na_plose": "#12151c",
        "zasoba": "#3f5a91", "chip": "#1b2334", "aktivni_radek": "#1a1d24",
        "uspech": "#7ee787", "varovani": "#d9822b", "chyba": "#f76f6f", "cas_logu": "#5c5c68",
    },
    "svetle": {
        "pozadi": "#eceef1", "panel": "#ffffff", "pole": "#eceef2",
        "tlacitko": "#e6e8ec", "tlacitko_aktivni": "#d9dce2", "linka": "#e0e2e6",
        "text": "#16181d", "text2": "#454951", "tlumeny": "#676c76",
        "stopa": "#dde0e5", "stopa2": "#b9bec7", "akcent": "#5a6fd8",
        # Velké plochy nesou ve světlém schématu inkoust, ne akcent - sytá
        # barva na papíře křičí. Akcent zůstává jen v drobnostech.
        "plocha": "#1c1e24", "plocha_aktivni": "#3a3d46", "na_plose": "#ffffff",
        "zasoba": "#b9bec7", "chip": "#e6ecf6", "aktivni_radek": "#eef2f8",
        "uspech": "#2e7d46", "varovani": "#b8641a", "chyba": "#c73a3a", "cas_logu": "#a2a6ae",
    },
}
BARVY = dict(PALETY["tmave"])


def nastav_paletu(tema: str) -> str:
    """Přepne sdílené BARVY na zvolené schéma. Vrací platný název schématu."""
    tema = tema if tema in PALETY else "tmave"
    BARVY.clear()
    BARVY.update(PALETY[tema])
    return tema

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


def nacti_metadata(cesta: Path) -> dict:
    """Název a autor z e-knihy. Co formát nenese, zůstane prázdné."""
    vysledek = {"titul": "", "autor": ""}
    pripona = cesta.suffix.lower()
    if pripona == ".epub":
        from ebooklib import epub

        kniha = epub.read_epub(str(cesta))
        for klic, pole in (("titul", "title"), ("autor", "creator")):
            hodnoty = kniha.get_metadata("DC", pole)
            vysledek[klic] = (hodnoty[0][0] or "").strip() if hodnoty else ""
    elif pripona == ".fb2":
        from bs4 import BeautifulSoup

        obsah = cesta.read_text(encoding=detekuj_kodovani(cesta), errors="replace")
        info = BeautifulSoup(obsah, "lxml-xml").find("title-info")
        if info is not None:
            titul = info.find("book-title")
            vysledek["titul"] = titul.get_text(" ", strip=True) if titul else ""
            autor = info.find("author")
            if autor is not None:
                vysledek["autor"] = " ".join(
                    x.get_text(strip=True) for x in autor.find_all(["first-name", "middle-name", "last-name"])
                    if x.get_text(strip=True))
    return vysledek


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


def priprav_bloky(kapitoly, jazyk: str, max_znaku: int):
    """Kapitoly -> (kapitoly s prvním blokem, bloky, bloky před slovníčkem, ručních náhrad, z Wikislovníku).

    Text se dělí na bloky ještě před slovníčkem výslovnosti a ten se použije
    až na hotové bloky. Hranice bloků i délky v otisku tak na slovníčku
    nezávisí: pravidlo "dacan" -> "datsan" prodloužilo slovo o znak
    a rozdělaného Ovidia už nešlo navázat.
    """
    info, bloky, puvodni = [], [], []
    nahrazeno = z_wiki = 0
    for kap in kapitoly:
        casti = rozdel_na_bloky(normalizuj_text(kap["text"]), max_znaku)
        if not casti:
            continue
        info.append({"nazev": kap.get("nazev") or "", "prvni_blok": len(bloky)})
        for cast in casti:
            text, kolik, kolik_wiki = uprav_vyslovnost(cast, jazyk)
            nahrazeno += kolik
            z_wiki += kolik_wiki
            bloky.append((len(info) - 1, text))
            puvodni.append(cast)
    return info, bloky, puvodni, nahrazeno, z_wiki


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
NASTAVENI_OPRAV = ("odstranit_lupance", "orezat_okraje", "rychly_dekoder", "kontrola_asr",
                   "mlx_presnost")


def otisk_hlasu(cesta_knihy: Path, p: dict, bloky) -> str:
    """Otisk zdroje, nastavení hlasu a rozdělení textu na bloky.

    Délky bloků jsou v otisku, aby navázání nikdy nesedlo na posunuté hranice -
    kus textu by se jinak přeskočil nebo zopakoval. Bloky se sem dávají
    v podobě před slovníčkem výslovnosti (priprav_bloky), takže změna
    slovníčku navázání nezablokuje, ani když pravidlo slovo prodlouží.
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

    'zadani' nese otisk_hlasu, otisk_verze_1, opravy a ulozitelne parametry,
    volitelně i otisk_po_slovnicku - otisk z bloků po slovníčku výslovnosti,
    jak ho ukládaly dřívější verze. Když navázat jde, seznam obsahuje opravy
    změněné od přerušení. Když ne, obsahuje změněná nastavení hlasu - prázdný
    seznam pak znamená, že se změnil text knihy, jeho rozdělení na bloky nebo
    soubor s nahrávkou hlasu.
    """
    if not data:
        return False, []
    verze = data.get("verze")
    if verze == 2:
        ulozeny = data.get("otisk_hlasu")
        jde = bool(ulozeny) and ulozeny in (zadani["otisk_hlasu"], zadani.get("otisk_po_slovnicku"))
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


OKNO_OBALKY_S = 0.1          # rozlišení obálky zvuku, ze které se kreslí vlna


def precti_pcm(cesta: Path, od: int = 0, do: int = None):
    """Mono 16bit PCM z WAV, který zapsala aplikace - i z rozepsaného.

    Rozepsaný soubor má v hlavičce nulovou délku, proto se čte přímo za
    kanonickou 44bajtovou hlavičkou, kterou píšou oba zapisovače.
    """
    import numpy as np

    vzorku = max(0, (Path(cesta).stat().st_size - WavZapisovacRaw.HLAVICKA) // 2)
    do = vzorku if do is None else min(int(do), vzorku)
    od = max(0, int(od))
    if do <= od:
        return np.zeros(0, dtype="<i2")
    with open(cesta, "rb") as f:
        f.seek(WavZapisovacRaw.HLAVICKA + od * 2)
        return np.frombuffer(f.read((do - od) * 2), dtype="<i2").copy()


def delka_souboru_vzorku(cesta: Path, sr: int) -> int:
    """Délka zvukového souboru ve vzorcích - WAV z hlavičky, MP3 přes ffprobe."""
    cesta = Path(cesta)
    if cesta.suffix.lower() == ".wav":
        return max(0, (cesta.stat().st_size - WavZapisovacRaw.HLAVICKA) // 2)
    vysledek = _spust([_ffprobe(), "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", str(cesta)])
    if vysledek.returncode != 0:
        raise RuntimeError(vysledek.stderr.decode("utf-8", "replace").strip()[:300])
    return int(float(vysledek.stdout.decode().strip()) * sr)


def rms_oken(pcm, okno: int):
    """RMS po oknech pevné délky. Neúplné poslední okno se vynechá."""
    import numpy as np

    pocet = len(pcm) // okno
    if pocet <= 0:
        return np.zeros(0, dtype="float32")
    d = pcm[:pocet * okno].astype("float32").reshape(pocet, okno) / 32768.0
    return np.sqrt((d * d).mean(axis=1)).astype("float32")


def sloupce_vlny(obalka, okno: int, dostupno: int, osa: int, pocet: int):
    """Výška (0 až 1) a dostupnost každého z 'pocet' sloupců přes osu 'osa' vzorků.

    Osa může být delší než to, co už je vygenerované - zbytek kapitoly se
    pak kreslí jako nízká linka.
    """
    vysky, dostupne = [], []
    if pocet <= 0 or osa <= 0:
        return vysky, dostupne
    krok = osa / float(pocet)
    for j in range(pocet):
        od = int(j * krok)
        if od >= dostupno or not len(obalka):
            vysky.append(0.0)
            dostupne.append(od < dostupno)
            continue
        do = min(int((j + 1) * krok), dostupno)
        a = min(od // okno, len(obalka) - 1)
        b = max(a + 1, min(len(obalka), -(-do // okno)))
        # Odmocnina, protože RMS řeči se drží nízko a vlna by byla plochá
        vysky.append(min(1.0, (float(obalka[a:b].max()) / 0.25) ** 0.5))
        dostupne.append(True)
    return vysky, dostupne


def lidsky_cas(sekundy) -> str:
    """'45 s', '19 min', '9 h 45 min' - pro souhrny, kde nejde o sekundy."""
    if sekundy is None or sekundy != sekundy or sekundy < 0:
        return "—"
    s = int(round(sekundy))
    if s < 60:
        return f"{s} s"
    minut = s // 60
    if minut < 60:
        return f"{minut} min"
    return f"{minut // 60} h {minut % 60} min"


def kratky_cas(sekundy) -> str:
    """'24:49' nebo '1:02:03'."""
    s = max(0, int(sekundy or 0))
    h, zbytek = divmod(s, 3600)
    m, s = divmod(zbytek, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Prehravac:
    """Přehrává knihu po kapitolách přímo ze souborů výstupu.

    Kapitola, která se hraje, se drží v paměti: hotová se jednou načte nebo
    dekóduje, ta, která právě vzniká, se dočítá z rozepsaného WAV, jak
    přibývají bloky. Proto jde skočit kamkoli do už vygenerovaného zvuku a
    poslech nepotřebuje náskok - když dojede na živý konec, počká.

    Kapitoly popisuje seznam slovníků: cesta, od a do (vzorky v souboru -
    kniha bez rozpadu má všechny kapitoly v jednom souboru), hotova, roste
    (právě se generuje) a odhad_s (délka podle textu, dokud není hotová).
    """

    KROK_S = 0.05

    def __init__(self, sr: int, log_fn):
        import numpy as np

        self.sr = int(sr)
        self.log = log_fn
        self._zamek = threading.Lock()
        self._probud = threading.Event()
        self._konec = threading.Event()
        self.kapitoly = []
        self.hrana = -1
        self._data = np.zeros(0, dtype="<i2")
        self._obalka = np.zeros(0, dtype="float32")
        self._cesta_dat = None
        self.pozice = 0
        self.hraje = False
        self.nacita = False
        self.chyba = None
        self._pozadavek = None          # (kapitola, pozice ve vzorcích, hrát, načíst znovu)
        self._vlakno = threading.Thread(target=self._smycka, daemon=True)
        self._vlakno.start()

    @staticmethod
    def dostupny() -> bool:
        try:
            import sounddevice  # noqa: F401
            return True
        except Exception:
            return False

    @property
    def okno(self) -> int:
        return max(1, int(self.sr * OKNO_OBALKY_S))

    @staticmethod
    def _ma_zvuk(k) -> bool:
        return bool(k["cesta"]) and (k["hotova"] or k["do"] > k["od"])

    # ------------------------------------------------------------------
    #  Stav kapitol - volá GUI vlákno
    # ------------------------------------------------------------------
    def nastav_kapitoly(self, odhady_s):
        import numpy as np

        with self._zamek:
            self.kapitoly = [{"cesta": None, "od": 0, "do": 0, "hotova": False, "roste": False,
                              "odhad_s": float(o)} for o in odhady_s]
            self.hrana, self._cesta_dat, self.pozice, self.hraje = -1, None, 0, False
            self._data = np.zeros(0, dtype="<i2")
            self._obalka = np.zeros(0, dtype="float32")
            self._pozadavek = None
        self._probud.set()

    def aktualizuj(self, kap_i: int, **zmeny):
        with self._zamek:
            if not 0 <= kap_i < len(self.kapitoly):
                return
            if zmeny.get("roste"):
                for k in self.kapitoly:
                    k["roste"] = False
            self.kapitoly[kap_i].update(zmeny)
        self._probud.set()

    def prejmenuj(self, stara: str, nova: str):
        """Hotová kapitola se převedla z WAV na MP3."""
        with self._zamek:
            for k in self.kapitoly:
                if k["cesta"] and Path(k["cesta"]) == Path(stara):
                    k["cesta"] = str(nova)
        self._probud.set()

    def ukonci_rust(self):
        with self._zamek:
            for k in self.kapitoly:
                k["roste"] = False
        self._probud.set()

    def zneplatni(self, cesta):
        """Soubor se změnil (oprava úseku) - hraná kapitola se načte znovu."""
        with self._zamek:
            for k in self.kapitoly:
                if k["cesta"] and Path(k["cesta"]) == Path(cesta) and k["od"] == 0:
                    k["do"] = 0          # doplní se po načtení
            if self._cesta_dat and Path(self._cesta_dat) == Path(cesta):
                self._pozadavek = (self.hrana, self.pozice, self.hraje, True)
        self._probud.set()

    def kapitola(self, kap_i: int):
        with self._zamek:
            return dict(self.kapitoly[kap_i]) if 0 <= kap_i < len(self.kapitoly) else None

    def podpis(self):
        """Stručný otisk stavu kapitol - GUI podle něj pozná, že má překreslit seznam."""
        with self._zamek:
            return tuple((k["cesta"], k["do"], k["hotova"], k["roste"]) for k in self.kapitoly)

    def stav(self) -> dict:
        with self._zamek:
            k = self.kapitoly[self.hrana] if 0 <= self.hrana < len(self.kapitoly) else None
            return {"kapitola": self.hrana if k else -1, "pozice_s": self.pozice / float(self.sr),
                    "dostupno": len(self._data), "osa": self._osa(k), "obalka": self._obalka,
                    "okno": self.okno, "hraje": self.hraje, "nacita": self.nacita,
                    "roste": bool(k and k["roste"]), "od": k["od"] if k else 0,
                    "cesta": k["cesta"] if k else None}

    def _osa(self, k) -> int:
        if k is None:
            return 0
        if k["hotova"] and not k["roste"]:
            return max(len(self._data), 1)
        return max(len(self._data), int(k["odhad_s"] * self.sr), 1)

    # ------------------------------------------------------------------
    #  Ovládání - volá GUI vlákno
    # ------------------------------------------------------------------
    def vyber(self, kap_i: int, sekundy: float = 0.0, hrat: bool = True):
        with self._zamek:
            self._pozadavek = (kap_i, int(sekundy * self.sr), hrat, False)
        self._probud.set()

    def prepni(self):
        with self._zamek:
            if self.hrana < 0:
                kandidati = ([i for i, k in enumerate(self.kapitoly) if k["roste"]]
                             + [i for i, k in enumerate(self.kapitoly) if self._ma_zvuk(k)])
                if kandidati:
                    self._pozadavek = (kandidati[0], 0, True, False)
            else:
                self.hraje = not self.hraje
        self._probud.set()

    def pozastav(self):
        with self._zamek:
            self.hraje = False
        self._probud.set()

    def posun(self, sekundy: float):
        with self._zamek:
            if self.hrana >= 0:
                self.pozice = max(0, min(len(self._data), self.pozice + int(sekundy * self.sr)))
        self._probud.set()

    def skoc(self, podil: float):
        """Skok na podíl osy kapitoly - jen do už vygenerovaného zvuku."""
        with self._zamek:
            if self.hrana >= 0:
                osa = self._osa(self.kapitoly[self.hrana])
                self.pozice = max(0, min(len(self._data), int(podil * osa)))
        self._probud.set()

    def dalsi(self, smer: int):
        with self._zamek:
            if self.hrana < 0:
                return
            i = self.hrana + smer
            while 0 <= i < len(self.kapitoly) and not self._ma_zvuk(self.kapitoly[i]):
                i += smer
            if 0 <= i < len(self.kapitoly):
                self._pozadavek = (i, 0, self.hraje, False)
        self._probud.set()

    def na_zive(self):
        with self._zamek:
            rostouci = [i for i, k in enumerate(self.kapitoly) if k["roste"]]
            if rostouci:
                self._pozadavek = (rostouci[0], -1, True, False)
        self._probud.set()

    def zastav(self):
        self._konec.set()
        self._probud.set()
        self._vlakno.join(timeout=2.0)

    # ------------------------------------------------------------------
    #  Vlákno přehrávače
    # ------------------------------------------------------------------
    def _smycka(self):
        try:
            import sounddevice as sd
        except Exception as chyba:
            sd = None
            self.chyba = str(chyba)
        proud = None
        krok = max(1, int(self.sr * self.KROK_S))
        while not self._konec.is_set():
            try:
                with self._zamek:
                    pozadavek, self._pozadavek = self._pozadavek, None
                if pozadavek is not None:
                    self._nacti(*pozadavek)
                self._dopln()

                with self._zamek:
                    hraje = self.hraje and sd is not None
                    kus = self._data[self.pozice:self.pozice + krok] if hraje else None
                    if kus is not None:
                        self.pozice += len(kus)
                if kus is not None and len(kus):
                    if proud is None:
                        proud = sd.OutputStream(samplerate=self.sr, channels=1, dtype="int16")
                        proud.start()
                    proud.write(kus.reshape(-1, 1))
                    continue

                if proud is not None:
                    proud.stop()
                    proud.close()
                    proud = None
                if hraje:
                    self._na_konci_kapitoly()
                self._probud.wait(0.1)
                self._probud.clear()
            except Exception as chyba:
                self.chyba = str(chyba)
                self.log(T("log_poslech_chyba", chyba))
                with self._zamek:
                    self.hraje = False
                if proud is not None:
                    proud.abort()
                    proud.close()
                    proud = None
                time.sleep(0.5)
        if proud is not None:
            proud.abort()
            proud.close()

    def _nacti(self, kap_i: int, pozice: int, hrat: bool, znovu: bool):
        with self._zamek:
            if not 0 <= kap_i < len(self.kapitoly):
                return
            k = dict(self.kapitoly[kap_i])
            sdileny = sum(1 for x in self.kapitoly if x["cesta"] == k["cesta"]) > 1
            stejna = kap_i == self.hrana and self._cesta_dat == k["cesta"] and not znovu
            if not k["cesta"]:
                return
            if not stejna:
                self.nacita = True
                self.hraje = False
        if not stejna:
            data = self._precti(k, sdileny)
            with self._zamek:
                self.hrana, self._data, self._cesta_dat = kap_i, data, k["cesta"]
                self._obalka = rms_oken(data, self.okno)
                if not sdileny and self.kapitoly[kap_i]["hotova"]:
                    self.kapitoly[kap_i]["do"] = self.kapitoly[kap_i]["od"] + len(data)
                self.nacita = False
        with self._zamek:
            if pozice < 0:
                pozice = len(self._data) - 2 * self.sr
            self.pozice = max(0, min(len(self._data), pozice))
            self.hraje = hrat

    def _precti(self, k: dict, sdileny: bool):
        cesta = Path(k["cesta"])
        od, do = int(k["od"]), int(k["do"])
        if cesta.suffix.lower() == ".wav":
            return precti_pcm(cesta, od, do if do > od else None)
        pcm = dekoduj_zvuk(cesta, self.sr)
        # MP3 kapitoly je celý soubor. Délka se od WAV liší o pár vzorků
        # vycpávky, proto se řeže jen tam, kde soubor sdílí víc kapitol.
        return pcm[od:do] if sdileny and do > od else pcm

    def _dopln(self):
        """Dočte bloky, které mezitím přibyly do rozepsané hrané kapitoly."""
        import numpy as np

        with self._zamek:
            if self.hrana < 0 or self._pozadavek is not None:
                return
            k = self.kapitoly[self.hrana]
            chybi = (k["do"] - k["od"]) - len(self._data)
            if k["cesta"] != self._cesta_dat:
                # Hotová kapitola se převedla na MP3. Když už je v paměti celá,
                # stačí si poznamenat nový soubor - zvuk je stejný.
                if chybi <= 0:
                    self._cesta_dat = k["cesta"]
                else:
                    self._pozadavek = (self.hrana, self.pozice, self.hraje, True)
                return
            if chybi <= 0 or Path(k["cesta"]).suffix.lower() != ".wav":
                return
            cesta, od = k["cesta"], k["od"] + len(self._data)
        try:
            nove = precti_pcm(Path(cesta), od, od + chybi)
        except FileNotFoundError:
            return                       # právě se převádí na MP3, přijde přejmenování
        if not len(nove):
            return
        with self._zamek:
            if self._cesta_dat != cesta:
                return
            self._data = np.concatenate([self._data, nove])
            self._obalka = rms_oken(self._data, self.okno)

    def _na_konci_kapitoly(self):
        with self._zamek:
            if self.hrana < 0 or self.pozice < len(self._data):
                return
            k = self.kapitoly[self.hrana]
            if k["roste"] or len(self._data) < k["do"] - k["od"]:
                return                   # čeká se na další blok
            dalsi = self.hrana + 1
            if dalsi < len(self.kapitoly):
                if self._ma_zvuk(self.kapitoly[dalsi]):
                    self._pozadavek = (dalsi, 0, True, False)
                    return
                if self.kapitoly[dalsi]["roste"]:
                    return               # další kapitola teprve začíná
            self.hraje = False


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


def uvolni_pamet_zarizeni(zarizeni: str = None):
    """Vrátí cache alokátoru zpátky systému.

    Na MPS to není kosmetika: alokátor si drží bloky podle největšího dosud
    viděného tvaru a mezi bloky knihy je nepustí, takže paměť roste s tím
    nejdelším blokem. Na CUDA má empty_cache() stejný smysl.
    """
    try:
        import torch
    except Exception:
        return
    try:
        if zarizeni in (None, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
        if zarizeni in (None, "mps") and getattr(torch.backends, "mps", None) \
                and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


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


class _ZarovnaniMlx:
    """Analyzátor z MLX v podobě, na kterou se dívá stav_zarovnani().

    Kontrola vadných bloků čte z torchového T3 cestu
    t3.patched_model.alignment_stream_analyzer a z ní completed_at a alignment.
    MLX má vlastní analyzátor se stejným stavem, jen jinde - tahle skořápka ho
    dává na stejné místo, aby hlídání běželo i na MLX. Bez ní by
    stav_zarovnani() vracelo (None, 0) a posud_zarovnani() by každý blok
    prohlásilo za bezvadný, tedy by kontrola potichu zmizela.
    """

    def __init__(self, model):
        self._model = model

    @property
    def alignment_stream_analyzer(self):
        return getattr(self._model, "last_analyzer", None)


class _MlxT3:
    """Zástupce chatterboxového T3, který tokeny počítá v MLX.

    Chatterbox z T3 potřebuje jen `hp` (kvůli speciálním tokenům) a
    `inference()`. Zbytek původního modulu jsou 2 GB vah, které by na MPS
    ležely ladem, takže se po záměně pustí.
    """

    def __init__(self, mlx_model, hp):
        self._model = mlx_model
        self.hp = hp
        # Aby kontrola vadných bloků našla, kam se text dočetl.
        self.patched_model = _ZarovnaniMlx(mlx_model)

    def inference(self, *, t3_cond, text_tokens, max_new_tokens=1000,
                  temperature=0.8, cfg_weight=0.5, repetition_penalty=2.0,
                  min_p=0.05, top_p=1.0, **_ostatni):
        import mlx.core as mx
        import torch

        def na_mlx(tenzor):
            return mx.array(tenzor.detach().cpu().numpy())

        prompt = t3_cond.cond_prompt_speech_tokens
        tokeny = self._model.inference(
            speaker_emb=na_mlx(t3_cond.speaker_emb),
            cond_prompt_speech_tokens=(None if prompt is None
                                       else na_mlx(prompt).astype(mx.int32)),
            emotion_adv=na_mlx(t3_cond.emotion_adv),
            text_tokens=na_mlx(text_tokens).astype(mx.int32),
            max_new_tokens=int(max_new_tokens or 1000),
            temperature=float(temperature),
            top_p=float(top_p),
            min_p=float(min_p),
            repetition_penalty=float(repetition_penalty),
            cfg_weight=float(cfg_weight),
        )
        # Chatterbox si z výsledku vezme [0] a pak odstraní speciální tokeny.
        return torch.tensor([tokeny], dtype=torch.long)


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
        self.mlx_presnost = MLX_VYPNUTO   # kvantizace backbonu T3 v MLX
        self._t3_soubor = None            # checkpoint T3, ze kterého umí načíst i MLX
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
                    rychly_dekoder: bool = False,
                    mlx_presnost: str = MLX_VYPNUTO):
        import torch

        pozadovane_zarizeni = self.vyber_zarizeni(volba_zarizeni)
        jazyk = jazyk_podle_klice(jazyk_klic)
        pozadovany_repo = jazyk.get("repo") if jazyk.get("zdroj") == "finetune" else None
        rychly_dekoder = bool(rychly_dekoder)
        if mlx_presnost not in MLX_PRESNOSTI:
            mlx_presnost = MLX_VYPNUTO

        if self.model is not None:
            # Jiné zařízení, jazykový checkpoint, dekodér nebo přesnost T3 =
            # čistý start. Nechat na T3 váhy po předchozím jazyce by bylo horší
            # než nic.
            if (pozadovane_zarizeni != self.zarizeni or self.nacteny_repo != pozadovany_repo
                    or rychly_dekoder != self.rychly_dekoder
                    or mlx_presnost != self.mlx_presnost):
                self.log(T("log_znovu"))
                self.uvolni()
            else:
                return

        self.zarizeni = pozadovane_zarizeni
        self.jazyk = jazyk
        self.rychly_dekoder = rychly_dekoder
        self.mlx_presnost = mlx_presnost
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

        # Až po fine-tunu: MLX si stejný checkpoint načte sám a torchový T3 se
        # pak pustí. Musí to být před prvním prepare_conditionals().
        if mlx_presnost != MLX_VYPNUTO:
            self._zapni_mlx(mlx_presnost)

        if rychly_dekoder:
            self._zapni_rychly_dekoder()

    # ------------------------------------------------------------------
    def _zakladni_t3(self):
        """Cesta k základnímu T3 v cache - pro jazyky bez fine-tunu."""
        try:
            from chatterbox.mtl_tts import REPO_ID
            from huggingface_hub import hf_hub_download

            return Path(hf_hub_download(REPO_ID, MLX_ZAKLADNI_T3,
                                        cache_dir=str(CACHE_DIR / "hub")))
        except Exception:
            return None

    # ------------------------------------------------------------------
    def _zapni_mlx(self, presnost: str):
        """Přesune T3 do MLX. Uživatel si to zapnul, takže selhání je chyba.

        T3 je autoregresivní, takže je to na MPS nejdražší část pipeline.
        V MLX s 8bitovým backbonem dá 158 tokenů/s proti 27 na torchi
        a hlavně stabilně: MLX cesta nestaví analyzátor, takže netrpí únavou
        popsanou v uvolni_hooky_t3(). Po záměně už T3 není úzké hrdlo -
        z 3,5 s generování na něj padá 1,3 s, zbytek je s3gen na MPS.

        Tiše se vrátit k PyTorchi by znamenalo, že otisk knihy tvrdí něco
        jiného, než z čeho zvuk opravdu vznikl - stejný důvod, proč se
        nevrací ani rychlý dekodér.
        """
        import gc

        if not mlx_mozny():
            raise RuntimeError(T("err_mlx", T("err_mlx_platforma")))
        if self.zarizeni != "mps":
            raise RuntimeError(T("err_mlx", T("err_mlx_zarizeni", self.zarizeni)))

        try:
            import mlx.core as mx
            from mlx_t3 import load_t3
        except Exception as chyba:
            raise RuntimeError(T("err_mlx", f"{type(chyba).__name__}: {chyba}")) from chyba

        soubor = self._t3_soubor or self._zakladni_t3()
        if soubor is None or Path(soubor).suffix != ".safetensors":
            raise RuntimeError(T("err_mlx", T("err_mlx_checkpoint")))

        try:
            hp = self.model.t3.hp
            model_mlx = load_t3(str(soubor), dtype=mx.float32,
                                bits=MLX_PRESNOSTI[presnost])
            # Torchový T3 pustit dřív, než si MLX vezme paměť pro svůj.
            self.model.t3 = None
            gc.collect()
            uvolni_pamet_zarizeni(self.zarizeni)
            self.model.t3 = _MlxT3(model_mlx, hp)
            mx.set_cache_limit(MLX_STROP_CACHE_MB * 1024 * 1024)
        except Exception as chyba:
            raise RuntimeError(T("err_mlx", f"{type(chyba).__name__}: {chyba}")) from chyba

        self.log(T("log_mlx_t3", presnost))

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
        # Odložit i pro MLX - načítá si tentýž checkpoint sám, bez torche.
        self._t3_soubor = soubor
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
        # Bloky mají různou délku a alokátor si drží tvar toho nejdelšího.
        # Bez tohoto roste paměť procesu po celou knihu - na MPS nejvíc.
        uvolni_pamet_zarizeni(self.zarizeni)
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
        self.mlx_presnost = MLX_VYPNUTO
        self._t3_soubor = None
        self._hlas_klic = None
        self._vychozi_conds = None
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        uvolni_pamet_zarizeni()


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
# Brblání uvnitř bloku - model mezi dvěma větami hučí a šumí místo pauzy.
# Měří se nejdelší úsek bez řeči, ne nejdelší ticho: hučení kolem -34 dBFS
# je na ticho moc hlasité. Řeč, která úsek přeruší, musí být nad polovinou
# hlasitosti řeči souvisle aspoň 0,15 s - hučení má krátké hlasitější
# záchvěvy a ty ho jinak rozsekaly pod práh (blok 64 Ovidia: 3,48 s místo
# 9,8 s). Naměřeno na dvou převodech kapitoly 1 Ovidia, uživatel poslechem
# potvrdil všech 11 nalezených míst: brblání 3,9 až 9,8 s, zbylé bloky
# nejvýš 3,1 s.
MAX_BEZ_RECI_S = 3.5
PRAH_RECI = 0.50
MIN_RECI_S = 0.15
# Záloha: blok nad 100 znaků, který se čte pomaleji, je skoro jistě protažený
# brbláním. Běžně 13,4 znaku za sekundu, 10 % nejpomalejších zdravých pod
# 11,5; vadné bloky 6,6 až 9,4.
MIN_ZNAKU_ZA_S = 9.5
# Opakovaná koncovka: model dočte text a pak řekne posledních pár slov ještě
# jednou. Typicky na krátkém bloku - "Hele," řekl. "Hned jsem zpátky." vyjde
# na 2,5 s, a když se koncovka zopakuje, na 4 až 8 s.
#
# Kalibrováno na 407 blocích (7 řádků x 21 hlasů plus 260 opakovaných
# generování jednoho krátkého řádku se dvěma hlasy). Značku dělal přepis
# whisperem: koncovka opakovaná, když poslední 2 až 5 slov textu zazní v
# přepisu dvakrát. Tak označených bylo 46 bloků.
#
#   rychlost < 11 zn/s sama            96 % zachyceno, ale 42 % planých
#   vyčnívání >= 0,35 samo             96 % zachyceno,  10  % planých
#   rychlost < 11 A vyčnívání >= 0,35  96 % zachyceno,  8,3 % planých
#
# Sama rychlost nestačí, protože i čistě přečtený krátký blok se čte 7,8 až
# 12,5 zn/s. Samotná shoda koncovky s dřívějším úsekem taky ne: stejné
# pravidlo s ní místo vyčnívání zachytilo jen 87 % a hučení na jednom tónu v
# ní dá 1,00, protože se potká s čímkoli. Proto se od nejlepší shody odečítá
# medián všech - opakovaná koncovka se potká s jedním místem, ne s blokem
# obecně (u označených vyčnívá 0,45 až 0,70, u čistých 0,16 až 0,44).
#
# Z planých poplachů byla navíc třetina delší než 1,45násobek mediánu délky
# svého řádku, tedy skoro jistě chyba značky - whisper má silný jazykový
# model a zopakovanou frázi umí přepsat jen jednou (7,08 s na 33 znaků
# přepsaných jako čistá věta). Cena planého poplachu je jedno generování
# navíc, ne vada v knize: blok se jen zkusí znovu s jiným seedem.
OPAKOVANI_DOTAZ_S = 0.6
OPAKOVANI_ODSTUP_S = 0.35
OPAKOVANI_OKNO = 1024       # rámec FFT
OPAKOVANI_HOP = 256         # posun rámce, tedy rozlišení odstupu (10,7 ms)
PRAH_OPAKOVANI = 0.35
OPAKOVANI_ZNAKU_ZA_S = 11.0
RE_SLOVO = re.compile(r"\w+")


def rozbor_reci(vzorky, sr: int):
    """Vrátí (kde končí slyšitelná řeč ve vzorcích, nejdelší úsek bez řeči uvnitř v s).

    Slyšitelná řeč je rámec nad 35 % hlasitosti 90. percentilu. Úsek bez
    řeči je všechno mezi první a poslední řečí, co není souvislou řečí nad
    PRAH_RECI delší než MIN_RECI_S - ticho, hučení i jeho krátké záchvěvy.
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
    souvisla = (rms[rec[0]:rec[-1] + 1] > uroven * PRAH_RECI).astype("int8")
    zmeny = np.diff(np.concatenate(([0], souvisla, [0])))
    for a, b in zip(np.flatnonzero(zmeny == 1), np.flatnonzero(zmeny == -1)):
        if (b - a) * krok / sr < MIN_RECI_S:
            souvisla[a:b] = 0            # záchvěv hučení, ne slabika
    zmeny = np.diff(np.concatenate(([0], 1 - souvisla, [0])))
    delky = np.flatnonzero(zmeny == -1) - np.flatnonzero(zmeny == 1)
    nejdelsi = float(delky.max()) * krok / sr if len(delky) else 0.0
    return int(zacatky[rec[-1]] + okno), nejdelsi


def _pasy_spektra(vzorky, sr: int, pasu: int = 24):
    """Blok -> (pásů, rámců) jednotkových vektorů k porovnávání úseků.

    Hledá se "totéž znovu", takže musí rozhodovat barva hlásek, ne hlasitost:
    energie se sečte do 24 logaritmicky rozložených pásů (hrubá melová banka,
    jen bez další závislosti), zlogaritmuje, z každého rámce se odečte jeho
    průměr a rámec se znormuje. Skalární součin dvou rámců je pak přímo
    kosinová podobnost.
    """
    import numpy as np

    y = np.asarray(vzorky, dtype="float32").reshape(-1)
    if len(y) < OPAKOVANI_OKNO * 2:
        return np.zeros((pasu, 0), dtype="float32")
    ramce = np.lib.stride_tricks.sliding_window_view(y, OPAKOVANI_OKNO)[::OPAKOVANI_HOP]
    okenko = np.hanning(OPAKOVANI_OKNO).astype("float32")
    spektrum = np.abs(np.fft.rfft(ramce * okenko, axis=1))

    hranice = np.geomspace(80.0, min(8000.0, sr / 2.0), pasu + 1)
    kosi = np.searchsorted(np.fft.rfftfreq(OPAKOVANI_OKNO, 1.0 / sr), hranice)
    f = np.stack([spektrum[:, a:max(b, a + 1)].sum(axis=1) for a, b in zip(kosi, kosi[1:])])
    f = np.log1p(f)
    f -= f.mean(axis=0, keepdims=True)
    return f / (np.linalg.norm(f, axis=0, keepdims=True) + 1e-9)


def opakovani_konce(vzorky, sr: int):
    """(o kolik konec řeči vyčnívá nad blok, odstup v s k nejlepší shodě).

    Opakovaná koncovka je totéž znovu, takže se v bloku pozná jako úsek,
    který už v něm byl. Dotazem je posledních OPAKOVANI_DOTAZ_S *řeči* -
    koncové ticho se zahodí, jinak by ticho našlo ticho a skórovalo přes 0,9
    v každém bloku. Dřívější úsek musí začínat aspoň OPAKOVANI_ODSTUP_S před
    dotazem, aby se dotaz nenašel sám v sobě.

    Vrací se rozdíl nejlepší shody proti mediánu všech, ne shoda sama.
    Opakovaná koncovka se potká s jedním místem, ne s blokem obecně - kdežto
    hučení na jednom tónu se potká se vším a v samotné shodě dá 1,00.
    """
    import numpy as np

    konec_reci, _ = rozbor_reci(vzorky, sr)
    y = np.asarray(vzorky, dtype="float32").reshape(-1)[:max(konec_reci, 1)]
    f = _pasy_spektra(y, sr)
    dotaz_ramcu = max(1, int(OPAKOVANI_DOTAZ_S * sr / OPAKOVANI_HOP))
    odstup_ramcu = int(OPAKOVANI_ODSTUP_S * sr / OPAKOVANI_HOP)
    kolik = f.shape[1] - dotaz_ramcu - odstup_ramcu     # kam všude se dotaz vejde
    if kolik < 1:
        return 0.0, 0.0

    # skore[s] = průměrná podobnost dotazu s úsekem začínajícím rámcem s.
    # Přes součin dotaz.T @ f a sčítání posunutých řádků, aby to byla lineární
    # algebra a ne pythonovská smyčka přes rámce.
    soucin = f[:, -dotaz_ramcu:].T @ f
    skore = np.zeros(kolik, dtype="float32")
    for k in range(dotaz_ramcu):
        skore += soucin[k, k:k + kolik]
    skore /= dotaz_ramcu

    kde = int(skore.argmax())
    vycnivani = float(skore[kde] - np.median(skore))
    return vycnivani, (f.shape[1] - dotaz_ramcu - kde) * OPAKOVANI_HOP / float(sr)


def konec_opakovany_v_textu(text: str) -> int:
    """Kolik posledních slov textu se v něm hned předtím opakuje (0 = žádné).

    "Pak jsem zvracel a zvracel a zvracel." nebo "Já to říkal! Já to říkal!"
    má opakovanou koncovku už v textu, takže do zvuku patří a hlídat se nesmí.
    Na knihách v knihy/ je takových bloků 19 z 30 tisíc, tedy 0,00 až 0,13 %
    podle knihy - na tak vzácný případ stačí tahle podmínka a nemusí se kvůli
    němu pouštět přepis.
    """
    slova = RE_SLOVO.findall(text.lower())
    for k in range(5, 1, -1):
        if len(slova) >= 2 * k and slova[-k:] == slova[-2 * k:-k]:
            return k
    return 0


def posud_opakovani(vzorky, sr: int, text: str):
    """(zazněla koncovka dvakrát?, vyčnívání, odstup v s).

    Dvě podmínky zároveň, každá sama o sobě dělá moc planých poplachů:
    blok trvá dýl, než na jeho text padne, a jeho konec už v něm jednou byl.
    Čísla a měření jsou u PRAH_OPAKOVANI.
    """
    if konec_opakovany_v_textu(text):
        return False, 0.0, 0.0
    rychlost = len(text) / max(len(vzorky) / float(sr), 1e-3)
    if rychlost >= OPAKOVANI_ZNAKU_ZA_S:
        return False, 0.0, 0.0
    opak, odstup = opakovani_konce(vzorky, sr)
    return opak >= PRAH_OPAKOVANI, opak, odstup


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


RE_TROJTECKA_NA_KONCI = re.compile(r"\.{2,}(?=[\"')\]]*$)")


def text_pro_model(text: str) -> str:
    """Trojtečku na konci bloku převede na tečku.

    Chatterbox trojtečku mění na čárku a čárku bere jako konec věty. Blok
    "Za třetí..." tak dostane jako otevřenou větu a model pak nepřestane.
    """
    return RE_TROJTECKA_NA_KONCI.sub(".", text.rstrip())


def _generuj_jednou(engine, text: str, p: dict, index: int, celkem: int, log):
    """Až tři pokusy o jeden kus textu. Vrátí (vzorky nebo None, vada vybraného pokusu).

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
            vzorky = engine.generuj(text_pro_model(text), p["referencni_wav"], p["exaggeration"],
                                    p["cfg_weight"], p["temperature"],
                                    p.get("min_p", 0.05))
            delka = len(vzorky) / float(engine.sr)
            if delka < 0.05:
                log(T("log_prazdny", index, celkem))
                continue

            dosel, snimku = stav_zarovnani(engine)
            konec_reci, _ = rozbor_reci(vzorky, engine.sr)
            if snimku:
                vada, ponechat = posud_zarovnani(dosel, snimku, len(vzorky), konec_reci)
            else:
                # Bez analyzátoru zbývá odhad z délky: čte se 13,5 znaku za sekundu
                vada = "dlouhy" if delka > len(text) / 10.0 + 3.0 else ""
                ponechat = len(vzorky)
            if vada == "ocas":
                log(T("log_ocas", index, celkem, (len(vzorky) - ponechat) / float(engine.sr)))
                vzorky = vzorky[:ponechat]
            elif vada == "nedocteno":
                log(T("log_nedocteno", index, celkem))
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

            # Brblání a rychlost se posuzují až na tom, co se opravdu zapíše.
            # Ořez okrajů mění hlasitostní rozložení bloku - blok 65 Ovidia
            # prošel před ořezem pod prahem a v hotové kapitole měl 4,6 s.
            if not vada:
                _, ticho = rozbor_reci(vzorky, engine.sr)
                rychlost = len(text) / max(len(vzorky) / float(engine.sr), 1e-3)
                if ticho > MAX_BEZ_RECI_S:
                    vada = "ticho"
                    log(T("log_ticho", index, celkem, ticho))
                elif len(text) >= 100 and rychlost < MIN_ZNAKU_ZA_S:
                    vada = "pomaly"
                    log(T("log_pomaly", index, celkem, rychlost))
                else:
                    # Krátké bloky sem propadnou i protažené: kontrola rychlosti
                    # výš platí až od 100 znaků, a právě na krátkém bloku model
                    # nejčastěji zopakuje koncovku.
                    dvakrat, opak, odstup = posud_opakovani(vzorky, engine.sr, text)
                    if dvakrat:
                        vada = "opakovani"
                        log(T("log_opakovani", index, celkem, opak, odstup))

            if not vada:
                return vzorky, ""
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
        return None, ""
    if kontrola and len(kandidati) > 1:
        body = [skore(v) for v, _ in kandidati]
        nejlepsi = max(range(len(kandidati)), key=lambda i: body[i])
        log(T("log_asr_vyber", index, celkem, nejlepsi + 1, len(kandidati), body[nejlepsi]))
        return kandidati[nejlepsi]
    # Bez přepisu: čistý pokus, jinak uříznutý ocas, jinak poslední
    poradi = {"": 0, "ocas": 1, "ticho": 2, "pomaly": 2, "dlouhy": 2,
              "opakovani": 2, "nedocteno": 3}
    return min(reversed(kandidati), key=lambda k: poradi[k[1]])


# Vady, které po uříznutí nezmizí - s takovým pokusem se blok raději rozdělí.
# "opakovani" tady schválně není: opakovaná koncovka roste právě na krátkém
# bloku, takže dělit by znamenalo přilévat. Zbývá na ni opakované generování.
VADY_K_DELENI = ("ticho", "pomaly", "dlouhy", "nedocteno")


def generuj_blok(engine, blok: str, p: dict, index: int, celkem: int, log, _hloubka: int = 0):
    """Vygeneruje blok. Vrací (vzorky nebo None, vynechané úseky textu).

    Když blok nevyjde ani na tři pokusy, rozdělí se na kratší části a zkusí se
    po nich - kratší text model rozhodí méně. Platí to i pro pokusy, které
    sice zvuk daly, ale všechny s brbláním nebo nedočteným textem: dřív si
    aplikace nechala ten nejméně vadný a brblání v knize zůstalo (blok 39
    Ovidia se 7,3 s). Uříznutý ocas za větou se nechává, ten je opravený.

    Zpátky do knihy se části vloží na stejné místo, takže pořadí sedí.
    Vynechá se jen to, co nevyjde ani po rozdělení, a to se vrátí, ať se to
    dá uživateli ukázat. Kde už dělit nejde, zůstane nejméně vadný pokus.

    Používají to obě cesty - jednoprocesová i jednotlivý pracovník poolu.
    """
    import numpy as np

    vzorky, vada = _generuj_jednou(engine, blok, p, index, celkem, log)
    if vzorky is not None and vada not in VADY_K_DELENI:
        return vzorky, []

    casti = rozdel_na_bloky(blok, max(MIN_CAST_ZNAKU, len(blok) // 2)) if _hloubka < 2 else []
    if len(casti) < 2:
        if vzorky is not None:
            return vzorky, []
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


def zkrat_text(text: str, font, sirka: int) -> str:
    """Zkrátí text se třemi tečkami tak, aby se vešel do 'sirka' bodů."""
    text = text or ""
    if sirka <= 8 or font.measure(text) <= sirka:
        return text
    dole, nahore = 0, len(text)
    while dole < nahore:
        stred = (dole + nahore + 1) // 2
        if font.measure(text[:stred] + "…") <= sirka:
            dole = stred
        else:
            nahore = stred - 1
    return text[:dole] + "…"


def kdy_text(cas: float) -> str:
    """'dnes 16:41', 'včera 16:41', jinak '8. 9. 21:02'."""
    import datetime

    kdy = datetime.datetime.fromtimestamp(cas)
    dnes = datetime.date.today()
    hodiny = kdy.strftime("%H:%M")
    if kdy.date() == dnes:
        return T("kdy_dnes", hodiny)
    if kdy.date() == dnes - datetime.timedelta(days=1):
        return T("kdy_vcera", hodiny)
    return T("kdy_datum", kdy.day, kdy.month, hodiny)


class Tlacitko(tk.Label):
    """Ploché tlačítko podle návrhu: 'tlacitko', 'plocha' (hlavní akce) nebo 'odkaz'."""

    def __init__(self, rodic, text, command, druh="tlacitko", font=None, bg_rodice=None,
                 padx=17, pady=10):
        self._druh, self._command, self._povoleno = druh, command, True
        self._bg_rodice = bg_rodice or BARVY["panel"]
        super().__init__(rodic, text=text, font=font, padx=padx, pady=pady, bd=0,
                         highlightthickness=0, cursor="hand2")
        self._obarvi()
        self.bind("<Button-1>", self._klik)
        self.bind("<Enter>", lambda _u: self._obarvi(najeto=True))
        self.bind("<Leave>", lambda _u: self._obarvi())

    def _obarvi(self, najeto=False):
        b = BARVY
        najeto = najeto and self._povoleno
        if self._druh == "plocha":
            bg = b["plocha_aktivni"] if najeto else (b["plocha"] if self._povoleno else b["stopa2"])
            fg = b["na_plose"]
        elif self._druh == "odkaz":
            bg = self._bg_rodice
            fg = (b["text"] if najeto else b["akcent"]) if self._povoleno else b["tlumeny"]
        else:
            bg = b["tlacitko_aktivni"] if najeto else b["tlacitko"]
            fg = b["text"] if self._povoleno else b["tlumeny"]
        self.configure(bg=bg, fg=fg, cursor="hand2" if self._povoleno else "arrow")

    def _klik(self, _u=None):
        if self._povoleno and self._command:
            self._command()

    def povol(self, ano: bool):
        self._povoleno = bool(ano)
        self._obarvi()

    def text(self, text: str):
        if self.cget("text") != text:
            self.configure(text=text)


class Chip(tk.Label):
    """Přepínač v podobě štítku. Zapnutý je tónovaný a v akcentu."""

    def __init__(self, rodic, text, promenna=None, command=None, font=None, padx=11, pady=6):
        super().__init__(rodic, text=text, font=font, padx=padx, pady=pady, bd=0, cursor="hand2")
        self._promenna, self._command, self._povoleno = promenna, command, True
        self.bind("<Button-1>", self._klik)
        self.obarvi()

    def obarvi(self):
        zapnuto = bool(self._promenna.get()) if self._promenna is not None else False
        self.configure(bg=BARVY["chip"] if zapnuto else BARVY["pole"],
                       fg=BARVY["akcent"] if zapnuto else BARVY["tlumeny"],
                       cursor="hand2" if self._povoleno else "arrow")

    def _klik(self, _u=None):
        if not self._povoleno:
            return
        if self._promenna is not None:
            self._promenna.set(not self._promenna.get())
        if self._command:
            self._command()
        self.obarvi()

    def povol(self, ano: bool):
        self._povoleno = bool(ano)
        self.obarvi()


class Ikona(tk.Canvas):
    """Tlačítko s kreslenou ikonou. Glyfy ⏮ ☀ ☾ přibalené písmo nemá."""

    def __init__(self, rodic, druh, command, velikost=36, kruh=False, font=None, bg_rodice=None):
        super().__init__(rodic, width=velikost, height=velikost, highlightthickness=0, bd=0,
                         bg=bg_rodice or BARVY["panel"], cursor="hand2")
        self.druh, self._command, self._v, self._kruh, self._font = druh, command, velikost, kruh, font
        self._povoleno, self._najeto = True, False
        self.bind("<Button-1>", lambda _u: self._povoleno and self._command and self._command())
        self.bind("<Enter>", lambda _u: self._hover(True))
        self.bind("<Leave>", lambda _u: self._hover(False))
        self.kresli()

    def _hover(self, ano):
        self._najeto = ano
        self.kresli()

    def nastav(self, druh: str):
        if druh != self.druh:
            self.druh = druh
            self.kresli()

    def povol(self, ano: bool):
        if bool(ano) != self._povoleno:
            self._povoleno = bool(ano)
            self.configure(cursor="hand2" if ano else "arrow")
            self.kresli()

    def _tvary(self, podklad: str, ink: str) -> list:
        """Ikona jako seznam tvarů (druh, souřadnice, barva[, tloušťka])."""
        import math

        v, d = self._v, self.druh
        c, s = v / 2.0, v / 36.0
        tvary = [("ovál" if self._kruh else "obdélník", (0, 0, v, v), podklad)]
        if d == "hrat":
            tvary.append(("mnohoúhelník", (c - 4 * s, c - 7 * s, c - 4 * s, c + 7 * s, c + 8 * s, c), ink))
        elif d == "pauza":
            for x in (c - 5 * s, c + 2 * s):
                tvary.append(("obdélník", (x, c - 7 * s, x + 3 * s, c + 7 * s), ink))
        elif d in ("predchozi", "dalsi"):
            znamenko = -1 if d == "predchozi" else 1
            kraj = c + znamenko * 6 * s
            tvary.append(("obdélník", (min(kraj, kraj + znamenko * 2 * s), c - 6 * s,
                                       max(kraj, kraj + znamenko * 2 * s), c + 6 * s), ink))
            tvary.append(("mnohoúhelník", (c - znamenko * 6 * s, c - 6 * s,
                                           c - znamenko * 6 * s, c + 6 * s, kraj, c), ink))
        elif d == "slunce":
            r = 3.5 * s
            tvary.append(("ovál", (c - r, c - r, c + r, c + r), ink))
            for i in range(8):
                u = i * math.pi / 4
                tvary.append(("čára", (c + math.cos(u) * 6 * s, c + math.sin(u) * 6 * s,
                                       c + math.cos(u) * 8.5 * s, c + math.sin(u) * 8.5 * s), ink, 1.4 * s))
        elif d == "mesic":
            r = 6.5 * s
            tvary.append(("ovál", (c - r, c - r, c + r, c + r), ink))
            tvary.append(("ovál", (c - r + 4.5 * s, c - r - 2 * s, c + r + 4.5 * s, c + r - 2 * s), podklad))
        return tvary

    def kresli(self):
        b, v, d = BARVY, self._v, self.druh
        self.delete("all")
        najeto = self._najeto and self._povoleno
        if self._kruh:
            podklad, ink = (b["plocha_aktivni"] if najeto else b["plocha"]), b["na_plose"]
        else:
            zaklad = b["pole"] if d in ("slunce", "mesic") else b["tlacitko"]
            podklad = b["tlacitko_aktivni"] if najeto else zaklad
            ink = b["tlumeny"] if d in ("slunce", "mesic") else b["text2"]
        if not self._povoleno:
            ink = b["stopa2"]

        # Tk kreslí kruhy a šikmé hrany bez vyhlazení, takže jsou zubaté.
        # Ikona se proto kreslí přes Pillow ve čtyřnásobku a zmenší se.
        from PIL import Image, ImageDraw, ImageTk

        k = 4
        obr = Image.new("RGB", (v * k, v * k), self.cget("bg"))
        kresba = ImageDraw.Draw(obr)
        for druh, body, barva, *tloustka in self._tvary(podklad, ink):
            body = [x * k for x in body]
            if druh == "ovál":
                kresba.ellipse([body[0], body[1], body[2] - 1, body[3] - 1], fill=barva)
            elif druh == "obdélník":
                kresba.rectangle([body[0], body[1], body[2] - 1, body[3] - 1], fill=barva)
            elif druh == "mnohoúhelník":
                kresba.polygon(body, fill=barva)
            else:
                kresba.line(body, fill=barva, width=max(1, int(tloustka[0] * k)))
        self._foto = ImageTk.PhotoImage(obr.resize((v, v), Image.LANCZOS), master=self)
        self.create_image(0, 0, anchor="nw", image=self._foto)
        if d in ("zpet", "vpred"):
            self.create_text(v / 2.0, v / 2.0, text="−15" if d == "zpet" else "+30",
                             fill=ink, font=self._font)


class ZkracenyPopisek(tk.Label):
    """Popisek, který dlouhý text (cestu, název) zkrátí třemi tečkami na šířku sloupce."""

    def __init__(self, rodic, font, **kw):
        kw.setdefault("anchor", "w")
        kw.setdefault("width", 1)
        super().__init__(rodic, font=font, **kw)
        self._plny, self._font = "", font
        self.bind("<Configure>", lambda _u: self._zkrat())

    def nastav(self, text: str):
        if text != self._plny:
            self._plny = text or ""
            self._zkrat()

    def _zkrat(self):
        kratky = zkrat_text(self._plny, self._font, self.winfo_width() - 4)
        if self.cget("text") != kratky:
            self.configure(text=kratky)


class DialogPokrocile(tk.Toplevel):
    """Pokročilé nastavení. Hodnoty jdou rovnou do proměnných hlavního okna."""

    def __init__(self, app, zamceno: bool):
        super().__init__(app)
        b, f, px = BARVY, app.F, app.px
        self.title(T("dlg_pokrocile"))
        self.configure(background=b["panel"])
        self.transient(app)
        self.resizable(False, False)

        ramec = tk.Frame(self, bg=b["panel"], padx=px(22), pady=px(18))
        ramec.pack(fill="both", expand=True)
        ramec.columnconfigure(1, weight=1, minsize=px(220))
        radek = [0]

        def popisek(text):
            tk.Label(ramec, text=text, font=f["pole"], bg=b["panel"], fg=b["text2"], anchor="w").grid(
                row=radek[0], column=0, sticky="w", padx=(0, px(16)), pady=px(5))

        def posuvnik(text, promenna, od, do):
            popisek(text)
            hodnota = tk.Label(ramec, font=f["pole"], bg=b["panel"], fg=b["akcent"], width=5, anchor="e",
                               text=f"{promenna.get():.2f}")
            meritko = ttk.Scale(ramec, from_=od, to=do, variable=promenna,
                                command=lambda _h: hodnota.configure(text=f"{promenna.get():.2f}"))
            meritko.grid(row=radek[0], column=1, sticky="ew")
            hodnota.grid(row=radek[0], column=2, sticky="e", padx=(px(10), 0))
            if zamceno:
                meritko.state(["disabled"])
            radek[0] += 1

        def cislo(text, promenna, od, do, krok):
            popisek(text)
            ttk.Spinbox(ramec, from_=od, to=do, increment=krok, textvariable=promenna, width=8,
                        state="disabled" if zamceno else "normal").grid(row=radek[0], column=1, sticky="w")
            radek[0] += 1

        posuvnik(T("lab_expresivita"), app.var_exag, 0.25, 1.0)
        posuvnik(T("lab_cfg"), app.var_cfg, 0.0, 1.0)
        posuvnik(T("lab_teplota"), app.var_temp, 0.05, 1.5)
        posuvnik(T("lab_min_p"), app.var_min_p, 0.0, 0.30)
        tk.Frame(ramec, height=1, bg=b["linka"]).grid(row=radek[0], column=0, columnspan=3,
                                                       sticky="ew", pady=px(12))
        radek[0] += 1
        cislo(T("lab_znaku"), app.var_max_znaku, 80, 400, 10)
        cislo(T("lab_pauza_ms"), app.var_pauza, 0, 2000, 50)
        cislo(T("lab_seed"), app.var_seed, 0, 999999, 1)
        cislo(T("lab_pracovniku"), app.var_pracovniku, 0, 4, 1)
        popisek(T("lab_zarizeni"))
        ttk.Combobox(ramec, textvariable=app.var_zarizeni, width=7, values=["auto", "cuda", "cpu"],
                     state="disabled" if zamceno else "readonly").grid(row=radek[0], column=1, sticky="w")
        radek[0] += 1
        # Na Windows ani CUDA se MLX neuplatní, takže se tam volba vůbec nenabízí.
        if mlx_mozny():
            popisek(T("lab_mlx"))
            ttk.Combobox(ramec, textvariable=app.var_mlx_presnost, width=7,
                         values=list(MLX_VOLBY),
                         state="disabled" if zamceno else "readonly").grid(
                row=radek[0], column=1, sticky="w")
            radek[0] += 1
            tk.Label(ramec, text=T("hint_mlx"), font=f["popis"], bg=b["panel"],
                     fg=b["tlumeny"], anchor="w", justify="left",
                     wraplength=px(420)).grid(row=radek[0], column=0, columnspan=3,
                                              sticky="w", pady=(px(6), 0))
            radek[0] += 1

        tk.Frame(ramec, height=1, bg=b["linka"]).grid(row=radek[0], column=0, columnspan=3,
                                                       sticky="ew", pady=px(12))
        radek[0] += 1
        volba = tk.Frame(ramec, bg=b["panel"])
        volba.grid(row=radek[0], column=0, columnspan=3, sticky="w")
        Chip(volba, T("lab_kontrola_asr"), app.var_kontrola_asr, font=f["maly"]).pack(side="left")
        radek[0] += 1
        tk.Label(ramec, text=T("hint_asr_stazeno") if asr_stazeny() else T("hint_asr_stahne", ASR_GB),
                 font=f["popis"], bg=b["panel"], fg=b["tlumeny"], anchor="w", justify="left",
                 wraplength=px(420)).grid(row=radek[0], column=0, columnspan=3, sticky="w", pady=(px(6), 0))
        radek[0] += 1
        if zamceno:
            for dite in volba.winfo_children():
                dite.povol(False)

        Tlacitko(ramec, T("btn_zavrit"), self.destroy, font=f["pole"], padx=px(16), pady=px(8)).grid(
            row=radek[0], column=0, columnspan=3, sticky="e", pady=(px(18), 0))

        self.bind("<Escape>", lambda _u: self.destroy())
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, app.winfo_rooty() + px(80))}")
        self.grab_set()


class DialogOprava(tk.Toplevel):
    """Najde blok podle času, vygeneruje ho znovu a vymění v hotovém souboru."""

    def __init__(self, app, slozka: Path, soubor: Path = None, cas: float = None):
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
        self.nahrazene = []        # soubory, které se změnily - přehrávač je načte znovu

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
        self._nabidni_soubory(Path(soubor).name if soubor else "")
        if cas is not None and self.zaznamy:
            self.var_cas.set(kratky_cas(cas))
            self._najdi()
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
                engine.nacti_model(p["zarizeni"], p["jazyk_textu"], p.get("rychly_dekoder", False),
                                   p.get("mlx_presnost", MLX_VYPNUTO))
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
                    self.nahrazene.append(self.soubor)
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
        # Konfigurace se musí načíst dřív než cokoli s textem - jazyk i barvy
        # se uplatní hned na titulku a prvním widgetu.
        self.config_data = self._nacti_config()
        nastav_jazyk(self.config_data.get("jazyk", "en"))
        self.tema = nastav_paletu(self.config_data.get("tema", "tmave"))

        self.title(f"{T('app_nazev')} v{VERSION}")
        self._meritko = max(1.0, self.winfo_fpixels("1i") / 96.0)
        # Velikost podle návrhu, ale nikdy větší než obrazovka - na 13" MacBooku
        # nebo notebooku s měřítkem se jinak spodek okna vůbec neukázal. Obsah
        # se posouvá, takže minimum může být malé a nic se neztratí pod hranou.
        sirka = min(self.px(1320), self.winfo_screenwidth() - 60)
        vyska = min(self.px(980), self.winfo_screenheight() - 120)
        self.geometry(f"{sirka}x{vyska}")
        self.minsize(min(self.px(640), sirka), min(self.px(460), vyska))

        self.fronta = queue.Queue()
        self.vlakno = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.engine = TtsEngine(self.log_z_vlakna)

        self.bloky = []          # (index_kapitoly, text_bloku)
        self.bloky_puvodni = []  # texty bloků před slovníčkem výslovnosti - do otisku
        self.kapitoly = []
        self.ma_kapitoly = False
        self.nazev_knihy = ""
        self.titul_knihy = ""
        self.autor_knihy = ""
        self.bezi = False
        # nezahajeno / bezi / pozastaveno / dokonceno / zastaveno / chyba
        self.stav_prevodu = "nezahajeno"
        self.obalka_cesta = None
        self.vysledek_cesta = None
        # Kopie, ne odkaz do DEFAULT_CONFIG - ten je sdílený
        self.sbalene_sekce = dict(self.config_data.get("sbalene_sekce") or {})
        self.nastaveni_otevrene = False
        self.filtr_kapitol = "vse"
        self.pocet_chyb = 0
        self._log_radky = []          # přežijí přestavbu okna
        self._prevod = {}             # čísla právě běžícího převodu pro kartu stavu
        self._stav_text = ""
        self._gui_hotove = False
        self.prehravac = Prehravac(24000, self.log_z_vlakna)

        self._vytvor_promenne()
        self._vytvor_gui()
        self._obnov_z_configu()

        self.protocol("WM_DELETE_WINDOW", self.pri_zavreni)
        self.after(100, self._zpracuj_frontu)
        self.after(250, self._obnov_prehravac)

        self.log(f"{T('app_nazev')} v{VERSION}")
        self.log(T("log_cache", CACHE_DIR))
        if self.font_rodina != FONT_RODINA:
            self.log(T("log_font", self.font_rodina))
        if not najdi_ffmpeg():
            self.log(T("log_ffmpeg"))
        if not Prehravac.dostupny():
            self.log(T("log_sd"))
        # Kniha z minula se načte sama, ať úvodní obrazovka rovnou ukáže, co převede
        if self.var_vstup.get().strip('" ') and Path(self.var_vstup.get().strip('" ')).exists():
            self.after(150, lambda: self.nacti_a_priprav(tise=True))

    def px(self, n: float) -> int:
        """Rozměr z návrhu (px při 100 %) přepočtený na DPI obrazovky."""
        return int(round(n * self._meritko))

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
            "mlx_presnost": self.var_mlx_presnost.get(),
            "seed": int(self.var_seed.get() or 0),
            "jazyk_textu": self._klic_jazyka_textu(),
            "zarizeni": self.var_zarizeni.get(),
            "testovaci_veta": self.var_test_veta.get(),
            "obalka": bool(self.var_obalka.get()),
            "pracovniku": int(self.var_pracovniku.get()),
            "jazyk": aktualni_jazyk(),
            "tema": self.tema,
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
        self._jazyk_stazeny = True
        if j.get("zdroj") != "finetune":
            self.var_jazyk_info.set(T("jaz_zakladni"))
            return

        stazeny = self._stazeny(j)
        self._jazyk_stazeny = stazeny
        velikost = j.get("velikost_gb", 2.1)
        casti = [T("jaz_stazeno_gb", velikost) if stazeny else T("jaz_stahne", velikost)]
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
        self.var_mlx_presnost = tk.StringVar(value=c["mlx_presnost"])
        self.var_seed = tk.IntVar(value=c["seed"])
        self.var_jazyk_textu = tk.StringVar(value=self._nazev_jazyka_textu(c["jazyk_textu"]))
        self.var_zarizeni = tk.StringVar(value=c["zarizeni"])
        self.var_test_veta = tk.StringVar(value=c["testovaci_veta"])
        self.var_obalka = tk.BooleanVar(value=c["obalka"])
        self.var_pracovniku = tk.IntVar(value=c["pracovniku"])

        self.var_stav = tk.StringVar(value=T("stav_pripraveno"))
        self.var_jazyk = tk.StringVar(value=JAZYKY.get(aktualni_jazyk(), "English"))
        self.var_jazyk_info = tk.StringVar(value="")
        self.var_soubor_info = tk.StringVar(value="")

        # Stopy na proměnných, ne na widgetech - okno se při změně jazyka nebo
        # schématu staví znovu a stopa na zničeném widgetu by padala.
        for promenna in (self.var_vstup, self.var_ref_wav, self.var_vystup_slozka,
                         self.var_vystup_nazev, self.var_format, self.var_bitrate,
                         self.var_obalka, self.var_lupance, self.var_orez,
                         self.var_rychly_dekoder, self.var_mlx_presnost,
                         self.var_pauza, self.var_jazyk_textu):
            promenna.trace_add("write", lambda *_a: self._po_zmene_nastaveni())
        self.var_vystup_slozka.trace_add("write", lambda *_a: self._odlozene_rozdelane())

    def _obnov_z_configu(self):
        self._popis_jazyka()
        self._po_zmene_nastaveni()

    # ------------------------------------------------------------------
    #  Sestavení okna
    # ------------------------------------------------------------------
    def _nastav_fonty(self):
        """Velikosti z návrhu (px) převedené na body, ať se škálují s DPI."""
        from tkinter import font as tkfont

        def font(body, tucne=False):
            return tkfont.Font(root=self, family=self.font_rodina, size=body,
                               weight="bold" if tucne else "normal")

        self.F = {"logo": font(11, True), "verze": font(9), "titul": font(15, True),
                  "nadpis": font(11, True), "pole": font(10), "pole_t": font(10, True),
                  "popis": font(9), "maly": font(9), "stitek": font(8), "log": font(9),
                  "odznak": font(8, True), "tlacitko_t": font(10, True), "cislo": font(10)}
        # Dialog opravy úseku a starší kód počítají s těmito jmény
        self.F_BEZNY, self.F_MALY, self.F_TITULEK = self.F["pole"], self.F["popis"], self.F["stitek"]

    def _vytvor_gui(self):
        self._gui_hotove = False
        self.font_rodina = nacti_font()
        self._nastav_pojmenovane_fonty()
        self._nastav_fonty()
        self._nastav_styl()
        b, f, px = BARVY, self.F, self.px
        self.configure(background=b["pozadi"])
        self.zamykatelne = []
        self._okraj, self._mezera = px(18), px(13)

        # ---------------- Hlavička ----------------
        hlavicka = tk.Frame(self, bg=b["panel"], padx=px(18), pady=px(8))
        hlavicka.pack(side="top", fill="x")
        tk.Label(hlavicka, text=T("znacka"), font=f["logo"], bg=b["panel"], fg=b["text"]).pack(side="left")
        tk.Label(hlavicka, text=T("podtitul", VERSION), font=f["verze"], bg=b["panel"],
                 fg=b["tlumeny"]).pack(side="left", padx=(px(12), 0))
        vyber = ttk.Combobox(hlavicka, textvariable=self.var_jazyk, width=9, state="readonly",
                             values=[JAZYKY[k] for k in ("en", "cs")])
        vyber.pack(side="right")
        vyber.bind("<<ComboboxSelected>>", lambda _u: self.zmen_jazyk())
        self.btn_tema = Ikona(hlavicka, "mesic" if self.tema == "svetle" else "slunce",
                              self.prepni_tema, velikost=px(32), bg_rodice=b["panel"])
        self.btn_tema.pack(side="right", padx=(0, px(12)))
        tk.Frame(self, height=1, bg=b["linka"]).pack(side="top", fill="x")

        # ---------------- Posuvná plocha ----------------
        telo = tk.Frame(self, bg=b["pozadi"])
        telo.pack(side="top", fill="both", expand=True)
        self.platno = tk.Canvas(telo, bg=b["pozadi"], highlightthickness=0, bd=0,
                                yscrollincrement=px(20))
        posuv = ttk.Scrollbar(telo, orient="vertical", command=self.platno.yview,
                              style="Tenky.Vertical.TScrollbar")
        self.platno.configure(yscrollcommand=posuv.set)
        posuv.pack(side="right", fill="y")
        self.platno.pack(side="left", fill="both", expand=True)
        self.obsah = tk.Frame(self.platno, bg=b["pozadi"])
        self.obsah.columnconfigure(0, weight=1)
        self._okno_obsahu = self.platno.create_window(0, 0, window=self.obsah, anchor="nw")
        self.obsah.bind("<Configure>",
                        lambda _u: self.platno.configure(scrollregion=self.platno.bbox("all")))
        self.platno.bind("<Configure>", self._pri_zmene_velikosti)
        for udalost in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(udalost, self._kolecko)

        self._vytvor_nastaveni(self.obsah)
        self.oblast_start = tk.Frame(self.obsah, bg=b["pozadi"])
        self.oblast_start.columnconfigure(0, weight=1)
        self._vytvor_start(self.oblast_start)
        self.oblast_prevod = tk.Frame(self.obsah, bg=b["pozadi"])
        self.levy = tk.Frame(self.oblast_prevod, bg=b["pozadi"])
        self.pravy = tk.Frame(self.oblast_prevod, bg=b["pozadi"])
        self.levy.columnconfigure(0, weight=1)
        self.pravy.columnconfigure(0, weight=1)
        self._vytvor_stav(self.levy)
        self._vytvor_prehravac(self.levy)
        self._vytvor_kapitoly(self.pravy)
        # Log je jeden a podle stavu se přesouvá: na úvodní obrazovce pod
        # kartami, během převodu do levého sloupce (grid -in).
        self.karta_log = self._vytvor_log(self.obsah)

        self._gui_hotove = True
        self._rozlozeni = None
        self._podpis_seznamu = None
        self._obnov_log_z_pameti()
        self._prepni_zobrazeni()
        self._po_zmene_nastaveni()
        self._obnov_rozdelane()
        self._obnov_stav()
        if self.obalka_cesta is not None and Path(self.obalka_cesta).exists():
            self.after(0, lambda: self.zobraz_obalku(Path(self.obalka_cesta)))

    # ---------------- Pruh nastavení ----------------
    def _vytvor_nastaveni(self, rodic):
        b, f, px = BARVY, self.F, self.px
        karta = tk.Frame(rodic, bg=b["panel"])
        karta.grid(row=0, column=0, sticky="ew", padx=self._okraj, pady=(px(14), 0))
        karta.columnconfigure(0, weight=1)
        self.karta_nastaveni = karta

        radek = tk.Frame(karta, bg=b["panel"], padx=px(16), pady=px(10), cursor="hand2")
        radek.columnconfigure(1, weight=1)
        self.radek_souhrn = radek
        stitek = tk.Label(radek, text=T("stitek_nastaveni"), font=f["stitek"], bg=b["panel"], fg=b["tlumeny"])
        stitek.grid(row=0, column=0, sticky="w", padx=(0, px(14)))
        self.lbl_souhrn = ZkracenyPopisek(radek, f["popis"], bg=b["panel"], fg=b["text2"])
        self.lbl_souhrn.grid(row=0, column=1, sticky="ew")
        self.lbl_upravit = tk.Label(radek, font=f["popis"], bg=b["panel"], fg=b["akcent"])
        self.lbl_upravit.grid(row=0, column=2, sticky="e", padx=(px(14), 0))
        for w in (radek, stitek, self.lbl_souhrn, self.lbl_upravit):
            w.bind("<Button-1>", lambda _u: self._prepni_nastaveni())

        self.telo_nastaveni = tk.Frame(karta, bg=b["panel"], padx=px(18), pady=px(18))
        self.sloupce_nastaveni = [self._sloupec_zdroj(self.telo_nastaveni),
                                  self._sloupec_hlas(self.telo_nastaveni),
                                  self._sloupec_vystup(self.telo_nastaveni)]

    def _sloupec(self, rodic, stitek: str):
        s = tk.Frame(rodic, bg=BARVY["panel"])
        s.columnconfigure(0, weight=1)
        tk.Label(s, text=stitek, font=self.F["stitek"], bg=BARVY["panel"], fg=BARVY["tlumeny"],
                 anchor="w").grid(row=0, column=0, sticky="w")
        s.radek = 1
        return s

    def _popisek_pole(self, sloupec, text: str):
        tk.Label(sloupec, text=text, font=self.F["popis"], bg=BARVY["panel"], fg=BARVY["tlumeny"],
                 anchor="w").grid(row=sloupec.radek, column=0, sticky="w",
                                  pady=(self.px(14), self.px(6)))
        sloupec.radek += 1

    def _pole(self, sloupec, text: str, vnitrni_okraj=True):
        """Popisek a pod ním podbarvené pole. Vrací rámeček pole."""
        self._popisek_pole(sloupec, text)
        barva = BARVY["pole"] if vnitrni_okraj else BARVY["panel"]
        pole = tk.Frame(sloupec, bg=barva, padx=self.px(11) if vnitrni_okraj else 0,
                        pady=self.px(8) if vnitrni_okraj else 0)
        pole.grid(row=sloupec.radek, column=0, sticky="ew")
        pole.columnconfigure(0, weight=1)
        sloupec.radek += 1
        return pole

    def _radek_pod(self, sloupec, pady=6):
        radek = tk.Frame(sloupec, bg=BARVY["panel"])
        radek.grid(row=sloupec.radek, column=0, sticky="ew", pady=(self.px(pady), 0))
        radek.columnconfigure(0, weight=1)
        sloupec.radek += 1
        return radek

    def _odkaz(self, rodic, text, command, bg=None, font=None):
        return Tlacitko(rodic, text, command, druh="odkaz", font=font or self.F["pole"],
                        bg_rodice=bg or BARVY["panel"], padx=0, pady=0)

    def _sloupec_zdroj(self, rodic):
        b, f, px = BARVY, self.F, self.px
        s = self._sloupec(rodic, T("stitek_zdroj"))
        pole = self._pole(s, T("lab_kniha"))
        self.lbl_kniha = ZkracenyPopisek(pole, f["pole"], bg=b["pole"], fg=b["text"])
        self.lbl_kniha.grid(row=0, column=0, sticky="ew")
        self.odkaz_kniha = self._odkaz(pole, T("btn_zmenit"), self.vyber_vstup, bg=b["pole"])
        self.odkaz_kniha.grid(row=0, column=1, padx=(px(10), 0))
        self.lbl_kniha_cesta = ZkracenyPopisek(self._radek_pod(s), f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_kniha_cesta.grid(row=0, column=0, sticky="ew")

        pole = self._pole(s, T("lab_jazyk_textu"), vnitrni_okraj=False)
        vyber = ttk.Combobox(pole, textvariable=self.var_jazyk_textu, state="readonly",
                             values=self._nabidka_jazyku(), width=1)
        vyber.grid(row=0, column=0, sticky="ew")
        vyber.bind("<<ComboboxSelected>>", lambda _u: self._popis_jazyka())
        self._zamknout(vyber)
        self.lbl_jazyk_info = ZkracenyPopisek(self._radek_pod(s), f["maly"], bg=b["panel"], fg=b["uspech"])
        self.lbl_jazyk_info.grid(row=0, column=0, sticky="ew")
        return s

    def _sloupec_hlas(self, rodic):
        b, f, px = BARVY, self.F, self.px
        s = self._sloupec(rodic, T("stitek_hlas"))
        pole = self._pole(s, T("lab_nahravka"))
        self.lbl_hlas = ZkracenyPopisek(pole, f["pole"], bg=b["pole"], fg=b["text"])
        self.lbl_hlas.grid(row=0, column=0, sticky="ew")
        self.odkaz_hlas = self._odkaz(pole, T("btn_zmenit"), self.vyber_ref_wav, bg=b["pole"])
        self.odkaz_hlas.grid(row=0, column=1, padx=(px(10), 0))
        self.odkaz_hlas_pryc = self._odkaz(pole, "×", lambda: self.var_ref_wav.set(""), bg=b["pole"])
        self.odkaz_hlas_pryc.grid(row=0, column=2, padx=(px(10), 0))
        self.lbl_hlas_cesta = ZkracenyPopisek(self._radek_pod(s), f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_hlas_cesta.grid(row=0, column=0, sticky="ew")

        pole = self._pole(s, T("lab_zkusebni_veta"), vnitrni_okraj=False)
        veta = ttk.Entry(pole, textvariable=self.var_test_veta, width=1)
        veta.grid(row=0, column=0, sticky="ew")
        self._zamknout(veta)
        self.btn_test = Tlacitko(pole, T("btn_prehrat"), self.test_hlasu, font=f["maly"],
                                 padx=px(14), pady=px(8))
        self.btn_test.grid(row=0, column=1, padx=(px(10), 0), sticky="ns")
        return s

    def _sloupec_vystup(self, rodic):
        b, f, px = BARVY, self.F, self.px
        s = self._sloupec(rodic, T("stitek_vystup"))
        pole = self._pole(s, T("lab_nazev_souboru"), vnitrni_okraj=False)
        nazev = ttk.Entry(pole, textvariable=self.var_vystup_nazev, width=1)
        nazev.grid(row=0, column=0, sticky="ew")
        self._zamknout(nazev)
        radek = self._radek_pod(s)
        self.lbl_vystup_cesta = ZkracenyPopisek(radek, f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_vystup_cesta.grid(row=0, column=0, sticky="ew")
        self.odkaz_vystup = self._odkaz(radek, T("btn_zmenit"), self.vyber_vystup, font=f["popis"])
        self.odkaz_vystup.grid(row=0, column=1, padx=(px(10), 0))

        radek = self._radek_pod(s, pady=14)
        radek.columnconfigure(0, weight=0)
        self.seg_wav = tk.Label(radek, text="WAV", font=f["maly"], padx=px(15), pady=px(8), cursor="hand2")
        self.seg_mp3 = tk.Label(radek, text="MP3", font=f["pole_t"], padx=px(15), pady=px(8), cursor="hand2")
        self.seg_wav.grid(row=0, column=0)
        self.seg_mp3.grid(row=0, column=1)
        self.seg_wav.bind("<Button-1>", lambda _u: not self.bezi and self.var_format.set("WAV"))
        self.seg_mp3.bind("<Button-1>", lambda _u: not self.bezi and self.var_format.set("MP3"))
        self.cb_bitrate = ttk.Combobox(radek, textvariable=self.var_bitrate, width=5, state="readonly",
                                       values=["96k", "128k", "160k", "192k"])
        self.cb_bitrate.grid(row=0, column=2, padx=(px(10), 0))
        self._zamknout(self.cb_bitrate)
        self.lbl_velikost = tk.Label(radek, font=f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_velikost.grid(row=0, column=3, padx=(px(10), 0))

        self.ramec_chipu = tk.Frame(s, bg=b["panel"], height=px(30))
        self.ramec_chipu.grid(row=s.radek, column=0, sticky="ew", pady=(px(14), 0))
        s.radek += 1
        self.chipy = [Chip(self.ramec_chipu, T("chip_obalka"), self.var_obalka, font=f["maly"]),
                      Chip(self.ramec_chipu, T("chip_lupance"), self.var_lupance, font=f["maly"]),
                      Chip(self.ramec_chipu, T("chip_orez"), self.var_orez, font=f["maly"]),
                      Chip(self.ramec_chipu, T("chip_rd"), self.var_rychly_dekoder, font=f["maly"]),
                      Chip(self.ramec_chipu, T("chip_pokrocile"), command=self.otevri_pokrocile,
                           font=f["maly"])]
        self.ramec_chipu.bind("<Configure>", lambda _u: self._preskladej_chipy())
        return s

    def _preskladej_chipy(self):
        """Tk nezná zalamování jako flex-wrap, chipy se proto rozmisťují ručně."""
        ramec = self.ramec_chipu
        sirka, mezera = max(ramec.winfo_width(), 1), self.px(8)
        x = y = vyska_radku = 0
        for chip in self.chipy:
            w, h = chip.winfo_reqwidth(), chip.winfo_reqheight()
            if x and x + w > sirka:
                x, y, vyska_radku = 0, y + vyska_radku + mezera, 0
            chip.place(x=x, y=y)
            x += w + mezera
            vyska_radku = max(vyska_radku, h)
        if int(ramec.cget("height")) != y + vyska_radku:
            ramec.configure(height=y + vyska_radku)

    # ---------------- Úvodní obrazovka ----------------
    def _vytvor_start(self, rodic):
        b, f, px = BARVY, self.F, self.px
        karta = tk.Frame(rodic, bg=b["panel"], padx=px(20), pady=px(18))
        karta.grid(row=0, column=0, sticky="ew")
        karta.columnconfigure(0, weight=1)
        self.karta_pripraveno = karta
        self.lbl_pripraveno = tk.Label(karta, font=f["nadpis"], bg=b["panel"], fg=b["text"], anchor="w")
        self.lbl_pripraveno.grid(row=0, column=0, sticky="w")
        self.lbl_pripraveno_info = tk.Label(karta, font=f["popis"], bg=b["panel"], fg=b["tlumeny"],
                                            anchor="w", justify="left")
        self.lbl_pripraveno_info.grid(row=1, column=0, sticky="w", pady=(px(5), 0))
        self.lbl_pripraveno_info.bind("<Configure>", lambda e: self.lbl_pripraveno_info.configure(
            wraplength=max(px(200), e.width)))
        self._odkaz(karta, T("btn_oprava_hotove"), self.oprav_usek, font=f["popis"]).grid(
            row=2, column=0, sticky="w", pady=(px(10), 0))
        self.tlacitka_start = tk.Frame(karta, bg=b["panel"])
        self.btn_jina_kniha = Tlacitko(self.tlacitka_start, T("btn_nacist_jinou"), self.vyber_vstup,
                                       font=f["pole"], padx=px(18), pady=px(11))
        self.btn_jina_kniha.pack(side="left")
        self.btn_start = Tlacitko(self.tlacitka_start, T("btn_start"), self.spust_prevod, druh="plocha",
                                  font=f["tlacitko_t"], padx=px(16), pady=px(11))
        self.btn_start.pack(side="left", padx=(px(9), 0))

        self.karta_rozdelane = tk.Frame(rodic, bg=b["panel"])
        self.karta_rozdelane.columnconfigure(0, weight=1)

    def _odlozene_rozdelane(self):
        if getattr(self, "_rozdelane_po", None):
            self.after_cancel(self._rozdelane_po)
        self._rozdelane_po = self.after(600, self._obnov_rozdelane)

    def _obnov_rozdelane(self):
        """Seznam přerušených převodů na úvodní obrazovce."""
        self._rozdelane_po = None
        if not self._gui_hotove:
            return
        b, f, px = BARVY, self.F, self.px
        karta = self.karta_rozdelane
        for dite in karta.winfo_children():
            dite.destroy()
        slozky = [Path(self.var_vystup_slozka.get().strip('" ') or (APP_DIR / "vystup")), APP_DIR / "vystup"]
        zaznamy = najdi_rozdelane(slozky)
        if not zaznamy:
            karta.grid_remove()
            return
        karta.grid(row=1, column=0, sticky="ew", pady=(px(16), 0))
        tk.Label(karta, text=T("stitek_rozdelane"), font=f["stitek"], bg=b["panel"], fg=b["tlumeny"],
                 padx=px(16), pady=px(12), anchor="w").grid(row=0, column=0, sticky="w")
        for i, z in enumerate(zaznamy[:8], start=1):
            tk.Frame(karta, height=1, bg=b["linka"]).grid(row=2 * i - 1, column=0, sticky="ew")
            radek = tk.Frame(karta, bg=b["panel"], padx=px(18), pady=px(12), cursor="hand2")
            radek.grid(row=2 * i, column=0, sticky="ew")
            radek.columnconfigure(0, weight=1)
            nazev = ZkracenyPopisek(radek, f["pole"], bg=b["panel"], fg=b["text"])
            nazev.nastav(z["nazev"])
            nazev.grid(row=0, column=0, sticky="ew")
            zdroj_chybi = bool(z.get("zdroj")) and not Path(z["zdroj"]).exists()
            popis = T("rozdelane_popis", z["hotovo"], z["celkem"], kdy_text(z["kdy"]))
            popis += T("rozdelane_zdroj_chybi") if zdroj_chybi else ""
            tk.Label(radek, text=popis, font=f["maly"], bg=b["panel"], fg=b["tlumeny"], anchor="w").grid(
                row=1, column=0, sticky="w", pady=(px(3), 0))
            pruh = tk.Canvas(radek, width=px(120), height=px(5), bg=b["stopa"], highlightthickness=0, bd=0)
            pruh.grid(row=0, column=1, rowspan=2, padx=px(16))
            pruh.create_rectangle(0, 0, px(120) * z["procenta"] / 100.0, px(5), fill=b["plocha"], outline="")
            akce = lambda _u=None, z=z: self.pokracuj_v_rozdelanem(z)
            self._odkaz(radek, T("btn_pokracovat_kratce"), akce).grid(row=0, column=2, rowspan=2)
            for w in (radek, nazev):
                w.bind("<Button-1>", akce)

    # ---------------- Karta stavu převodu ----------------
    def _vytvor_stav(self, rodic):
        b, f, px = BARVY, self.F, self.px
        karta = tk.Frame(rodic, bg=b["panel"], padx=px(20), pady=px(18))
        karta.grid(row=0, column=0, sticky="ew")
        karta.columnconfigure(0, weight=1)

        horni = tk.Frame(karta, bg=b["panel"])
        horni.grid(row=0, column=0, sticky="ew")
        horni.columnconfigure(1, weight=1)
        self.platno_obalka = tk.Canvas(horni, width=px(76), height=px(76), bg=b["pole"],
                                       highlightthickness=0, bd=0)
        self.platno_obalka.grid(row=0, column=0, sticky="nw", padx=(0, px(18)))
        stred = tk.Frame(horni, bg=b["panel"])
        stred.grid(row=0, column=1, sticky="new")
        odznak = tk.Frame(stred, bg=b["panel"])
        odznak.pack(anchor="w")
        self.lbl_odznak = tk.Label(odznak, font=f["odznak"], padx=px(10), pady=px(4))
        self.lbl_odznak.pack(side="left")
        self.lbl_stav_popis = tk.Label(odznak, font=f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_stav_popis.pack(side="left", padx=(px(10), 0))
        self.lbl_titul = tk.Label(stred, font=f["titul"], bg=b["panel"], fg=b["text"], anchor="w",
                                  justify="left")
        self.lbl_titul.pack(anchor="w", fill="x", pady=(px(9), 0))
        self.lbl_autor = tk.Label(stred, font=f["popis"], bg=b["panel"], fg=b["tlumeny"], anchor="w")
        self.lbl_autor.pack(anchor="w", pady=(px(4), 0))
        stred.bind("<Configure>", lambda e: self.lbl_titul.configure(wraplength=max(px(120), e.width)))

        self.tlacitka_stavu = tk.Frame(horni, bg=b["panel"])
        self.tlacitka_stavu.grid(row=0, column=2, sticky="ne")
        self.btn_pauza = Tlacitko(self.tlacitka_stavu, T("btn_pauza"), self.prepni_pauzu, font=f["pole_t"],
                                  padx=px(17), pady=px(10))
        self.btn_stop = Tlacitko(self.tlacitka_stavu, T("btn_zastavit"), self.zastav, font=f["pole_t"],
                                 padx=px(17), pady=px(10))
        self.btn_otevrit = Tlacitko(self.tlacitka_stavu, T("btn_otevrit"), self.otevri_vystup,
                                    font=f["pole_t"], padx=px(17), pady=px(10))
        self.btn_novy = Tlacitko(self.tlacitka_stavu, T("btn_novy_prevod"), self.novy_prevod,
                                 font=f["pole_t"], padx=px(17), pady=px(10))

        radek = tk.Frame(karta, bg=b["panel"])
        radek.grid(row=1, column=0, sticky="ew", pady=(px(22), px(9)))
        radek.columnconfigure(1, weight=1)
        self.lbl_generuje = tk.Label(radek, font=f["nadpis"], bg=b["panel"], fg=b["text"])
        self.lbl_generuje.grid(row=0, column=0, sticky="sw")
        self.lbl_kap_nazev = ZkracenyPopisek(radek, f["maly"], bg=b["panel"], fg=b["text2"])
        self.lbl_kap_nazev.grid(row=0, column=1, sticky="sew", padx=px(10))
        self.lbl_procenta = tk.Label(radek, font=f["cislo"], bg=b["panel"], fg=b["text"])
        self.lbl_procenta.grid(row=0, column=2, sticky="se")
        self.platno_postup = tk.Canvas(karta, height=px(9), bg=b["stopa"], highlightthickness=0, bd=0)
        self.platno_postup.grid(row=2, column=0, sticky="ew")
        self.platno_postup.bind("<Configure>", lambda _u: self._kresli_postup())
        casy = tk.Frame(karta, bg=b["panel"])
        casy.grid(row=3, column=0, sticky="ew", pady=(px(9), 0))
        casy.columnconfigure(0, weight=1)
        self.lbl_hotovo = tk.Label(casy, font=f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_hotovo.grid(row=0, column=0, sticky="w")
        self.lbl_zbyva = tk.Label(casy, font=f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_zbyva.grid(row=0, column=1, sticky="e")

        self.ramec_mapa = tk.Frame(karta, bg=b["panel"])
        self.ramec_mapa.grid(row=4, column=0, sticky="ew", pady=(px(20), 0))
        self.ramec_mapa.columnconfigure(0, weight=1)
        self.lbl_mapa = tk.Label(self.ramec_mapa, font=f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_mapa.grid(row=0, column=0, sticky="e", pady=(0, px(8)))
        self.platno_mapa = tk.Canvas(self.ramec_mapa, height=px(26), bg=b["panel"], highlightthickness=0, bd=0)
        self.platno_mapa.grid(row=1, column=0, sticky="ew")
        self.platno_mapa.bind("<Configure>", lambda _u: self._kresli_mapu())
        self.platno_mapa.bind("<Button-1>", self._klik_mapa)

    # ---------------- Přehrávač ----------------
    def _vytvor_prehravac(self, rodic):
        b, f, px = BARVY, self.F, self.px
        karta = tk.Frame(rodic, bg=b["panel"], padx=px(20), pady=px(15))
        karta.grid(row=1, column=0, sticky="ew", pady=(self._mezera, 0))
        karta.columnconfigure(0, weight=1)

        horni = tk.Frame(karta, bg=b["panel"])
        horni.grid(row=0, column=0, sticky="ew")
        horni.columnconfigure(1, weight=1)
        ovladani = tk.Frame(horni, bg=b["panel"])
        ovladani.grid(row=0, column=0, sticky="w")
        v = px(36)
        p = self.prehravac
        self.ik_predchozi = Ikona(ovladani, "predchozi", lambda: self.prehravac.dalsi(-1), velikost=v)
        self.ik_zpet = Ikona(ovladani, "zpet", lambda: self.prehravac.posun(-15), velikost=v, font=f["stitek"])
        self.ik_hrat = Ikona(ovladani, "hrat", lambda: self.prehravac.prepni(), velikost=px(42), kruh=True)
        self.ik_vpred = Ikona(ovladani, "vpred", lambda: self.prehravac.posun(30), velikost=v, font=f["stitek"])
        self.ik_dalsi = Ikona(ovladani, "dalsi", lambda: self.prehravac.dalsi(1), velikost=v)
        for i, ikona in enumerate((self.ik_predchozi, self.ik_zpet, self.ik_hrat, self.ik_vpred, self.ik_dalsi)):
            ikona.grid(row=0, column=i, padx=(0 if i == 0 else px(7), 0))
        del p

        prave = tk.Frame(horni, bg=b["panel"])
        prave.grid(row=0, column=2, sticky="e")
        self.btn_opravit_misto = Tlacitko(prave, T("btn_opravit_misto"), self.oprav_hrane_misto,
                                          font=f["pole"], padx=px(11), pady=px(7))
        self.btn_opravit_misto.grid(row=0, column=0)
        self.btn_zive = Tlacitko(prave, T("btn_zive"), lambda: self.prehravac.na_zive(),
                                 font=f["pole"], padx=px(11), pady=px(7))
        self.btn_zive.grid(row=0, column=1, padx=(px(9), 0))
        self.ramec_ovladani = horni

        self.platno_vlna = tk.Canvas(karta, height=px(42), bg=b["panel"], highlightthickness=0, bd=0,
                                     cursor="hand2")
        self.platno_vlna.grid(row=1, column=0, sticky="ew", pady=(px(14), 0))
        self.platno_vlna.bind("<Button-1>", self._klik_vlna)
        self.platno_vlna.bind("<B1-Motion>", self._klik_vlna)
        popisky = tk.Frame(karta, bg=b["panel"])
        popisky.grid(row=2, column=0, sticky="ew", pady=(px(2), 0))
        popisky.columnconfigure(1, weight=1)
        self.lbl_pozice = tk.Label(popisky, font=f["maly"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_pozice.grid(row=0, column=0, sticky="w")
        self.lbl_vlna_info = ZkracenyPopisek(popisky, f["maly"], bg=b["panel"], fg=b["text2"], anchor="center")
        self.lbl_vlna_info.grid(row=0, column=1, sticky="ew", padx=px(14))
        self.lbl_delka = tk.Label(popisky, font=f["maly"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_delka.grid(row=0, column=2, sticky="e")

    # ---------------- Podrobný průběh ----------------
    def _vytvor_log(self, rodic):
        b, f, px = BARVY, self.F, self.px
        karta = tk.Frame(rodic, bg=b["panel"])
        karta.columnconfigure(0, weight=1)
        hlava = tk.Frame(karta, bg=b["panel"], padx=px(20), pady=px(12), cursor="hand2")
        hlava.grid(row=0, column=0, sticky="ew")
        hlava.columnconfigure(1, weight=1)
        stitek = tk.Label(hlava, text=T("stitek_log"), font=f["stitek"], bg=b["panel"], fg=b["tlumeny"])
        stitek.grid(row=0, column=0, sticky="w")
        self.lbl_log_souhrn = tk.Label(hlava, font=f["popis"], bg=b["panel"], fg=b["tlumeny"])
        self.lbl_log_souhrn.grid(row=0, column=1, sticky="w", padx=(px(10), 0))
        self.lbl_log_prepinac = tk.Label(hlava, font=f["pole"], bg=b["panel"], fg=b["akcent"])
        self.lbl_log_prepinac.grid(row=0, column=2, sticky="e")
        for w in (hlava, stitek, self.lbl_log_souhrn, self.lbl_log_prepinac):
            w.bind("<Button-1>", lambda _u: self._prepni_log())

        self.telo_log = tk.Frame(karta, bg=b["panel"], padx=px(20))
        self.telo_log.columnconfigure(0, weight=1)
        self.log_box = tk.Text(self.telo_log, height=12, wrap="word", state="disabled", font=f["log"],
                               bg=b["pole"], fg=b["tlumeny"], insertbackground=b["text"],
                               selectbackground=b["tlacitko_aktivni"], selectforeground=b["text"],
                               relief="flat", borderwidth=0, highlightthickness=0,
                               padx=px(14), pady=px(12), spacing1=px(2), spacing3=px(2))
        self.log_box.grid(row=0, column=0, sticky="ew")
        posuv = ttk.Scrollbar(self.telo_log, orient="vertical", command=self.log_box.yview,
                              style="TenkyPanel.Vertical.TScrollbar")
        posuv.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=posuv.set)
        self.log_box.tag_configure("cas", foreground=b["cas_logu"])
        self.log_box.tag_configure("bezny", foreground=b["tlumeny"])
        self.log_box.tag_configure("chyba", foreground=b["chyba"])
        self.log_box.tag_configure("varovani", foreground=b["varovani"])
        self.log_box.tag_configure("uspech", foreground=b["uspech"])
        self.log_otevreny = not bool(self.sbalene_sekce.get("log", True))
        self._vykresli_log_prepinac()
        return karta

    # ---------------- Seznam kapitol ----------------
    def _vytvor_kapitoly(self, rodic):
        b, f, px = BARVY, self.F, self.px
        karta = tk.Frame(rodic, bg=b["panel"])
        karta.grid(row=0, column=0, sticky="new")
        karta.columnconfigure(0, weight=1)
        hlava = tk.Frame(karta, bg=b["panel"], padx=px(16), pady=px(12))
        hlava.grid(row=0, column=0, sticky="ew")
        hlava.columnconfigure(0, weight=1)
        tk.Label(hlava, text=T("stitek_kapitoly"), font=f["stitek"], bg=b["panel"], fg=b["tlumeny"]).grid(
            row=0, column=0, sticky="w")
        pilulky = tk.Frame(hlava, bg=b["panel"])
        pilulky.grid(row=0, column=1, sticky="e")
        self.pil_vse = tk.Label(pilulky, text=T("filtr_kap_vse"), font=f["maly"], padx=px(11), pady=px(5),
                                cursor="hand2")
        self.pil_poslech = tk.Label(pilulky, text=T("filtr_kap_poslech"), font=f["maly"], padx=px(11),
                                    pady=px(5), cursor="hand2")
        self.pil_vse.grid(row=0, column=0)
        self.pil_poslech.grid(row=0, column=1, padx=(px(6), 0))
        self.pil_vse.bind("<Button-1>", lambda _u: self._nastav_filtr("vse"))
        self.pil_poslech.bind("<Button-1>", lambda _u: self._nastav_filtr("poslech"))
        tk.Frame(karta, height=1, bg=b["linka"]).grid(row=1, column=0, sticky="ew")
        telo = tk.Frame(karta, bg=b["panel"])
        telo.grid(row=2, column=0, sticky="ew")
        telo.columnconfigure(0, weight=1)
        self.platno_kap = tk.Canvas(telo, bg=b["panel"], highlightthickness=0, bd=0, height=px(44),
                                    yscrollincrement=px(22))
        self.platno_kap.grid(row=0, column=0, sticky="ew")
        self.posuv_kap = ttk.Scrollbar(telo, orient="vertical", command=self.platno_kap.yview,
                                       style="TenkyPanel.Vertical.TScrollbar")
        self.posuv_kap.grid(row=0, column=1, sticky="ns")
        self.platno_kap.configure(yscrollcommand=self.posuv_kap.set)
        self.platno_kap.bind("<Configure>", lambda _u: self._kresli_kapitoly())
        self.platno_kap.bind("<Button-1>", self._klik_kapitola)
        self.karta_kapitoly = karta
        self._obarvi_filtr()

    # ------------------------------------------------------------------
    #  Rozvržení podle šířky okna
    # ------------------------------------------------------------------
    def _pri_zmene_velikosti(self, udalost=None):
        sirka = udalost.width if udalost is not None else self.platno.winfo_width()
        self.platno.itemconfigure(self._okno_obsahu, width=sirka)
        self._rozloz(sirka)

    def _rozloz(self, sirka_okna: int):
        px = self.px
        sirka = sirka_okna - 2 * self._okraj
        sloupcu = 3 if sirka >= px(876) else (2 if sirka >= px(578) else 1)
        seznam = len(self.kapitoly) > 1
        dva = seznam and sirka >= px(918)
        uzky = sirka < px(620)
        klic = (sloupcu, dva, seznam, uzky, self.stav_prevodu, self.nastaveni_otevrene)
        if klic == self._rozlozeni:
            return
        self._rozlozeni = klic

        telo = self.telo_nastaveni
        for i in range(3):
            telo.columnconfigure(i, weight=1 if i < sloupcu else 0, uniform="nastaveni" if i < sloupcu else "")
        for i, sloupec in enumerate(self.sloupce_nastaveni):
            radek, sl = divmod(i, sloupcu)
            sloupec.grid(row=radek, column=sl, sticky="new",
                         padx=(0 if sl == 0 else px(18), 0), pady=(0 if radek == 0 else px(22), 0))

        if uzky:
            self.tlacitka_start.grid(row=3, column=0, rowspan=1, sticky="w", padx=0, pady=(px(14), 0))
            self.tlacitka_stavu.grid(row=1, column=1, columnspan=2, sticky="w", pady=(px(12), 0))
        else:
            self.tlacitka_start.grid(row=0, column=1, rowspan=3, sticky="e", padx=(px(18), 0), pady=0)
            self.tlacitka_stavu.grid(row=0, column=2, columnspan=1, sticky="ne", pady=0)

        oblast = self.oblast_prevod
        if dva:
            oblast.columnconfigure(0, weight=3, minsize=px(420))
            oblast.columnconfigure(1, weight=2, minsize=px(300))
            self.levy.grid(row=0, column=0, sticky="new", pady=0)
            self.pravy.grid(row=0, column=1, sticky="new", padx=(px(18), 0), pady=0)
        else:
            oblast.columnconfigure(0, weight=1, minsize=0)
            oblast.columnconfigure(1, weight=0, minsize=0)
            self.levy.grid(row=0, column=0, sticky="new", pady=0)
            if seznam:
                self.pravy.grid(row=1, column=0, sticky="new", padx=0, pady=(self._mezera, 0))
            else:
                self.pravy.grid_remove()
        # Tlačítka vedle ovládání přehrávače se na úzkém okně přesunou pod něj
        prave = self.btn_zive.master
        if uzky:
            prave.grid(row=1, column=0, columnspan=3, sticky="w", pady=(px(10), 0))
        else:
            prave.grid(row=0, column=2, columnspan=1, sticky="e", pady=0)

    def _prepni_zobrazeni(self):
        """Co okno ukáže: úvodní obrazovku, nebo převod s přehrávačem."""
        px = self.px
        start = self.stav_prevodu == "nezahajeno"
        if start:
            self.oblast_prevod.grid_remove()
            self.oblast_start.grid(row=1, column=0, sticky="ew", padx=self._okraj, pady=(px(16), 0))
            self.karta_log.grid(in_=self.obsah, row=2, column=0, sticky="ew", padx=self._okraj,
                                pady=(px(16), px(22)))
            self.radek_souhrn.grid_remove()
            self.telo_nastaveni.grid(row=1, column=0, sticky="ew")
        else:
            self.oblast_start.grid_remove()
            self.oblast_prevod.grid(row=1, column=0, sticky="ew", padx=self._okraj, pady=(px(14), px(22)))
            self.karta_log.grid(in_=self.levy, row=2, column=0, sticky="ew", padx=0,
                                pady=(self._mezera, 0))
            self.radek_souhrn.grid(row=0, column=0, sticky="ew")
            if self.nastaveni_otevrene:
                self.telo_nastaveni.grid(row=1, column=0, sticky="ew")
            else:
                self.telo_nastaveni.grid_remove()
        self.lbl_upravit.configure(text=T("odkaz_sbalit") if self.nastaveni_otevrene else T("odkaz_upravit"))
        self._rozlozeni = None
        self._rozloz(max(self.platno.winfo_width(), self.px(1000)) if not self.platno.winfo_ismapped()
                     else self.platno.winfo_width())

    def _prepni_nastaveni(self):
        if self.stav_prevodu == "nezahajeno":
            return
        self.nastaveni_otevrene = not self.nastaveni_otevrene
        self._prepni_zobrazeni()

    def _prepni_log(self):
        self.log_otevreny = not self.log_otevreny
        self.sbalene_sekce["log"] = not self.log_otevreny
        self._vykresli_log_prepinac()

    def _vykresli_log_prepinac(self):
        if self.log_otevreny:
            self.telo_log.grid(row=1, column=0, sticky="ew", pady=(0, self.px(15)))
            self.log_box.see("end")
        else:
            self.telo_log.grid_remove()
        self.lbl_log_prepinac.configure(text=T("odkaz_skryt") if self.log_otevreny else T("odkaz_zobrazit"))

    def _kolecko(self, udalost):
        """Kolečko posouvá seznam kapitol, když je nad ním, jinak celé okno."""
        if not self._gui_hotove:
            return
        widget = self.winfo_containing(udalost.x_root, udalost.y_root)
        if widget is None or widget.winfo_toplevel() is not self:
            return
        if isinstance(widget, (tk.Text, tk.Listbox)):
            return                      # log se posouvá sám
        if getattr(udalost, "num", 0) == 4 or getattr(udalost, "delta", 0) > 0:
            krok = -1
        else:
            krok = 1
        cil = self.platno
        if widget is self.platno_kap and self.posuv_kap.winfo_ismapped():
            cil = self.platno_kap
        if cil.yview() != (0.0, 1.0):
            cil.yview_scroll(krok * 3, "units")

    # ------------------------------------------------------------------
    #  Vzhled
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

    def _nastav_styl(self):
        """ttk podle schématu. Základem je 'clam' - jediné téma, které se dá plně přebarvit."""
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        b, px = BARVY, self.px
        poz, panel, pole = b["pozadi"], b["panel"], b["pole"]
        text, tlumeny, akcent, linka = b["text"], b["tlumeny"], b["akcent"], b["linka"]

        # clam kreslí 3D okraje přes bordercolor/lightcolor/darkcolor. Dokud
        # nejsou srovnané s pozadím, svítí kolem každého pole světlý rámeček.
        s.configure(".", background=poz, foreground=text, font=self.F["pole"],
                    borderwidth=0, focuscolor=poz, relief="flat",
                    bordercolor=poz, lightcolor=poz, darkcolor=poz, troughcolor=pole)
        s.configure("TFrame", background=poz)
        s.configure("TLabel", background=poz, foreground=text, font=self.F["pole"])
        s.configure("Tlumeny.TLabel", foreground=tlumeny, font=self.F["popis"])
        s.configure("Hodnota.TLabel", foreground=akcent, font=self.F["popis"])

        # Tlačítka dialogu opravy úseku
        s.configure("Tichy.TButton", background=b["tlacitko"], foreground=text,
                    borderwidth=0, relief="flat", padding=(px(14), px(7)), font=self.F["pole"])
        s.map("Tichy.TButton",
              background=[("pressed", linka), ("active", b["tlacitko_aktivni"]), ("disabled", poz)],
              foreground=[("disabled", tlumeny)])
        s.configure("Akce.TButton", background=b["plocha"], foreground=b["na_plose"],
                    borderwidth=0, relief="flat", padding=(px(18), px(7)), font=self.F["tlacitko_t"])
        s.map("Akce.TButton",
              background=[("pressed", b["plocha_aktivni"]), ("active", b["plocha_aktivni"]),
                          ("disabled", b["stopa2"])],
              foreground=[("disabled", b["na_plose"])])

        for jmeno in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(jmeno, fieldbackground=pole, background=pole, foreground=text,
                        insertcolor=text, arrowcolor=tlumeny, borderwidth=0,
                        relief="flat", padding=(px(11), px(8)), selectbackground=b["tlacitko_aktivni"],
                        selectforeground=text, arrowsize=px(9),
                        bordercolor=pole, lightcolor=pole, darkcolor=pole, troughcolor=pole)
            s.map(jmeno,
                  fieldbackground=[("readonly", pole), ("disabled", pole)],
                  foreground=[("disabled", tlumeny)],
                  bordercolor=[("focus", pole)],
                  lightcolor=[("focus", pole)],
                  arrowcolor=[("active", text)])

        # Rozbalovací seznam comboboxu je klasický tk widget, styl na něj neplatí
        self.option_add("*TCombobox*Listbox.background", panel)
        self.option_add("*TCombobox*Listbox.foreground", text)
        self.option_add("*TCombobox*Listbox.selectBackground", b["plocha"])
        self.option_add("*TCombobox*Listbox.selectForeground", b["na_plose"])
        self.option_add("*TCombobox*Listbox.borderWidth", 0)
        self.option_add("*TCombobox*Listbox.font", self.F["pole"])

        s.configure("Horizontal.TScale", background=b["plocha"], troughcolor=pole,
                    borderwidth=0, sliderthickness=px(14), sliderrelief="flat", gripcount=0,
                    bordercolor=pole, lightcolor=b["plocha"], darkcolor=b["plocha"])
        s.map("Horizontal.TScale", background=[("active", b["plocha_aktivni"]), ("disabled", b["stopa2"])])

        for jmeno, zlab in (("Tenky.Vertical.TScrollbar", poz), ("TenkyPanel.Vertical.TScrollbar", panel)):
            s.configure(jmeno, background=b["stopa2"], troughcolor=zlab, bordercolor=zlab,
                        arrowcolor=zlab, borderwidth=0, arrowsize=1, width=px(6))
            s.map(jmeno, background=[("active", tlumeny)])

    def prepni_tema(self):
        self.tema = nastav_paletu("svetle" if self.tema == "tmave" else "tmave")
        self._uloz_config()
        self._prestav_okno()

    def zmen_jazyk(self):
        """Přepne jazyk rozhraní a postaví okno znovu."""
        nazev = self.var_jazyk.get()
        kod = next((k for k, v in JAZYKY.items() if v == nazev), "en")
        if kod == aktualni_jazyk():
            return
        nastav_jazyk(kod)
        self._uloz_config()
        self._prestav_okno()
        self.log(T("log_jazyk", JAZYKY[kod]))

    def _prestav_okno(self):
        """Widgety si texty i barvy drží v sobě. Postavit okno znovu je jednodušší
        i spolehlivější než přebarvovat každý zvlášť - proměnné, přehrávač
        i běžící převod to přežijí."""
        for udalost in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_all(udalost)
        for potomek in self.winfo_children():
            potomek.destroy()
        self._vytvor_gui()
        self._obnov_z_configu()
        self.title(f"{T('app_nazev')} v{VERSION}")
        if self.bezi:
            self._zamkni_ovladani(True)

    # ------------------------------------------------------------------
    #  Zamykání a obsah nastavení
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
        # Během převodu jsou pole jen ke čtení a volby výběru zmizí
        for odkaz in (self.odkaz_kniha, self.odkaz_hlas, self.odkaz_vystup):
            if zamknout:
                odkaz.grid_remove()
            else:
                odkaz.grid()
        for chip in self.chipy[:-1]:
            chip.povol(not zamknout)
        self.btn_test.povol(not zamknout)
        self._po_zmene_nastaveni()

    def _po_zmene_nastaveni(self):
        """Promítne proměnné do popisků, souhrnu a odhadů. Volá se ze stop proměnných."""
        if not self._gui_hotove:
            return
        b = BARVY
        kniha = self.var_vstup.get().strip('" ')
        hlas = self.var_ref_wav.get().strip('" ')
        slozka = self.var_vystup_slozka.get().strip('" ')
        mp3 = self.var_format.get() == "MP3"

        self.lbl_kniha.nastav(Path(kniha).name if kniha else T("zadna_kniha"))
        self.lbl_kniha.configure(fg=b["text"] if kniha else b["tlumeny"])
        self.lbl_kniha_cesta.nastav(str(Path(kniha).parent) if kniha else "")
        self.lbl_hlas.nastav(Path(hlas).name if hlas else T("vychozi_hlas"))
        self.lbl_hlas.configure(fg=b["text"] if hlas else b["tlumeny"])
        self.lbl_hlas_cesta.nastav(str(Path(hlas).parent) if hlas else T("hint_hlas"))
        if hlas and not self.bezi:
            self.odkaz_hlas_pryc.grid()
        else:
            self.odkaz_hlas_pryc.grid_remove()
        self.lbl_vystup_cesta.nastav(slozka)
        self.lbl_jazyk_info.nastav(self.var_jazyk_info.get())
        self.lbl_jazyk_info.configure(fg=b["uspech"] if getattr(self, "_jazyk_stazeny", True) else b["tlumeny"])

        zapnuty, vypnuty = (b["plocha"], b["na_plose"]), (b["pole"], b["tlumeny"])
        for seg, aktivni in ((self.seg_wav, not mp3), (self.seg_mp3, mp3)):
            bg, fg = zapnuty if aktivni else vypnuty
            seg.configure(bg=bg, fg=fg, font=self.F["pole_t"] if aktivni else self.F["maly"])
        if mp3:
            self.cb_bitrate.grid()
        else:
            self.cb_bitrate.grid_remove()
        odhad = self._odhad_audia_s()
        if odhad:
            bajtu_za_s = (int(self.var_bitrate.get().rstrip("k") or 128) * 1000 / 8.0) if mp3 else 24000 * 2
            self.lbl_velikost.configure(text=T("velikost_odhad", round(odhad * bajtu_za_s / 1024 ** 2)))
        else:
            self.lbl_velikost.configure(text="")
        for chip in self.chipy:
            chip.obarvi()

        vystup = f"MP3 {self.var_bitrate.get()}" if mp3 else "WAV"
        casti = [Path(kniha).name if kniha else T("zadna_kniha"),
                 Path(hlas).name if hlas else T("vychozi_hlas"), vystup]
        self.lbl_souhrn.nastav("   ·   ".join(casti))

        if self.bloky:
            znaku = sum(len(t) for _, t in self.bloky)
            self.lbl_pripraveno.configure(text=T("pripraveno"))
            self.lbl_pripraveno_info.configure(text=T(
                "pripraveno_info", Path(kniha).name, f"{znaku:,}".replace(",", " "), len(self.bloky),
                lidsky_cas(odhad)))
            self.btn_jina_kniha.text(T("btn_nacist_jinou"))
            self.btn_start.povol(True)
        else:
            self.lbl_pripraveno.configure(text=T("vyberte_knihu"))
            self.lbl_pripraveno_info.configure(text=self.var_soubor_info.get() or T("vyberte_knihu_info"))
            self.btn_jina_kniha.text(T("btn_vybrat_knihu"))
            self.btn_start.povol(False)

    def _odhad_audia_s(self) -> float:
        return sum(self._odhady_kapitol())

    def _odhady_kapitol(self) -> list:
        """Délka kapitol podle textu - čte se 13,5 znaku za sekundu plus pauzy."""
        pauza = max(0, int(self.var_pauza.get() or 0)) / 1000.0
        odhady = [0.0] * len(self.kapitoly)
        for kap_i, text in self.bloky:
            odhady[kap_i] += len(text) / 13.5 + pauza
        return odhady

    def otevri_pokrocile(self):
        dialog = DialogPokrocile(self, zamceno=self.bezi)
        self.wait_window(dialog)
        self._po_zmene_nastaveni()

    # ------------------------------------------------------------------
    #  Karta stavu
    # ------------------------------------------------------------------
    def _obnov_stav(self):
        if not self._gui_hotove or self.stav_prevodu == "nezahajeno":
            return
        b, stav, p = BARVY, self.stav_prevodu, self._prevod
        odznaky = {"bezi": ("odznak_prevadi", b["plocha"]), "pozastaveno": ("odznak_pauza", b["varovani"]),
                   "dokonceno": ("odznak_hotovo", b["uspech"]), "zastaveno": ("odznak_zastaveno", b["stopa2"]),
                   "chyba": ("odznak_chyba", b["chyba"])}
        klic, barva = odznaky[stav]
        self.lbl_odznak.configure(text=T(klic), bg=barva,
                                  fg=b["text"] if stav == "zastaveno" else b["na_plose"])
        if stav in ("bezi", "pozastaveno"):
            if self.stop_event.is_set():
                popis = T("stav_zastavuji")
            elif self._stav_text == T("stav_mp3"):
                popis = self._stav_text
            else:
                popis = T("uplynulo", kratky_cas(time.time() - p.get("start", time.time())))
        else:
            popis = {"dokonceno": T("ulozeno_ve_vystupu"), "zastaveno": T("lze_navazat"),
                     "chyba": T("stav_chyba")}[stav]
        self.lbl_stav_popis.configure(text=popis)
        self.lbl_titul.configure(text=self.titul_knihy or self.nazev_knihy)
        self.lbl_autor.configure(text=self.autor_knihy)

        for tlacitko in self.tlacitka_stavu.winfo_children():
            tlacitko.pack_forget()
        if stav in ("bezi", "pozastaveno"):
            self.btn_pauza.text(T("btn_pokracovat") if stav == "pozastaveno" else T("btn_pauza"))
            self.btn_pauza.pack(side="left")
            self.btn_stop.pack(side="left", padx=(self.px(9), 0))
        else:
            self.btn_otevrit.pack(side="left")
            self.btn_novy.pack(side="left", padx=(self.px(9), 0))

        hotovo, celkem = p.get("hotovo", 0), max(1, p.get("celkem", len(self.bloky)) or 1)
        kap = p.get("kapitola", 0)
        n = len(self.kapitoly)
        if n > 1:
            if stav in ("bezi", "pozastaveno"):
                self.lbl_generuje.configure(text=T("generuje_kapitolu", kap + 1, n))
            else:
                self.lbl_generuje.configure(text=T("kapitola_x_z", min(kap + 1, n), n))
            self.lbl_kap_nazev.nastav(self.kapitoly[kap]["nazev"] if 0 <= kap < n else "")
        else:
            self.lbl_generuje.configure(text=T("generuje_blok", hotovo, celkem) if stav in ("bezi", "pozastaveno")
                                        else T("bloky_x_z", hotovo, celkem))
            self.lbl_kap_nazev.nastav("")
        self.lbl_procenta.configure(text=T("procent_knihy", int(100 * hotovo / celkem)))

        vygenerovano = odhad = 0.0
        odhady = self._odhady_kapitol()
        for i in range(n):
            info = self.prehravac.kapitola(i)
            delka = (info["do"] - info["od"]) / float(self.prehravac.sr) if info and info["do"] > info["od"] else 0.0
            vygenerovano += delka
            odhad += delka if info and info["hotova"] else max(delka, odhady[i] if i < len(odhady) else 0.0)
        self.lbl_hotovo.configure(text=T("hotovo_z", lidsky_cas(vygenerovano), lidsky_cas(odhad)))
        zbyva = p.get("zbyva", -1)
        self.lbl_zbyva.configure(text=T("zbyva", lidsky_cas(zbyva)) if stav == "bezi" and zbyva > 0 else "")
        self._kresli_postup()
        self._kresli_mapu()

    def _kresli_postup(self):
        c = self.platno_postup
        c.delete("all")
        p = self._prevod
        podil = p.get("hotovo", 0) / float(max(1, p.get("celkem", 1) or 1))
        c.create_rectangle(0, 0, c.winfo_width() * min(1.0, podil), c.winfo_height(),
                           fill=BARVY["plocha"], outline="")

    def _konec_kapitoly(self, kap_i: int) -> int:
        """Kolik bloků knihy je hotovo, když skončí kapitola kap_i."""
        if kap_i + 1 < len(self.kapitoly):
            return self.kapitoly[kap_i + 1]["prvni_blok"]
        return len(self.bloky)

    def _kresli_mapu(self):
        """Proužek za každou kapitolu: hotová plná, převáděná vyšší s výplní podle postupu."""
        n = len(self.kapitoly)
        if n <= 1:
            self.ramec_mapa.grid_remove()
            return
        self.ramec_mapa.grid()
        b, px, c = BARVY, self.px, self.platno_mapa
        c.delete("all")
        sirka, vyska = max(c.winfo_width(), 1), px(26)
        mezera = px(2) if sirka / n > px(6) else (1 if sirka / n > 2 else 0)
        w = (sirka - mezera * (n - 1)) / float(n)
        kap_b = self._prevod.get("kapitola", -1)
        generuje = self.stav_prevodu in ("bezi", "pozastaveno")
        hotovych = 0
        for i in range(n):
            x0 = i * (w + mezera)
            info = self.prehravac.kapitola(i)
            hotova = bool(info and info["hotova"])
            hotovych += hotova
            if generuje and i == kap_b:
                prvni = self.kapitoly[i]["prvni_blok"]
                bloku = max(1, self._konec_kapitoly(i) - prvni)
                podil = min(1.0, max(0.0, (self._prevod.get("hotovo", 0) - prvni) / float(bloku)))
                c.create_rectangle(x0, 0, x0 + w, vyska, fill=b["stopa2"], outline="")
                c.create_rectangle(x0, vyska * (1 - podil), x0 + w, vyska, fill=b["plocha"], outline="")
            else:
                c.create_rectangle(x0, vyska - px(16), x0 + w, vyska,
                                   fill=b["plocha"] if hotova else b["stopa"], outline="")
        self.lbl_mapa.configure(text=T("mapa_kapitol", hotovych, n))
        self._sirka_pruzku = (w, mezera)

    def _klik_mapa(self, udalost):
        n = len(self.kapitoly)
        w, mezera = getattr(self, "_sirka_pruzku", (0, 0))
        if n <= 1 or w <= 0:
            return
        i = min(n - 1, int(udalost.x // (w + mezera)))
        info = self.prehravac.kapitola(i)
        if info and Prehravac._ma_zvuk(info):
            self.prehravac.vyber(i)

    # ------------------------------------------------------------------
    #  Přehrávač a seznam kapitol
    # ------------------------------------------------------------------
    def _obnov_prehravac(self):
        """Čtyřikrát za sekundu: vlna, pozice, ikona a seznam, když se změnil."""
        if self._gui_hotove and self.stav_prevodu != "nezahajeno":
            st = self.prehravac.stav()
            self.ik_hrat.nastav("pauza" if st["hraje"] else "hrat")
            self._kresli_vlnu(st)
            podpis = (st["kapitola"], st["hraje"], self.filtr_kapitol, self.prehravac.podpis())
            if podpis != self._podpis_seznamu:
                self._podpis_seznamu = podpis
                self._kresli_kapitoly()
            rostouci = any(k[3] for k in podpis[3])
            self.btn_zive.povol(rostouci)
            self.btn_opravit_misto.povol(not self.bezi and st["kapitola"] >= 0)
            for ikona in (self.ik_predchozi, self.ik_zpet, self.ik_vpred, self.ik_dalsi):
                ikona.povol(st["kapitola"] >= 0)
            self._tiky = getattr(self, "_tiky", 0) + 1
            if self._tiky % 4 == 0 and self.stav_prevodu in ("bezi", "pozastaveno"):
                self._obnov_stav()
        self.after(250, self._obnov_prehravac)

    def _kresli_vlnu(self, st: dict):
        b, px, c = BARVY, self.px, self.platno_vlna
        c.delete("all")
        sirka, vyska = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        kap = st["kapitola"]
        if kap < 0:
            c.create_text(sirka / 2, vyska / 2, text=T("prehravac_prazdny"), fill=b["tlumeny"], font=self.F["maly"])
            for popisek in (self.lbl_pozice, self.lbl_delka):
                popisek.configure(text="")
            self.lbl_vlna_info.nastav("")
            return
        sloupec, mezera = max(2, px(3)), 1
        pocet = max(10, sirka // (sloupec + mezera))
        vysky, dostupne = sloupce_vlny(st["obalka"], st["okno"], st["dostupno"], st["osa"], pocet)
        pozice = st["pozice_s"] * self.prehravac.sr
        osa = max(1, st["osa"])
        stred = vyska / 2.0
        for j, (h, ma) in enumerate(zip(vysky, dostupne)):
            x = j * (sloupec + mezera)
            if not ma:
                v, barva = px(4), b["stopa"]
            else:
                v = px(6) + h * (vyska - px(6))
                barva = b["plocha"] if (j + 0.5) / pocet * osa <= pozice else b["zasoba"]
            c.create_rectangle(x, stred - v / 2, x + sloupec, stred + v / 2, fill=barva, outline="")
        xp = pozice / osa * pocet * (sloupec + mezera)
        c.create_rectangle(xp - 1, 0, xp + 1, vyska, fill=b["text"], outline="")
        self._geometrie_vlny = (pocet, sloupec + mezera)

        nazev = self.kapitoly[kap]["nazev"] if kap < len(self.kapitoly) else ""
        info = T("vlna_info", kap + 1, nazev or T("kapitola_n", kap + 1))
        if st["nacita"]:
            info = T("prehravac_nacita")
        elif st["roste"]:
            info += T("vlna_vygenerovano", formatuj_cas(st["dostupno"] / float(self.prehravac.sr)))
        self.lbl_pozice.configure(text=formatuj_cas(st["pozice_s"]))
        self.lbl_vlna_info.nastav(info)
        self.lbl_delka.configure(text=("~" if st["roste"] else "") + formatuj_cas(osa / float(self.prehravac.sr)))

    def _klik_vlna(self, udalost):
        pocet, krok = getattr(self, "_geometrie_vlny", (0, 0))
        if pocet and krok:
            self.prehravac.skoc(max(0.0, udalost.x / float(pocet * krok)))

    def _nastav_filtr(self, filtr: str):
        self.filtr_kapitol = filtr
        self._obarvi_filtr()
        self.platno_kap.yview_moveto(0)
        self._kresli_kapitoly()

    def _obarvi_filtr(self):
        b = BARVY
        for pilulka, klic in ((self.pil_vse, "vse"), (self.pil_poslech, "poslech")):
            aktivni = self.filtr_kapitol == klic
            pilulka.configure(bg=b["plocha"] if aktivni else b["pole"],
                              fg=b["na_plose"] if aktivni else b["tlumeny"])

    def _radky_kapitol(self) -> list:
        st = self.prehravac.stav()
        radky = []
        for i, kap in enumerate(self.kapitoly):
            info = self.prehravac.kapitola(i)
            ma_zvuk = bool(info and Prehravac._ma_zvuk(info))
            if info and info["roste"]:
                stav = "generuje"
            elif ma_zvuk:
                stav = "hotova"
            else:
                stav = "ceka"
            if self.filtr_kapitol == "poslech" and not ma_zvuk:
                continue
            delka = (info["do"] - info["od"]) / float(self.prehravac.sr) if ma_zvuk and info["do"] > info["od"] else None
            radky.append({"i": i, "nazev": kap["nazev"] or T("kapitola_n", i + 1), "stav": stav,
                          "delka": delka, "aktivni": i == st["kapitola"], "hraje": st["hraje"],
                          "ma_zvuk": ma_zvuk})
        return radky

    def _kresli_kapitoly(self):
        if not self._gui_hotove:
            return
        b, f, px, c = BARVY, self.F, self.px, self.platno_kap
        c.delete("all")
        sirka, vr = max(c.winfo_width(), 10), px(44)
        self._radky_seznamu = self._radky_kapitol()
        for r, p in enumerate(self._radky_seznamu):
            y = r * vr
            if p["aktivni"]:
                c.create_rectangle(0, y, sirka, y + vr, fill=b["aktivni_radek"], outline="")
                c.create_rectangle(0, y, px(3), y + vr, fill=b["plocha"], outline="")
            c.create_text(px(18), y + vr / 2, text=f"{p['i'] + 1:02d}", anchor="w", font=f["maly"], fill=b["tlumeny"])
            font_nazvu = f["pole_t"] if p["aktivni"] else f["pole"]
            nazev = zkrat_text(p["nazev"], font_nazvu, sirka - px(50) - px(44))
            barva_nazvu = b["text"] if p["stav"] != "ceka" else b["tlumeny"]
            if p["stav"] == "generuje":
                stav_text = T("kap_generuje", formatuj_cas(p["delka"] or 0))
            elif p["stav"] == "hotova":
                stav_text = formatuj_cas(p["delka"]) if p["delka"] else "—"
            else:
                stav_text = ""
            if stav_text:
                c.create_text(px(50), y + px(15), text=nazev, anchor="w", font=font_nazvu, fill=barva_nazvu)
                c.create_text(px(50), y + px(31), text=stav_text, anchor="w", font=f["maly"],
                              fill=b["akcent"] if p["stav"] == "generuje" else b["tlumeny"])
            else:
                c.create_text(px(50), y + vr / 2, text=nazev, anchor="w", font=font_nazvu, fill=barva_nazvu)
            if p["ma_zvuk"]:
                x, yc = sirka - px(24), y + vr / 2
                barva = b["akcent"] if p["aktivni"] else b["tlumeny"]
                if p["aktivni"] and p["hraje"]:
                    for dx in (0, px(6)):
                        c.create_rectangle(x + dx, yc - px(5), x + dx + px(3), yc + px(5), fill=barva, outline="")
                else:
                    c.create_polygon(x, yc - px(5), x, yc + px(5), x + px(8), yc, fill=barva, outline="")
            c.create_line(0, y + vr - 1, sirka, y + vr - 1, fill=b["linka"])
        celkem = len(self._radky_seznamu) * vr
        vyska = min(px(620), max(vr, celkem))
        c.configure(scrollregion=(0, 0, sirka, max(celkem, 1)))
        if int(float(c.cget("height"))) != vyska:
            c.configure(height=vyska)
        if celkem > vyska:
            self.posuv_kap.grid()
        else:
            self.posuv_kap.grid_remove()

    def _klik_kapitola(self, udalost):
        r = int(self.platno_kap.canvasy(udalost.y) // self.px(44))
        radky = getattr(self, "_radky_seznamu", [])
        if 0 <= r < len(radky) and radky[r]["ma_zvuk"]:
            st = self.prehravac.stav()
            if st["kapitola"] == radky[r]["i"]:
                self.prehravac.prepni()
            else:
                self.prehravac.vyber(radky[r]["i"])

    def oprav_hrane_misto(self):
        st = self.prehravac.stav()
        if self.bezi or st["kapitola"] < 0 or not st["cesta"]:
            return
        # U knihy v jednom souboru je kapitola jen úsek, čas se počítá od začátku souboru
        self.oprav_usek(Path(st["cesta"]), st["pozice_s"] + st["od"] / float(self.prehravac.sr))

    def zobraz_obalku(self, cesta: Path):
        """Vykreslí vygenerovanou obálku do karty stavu."""
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return
        v = self.px(76)
        obr = Image.open(str(cesta)).resize((v, v), Image.LANCZOS)
        self._obalka_foto = ImageTk.PhotoImage(obr, master=self)   # nesmí ji sebrat GC
        self.platno_obalka.delete("all")
        self.platno_obalka.create_image(0, 0, anchor="nw", image=self._obalka_foto)
        self.obalka_cesta = Path(cesta)

    # ------------------------------------------------------------------
    #  Log a fronta zpráv z pracovního vlákna
    # ------------------------------------------------------------------
    def log(self, zprava: str):
        if zprava.startswith(("CHYBA", "ERROR")):
            znacka = "chyba"
        elif zprava.startswith(("VAROVÁNÍ", "WARNING")):
            znacka = "varovani"
        elif zprava.startswith(("HOTOVO", "DONE")):
            znacka = "uspech"
        else:
            znacka = "bezny"
        if znacka == "chyba":
            self.pocet_chyb += 1
        radek = (time.strftime("%H:%M:%S  "), zprava, znacka)
        self._log_radky.append(radek)
        del self._log_radky[:-5000]
        if self._gui_hotove:
            self._vloz_do_logu([radek])
            self._obnov_souhrn_logu()

    def _vloz_do_logu(self, radky):
        self.log_box.config(state="normal")
        for cas, zprava, znacka in radky:
            self.log_box.insert("end", cas, "cas")
            self.log_box.insert("end", f"{zprava}\n", znacka)
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _obnov_log_z_pameti(self):
        self._vloz_do_logu(self._log_radky)
        self._obnov_souhrn_logu()

    def _obnov_souhrn_logu(self):
        if self._prevod:
            text = T("log_souhrn_radek", self._prevod.get("hotovo", 0), self.pocet_chyb)
        else:
            text = T("log_chyb", self.pocet_chyb)
        self.lbl_log_souhrn.configure(text=text)

    def log_z_vlakna(self, zprava: str):
        self.fronta.put(("log", zprava))

    def _zpracuj_frontu(self):
        try:
            while True:
                typ, data = self.fronta.get_nowait()
                if typ == "log":
                    self.log(data)
                elif typ == "postup":
                    hotovo, celkem, uplynulo, zbyva, kap_i = data
                    self._prevod.update(hotovo=hotovo, celkem=celkem, zbyva=zbyva, kapitola=kap_i)
                    self._obnov_stav()
                    self._obnov_souhrn_logu()
                elif typ == "zvuk":
                    kap_i, cesta, od, do, hotova = data
                    self.prehravac.aktualizuj(kap_i, cesta=cesta, od=od, do=do, hotova=hotova,
                                              roste=bool(self.bezi and not hotova))
                    if self.prehravac.stav()["kapitola"] < 0:
                        # Přehrávač rovnou ukáže, co vzniká - hrát začne až na pokyn
                        self.prehravac.vyber(kap_i, 0.0, hrat=False)
                elif typ == "kapitola_hotova":
                    self.prehravac.aktualizuj(data, hotova=True, roste=False)
                elif typ == "prejmenovano":
                    self.prehravac.prejmenuj(*data)
                elif typ == "sr":
                    if data != self.prehravac.sr:
                        self.prehravac.zastav()
                        self.prehravac = Prehravac(data, self.log_z_vlakna)
                        self.prehravac.nastav_kapitoly(self._odhady_kapitol())
                elif typ == "stav":
                    self.var_stav.set(data)
                    self._stav_text = data
                elif typ == "hotovo":
                    self._prevod_dokoncen(data)
                elif typ == "chyba":
                    self._prevod_dokoncen(None, chyba=data)
                elif typ == "obalka":
                    self.zobraz_obalku(Path(data))
                elif typ == "test_hotovo":
                    self.btn_test.povol(True)
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
            otevri_v_systemu(slozka)
        except Exception as chyba:
            messagebox.showerror(T("dlg_chyba"), T("dlg_slozka", chyba))

    def oprav_usek(self, soubor: Path = None, cas: float = None):
        if self.bezi:
            messagebox.showinfo(T("dlg_probiha"), T("dlg_pockejte"))
            return
        self.prehravac.pozastav()
        if soubor is not None:
            slozka = Path(soubor).parent
        else:
            slozka = Path(self.var_vystup_slozka.get().strip('" ') or (APP_DIR / "vystup"))
            nazev = re.sub(ZAKAZANE_ZNAKY, "_", self.var_vystup_nazev.get().strip()
                           or self.nazev_knihy or "audiokniha")
            if (slozka / nazev).is_dir():
                slozka = slozka / nazev
        dialog = DialogOprava(self, slozka, soubor=soubor, cas=cas)
        self.wait_window(dialog)
        for cesta in dialog.nahrazene:
            self.prehravac.zneplatni(cesta)

    def prehraj(self, cesta: Path):
        """Krátká ukázka (test hlasu, oprava úseku) přímo na zvukovou kartu."""
        if not Prehravac.dostupny():
            try:
                otevri_v_systemu(cesta)
            except Exception:
                self.log(T("log_ulozen", cesta))
            return
        import numpy as np
        import sounddevice as sd

        self.prehravac.pozastav()
        with wave.open(str(cesta), "rb") as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            sd.play(data, w.getframerate())

    # ------------------------------------------------------------------
    #  Načtení a příprava textu
    # ------------------------------------------------------------------
    def nacti_a_priprav(self, tise: bool = False):
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
            self.kapitoly, self.bloky, self.bloky_puvodni, nahrazeno, z_wiki = priprav_bloky(
                kapitoly, self._kod_jazyka_textu(), max_znaku)

            if not self.bloky:
                raise ValueError("Text se nepodařilo rozdělit na bloky.")
            self.ma_kapitoly = ma_kapitoly and len(self.kapitoly) > 1
            self.nazev_knihy = cesta.stem
            metadata = nacti_metadata(cesta)
            self.titul_knihy, self.autor_knihy = metadata["titul"], metadata["autor"]
            znaku = sum(len(b) for _, b in self.bloky)

            self.var_soubor_info.set("")
            if not self.bezi and self.stav_prevodu != "nezahajeno":
                self.stav_prevodu = "nezahajeno"
                self._prepni_zobrazeni()
            self._po_zmene_nastaveni()
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
            self.bloky_puvodni = []
            self.kapitoly = []
            self.ma_kapitoly = False
            self.var_soubor_info.set(T("info_nezdarilo"))
            self._po_zmene_nastaveni()
            self.log(T("log_chyba", chyba))
            if not tise:
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

        self.btn_test.povol(False)
        self.var_stav.set(T("stav_ukazka"))
        self._uloz_config()

        parametry = self._posbirej_parametry()
        vlakno = threading.Thread(target=self._worker_test, args=(veta, parametry), daemon=True)
        vlakno.start()

    def _worker_test(self, veta: str, p: dict):
        vystup = None
        try:
            self.engine.nacti_model(p["zarizeni"], p["jazyk_textu"], p.get("rychly_dekoder", False),
                                    p.get("mlx_presnost", MLX_VYPNUTO))
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
            "mlx_presnost": self.var_mlx_presnost.get(),
            "seed": int(self.var_seed.get() or 0),
            "zarizeni": self.var_zarizeni.get(),
            "jazyk_textu": self._klic_jazyka_textu(),
            "pauza_ms": int(self.var_pauza.get()),
            "format": self.var_format.get(),
            "bitrate": self.var_bitrate.get(),
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

    def pokracuj_v_rozdelanem(self, vybrany: dict):
        """Pokračuje v přerušeném převodu vybraném na úvodní obrazovce."""
        if self.bezi:
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
            ("mlx_presnost", self.var_mlx_presnost, str),
        ]
        # Kniha uložená dřív, než rychlý dekodér nebo MLX existovaly, vznikla bez nich
        parametry = {"rychly_dekoder": False, "mlx_presnost": MLX_VYPNUTO, **parametry}
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

        self._po_zmene_nastaveni()
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
        # Obálka nese název tak, jak ho uživatel napsal - i s dvojtečkou,
        # kterou je v názvu souboru potřeba nahradit
        self.nazev_vystupu = nazev
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
        parametry["otisk_hlasu"] = otisk_hlasu(cesta_knihy, parametry, self.bloky_puvodni)
        # Knihy rozdělané dřív mají otisk z bloků až po slovníčku výslovnosti
        parametry["otisk_po_slovnicku"] = otisk_hlasu(cesta_knihy, parametry, texty)
        parametry["otisk_verze_1"] = otisk_verze_1(cesta_knihy, parametry, len(texty))
        parametry["opravy"] = {**{k: parametry.get(k) for k in NASTAVENI_OPRAV},
                               "slovnik": otisk_vyslovnosti(kod)}
        parametry["zdroj"] = self.var_vstup.get().strip('" ')
        parametry["ulozitelne"] = {k: v for k, v in parametry.items()
                                   if k not in ("otisk_hlasu", "otisk_po_slovnicku", "otisk_verze_1",
                                                "opravy", "ulozitelne")}
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

        self.bezi = True
        self.stop_event.clear()
        self.pause_event.clear()
        self._zahaj_zobrazeni_prevodu(od_bloku)

        self.vlakno = threading.Thread(
            target=self._worker_prevod,
            args=(list(self.bloky), zaklad, parametry, od_bloku, postup),
            daemon=True)
        self.vlakno.start()

    def _zahaj_zobrazeni_prevodu(self, od_bloku: int):
        """Okno přejde z úvodní obrazovky na převod s přehrávačem."""
        self.stav_prevodu = "bezi"
        self.nastaveni_otevrene = False
        self._stav_text = ""
        self.pocet_chyb = 0
        self.vysledek_cesta = None
        prvni = self.bloky[min(od_bloku, len(self.bloky) - 1)][0]
        self._prevod = {"hotovo": od_bloku, "celkem": len(self.bloky), "zbyva": -1,
                        "kapitola": prvni, "start": time.time()}
        self.prehravac.nastav_kapitoly(self._odhady_kapitol())
        self._zamkni_ovladani(True)
        self._prepni_zobrazeni()
        self._obnov_stav()
        self._obnov_souhrn_logu()
        self._kresli_kapitoly()

    def prepni_pauzu(self):
        if not self.bezi:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.stav_prevodu = "bezi"
            self.log(T("log_pokracuji"))
        else:
            self.pause_event.set()
            self.stav_prevodu = "pozastaveno"
            self.log(T("log_pozastaveno"))
        self._obnov_stav()

    def zastav(self):
        if not self.bezi:
            return
        if messagebox.askyesno(T("dlg_zastavit"), T("dlg_zastavit_text")):
            self.stop_event.set()
            self.pause_event.clear()
            self.stav_prevodu = "bezi"
            self.var_stav.set(T("stav_zastavuji"))
            self._obnov_stav()

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
                self.engine.nacti_model(p["zarizeni"], p["jazyk_textu"], p.get("rychly_dekoder", False),
                                        p.get("mlx_presnost", MLX_VYPNUTO))
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
                if kandidat.exists() or vytvor_obalku(self.nazev_vystupu or zaklad.stem, kandidat):
                    obalka_cesta = kandidat
                    self.log_z_vlakna(T("log_obalka", kandidat.name))
                    self.fronta.put(("obalka", str(kandidat)))
                else:
                    self.log_z_vlakna(T("log_obalka_ne"))

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
                meta = {"title": popis, "album": self.nazev_vystupu or zaklad.stem,
                        "track": str(kap_i + 1), "genre": "Audiobook"}
                if prevod_na_mp3(wav, mp3, p["bitrate"], meta, obalka_cesta):
                    wav.unlink(missing_ok=True)       # WAV už není k ničemu
                    self.fronta.put(("prejmenovano", (str(wav), str(mp3))))
                    hotove.append(mp3.name)
                    self.log_z_vlakna(T("log_kapitola_hotova", kap_i + 1, mp3.name))
                else:
                    self.log_z_vlakna(T("log_mp3_selhal"))
                    hotove.append(wav.name)
                self.fronta.put(("kapitola_hotova", kap_i))

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

            # Přehrávači říct, co z knihy už zní: hotové kapitoly a rozepsaný soubor
            self.fronta.put(("sr", sr))
            kap_zvuku, od_kapitoly = -1, 0
            if od_bloku:
                kap_zvuku, od_kapitoly = self._ohlas_hotovy_zvuk(bloky, od_bloku, po_kapitolach,
                                                                 slozka, mapa, sr, p)

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
                if kap_i != kap_zvuku:
                    # Kniha bez rozpadu má kapitoly za sebou v jednom souboru
                    if kap_zvuku >= 0 and not po_kapitolach:
                        self.fronta.put(("kapitola_hotova", kap_zvuku))
                    kap_zvuku, od_kapitoly = kap_i, self._zapisovac.pocet_vzorku

                for text in vynechano:
                    preskocene.append({"blok": index, "kapitola": kap_i + 1, "text": text})
                if vzorky is None:
                    neuspesne += 1
                else:
                    mapa.pridej(index, self._zapisovac.cesta.stem, self._zapisovac.pocet_vzorku,
                                len(vzorky), blok)
                    self._zapisovac.zapis(vzorky)
                    self._zapisovac.zapis_ticho(p["pauza_ms"])
                    # Přehrávač čte rozepsaný soubor z disku, blok tam musí být celý
                    self._zapisovac.soubor.flush()
                    self.fronta.put(("zvuk", (kap_i, str(self._zapisovac.cesta), od_kapitoly,
                                              self._zapisovac.pocet_vzorku, False)))

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
                    meta = {"title": self.nazev_vystupu or zaklad.stem, "genre": "Audiobook",
                            "comment": "Vytvořeno pomocí Chatterbox TTS"}
                    if prevod_na_mp3(jediny_wav, mp3, p["bitrate"], meta, obalka_cesta):
                        jediny_wav.unlink(missing_ok=True)   # při MP3 WAV neuchováváme
                        self.fronta.put(("prejmenovano", (str(jediny_wav), str(mp3))))
                        vysledek = mp3
                        self.log_z_vlakna(T("log_mp3_hotovo", mp3))
                    else:
                        self.log_z_vlakna(T("log_mp3_selhal"))
                if not zastaveno and kap_zvuku >= 0:
                    self.fronta.put(("kapitola_hotova", kap_zvuku))
                velikost = vysledek.stat().st_size / (1024 * 1024) if vysledek.exists() else 0.0
                souhrn = T("log_souhrn", formatuj_cas(delka), velikost)

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
            self.log_z_vlakna(T("log_chyba", chyba))
            self.log_z_vlakna(traceback.format_exc(limit=5))
            self.fronta.put(("chyba", str(chyba)))

    def _ohlas_hotovy_zvuk(self, bloky, od_bloku, po_kapitolach, slozka, mapa, sr, p):
        """Při navázání pošle přehrávači kapitoly, které už zní.

        Vrací (kapitola rozepsaného souboru, její začátek v souboru), aby na ni
        další bloky navázaly.
        """
        z = self._zapisovac
        kap_ted = bloky[od_bloku][0] if od_bloku < len(bloky) else len(self.kapitoly) - 1
        if po_kapitolach:
            for kap_i in range(kap_ted):
                zaklad = str(slozka / nazev_souboru_kapitoly(kap_i, self.kapitoly[kap_i].get("nazev")))
                for pripona in (".mp3", ".wav"):
                    cesta = Path(zaklad + pripona)
                    if cesta.exists():
                        self.fronta.put(("zvuk", (kap_i, str(cesta), 0, delka_souboru_vzorku(cesta, sr), True)))
                        break
            if z is None:
                return -1, 0
            self.fronta.put(("zvuk", (kap_ted, str(z.cesta), 0, z.pocet_vzorku, False)))
            return kap_ted, 0

        # Jeden soubor na celou knihu: kde která kapitola začíná, ví mapa bloků
        if z is None:
            return -1, 0
        pauza = int(sr * p["pauza_ms"] / 1000.0)
        zacatky, konce = {}, {}
        for zaznam in mapa.nacti()[1].get(Path(z.cesta).stem, []):
            if zaznam["blok"] > od_bloku:
                continue
            kap_i = bloky[zaznam["blok"] - 1][0]
            zacatky.setdefault(kap_i, zaznam["od"])
            konce[kap_i] = zaznam["od"] + zaznam["delka"] + pauza
        for kap_i in sorted(zacatky):
            hotova = kap_i < kap_ted
            do = min(konce[kap_i] if hotova else z.pocet_vzorku, z.pocet_vzorku)
            self.fronta.put(("zvuk", (kap_i, str(z.cesta), zacatky[kap_i], do, hotova)))
        return kap_ted, zacatky.get(kap_ted, 0)

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
        self.prehravac.ukonci_rust()
        self._zamkni_ovladani(False)
        self.vysledek_cesta = cesta
        if chyba:
            self.stav_prevodu = "chyba"
        else:
            zastaveno = self.stop_event.is_set()
            self.stav_prevodu = "zastaveno" if zastaveno else "dokonceno"
            if not zastaveno:
                self._prevod["hotovo"] = self._prevod.get("celkem", len(self.bloky))
        self._obnov_stav()
        self._kresli_kapitoly()
        self._obnov_rozdelane()
        if chyba:
            messagebox.showerror(T("dlg_selhal"), str(chyba))

    def novy_prevod(self):
        """Zpátky na úvodní obrazovku. Kniha i nastavení zůstanou."""
        if self.bezi:
            return
        self.prehravac.pozastav()
        self.stav_prevodu = "nezahajeno"
        self._prevod = {}
        self._prepni_zobrazeni()
        self._po_zmene_nastaveni()
        self._obnov_rozdelane()
        self._obnov_souhrn_logu()

    # ------------------------------------------------------------------
    def pri_zavreni(self):
        if self.bezi:
            if not messagebox.askyesno(T("dlg_ukoncit"), T("dlg_ukoncit_text")):
                return
            self.stop_event.set()
            self.pause_event.clear()
            self.prehravac.zastav()
            self.var_stav.set(T("stav_ukoncuji"))
            self._uloz_config()
            # Nesmíme zavřít okno dřív, než vlákno dopíše WAV hlavičku,
            # jinak by zůstal poškozený soubor.
            self._pockej_na_vlakno()
            return

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
