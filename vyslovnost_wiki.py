# -*- coding: utf-8 -*-
"""Vygeneruje slovníček měkkého ti/di/ni z Wikislovníku.

Model občas přečte měkké ti/di/ni tvrdě - 'tichý' jako 'tychý'. Pravidlo
'ti se čte měkce' ale neplatí u přejatých slov (politika, diplom, titul),
takže se nedá uplatnit plošně. Wikislovník u hesel uvádí výslovnost v IPA
a ta obě čtení rozlišuje: tichý [cɪxiː], politika [pɔlɪtɪka].

Skript bere jen česká hesla s IPA. U každého t/d/n před i/í v pravopisu
najde odpovídající souhlásku ve výslovnosti a háček doplní jen tam, kde IPA
potvrzuje měkkou (c, ɟ, ɲ). Kde se zdroje neshodnou nebo zarovnání nesedí,
slovo zůstane, jak je. Háček délku slova nemění, takže se neposunou hranice
bloků.

IPA ve Wikislovníku ale není všude stejně spolehlivá. U hesel s nahrávkou
rodilého mluvčího sedí, u vzácných hesel bez nahrávky ji někdo často přepsal
naivně podle pravopisu - anestetický [anɛstɛcɪtskiː], arcikomunista
[art͡sɪkɔmʊɲɪsta]. Hesla s nahrávkou se proto berou celá, u hesel bez ní se
měkkému čtení věří jen v koncovce, kde ho určuje mluvnice (poslední, spojení,
ním, posadil). Tvrdému čtení se věří vždy - naivní přepis by ho neuvedl.

Tvary bez vlastní výslovnosti dědí čtení od hesla, ale jen pro souhlásku,
kterou mají s heslem ve společném začátku slova - co je v koncovce, se nechá
být. Kromě ti/di/ni hesla se dědí i měkkost z 'tě/dě/ně' (vidět -> vidím),
protože ě ji zaručuje pravopisem. Zvratná slovesa se berou bez 'se/si' a
u přídavných jmen, sloves a příslovcí se přidá i tvar se záporem 'ne-'.

Data: kaikki.org, extrakt Wikislovníku (wiktextract). Licence dat
CC BY-SA 4.0 a výsledný soubor ji přebírá.

Použití:
    python vyslovnost_wiki.py cs-extract.jsonl.gz vyslovnost_cs.json [--souhlasky tdn] [--jen-s-nahravkou]
"""
import collections
import datetime
import gzip
import json
import re
import sys
import unicodedata
from pathlib import Path

MEKKE = {"t": "ť", "d": "ď", "n": "ň"}
IPA_MEKKE = {"t": "c", "d": "ɟ", "n": "ɲ"}
IPA_TVRDE = {"t": "t", "d": "d", "n": "n"}
# Přízvuk, závorky, hranice slabik, spojovací oblouček, neslabičnost, slabičnost
IPA_ODSTRANIT = set("[]/ˈˌ.‿̯̩͜͡")
VYNECHAT_DRUHY = {"abbrev", "prefix", "suffix", "phrase", "name", "symbol",
                  "character", "letter", "punct", "romanization"}
ZAPOR_DRUHY = {"adj", "verb", "adv"}
# Bez nahrávky se měkkému čtení věří jen v koncovce. Tam ho určuje mluvnice
# (poslední, spojení, ním, nich, posadil), kdežto uvnitř kmene bývá u přejatých
# slov IPA přepsaná naivně podle pravopisu (anestetický [anɛstɛcɪtskiː]).
KONCOVKA = re.compile(r"(?:í(?:m|ch|mu|ho|mi)?|i(?:ch|mi|m)?)")
KONCOVKA_SLOVESA = re.compile(r"(?:i(?:t|ti|l|la|lo|li|ly|v|vše|vši|š|me|te|mi)?|í(?:m|š|me|te|c|ce|cí)?)")
TESTOVACI = ["tichý", "tichého", "tiší", "ticho", "divadlo", "divadle", "nic", "ni", "ti",
             "chodit", "chodili", "dítě", "politika", "politice", "politické", "titul",
             "diplom", "diplomati", "nikotin", "technika", "mladí", "nikdo", "nikoho",
             "tisíc", "díval", "podíval", "podívej", "vidím", "nevlastní", "nevadilo",
             "okolností", "místnosti", "rameni", "tažení", "zakroutil", "odvrátil",
             "germáni", "tiberius", "studenti", "utichl"]


def bez_zvratneho(slovo: str):
    """'dívat se' -> 'dívat'. None, když slovo zvratné není."""
    for castice in (" se", " si"):
        if slovo.endswith(castice):
            return slovo[:-len(castice)]
    return None


def ipa_bez_zvratneho(ipa: str):
    t = ipa.strip().strip("[]/").strip()
    for castice in (" sɛ", " sɪ", " se", " si"):
        if t.endswith(castice):
            return t[:-len(castice)]
    return None


def mista_pravopis(slovo: str):
    """t/d/n před i/í/y/ý: [(pozice, souhláska, jde_o_i)]."""
    return [(i, slovo[i], slovo[i + 1] in "ií")
            for i in range(len(slovo) - 1)
            if slovo[i] in "tdn" and slovo[i + 1] in "iíyý"]


def mista_ipa(ipa: str):
    """Souhlásky t/d/n/c/ɟ/ɲ před i-ovou samohláskou, v pořadí. None = nečitelné."""
    ipa = ipa.replace("(ʔ)", "").strip()
    if "(" in ipa or ")" in ipa or " " in ipa:
        return None
    t = "".join(ch for ch in ipa if ch not in IPA_ODSTRANIT)
    return [t[i] for i in range(len(t) - 1) if t[i] in "tdncɟɲ" and t[i + 1] in "ɪi"]


def klasifikuj(slovo: str, ipas):
    """{pozice: měkká?} pro každé t/d/n před i/í. None, když výslovnost na pravopis nesedí."""
    mista = mista_pravopis(slovo)
    vysledek = None
    for ipa in ipas:
        souhlasky = mista_ipa(ipa)
        if souhlasky is None or len(souhlasky) != len(mista):
            return None
        tento = {}
        for (pozice, s, jde_o_i), c in zip(mista, souhlasky):
            if c == IPA_MEKKE[s]:
                mekka = True
            elif c == IPA_TVRDE[s]:
                mekka = False
            else:
                return None                  # jiná souhláska - zarovnání se rozjelo
            if not jde_o_i:
                if mekka:
                    return None              # 'ty' čtené měkce - data jsou podezřelá
                continue
            tento[pozice] = mekka
        if vysledek is None:
            vysledek = tento
        else:
            # Varianty výslovnosti se neshodnou - sporná místa vyřadit
            vysledek = {p: m for p, m in vysledek.items() if tento.get(p) == m}
    return vysledek


def v_koncovce(slovo: str, pozice: int, druh: str) -> bool:
    """Leží samohláska po t/d/n na začátku koncovky, kde měkkost dává mluvnice?"""
    zbytek = slovo[pozice + 1:]
    if KONCOVKA.fullmatch(zbytek):
        return True
    return druh == "verb" and bool(KONCOVKA_SLOVESA.fullmatch(zbytek))


def spolecny_zacatek(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def cte_se(slovo: str) -> bool:
    return slovo.isalpha() and slovo.islower()


def hlavni():
    argumenty = [a for a in sys.argv[1:] if not a.startswith("--") and
                 (sys.argv.index(a) == 0 or sys.argv[sys.argv.index(a) - 1] != "--souhlasky")]
    if len(argumenty) != 2:
        sys.exit(__doc__)
    vstup, vystup = Path(argumenty[0]), Path(argumenty[1])
    povolene = set("tdn")
    jen_s_nahravkou = "--jen-s-nahravkou" in sys.argv
    if "--souhlasky" in sys.argv:
        povolene = set(sys.argv[sys.argv.index("--souhlasky") + 1])

    # tvar -> pozice -> množina čtení ze všech zdrojů
    zdroje = collections.defaultdict(lambda: collections.defaultdict(set))
    stat = collections.Counter()
    nadpisy = collections.Counter()

    with gzip.open(vstup, "rt", encoding="utf-8") as f:
        for radek in f:
            e = json.loads(radek)
            if e.get("lang_code") != "cs":
                continue
            stat["hesel"] += 1
            nadpisy[e.get("pos_title")] += 1
            slovo = unicodedata.normalize("NFC", e.get("word") or "")
            ipas = [s["ipa"] for s in e.get("sounds", []) if s.get("ipa")]

            zvratne = bez_zvratneho(slovo)
            if zvratne is not None:
                slovo = zvratne
                ipas = [i for i in (ipa_bez_zvratneho(x) for x in ipas) if i]
                stat["zvratnych"] += 1

            if e.get("pos") in VYNECHAT_DRUHY or not cte_se(slovo):
                stat["vynechano_druh_nebo_tvar"] += 1
                continue
            if not ipas:
                stat["bez_vyslovnosti"] += 1
                continue
            # Nahrávka rodilého mluvčího je známka, že heslo někdo prošel. U hesel
            # bez ní bývá IPA přepsaná naivně podle pravopisu i u přejatých slov
            # (anestetický [anɛstɛcɪtskiː]).
            overeno = any(s.get("audio") for s in e.get("sounds", []))
            if not overeno:
                stat["bez_nahravky"] += 1
                if jen_s_nahravkou:
                    continue
            k = klasifikuj(slovo, ipas)
            if k is None:
                stat["vyslovnost_nesedi"] += 1
                continue
            if not overeno:
                puvodne = len(k)
                k = {p: m for p, m in k.items() if not m or v_koncovce(slovo, p, e.get("pos"))}
                stat["mekkych_bez_nahravky_vyrazeno"] += puvodne - len(k)
            # Měkkost zaručená pravopisem: tě, dě, ně
            mekke_e = {p for p in range(len(slovo) - 1) if slovo[p] in "tdn" and slovo[p + 1] == "ě"}
            if not k and not mekke_e:
                continue
            stat["hesel_pouzito"] += 1

            zapis = [(slovo, p, m) for p, m in k.items()]
            for forma in e.get("forms", []):
                tvar = unicodedata.normalize("NFC", forma.get("form") or "")
                if zvratne is not None:
                    tvar = bez_zvratneho(tvar) or tvar
                if not cte_se(tvar) or tvar == slovo:
                    continue
                spolecne = spolecny_zacatek(slovo, tvar)
                for pozice, _, jde_o_i in mista_pravopis(tvar):
                    # Souhláska musí ležet ve společném začátku, jinak nevíme nic
                    if not jde_o_i or pozice >= spolecne:
                        continue
                    if pozice in k:
                        zapis.append((tvar, pozice, k[pozice]))
                        stat["mist_zdedeno_z_ti"] += 1
                    elif pozice in mekke_e:
                        zapis.append((tvar, pozice, True))
                        stat["mist_zdedeno_z_e"] += 1

            for tvar, pozice, mekka in zapis:
                zdroje[tvar][pozice].add(mekka)
                if e.get("pos") in ZAPOR_DRUHY and not tvar.startswith("ne"):
                    zdroje["ne" + tvar][pozice + 2].add(mekka)
                    stat["mist_se_zaporem"] += 1

    nahrady, sporne = {}, 0
    po_souhlaskach = collections.Counter()
    for tvar, mista in zdroje.items():
        znaky = list(tvar)
        zmeneno = False
        for pozice, cteni in mista.items():
            if len(cteni) != 1:
                sporne += 1
                continue
            if True in cteni and znaky[pozice] in povolene:
                po_souhlaskach[znaky[pozice]] += 1
                znaky[pozice] = MEKKE[znaky[pozice]]
                zmeneno = True
        if zmeneno:
            nahrady[tvar] = "".join(znaky)

    data = {
        "_zdroj": "Wikislovník, extrakt kaikki.org (wiktextract): "
                  "https://kaikki.org/dictionary/rawdata.html",
        "_licence": "CC BY-SA 4.0, https://creativecommons.org/licenses/by-sa/4.0/ - "
                    "odvozeno z Wikislovníku, přispěvatelé Wikislovníku",
        "_vytvoreno": f"{datetime.date.today().isoformat()} skriptem vyslovnost_wiki.py "
                      f"ze souboru {vstup.name}",
        "_souhlasky": "".join(sorted(povolene)),
        "nahrady": dict(sorted(nahrady.items())),
    }
    vystup.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"vydání: {nadpisy.most_common(3)}")
    for klic in ("hesel", "zvratnych", "vynechano_druh_nebo_tvar", "bez_vyslovnosti", "bez_nahravky",
                 "vyslovnost_nesedi", "mekkych_bez_nahravky_vyrazeno", "hesel_pouzito", "mist_zdedeno_z_ti",
                 "mist_zdedeno_z_e", "mist_se_zaporem"):
        print(f"  {klic:26s} {stat[klic]}")
    print(f"  sporných míst              {sporne}")
    print(f"  tvarů k přepisu            {len(nahrady)}  (háčky: {dict(po_souhlaskach)})")
    print(f"  soubor                     {vystup} {vystup.stat().st_size / 1e6:.1f} MB")
    print("\nkontrolní slova:")
    for w in TESTOVACI:
        if w in nahrady:
            stav = f"-> {nahrady[w]}"
        elif w in zdroje:
            stav = "beze změny (" + ", ".join(
                f"{w[p]}{w[p + 1]}:{'?' if len(c) != 1 else ('měkké' if True in c else 'tvrdé')}"
                for p, c in sorted(zdroje[w].items())) + ")"
        else:
            stav = "neznámé - beze změny"
        print(f"  {w:12s} {stav}")


if __name__ == "__main__":
    hlavni()
