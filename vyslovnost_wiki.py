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

Tvary bez vlastní výslovnosti dědí čtení od hesla, ale jen pro souhlásku,
kterou mají s heslem ve společném začátku slova - co je v koncovce, se nechá
být. Kromě ti/di/ni hesla se dědí i měkkost z 'tě/dě/ně' (vidět -> vidím),
protože ě ji zaručuje pravopisem. Zvratná slovesa se berou bez 'se/si' a
u přídavných jmen, sloves a příslovcí se přidá i tvar se záporem 'ne-'.

IPA ve Wikislovníku ale není všude stejně spolehlivá. U hesel s nahrávkou
rodilého mluvčího sedí, u vzácných hesel bez nahrávky ji někdo často přepsal
naivně podle pravopisu - anestetický [anɛstɛcɪtskiː], arcikomunista
[art͡sɪkɔmʊɲɪsta]. Hesla s nahrávkou se proto berou celá. U hesel bez ní se
měkkému čtení věří v koncovce, kde ho určuje mluvnice (poslední, spojení,
ním, posadil), a v kmeni jen tehdy, když slovo nevypadá jako přejaté.
Tvrdému čtení se věří vždy - naivní přepis by ho neuvedl.

Jestli slovo vypadá jako přejaté, se skript naučí z Wikislovníku samotného:
  - tvrdé ti/di/ni v kmeni někdo zapsal úmyslně, takže jde o přejatá slova,
  - měkké ti/di/ni v kmeni potvrzené nahrávkou jsou slova domácí,
  - domácí příklady doplní znaky, které přejatá slova prakticky nemají.
    Každý znak se nejdřív ověří proti přejatým slovům a použije se, jen když
    se u nich objeví nejvýš dvakrát.
Naivní Bayes nad skupinami písmen pak ohodnotí nejistá slova. Práh se
nastaví křížovou validací tak, aby za domácí prošlo nejvýš 1 % přejatých.
Zájmena, spojky, částice a číslovky se berou bez klasifikace - přejatých
slov je mezi nimi zanedbatelně.

Data: kaikki.org, extrakt Wikislovníku (wiktextract). Licence dat
CC BY-SA 4.0 a výsledný soubor ji přebírá.

Použití:
    python vyslovnost_wiki.py cs-extract.jsonl.gz vyslovnost_cs.json
        [--souhlasky tdn] [--jen-s-nahravkou] [--bez-klasifikatoru]
"""
import collections
import datetime
import gzip
import json
import math
import re
import sys
import unicodedata
import zlib
from pathlib import Path

MEKKE = {"t": "ť", "d": "ď", "n": "ň"}
IPA_MEKKE = {"t": "c", "d": "ɟ", "n": "ɲ"}
IPA_TVRDE = {"t": "t", "d": "d", "n": "n"}
# Přízvuk, závorky, hranice slabik, spojovací oblouček, neslabičnost, slabičnost
IPA_ODSTRANIT = set("[]/ˈˌ.‿̯̩͜͡")
VYNECHAT_DRUHY = {"abbrev", "prefix", "suffix", "phrase", "name", "symbol",
                  "character", "letter", "punct", "romanization"}
ZAPOR_DRUHY = {"adj", "verb", "adv"}
# Neohebná a zájmenná slova - přejatých je mezi nimi zanedbatelně
UZAVRENE_DRUHY = {"pron", "conj", "particle", "prep", "num", "intj"}
# Bez nahrávky se měkkému čtení věří v koncovce. Tam ho určuje mluvnice
# (poslední, spojení, ním, nich, posadil), kdežto uvnitř kmene bývá u přejatých
# slov IPA přepsaná naivně podle pravopisu (anestetický [anɛstɛcɪtskiː]).
KONCOVKA = re.compile(r"(?:í(?:m|ch|mu|ho|mi)?|i(?:ch|mi|m)?)")
KONCOVKA_SLOVESA = re.compile(r"(?:i(?:t|ti|l|la|lo|li|ly|v|vše|vši|š|me|te|mi)?|í(?:m|š|me|te|c|ce|cí)?)")

CISLOVKY = ("pěti", "šesti", "devíti", "deseti", "jedenácti", "dvanácti", "třinácti", "čtrnácti",
            "patnácti", "šestnácti", "sedmnácti", "osmnácti", "devatenácti", "dvaceti", "třiceti",
            "čtyřiceti", "padesáti", "šedesáti", "sedmdesáti", "osmdesáti", "devadesáti")
# Kandidáti na znaky domácího slova. Použije se jen ten, který projde ověřením
# proti přejatým slovům; ostatní tu zůstávají, aby bylo vidět, co neprošlo.
ZNAKY_DOMACICH = {
    "ř nebo ů": lambda s, p: "ř" in s or "ů" in s,
    "-ník": lambda s, p: s[p:p + 3] == "ník",
    "-ština": lambda s, p: p > 0 and s[p - 1:p + 3] == "štin",
    "číslovka -ti-": lambda s, p: s[:p + 2].endswith(CISLOVKY),
    "tisíc": lambda s, p: s[p:p + 5] == "tisíc",
    "proti-": lambda s, p: s.startswith("proti") and p == 3,
    "nič/nij uvnitř": lambda s, p: s[p:p + 3] in ("nič", "nij"),
    "tiš uvnitř": lambda s, p: s[p:p + 3] == "tiš",
    "-tivý/-tivost": lambda s, p: s[p:p + 4] in ("tivý", "tivé") or s[p:] == "tivá" or s[p:p + 6] == "tivost",
    "^tiš": lambda s, p: p == 0 and s.startswith("tiš"),
    "^nij/^nič": lambda s, p: p == 0 and s[:3] in ("nij", "nič"),
    "^znič": lambda s, p: p == 1 and s.startswith("znič"),
    "díl": lambda s, p: s[p:p + 3] == "díl",
    "vtip": lambda s, p: p > 0 and s[p - 1:p + 3] == "vtip",
}
ZNAK_MAX_PREJATYCH = 2
CIL_PREJATYCH = 0.01

TESTOVACI = ["tichý", "tichého", "ticho", "divadlo", "nic", "ni", "chodit", "chodili", "dítě",
             "tiše", "podíval", "nikomu", "nijak", "aniž", "totiž", "vtip", "chamtivý", "básník",
             "politika", "politice", "titul", "diplom", "nikotin", "technika", "anestetický",
             "arcikomunista", "transhumanismus", "lokomotivám", "fetišista", "okolností", "utichl"]


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


def nacti_hesla(vstup: Path, stat):
    """Česká hesla s výslovností, připravená pro rozpoznání i pro slovník."""
    hesla = []
    with gzip.open(vstup, "rt", encoding="utf-8") as f:
        for radek in f:
            e = json.loads(radek)
            if e.get("lang_code") != "cs":
                continue
            stat["hesel"] += 1
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
            hesla.append({
                "slovo": slovo,
                "druh": e.get("pos"),
                "k": klasifikuj(slovo, ipas),
                # Nahrávka rodilého mluvčího je známka, že heslo někdo prošel
                "overeno": any(s.get("audio") for s in e.get("sounds", [])),
                "zvratne": zvratne is not None,
                "tvary": [unicodedata.normalize("NFC", x.get("form") or "") for x in e.get("forms", [])],
            })
    return hesla


# ----------------------------------------------------------------------------
#  Rozpoznání přejatých slov
# ----------------------------------------------------------------------------

def rysy(slovo: str, pozice: int):
    """Skupiny písmen celého slova a okna kolem ti/di/ni."""
    w = "^" + slovo + "$"
    q = pozice + 1
    r = set()
    for n in (2, 3, 4):
        for i in range(len(w) - n + 1):
            r.add("w:" + w[i:i + n])
    for vlevo in range(4):
        for vpravo in range(2, 6):
            r.add(f"o{vlevo}{vpravo}:" + w[max(0, q - vlevo):q + vpravo])
    return r


def trenuj(vzorky):
    pocty = {"domaci": collections.Counter(), "cizi": collections.Counter()}
    n = collections.Counter()
    for r, stitek in vzorky:
        pocty[stitek].update(r)
        n[stitek] += 1
    return pocty, n


def skore(model, r, alfa: float = 0.5) -> float:
    """Kladné = domácí. Rys, který v učení nebyl, o slově nic neříká."""
    pocty, n = model
    s = 0.0
    for x in sorted(r):                       # pevné pořadí sčítání = stejný výsledek
        cd, cc = pocty["domaci"][x], pocty["cizi"][x]
        if cd + cc == 0:
            continue
        s += math.log(((cd + alfa) / (n["domaci"] + 2 * alfa)) /
                      ((cc + alfa) / (n["cizi"] + 2 * alfa)))
    return s


def rozpoznej_domaci(hesla, stat):
    """Vrátí množinu (slovo, pozice) nejistých měkkých míst v kmeni, kterým se dá věřit."""
    poradi = {"nejiste": 0, "domaci": 1, "cizi": 2}
    mista, druhy = {}, collections.defaultdict(set)
    for h in hesla:
        if not h["k"]:
            continue
        for p, mekka in h["k"].items():
            if v_koncovce(h["slovo"], p, h["druh"]):
                continue
            stitek = "cizi" if not mekka else ("domaci" if h["overeno"] else "nejiste")
            druhy[(h["slovo"], p)].add(h["druh"])
            if poradi[stitek] > poradi.get(mista.get((h["slovo"], p)), -1):
                mista[(h["slovo"], p)] = stitek

    cizi = [m for m, l in mista.items() if l == "cizi"]
    nejiste = [m for m, l in mista.items() if l == "nejiste"]
    overene = [m for m, l in mista.items() if l == "domaci"]

    print("\nznaky domácích slov (výskyt mezi přejatými -> použit?):")
    pouzite = []
    for jmeno, znak in ZNAKY_DOMACICH.items():
        u_cizich = [s for s, p in cizi if znak(s, p)]
        ok = len(u_cizich) <= ZNAK_MAX_PREJATYCH
        if ok:
            pouzite.append(znak)
        print(f"  {jmeno:16s} {len(u_cizich):4d}  {'ano' if ok else 'ne '}  {u_cizich[:4]}")

    podle_znaku = {m for m in nejiste if any(znak(*m) for znak in pouzite)}
    uzavrene = {m for m in nejiste if druhy[m] <= UZAVRENE_DRUHY}
    vzorky = ([(m, "domaci") for m in overene] + [(m, "domaci") for m in sorted(podle_znaku)] +
              [(m, "cizi") for m in cizi])

    # Práh z křížové validace - dělí se podle slova, ať se tvary jednoho
    # slova nedostanou do učení i do testu zároveň
    cv = []
    for slozka in range(5):
        model = trenuj([(rysy(*m), l) for m, l in vzorky if zlib.crc32(m[0].encode()) % 5 != slozka])
        cv += [(skore(model, rysy(*m)), l, m in podle_znaku)
               for m, l in vzorky if zlib.crc32(m[0].encode()) % 5 == slozka]
    cizi_sk = sorted(sk for sk, l, _ in cv if l == "cizi")
    prah = cizi_sk[min(len(cizi_sk) - 1, int(math.ceil(len(cizi_sk) * (1 - CIL_PREJATYCH))))]
    propusteno = sum(sk > prah for sk in cizi_sk) / len(cizi_sk)
    overene_sk = [sk for sk, l, znak in cv if l == "domaci" and not znak]
    print(f"\nkřížová validace: práh {prah:.2f}, přejatých prošlo {propusteno * 100:.2f} %, "
          f"domácích s nahrávkou poznáno {sum(sk > prah for sk in overene_sk) / len(overene_sk) * 100:.0f} %")

    model = trenuj([(rysy(*m), l) for m, l in vzorky])
    klasifikatorem = {m for m in nejiste if skore(model, rysy(*m)) > prah}
    prijate = klasifikatorem | podle_znaku | uzavrene
    stat["mist_domacich_s_nahravkou"] = len(overene)
    stat["mist_prejatych"] = len(cizi)
    stat["mist_nejistych"] = len(nejiste)
    stat["nejistych_prijato"] = len(prijate)
    return prijate


# ----------------------------------------------------------------------------

def hlavni():
    hodnoty_prepinacu = {"--souhlasky"}
    argumenty = [a for i, a in enumerate(sys.argv[1:], 1)
                 if not a.startswith("--") and sys.argv[i - 1] not in hodnoty_prepinacu]
    if len(argumenty) != 2:
        sys.exit(__doc__)
    vstup, vystup = Path(argumenty[0]), Path(argumenty[1])
    povolene = set("tdn")
    if "--souhlasky" in sys.argv:
        povolene = set(sys.argv[sys.argv.index("--souhlasky") + 1])
    jen_s_nahravkou = "--jen-s-nahravkou" in sys.argv

    stat = collections.Counter()
    hesla = nacti_hesla(vstup, stat)
    prijate = set()
    if not jen_s_nahravkou and "--bez-klasifikatoru" not in sys.argv:
        prijate = rozpoznej_domaci(hesla, stat)

    # tvar -> pozice -> množina čtení ze všech zdrojů
    zdroje = collections.defaultdict(lambda: collections.defaultdict(set))
    for h in hesla:
        slovo, druh = h["slovo"], h["druh"]
        if not h["overeno"]:
            stat["bez_nahravky"] += 1
            if jen_s_nahravkou:
                continue
        if h["k"] is None:
            stat["vyslovnost_nesedi"] += 1
            continue
        k = h["k"]
        if not h["overeno"]:
            puvodne = len(k)
            k = {p: m for p, m in k.items()
                 if not m or v_koncovce(slovo, p, druh) or (slovo, p) in prijate}
            stat["mekkych_bez_nahravky_vyrazeno"] += puvodne - len(k)
        # Měkkost zaručená pravopisem: tě, dě, ně
        mekke_e = {p for p in range(len(slovo) - 1) if slovo[p] in "tdn" and slovo[p + 1] == "ě"}
        if not k and not mekke_e:
            continue
        stat["hesel_pouzito"] += 1

        zapis = [(slovo, p, m) for p, m in k.items()]
        for tvar in h["tvary"]:
            if h["zvratne"]:
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
            if druh in ZAPOR_DRUHY and not tvar.startswith("ne"):
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

    print()
    for klic in ("hesel", "zvratnych", "vynechano_druh_nebo_tvar", "bez_vyslovnosti", "bez_nahravky",
                 "vyslovnost_nesedi", "mist_domacich_s_nahravkou", "mist_prejatych", "mist_nejistych",
                 "nejistych_prijato", "mekkych_bez_nahravky_vyrazeno", "hesel_pouzito",
                 "mist_zdedeno_z_ti", "mist_zdedeno_z_e", "mist_se_zaporem"):
        print(f"  {klic:30s} {stat[klic]}")
    print(f"  {'sporných míst':30s} {sporne}")
    print(f"  {'tvarů k přepisu':30s} {len(nahrady)}  (háčky: {dict(po_souhlaskach)})")
    print(f"  {'soubor':30s} {vystup} {vystup.stat().st_size / 1e6:.1f} MB")
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
        print(f"  {w:16s} {stav}")


if __name__ == "__main__":
    hlavni()
