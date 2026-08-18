# -*- coding: utf-8 -*-
"""Texty rozhraní. Klíč -> (English, Čeština).

Přidání dalšího jazyka: rozšířit JAZYKY a doplnit třetí položku do každé n-tice.
Zástupné symboly {0}, {1} ... se plní přes T("klic", hodnota).
"""

JAZYKY = {"en": "English", "cs": "Čeština"}
PORADI = ("en", "cs")

_aktualni = "en"


def nastav_jazyk(kod: str):
    global _aktualni
    if kod in JAZYKY:
        _aktualni = kod


def aktualni_jazyk() -> str:
    return _aktualni


def T(klic: str, *args) -> str:
    polozka = TEXTY.get(klic)
    if polozka is None:
        return klic
    sablona = polozka[PORADI.index(_aktualni)]
    try:
        return sablona.format(*args) if args else sablona
    except (IndexError, KeyError):
        return sablona


TEXTY = {
    # ---------------- Okno a sekce ----------------
    "app_nazev":        ("Audiobookery", "Audiobookery"),
    "znacka":           ("audiobookery", "audiobookery"),
    "podtitul":         ("chatterbox · {0} · v{1}", "chatterbox · {0} · v{1}"),
    "sekce_kniha":      ("book", "kniha"),
    "sekce_hlas":       ("voice", "hlas"),
    "sekce_vystup":     ("output", "výstup"),
    "sekce_poslech":    ("listening", "poslech"),
    "sekce_pokrocile":  ("advanced settings", "pokročilé nastavení"),
    "sekce_prubeh":     ("activity", "průběh"),

    # ---------------- Tlačítka ----------------
    "btn_vybrat":       ("browse", "vybrat"),
    "btn_nacist":       ("load", "načíst"),
    "btn_test":         ("test voice", "test hlasu"),
    "btn_start":        ("start conversion", "spustit převod"),
    "btn_pauza":        ("pause", "pauza"),
    "btn_pokracovat":   ("resume", "pokračovat"),
    "btn_zastavit":     ("stop", "zastavit"),
    "btn_otevrit":      ("open output", "otevřít výstup"),
    "btn_poslech_pauza": ("pause playback", "pozastavit poslech"),
    "btn_poslech_hrat": ("resume playback", "pokračovat v poslechu"),

    # ---------------- Popisky ----------------
    "lab_jazyk":        ("language", "jazyk"),
    "lab_naskok":       ("head start (s)", "náskok (s)"),
    "lab_prehravat":    ("play while converting", "přehrávat během převodu"),
    "lab_expresivita":  ("expressiveness", "expresivita"),
    "lab_cfg":          ("cfg / pace", "cfg / tempo"),
    "lab_teplota":      ("temperature", "teplota"),
    "lab_znaku":        ("chars per block", "znaků na blok"),
    "lab_pauza_ms":     ("gap (ms)", "pauza (ms)"),
    "lab_seed":         ("seed", "seed"),
    "lab_zarizeni":     ("device", "zařízení"),
    "lab_jazyk_textu":  ("book language", "jazyk knihy"),
    "jaz_zakladni":     ("built into the base model, nothing to download",
                         "je v základním modelu, nic se nestahuje"),
    "jaz_stazeno":      ("model already downloaded", "model už je stažený"),
    "jaz_stahne":       ("downloads {0:.1f} GB on first use",
                         "při prvním použití stáhne {0:.1f} GB"),
    "jaz_neovereno":    ("community model, untested", "komunitní model, neověřený"),
    "jaz_bez_tokenu":   ("tokenizer has no token for this language",
                         "tokenizer pro tento jazyk nemá token"),
    "jaz_gated":        ("needs a Hugging Face login", "vyžaduje přihlášení k Hugging Face"),
    "lab_obalka":       ("generate cover", "vygenerovat obálku"),
    "hint_hlas":        ("without a reference recording the model's default english voice is used",
                         "bez referenční nahrávky se použije výchozí anglický hlas modelu"),

    # ---------------- Stavy ----------------
    "stav_pripraveno":  ("Ready", "Připraveno"),
    "stav_generuji":    ("Generating audio...", "Generuji audio..."),
    "stav_mp3":         ("Converting to MP3...", "Převádím do MP3..."),
    "stav_hotovo":      ("Done", "Hotovo"),
    "stav_zastaveno":   ("Stopped", "Zastaveno"),
    "stav_chyba":       ("Error", "Chyba"),
    "stav_ukazka":      ("Generating voice sample...", "Generuji ukázku hlasu..."),
    "stav_zastavuji":   ("Stopping after current block...",
                         "Zastavuji po dokončení aktuálního bloku..."),
    "stav_ukoncuji":    ("Finishing - closing the file...",
                         "Ukončuji - dokončuji zápis souboru..."),
    "info_zadny":       ("no file loaded", "není načten žádný soubor"),
    "info_nezdarilo":   ("loading failed", "načtení se nezdařilo"),
    "info_soubor":      ("{0}  |  {1} chars  |  {2} blocks  |  est. audio ~{3}",
                         "{0}  |  {1} znaků  |  {2} bloků  |  odhad délky audia ~{3}"),
    "prubeh_bloky":     ("{0}/{1}", "{0}/{1}"),
    "prubeh_cas":       ("elapsed {0}  /  left {1}", "uplynulo {0}  /  zbývá {1}"),
    "vizu_ticho":       ("silent", "ticho"),

    # ---------------- Poslech ----------------
    "poslech_naskok":   ("collecting head start … {0} of {1}",
                         "sbírám náskok … {0} z {1}"),
    "poslech_hraje":    ("playing · buffer {0} · played {1} · lasts ~{2}",
                         "přehrávám · zásoba {0} · přehráno {1} · vydrží ~{2}"),
    "poslech_pauza":    ("paused · buffer {0} · played {1}",
                         "pozastaveno · zásoba {0} · přehráno {1}"),
    "poslech_konec":    ("playback finished", "přehrávání skončilo"),

    # ---------------- Dialogy ----------------
    "dlg_chybi_soubor": ("No file", "Chybí soubor"),
    "dlg_vyberte":      ("Select an input file first.", "Nejprve vyberte vstupní soubor."),
    "dlg_chyba_nacteni": ("Loading error", "Chyba při načítání"),
    "dlg_probiha":      ("Conversion running", "Probíhá převod"),
    "dlg_pockejte":     ("Wait until the conversion finishes.", "Počkejte na dokončení převodu."),
    "dlg_chybi_text":   ("No text", "Chybí text"),
    "dlg_zadejte":      ("Enter a test sentence.", "Zadejte testovací větu."),
    "dlg_existuje":     ("File exists", "Soubor existuje"),
    "dlg_prepsat":      ("File already exists:\n{0}\n\nOverwrite?",
                         "Soubor už existuje:\n{0}\n\nPřepsat?"),
    "dlg_ffmpeg":       ("ffmpeg not found", "ffmpeg nenalezen"),
    "dlg_ffmpeg_text":  ("MP3 export needs ffmpeg on PATH.\nOutput will be saved as WAV.",
                         "Pro export do MP3 je potřeba ffmpeg v PATH.\nVýstup bude uložen jako WAV."),
    "dlg_zastavit":     ("Stop", "Zastavit"),
    "dlg_zastavit_text": ("Really stop the conversion?\nWhat has been generated stays saved.",
                          "Opravdu zastavit převod?\nDosud vygenerovaná část zůstane uložena."),
    "dlg_selhal":       ("Conversion failed", "Převod selhal"),
    "dlg_hotovo":       ("Done", "Hotovo"),
    "dlg_hotovo_text":  ("The audiobook was created:", "Audiokniha byla vytvořena:"),
    "dlg_zastaveno_text": ("Conversion stopped, partial file saved:",
                           "Převod byl zastaven, rozpracovaná část je uložena:"),
    "dlg_otevrit":      ("Open the output folder?", "Otevřít složku s výstupem?"),
    "dlg_ukoncit":      ("Quit", "Ukončit"),
    "dlg_ukoncit_text": ("A conversion is running. Really quit?\nThe file will be closed properly.",
                         "Probíhá převod. Opravdu ukončit?\nRozpracovaný soubor se ještě korektně uzavře."),
    "dlg_chyba":        ("Error", "Chyba"),
    "dlg_slozka":       ("Could not open the folder:\n{0}", "Složku se nepodařilo otevřít:\n{0}"),
    "dlg_vyber_knihu":  ("Select an e-book", "Vyberte e-knihu"),
    "dlg_vyber_hlas":   ("Select a reference voice recording", "Vyberte referenční nahrávku hlasu"),
    "dlg_vyber_slozku": ("Select the output folder", "Vyberte výstupní složku"),
    "filtr_vse":        ("All supported", "Všechny podporované"),
    "filtr_text":       ("Text file", "Textový soubor"),
    "filtr_html":       ("HTML", "HTML"),
    "filtr_zvuk":       ("Audio files", "Zvukové soubory"),
    "filtr_vsechny":    ("All files", "Všechny soubory"),

    # ---------------- Log ----------------
    "log_cache":        ("Model cache: {0}", "Cache modelů: {0}"),
    "log_font":         ("Note: JetBrains Mono could not be loaded, using {0}.",
                         "Poznámka: JetBrains Mono se nepodařilo načíst, používám {0}."),
    "log_ffmpeg":       ("Note: ffmpeg not found - MP3 export unavailable.",
                         "Poznámka: ffmpeg nebyl nalezen - export do MP3 nebude dostupný."),
    "log_sd":           ("Note: sounddevice missing - listening while converting unavailable.",
                         "Poznámka: modul sounddevice chybí - poslech během převodu nebude dostupný."),
    "log_zarizeni":     ("Device: {0}", "Zařízení: {0}"),
    "log_gpu":          ("GPU: {0} ({1:.1f} GB VRAM)", "GPU: {0} ({1:.1f} GB VRAM)"),
    "log_cuda_ne":      ("WARNING: CUDA unavailable, switching to CPU.",
                         "VAROVÁNÍ: CUDA není dostupná, přepínám na CPU."),
    "log_znovu":        ("Model settings changed, reloading.",
                         "Nastavení modelu se změnilo, načítám model znovu."),
    "log_nacitam":      ("Loading Chatterbox Multilingual (~3 GB downloaded on first run only)...",
                         "Načítám model Chatterbox Multilingual (jen při prvním spuštění se stahuje ~3 GB, pak už z cache)..."),
    "log_nacten":       ("Model loaded. Sample rate: {0} Hz",
                         "Model načten. Vzorkovací frekvence: {0} Hz"),
    "log_jazyk_doplnen": ("Language '{0}' added to the model's allowed list (token [{0}]).",
                          "Jazyk '{0}' doplněn do seznamu povolených jazyků (token [{0}])."),
    "log_bez_ft":       ("Note: no language model is loaded for {0}, using the base weights. "
                         "Pronunciation may be accented.",
                         "Poznámka: pro {0} není načten jazykový model, jedou základní váhy. "
                         "Výslovnost může mít přízvuk."),
    "log_ft_cache":     ("Language model loaded from cache (not downloaded again).",
                         "Jazykový model načten z cache (nestahuje se znovu)."),
    "log_ft_stahuji":   ("Downloading language model: {0} (~{1:.1f} GB), one-off - cached afterwards.",
                         "Stahuji jazykový model: {0} (~{1:.1f} GB), jednorázově - příště se vezme z cache."),
    "log_ft_chyba":     ("WARNING: could not download the language checkpoint: {0}",
                         "VAROVÁNÍ: jazykový checkpoint se nepodařilo stáhnout: {0}"),
    "log_ft_zaklad":    ("Continuing with the base multilingual model.",
                         "Pokračuji se základním multilingválním modelem."),
    "log_ft_nenalezen": ("WARNING: no usable checkpoint found in the repository.",
                         "VAROVÁNÍ: v repozitáři nebyl nalezen použitelný checkpoint."),
    "log_ft_aplikuji":  ("Applying checkpoint: {0}", "Aplikuji checkpoint: {0}"),
    "log_ft_hotovo":    ("Language model applied: {0} tensors ({1}).",
                         "Jazykový model aplikován: {0} tenzorů ({1})."),
    "log_ft_castecne":  ("WARNING: checkpoint only partially matches ({0} unknown, {1} uncovered) - "
                         "if the voice sounds wrong, pick a different language model.",
                         "VAROVÁNÍ: checkpoint sedí jen částečně (nerozpoznaných {0}, nepokrytých {1}) - "
                         "pokud bude hlas divný, zvolte jiný jazykový model."),
    "log_ft_nepovedlo": ("WARNING: could not apply the checkpoint ({0}). Continuing with the base model.",
                         "VAROVÁNÍ: checkpoint se nepodařilo použít ({0}). Pokračuji se základním modelem."),
    "log_lang_ne":      ("WARNING: the model rejected language_id='{0}' ({1}). "
                         "Generating without a language - quality may suffer.",
                         "VAROVÁNÍ: parametr language_id='{0}' model nepřijal ({1}). "
                         "Generuji bez určení jazyka - kvalita může být horší."),
    "log_stahovani":    ("{0}: {1:.2f} GB downloaded ...", "{0}: staženo {1:.2f} GB ..."),
    "log_zaklad_model": ("Base model", "Základní model"),
    "log_ft_popis":     ("Czech fine-tune", "Český fine-tune"),
    "log_nacitam_sbor": ("Loading {0} ...", "Načítám {0} ..."),
    "log_nacteno":      ("Loaded: {0} chars, {1} blocks (max {2} chars per block).",
                         "Načteno: {0} znaků, {1} bloků (max {2} znaků na blok)."),
    "log_ukazka_bloku": ("First block preview: {0}", "Ukázka prvního bloku: {0}"),
    "log_chyba":        ("ERROR: {0}", "CHYBA: {0}"),
    "log_generuji_uk":  ("Generating sample...", "Generuji ukázku..."),
    "log_ukazka_hotova": ("Sample done in {0:.1f} s (length {1:.1f} s): {2}",
                          "Ukázka hotova za {0:.1f} s (délka {1:.1f} s): {2}"),
    "log_chyba_test":   ("ERROR during voice test: {0}", "CHYBA při testu hlasu: {0}"),
    "log_ulozen":       ("File saved: {0}", "Soubor uložen: {0}"),
    "log_start":        ("Conversion started: {0} blocks -> {1}",
                         "Start převodu: {0} bloků -> {1}"),
    "log_obalka":       ("Cover generated: {0}", "Obálka vygenerována: {0}"),
    "log_obalka_ne":    ("WARNING: could not generate the cover.",
                         "VAROVÁNÍ: obálku se nepodařilo vygenerovat."),
    "log_obalka_nahled": ("Could not show the cover preview: {0}",
                          "Náhled obálky se nepodařilo zobrazit: {0}"),
    "log_zastaveno_na": ("Stopped by user at block {0}/{1}.",
                         "Zastaveno uživatelem na bloku {0}/{1}."),
    "log_prubeh":       ("{0}/{1} blocks  |  audio {2}  |  elapsed {3}  |  left ~{4}",
                         "{0}/{1} bloků  |  audio {2}  |  uplynulo {3}  |  zbývá ~{4}"),
    "log_dobira":       ("Generating finished, playback is draining the remaining {0}.",
                         "Generování hotovo, přehrávání dobírá zbývajících {0}."),
    "log_mp3":          ("Converting output to MP3 (ffmpeg)...",
                         "Převádím výstup do MP3 (ffmpeg)..."),
    "log_mp3_hotovo":   ("MP3 done: {0}", "MP3 hotovo: {0}"),
    "log_mp3_selhal":   ("WARNING: MP3 conversion failed, keeping WAV.",
                         "VAROVÁNÍ: převod do MP3 selhal, zůstává WAV."),
    "log_hotovo":       ("DONE.", "HOTOVO."),
    "log_zastaveno_ul": ("STOPPED - partial file saved.",
                         "ZASTAVENO - uložena rozpracovaná část."),
    "log_souhrn":       (" Audio length {0}, size {1:.1f} MB.",
                         " Délka audia {0}, velikost {1:.1f} MB."),
    "log_neuspesne":    (" Failed blocks: {0}.", " Neúspěšných bloků: {0}."),
    "log_dlouhy":       ("Block {0}/{1}: suspiciously long output ({2:.1f} s for {3} chars), retrying.",
                         "Blok {0}/{1}: podezřele dlouhý výstup ({2:.1f} s pro {3} znaků), generuji znovu."),
    "log_prazdny":      ("Block {0}/{1}: empty output, retrying.",
                         "Blok {0}/{1}: prázdný výstup, opakuji."),
    "log_pokus":        ("Block {0}/{1} - attempt {2} failed: {3}",
                         "Blok {0}/{1} - pokus {2} selhal: {3}"),
    "log_preskocen":    ("Block {0}/{1} SKIPPED: {2}...", "Blok {0}/{1} PŘESKOČEN: {2}..."),
    "log_pokracuji":    ("Resuming...", "Pokračuji..."),
    "log_pozastaveno":  ("Paused. Playback keeps draining the buffer, generating is halted.",
                         "Pozastaveno. Přehrávání dobírá zásobu, generování stojí."),
    "log_poslech_zar":  ("Listening: {0}, head start {1} s", "Poslech: {0}, náskok {1} s"),
    "log_poslech_start": ("Starting playback (head start {0} s).",
                          "Spouštím přehrávání (náskok {0} s)."),
    "log_poslech_kratky": ("Starting playback - generating ended before the head start was reached "
                           "(got {0} s).",
                           "Spouštím přehrávání - generování skončilo dřív, než se nastřádal náskok "
                           "(mám {0} s)."),
    "log_poslech_chyba": ("PLAYBACK ERROR: {0}", "CHYBA přehrávání: {0}"),
    "log_poslech_vyp":  ("WARNING: listening while converting disabled - {0}",
                         "VAROVÁNÍ: poslech během převodu vypnut - {0}"),
    "log_poslech_zap":  ("Listening enabled mid-conversion - it picks up from the block being "
                         "generated now, not from the start of the book.",
                         "Poslech zapnut během převodu - navazuje se od právě generovaného bloku, "
                         "ne od začátku knihy."),
    "log_poslech_off":  ("Listening turned off, conversion continues.",
                         "Poslech vypnut, převod běží dál."),
    "log_poslech_pauza": ("Playback paused, the buffer keeps growing.",
                          "Poslech pozastaven, zásoba mezitím roste."),
    "log_poslech_hraj": ("Resuming playback.", "Pokračuji v poslechu."),
    "log_kapitoly":     ("Chapters detected: {0}", "Rozpoznáno kapitol: {0}"),
    "log_po_kapitolach": ("Writing one MP3 per chapter ({0} chapters).",
                          "Zapisuji jeden MP3 na kapitolu ({0} kapitol)."),
    "log_kapitola_hotova": ("Chapter {0} done: {1}", "Kapitola {0} hotova: {1}"),
    "log_souhrn_kapitoly": (" {0} chapter files written.", " Zapsáno {0} souborů kapitol."),
    "log_navazuji":     ("Resuming at block {0} of {1}.", "Navazuji od bloku {0} z {1}."),
    "log_navazuji_soubor": ("Continuing file {0}, already {1} of audio.",
                            "Navazuji na soubor {0}, zatím {1} audia."),
    "log_lze_navazat":  ("Progress saved - you can resume this book later.",
                         "Postup uložen - v knize lze později pokračovat."),
    "log_postup_neplatny": ("A saved progress file exists but the book or settings changed, "
                            "so it cannot be resumed. Starting over.",
                            "Uložený postup existuje, ale kniha nebo nastavení se změnily, "
                            "takže na něj nelze navázat. Začínám znovu."),
    "log_kapitola_rozdelana": ("Chapter left unfinished as {0} - it will be completed on resume.",
                               "Kapitola zůstala rozdělaná jako {0} - dokončí se při navázání."),
    "log_pool_start":   ("Starting {0} generator processes ({1:.1f} GB VRAM free)...",
                         "Spouštím {0} generujících procesů (volných {1:.1f} GB VRAM)..."),
    "log_pool_pripraven": ("{0} processes ready, generating in parallel.",
                           "{0} procesů připraveno, generuji souběžně."),
    "log_pool_selhal":  ("WARNING: parallel generation failed to start ({0}), "
                         "falling back to a single process.",
                         "VAROVÁNÍ: souběžné generování se nepodařilo spustit ({0}), "
                         "pokračuji jedním procesem."),
    "log_pool_jeden":   ("Generating in a single process.", "Generuji jedním procesem."),
    "lab_pracovniku":   ("parallel processes", "souběžných procesů"),
    "dlg_navazat":      ("Resume", "Navázat"),
    "dlg_navazat_text": ("This book was interrupted at block {0} of {1} ({2:.0f} %).\n\nYes - continue where it stopped\nNo - start over\nCancel - do nothing",
                         "Tato kniha byla přerušena na bloku {0} z {1} ({2:.0f} %).\n\nAno - pokračovat tam, kde to skončilo\nNe - začít znovu\nZrušit - neprovádět nic"),
    "log_jazyk":        ("Interface language: {0}", "Jazyk rozhraní: {0}"),
}
