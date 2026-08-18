# -*- coding: utf-8 -*-
"""Tělo generujícího procesu.

Schválně samostatný modul. Windows spouští procesy metodou spawn, při které
si potomek naimportuje modul, kde je cílová funkce. Kdyby funkce žila v
`audiobookery.py` spuštěném jako `__main__`, potomek by znovu spustil celý
skript - a bez ochrany `if __name__ == "__main__"` by se rekurzivně zacyklil.
Takhle potomek importuje tenhle modul a `audiobookery` jen jako knihovnu.
"""


def bezet(ukoly, vysledky, nastaveni):
    """Načte vlastní kopii modelu a generuje bloky z fronty, dokud nepřijde None."""
    import audiobookery as ab

    id_p = nastaveni.get("id", 0)
    engine = None

    def log(zprava):
        try:
            vysledky.put(("log", id_p, zprava))
        except Exception:
            pass

    try:
        engine = ab.TtsEngine(log)
        engine.nacti_model(nastaveni["zarizeni"], nastaveni["jazyk_textu"])
        vysledky.put(("pripraven", id_p, engine.sr))

        while True:
            ukol = ukoly.get()
            if ukol is None:
                break
            index, blok, celkem, p = ukol
            # Seed odvozený od indexu bloku - výsledek pak nezávisí na tom,
            # kolik pracovníků běží ani kdo blok zrovna dostal.
            if p.get("seed"):
                ab.nastav_seed(int(p["seed"]) + index)
            vzorky = ab.generuj_blok(engine, blok, p, index, celkem, log)
            vysledky.put(("audio", index, vzorky))
    except Exception as chyba:
        try:
            vysledky.put(("chyba", id_p, "{}: {}".format(type(chyba).__name__, chyba)))
        except Exception:
            pass
    finally:
        try:
            if engine is not None:
                engine.uvolni()
        except Exception:
            pass
