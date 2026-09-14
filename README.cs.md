# Audiobookery — česká verze

*[English version](README.md)*

Desktopová aplikace (Python + tkinter) pro výrobu audioknih z vlastní knihovny.
Běží celá lokálně na vaší GPU — text jde dovnitř, hotová audiokniha ven.
Postavená na [Chatterbox TTS](https://github.com/resemble-ai/chatterbox) od Resemble AI.

## Co umí

- Načte **TXT, EPUB, FB2, HTML** (a Markdown), automaticky detekuje kódování (chardet)
- Vyčistí text a rozdělí ho na bloky po větách (výchozí limit 200 znaků)
- Generuje řeč modelem Chatterbox Multilingual s češtinou (`language_id="cs"`,
  viz [poznámka o češtině](#čeština-v-chatterboxu--jak-to-doopravdy-je))
- Volitelné **klonování hlasu** z vlastní referenční nahrávky
- Průběžně zapisuje výsledek do jednoho WAV, volitelně převede na MP3 (ffmpeg)
- **Přehrává během převodu** — nemusíte čekat, až se kniha dogeneruje celá
- Progress bar, ETA, log, pauza a zastavení
- Tmavé minimalistické rozhraní, písmo JetBrains Mono (přibalené, neinstaluje se)
- Tlačítko **Test hlasu** pro rychlou ukázku před plným převodem
- Model i cache se ukládají do `./model_cache/` vedle skriptu

## Instalace a spuštění

Stačí spustit:

```bat
run.bat
```

Skript při prvním běhu vytvoří `.venv` (přes `uv`, jinak přes `python -m venv`),
nainstaluje PyTorch s CUDA (cu124) a zbytek závislostí, a spustí aplikaci.

### Ruční instalace

```bat
uv venv .venv --python 3.11
uv pip install --python .venv\Scripts\python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cu124
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe audiobookery.py
```

Ověření GPU:

```bat
.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"
```

### MP3 export

Vyžaduje `ffmpeg` v PATH (nebo `./ffmpeg/bin/ffmpeg.exe`). Bez něj aplikace
nabídne jen WAV.

## První spuštění

Model (~3 GB) se stáhne z Hugging Face automaticky do `./model_cache/`.
S českým fine-tune je to o 2,1 GB víc. Stažení proběhne až při prvním generování
(test hlasu nebo převod) — do té doby se nic nestahuje.

## Postup použití

1. **Zdrojová kniha** — vyberte soubor, aplikace hned ukáže počet znaků, bloků
   a odhad výsledné délky audia.
2. **Hlas** — vyberte referenční WAV (7–20 s, mono, čistý záznam bez hudby
   a šumu). Tlačítkem **Test hlasu** si poslechněte ukázku.

   Referenční nahrávka je formálně nepovinná, ale pro audioknihu ji chcete.
   Bez ní se použije výchozí hlas zabudovaný v modelu (`conds.pt`) — konkrétní
   anglicky mluvící člověk, kterého si Resemble AI vybral. Ten hlas nezměníte
   ničím jiným než vlastní nahrávkou: český fine-tune opravuje **výslovnost**,
   ne to, kdo mluví. Identita mluvčího pochází výhradně z referenčního audia.
3. **Nastavení** — viz tabulka níže.
4. **Výstup** — složka, název, WAV nebo MP3.
5. **Spustit převod.** Průběh se dá kdykoli pozastavit i zastavit; dosud
   vygenerovaná část zůstane v souboru.

## Okno

Po spuštění je vidět karta nastavení ve třech sloupcích — **zdroj** (kniha
a jazyk), **hlas** (nahrávka a zkušební věta s tlačítkem *přehrát*)
a **výstup** (název, složka, WAV nebo MP3 s odhadem velikosti a přepínače
obálky, lupanců, ořezu a rychlého dekodéru). Expresivita, tempo, teplota,
velikost bloku, seed a kontrola přepisem jsou pod *pokročilé…*. Pod nastavením
je karta *připraveno k převodu* a seznam rozdělaných převodů s odkazem
*pokračovat*.

*Spustit převod* sbalí nastavení do jednoho souhrnného řádku a ukáže převod:
obálku, průběh knihy, proužek s jedním dílkem za kapitolu, přehrávač a seznam
kapitol. Podrobný průběh (log) je dole a sbalený.

Tlačítko ☀/☾ v záhlaví přepíná tmavé a světlé schéma. Okno se přizpůsobí
velikosti — na malé obrazovce se sloupce složí pod sebe a obsah se posouvá,
takže nic nezůstane pod hranou.

## Poslech během převodu

Přehrávač hraje přímo z výstupní složky: hotové kapitoly i tu, která právě
vzniká — tu dočítá, jak přibývají bloky. Na žádný náskok se nečeká. Posouvat
se dá kamkoli do už vygenerovaného zvuku, o 15 a 30 s, mezi kapitolami nebo
výběrem v seznamu. *Skočit na živé* přeskočí na nejnovější zvuk. Když
přehrávání dožene generování, počká na další blok.

Vlna pod ovládáním pokrývá hranou kapitolu: přehraná část plnou barvou,
vygenerovaná tlumeně a zbytek kapitoly — délka odhadnutá z textu — jako
nízká linka.

Zvukový výstup obstarává `sounddevice` (PortAudio). Když modul chybí,
přehrávač nebude dostupný a do logu se to napíše — převod běží dál.

## Jazyk rozhraní

Rozhraní umí **anglicky a česky**, výchozí je angličtina. Přepíná se rozbalovacím
seznamem vpravo nahoře a volba se ukládá do `config.json`. Přepnutí okno
přestaví za běhu — rozdělaný převod ani obsah logu se neztratí.

Texty jsou v samostatném souboru [`preklady.py`](preklady.py) jako slovník
`klíč -> (English, Čeština)`. Další jazyk se přidá rozšířením `JAZYKY` a
doplněním třetí položky do každé n-tice; kód aplikace se měnit nemusí.

**Výstup `run.bat` v konzoli je vždy anglicky**, bez ohledu na nastavení
v aplikaci. Je to první věc, kterou uživatel uvidí — ještě před otevřením okna —
a nemá smysl, aby na cizince vyskočil text v jazyce, kterému nerozumí.

Pozor na záměnu: `chatterbox · cs` v záhlaví je jazyk **syntézy** (čeština),
ne jazyk rozhraní.

## Vzhled a písmo

Rozhraní je tmavé a záměrně strohé. Písmo
[JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono) je přibalené
ve složce `fonts/` a načítá se **jen pro běžící proces** přes
`AddFontResourceEx` s příznakem `FR_PRIVATE` — do systému se nic neinstaluje
a nejsou potřeba práva správce. Když se načtení nepodaří, aplikace sáhne po
Cascadia Mono nebo Consolas a napíše to do logu.

Font je pod licencí OFL 1.1.

## Příprava referenční nahrávky

Model si z referenčního audia bere identitu mluvčího. Chce to 10–20 s souvislé
čisté řeči — bez hudby, ozvěny, šumu a bez druhého hlasu. Delší nahrávka nic
nezkazí, ale ani nepomůže: dekodér si stejně bere jen prvních 10 sekund.

Máte-li delší záznam, najděte v něm souvislý úsek ohraničený pauzami:

```bash
ffmpeg -hide_banner -i zdroj.mp3 -af "silencedetect=noise=-32dB:d=0.6" -f null -
```

Výpis ukáže `silence_start` / `silence_end`. Vyberte úsek mezi dvěma pauzami
a vyřízněte ho rovnou do formátu, který model používá nativně — mono, 24 kHz:

```bash
ffmpeg -y -ss 47.65 -t 14 -i zdroj.mp3 -vn -ac 1 -ar 24000 -af "loudnorm=I=-20:TP=-3:LRA=7" -c:a pcm_s16le hlasy/muj_hlas.wav
```

`-vn` zahodí vloženou obálku, `loudnorm` srovná hlasitost. Vyhněte se začátku
nahrávky — bývá tam znělka nebo ohlášení titulu jiným hlasem.

## Parametry generování

| Parametr | Výchozí | Co dělá |
|---|---|---|
| Expresivita (exaggeration) | 0.5 | Míra emocí. Pro klidné čtení audioknihy 0.3–0.5, dramatičtější přednes 0.7+. |
| CFG / tempo (cfg_weight) | 0.5 | Nižší hodnota = pomalejší, rozvážnější tempo. Pro rychle mluvící referenční hlas zkuste 0.3. |
| Teplota | 0.8 | Variabilita. Nižší = stabilnější a předvídatelnější, vyšší = živější, ale rizikovější. |
| Max. znaků na blok | 200 | Delší bloky znějí plynuleji, ale zvyšují riziko artefaktů a nároky na VRAM. |
| Pauza mezi bloky | 250 ms | Ticho vkládané mezi bloky. |
| Seed | 0 | 0 = náhodný. Nenulová hodnota dělá výsledek reprodukovatelný. |
| Zařízení | auto | `auto` zvolí CUDA, pokud je dostupná. |
| Rychlý dekodér | vypnuto | Destilovaný dekodér z Chatterbox Turbo. Na 2080 Ti převod zrychlí zhruba o 11 %, zvuk zůstává obdobný. Při prvním zapnutí stáhne 1,1 GB. |
| Kontrola přepisem | vypnuto | U bloku, kde selžou všechny tři pokusy, vybere přes Whisper ten nejbližší textu. Při prvním zapnutí stáhne 1,6 GB. |

## Jazyk knihy a katalog modelů

Jazyk syntézy je **nezávislý na jazyku rozhraní** — v českém rozhraní klidně
vyrobíte anglickou audioknihu. Vybírá se v sekci *kniha* a nabídka se čte
z [`modely.json`](modely.json), který si můžete rozšířit sami.

Nabídka má dvě vrstvy:

**23 jazyků přímo v základním modelu** — angličtina, španělština, němčina,
francouzština, italština, portugalština, nizozemština, polština, ruština,
švédština, dánština, norština, finština, řečtina, turečtina, hebrejština,
arabština, hindština, japonština, korejština, čínština, malajština, svahilština.
Nic se nestahuje, fungují hned.

**Jazyky přes komunitní checkpoint** — stáhne se ~2,1 GB při prvním použití
a přepíše váhy modulu T3. Katalog nese jen repozitáře, které jsem ověřil proti
Hugging Face API:

| Jazyk | Repozitář | Ověřeno | Pozn. |
|---|---|---|---|
| Čeština | `Thomcles/Chatterbox-TTS-Czech` | ano | vyžaduje přihlášení |
| Slovenština | `pekiskol/chatterbox-tts-slovak` | ano | |
| Portugalština (BR) | `ResembleAI/Chatterbox-Multilingual-pt-br` | ne | oficiální od Resemble AI |
| Francouzština | `Thomcles/Chatterbox-TTS-French` | ne | zlepšuje nativní jazyk |
| Perština | `Thomcles/Chatterbox-TTS-Persian-Farsi` | ano | tokenizer nemá token |
| Estonština | `Mamsu/chatterbox-tts-et-lobiseja` | ano | tokenizer nemá token |

*Ověřeno* znamená, že checkpoint má **přesně stejnou velikost jako
`t3_mtl23ls_v2.safetensors`** (2 143 989 752 B), takže je to fine-tune téhož
modulu a váhy sednou klíč po klíči. U neověřených se velikost liší; aplikace
je zkusí načíst a do logu napíše, kolik klíčů nesedlo.

Popisek vedle výběru rovnou říká, co daná volba obnáší — jestli je model už
stažený, kolik se stáhne, jestli je komunitní a jestli vyžaduje přihlášení.

Při změně jazyka se model načte znovu od základu. Nechat na T3 váhy
z předchozího jazyka by bylo horší než nic.

### Přidání vlastního modelu

Do `modely.json` přidejte položku:

```json
{"kod": "hu", "nazev": "Magyar", "zdroj": "finetune", "token": true,
 "repo": "uzivatel/muj-chatterbox-hu", "gated": false, "overeno": false,
 "velikost_gb": 2.1}
```

`token` říká, jestli má tokenizer jazykový token `[kod]`. Bez něj model jazyk
neumí označit a výsledek bývá horší. Tokeny navíc oproti 23 trénovaným jazykům:
`cs`, `sk`, `bg`, `hu`, `ro`, `ta`, `vi`.

## Čeština v Chatterboxu — jak to doopravdy je

Stojí za to vědět, co se pod kapotou děje, protože oficiální dokumentace mluví
o 23 jazycích a čeština mezi nimi není:

- Tokenizer, který se s modelem načítá (`grapheme_mtl_merged_expanded_v1.json`),
  **jazykový token `[cs]` obsahuje** — stejně jako `[sk]`, `[bg]`, `[hu]`.
- Českou diakritiku pokrývá beze zbytku: text se normalizuje přes NFKD, takže
  `š` se rozloží na `s` + kombinovaný háček, a všechny tyto znaky ve slovníku
  jsou.
- Blokuje to jen kontrolní seznam `SUPPORTED_LANGUAGES` v kódu balíku —
  `generate(language_id="cs")` by jinak skončilo na `ValueError`. Aplikace do
  toho seznamu češtinu při startu doplní a do logu to napíše.
- Váhy základního modelu ale na češtině trénované nebyly. Výsledek je
  srozumitelný, s cizím přízvukem a občasnou chybnou výslovností.

**Proto je český fine-tune ve výchozím stavu zapnutý.** Bez něj model čte českou
větu jako cizinec, který jazyk nikdy neslyšel.

### Český fine-tune

Volba *Český fine-tune* stáhne checkpoint
[`Thomcles/Chatterbox-TTS-Czech`](https://huggingface.co/Thomcles/Chatterbox-TTS-Czech)
(soubor `t3_cs.safetensors`, ~2,1 GB) a aplikuje jeho váhy na modul T3
základního modelu. Checkpoint je fine-tune přesně toho T3, který Chatterbox
načítá (`t3_mtl23ls_v2`), takže váhy sedí jedna ku jedné — všech 292 tenzorů.

Z těch 292 se jich od základního modelu liší 277: přetrénovaný je transformer,
`text_head` i `speech_emb`. Beze změny zůstal `cond_enc`, tedy kondicionální
enkodér — a právě proto fine-tune mění výslovnost, ale ne to, kdo mluví.

Repozitář je na Hugging Face **chráněný (gated)** — bez přihlášení stahování
skončí chybou 401. Jednorázově je potřeba:

1. Přihlásit se na [huggingface.co](https://huggingface.co) a na
   [stránce modelu](https://huggingface.co/Thomcles/Chatterbox-TTS-Czech)
   potvrdit přístup (schvaluje se automaticky).
2. Vytvořit si *read* token v Settings → Access Tokens.
3. Přihlásit se v prostředí aplikace:

```bat
.venv\Scripts\huggingface-cli.exe login
```

Případně stačí nastavit proměnnou prostředí `HF_TOKEN`. Pokud se checkpoint
stáhnout nepodaří, aplikace to napíše do logu i s návodem a pokračuje se
základním modelem.

## Výkon (naměřeno na RTX 2080 Ti, 11 GB)

| Veličina | Hodnota |
|---|---|
| Rychlost generování | zhruba **1× realtime** — minuta audia trvá asi minutu |
| Obsazená VRAM | ~3,3 GB (zbytek karty zůstává volný) |
| Načtení modelu při startu | ~20 s (z lokální cache) |

Počítejte tedy, že osmihodinová audiokniha se generuje zhruba **osm hodin**.
První generování po startu je pomalejší kvůli zahřátí CUDA jader — do rychlosti
se to srovná po pár blocích. Aplikaci můžete kdykoli pozastavit; hotová část
zůstává v souboru.

Aplikace hlídá typickou vadu autoregresivních TTS modelů: pokud je vygenerovaný
blok nepřiměřeně dlouhý vůči počtu znaků (halucinační smyčka), zopakuje ho
s jiným seedem. Po třech neúspěších blok přeskočí a napíše to do logu.

## Struktura

```
audiobookery/
  audiobookery.py      # celá aplikace
  preklady.py          # texty rozhraní (en / cs)
  run.bat          # instalace + spuštění
  requirements.txt     # závislosti
  README.md            # tento soubor
  config.json          # nastavení (vytvoří se automaticky)
  fonts/               # přibalený JetBrains Mono (OFL 1.1)
  hlasy/               # referenční nahrávky pro klonování hlasu
  model_cache/         # stažené modely a HF cache
  temp/                # ukázky z testu hlasu
  vystup/              # výchozí složka pro audioknihy
```

## Řešení problémů

**`CUDA: False`** — nainstaloval se CPU build PyTorche. Smažte `.venv` a spusťte
`run.bat` znovu, nebo torch přeinstalujte ručně s `--index-url .../cu124`.
Verze torche je v `run.bat` připnutá na 2.6.0 záměrně: chatterbox-tts ji
vyžaduje přesně, a bez pinu by pip CUDA build přepsal CPU verzí z PyPI.

**`TypeError: 'NoneType' object is not callable` u `PerthImplicitWatermarker`** —
watermarker uvnitř chatterboxu importuje `pkg_resources`. To dodává `setuptools`,
které ale ve venv od `uv` chybí a od verze 81 už `pkg_resources` neobsahuje.
Řeší to pin `setuptools>=70,<81` v `requirements.txt`:

```bat
.venv\Scripts\python.exe -m pip install "setuptools<81"
```

**Instalace spadne na `pkuseg` / `No module named 'numpy'`** — resolver sáhl po
staré verzi chatterboxu (0.1.3), která závisí na balíku `pkuseg` a ten se na
Windows musí kompilovat ze zdroje. Proto je v `requirements.txt`
`chatterbox-tts==0.1.7` — toto vydání používá hotové kolo `spacy-pkuseg`.
Verze je zamčená přesně, ne jako minimum: vývojová větev chatterboxu už nemá
analyzátor zarovnání, na kterém stojí kontrola vadných bloků i oprava
usekávání vět, a novější vydání by je potichu vypnulo.

**Stahuje se `spacy_ontonotes.zip` (34 MB) do `C:\Users\<vy>\.pkuseg`** — to je
čínský segmentátor, který si tokenizer inicializuje bez ohledu na zvolený jazyk.
Cestu má napevno v sobě, na češtinu vliv nemá a stáhne se jen jednou.

**`CUDA out of memory`** — snižte *Max. znaků na blok* (např. na 120), zavřete
ostatní aplikace využívající GPU, případně přepněte zařízení na `cpu`
(výrazně pomalejší).

**Špatná diakritika u TXT** — chardet se netrefil. Uložte soubor v UTF-8;
aplikace zkouší UTF-8, CP1250 a ISO-8859-2 jako fallback.

**Robotický nebo kolísavý hlas** — kvalita referenční nahrávky je zásadní.
Použijte 10–20 s čisté řeči bez hudby, ozvěny a šumu, ideálně mono 24 kHz WAV.
Pomáhá také snížit teplotu na 0.6 a expresivitu na 0.4.

**Model se stahuje pořád dokola** — zkontrolujte, že složka `model_cache`
je zapisovatelná; aplikace do ní směruje `HF_HOME` i `HUGGINGFACE_HUB_CACHE`.

## Než něco zveřejníte

**Práva ke knize.** Převádějte jen díla, u nichž na to máte právo — vlastní
texty, volná díla, nebo knihy, jejichž licence to dovoluje. Legálně koupená
e-kniha vám sama o sobě nedává právo vydat její zvukovou verzi.

**Práva k hlasu.** Referenční nahrávka je hlas konkrétního člověka. Naklonovat
interpreta z komerční audioknihy a výsledek zveřejnit je problém hned dvakrát —
nahrávka je chráněná a hlas patří někomu, kdo k tomu nedal souhlas. Pro vlastní
poslech je to jiná situace, ale „udělal jsem si to pro sebe" přestává platit ve
chvíli, kdy to začnete sdílet.

**Nevydávejte se za někoho jiného.** Nepoužívejte naklonovaný hlas k tomu, aby
to vypadalo, že někdo řekl něco, co nikdy neřekl.

**Watermark.** Chatterbox vkládá do všeho, co vygeneruje, neslyšitelnou značku
[Perth](https://github.com/resemble-ai/perth). Aplikace ji neodstraňuje a její
odstraňování není podporované použití tohoto projektu.

Za to, co s nástrojem vyrobíte, autoři neodpovídají.

## Výslovnost měkkého ti, di, ni

Model občas přečte *tichý* jako *tychý*. U českých knih proto aplikace sama doplní
háček do 53 330 tvarů slov, u kterých Wikislovník potvrzuje měkkou výslovnost —
*ťichý, ďivadlo, ňikdo* — a přejatá slova jako *politika* nebo *diplom* nechá být.
U slov, která ve Wikislovníku nemají nahrávku, o přejatosti rozhoduje jednoduchý
klasifikátor naučený na datech Wikislovníku.
Slovník je v `vyslovnost_cs.json` a vyrábí ho `vyslovnost_wiki.py`; podrobnosti
a měření jsou v anglickém README.

Slova, která model přesto čte špatně, dopište do `vyslovnost.json`. Vaše pravidla
se uplatní dřív a mají přednost. Data slovníku jsou pod licencí CC BY-SA 4.0.

## Vadné bloky a oprava úseku

Model občas po dočtení věty nepřestane a pauzu vyplní desítkou sekund hučení,
jindy kus textu vynechá. Chatterbox zakazuje token konce řeči, dokud pozornost
nedojde na konec textu — když se zarovnání ztratí, generuje až do stropu 1000
tokenů. V nahlášené kapitole měl blok o 192 znacích **25,5 s**, z toho posledních
13 s hučení a šumu.

Aplikace po každém bloku přečte analyzátor zarovnání modelu a blok vygeneruje
znovu s jiným seedem, když:

- za bodem, kde text došel, následuje víc než 1,6 s, nebo je víc než 0,8 s za
  ním slyšet řeč,
- model text nedočetl do konce,
- uvnitř bloku je úsek bez řeči delší než 3,5 s — ticho i brblání, protože
  hučení kolem −34 dBFS je na pauzu moc hlasité a jeho krátké hlasitější
  záchvěvy se za řeč nepočítají,
- blok nad 100 znaků se čte pomaleji než 9,5 znaku za sekundu — běžné čtení
  jede 13,4 a takhle protažený blok skoro vždy vyplňuje brblání.

Když jsou vadné všechny tři pokusy, záleží na vadě. Ocas za větou jde uříznout,
takže pokus zůstane a ocas se usekne 0,8 s za koncem textu. Brblání uvnitř
bloku, pomalé čtení ani nedočtený text vyříznout nejde — blok se proto rozdělí
na kratší části, ty se vygenerují zvlášť a vloží na stejné místo. Kratší text
model rozhodí méně. Nejméně poškozený pokus zůstane jen tam, kde už dělit nejde. Kalibrováno na 58 blocích z nahlášené knihy: ze 41
běžných generování kontrola označila 4 a přepis u každého potvrdil skutečnou
chybu — chybějící „Ani zdaleka.", useknutou větu, zkomolený začátek a jméno
opakované ve smyčce. Převod se tím prodlouží zhruba o desetinu.

**Chyba v chatterboxu, která usekávala věty.** Chatterbox 0.1.7 vynutí konec
řeči, jakmile jsou dva poslední řečové tokeny stejné — kdykoli, podmínka „až po
dočtení textu" je v jeho kódu zakomentovaná. Dva stejné tokeny za sebou jsou
přitom běžné, třeba v delší pauze, a zbytek bloku se ztratil. Aplikace teď to
pravidlo nechá platit až po dočtení. Na 45 blocích se stejnými seedy se konec
vynutil třikrát a pokaždé chyběla poslední věta (shoda přepisu 0,84–0,93). Po
opravě se všechny tři dočetly (0,98–0,99) a jiný blok se nezměnil. Hlavní větev
chatterboxu mezitím analyzátor úplně odstranila, opravené vydání tedy nepřijde.

**Kontrola přepisem** (pokročilé nastavení, poprvé stáhne 1,6 GB). Když jsou
vadné všechny tři pokusy, Whisper je přepíše a vybere ten nejbližší textu. Běží
na kartě, když je vedle modelu hlasu místo, jinak na procesoru. Přeřek v jedné
hlásce nenajde — „pšipomínal" přepíše jako „připomínal".

**Oprava úseku.** *Opravit toto místo* v přehrávači otevře dialog rovnou na
hraném místě, *opravit úsek v hotové knize…* na úvodní obrazovce nad libovolným
hotovým souborem.
Zadáte čas, kde jste chybu slyšeli, aplikace najde blok a ukáže jeho text — už
po úpravách výslovnosti, takže „první" se tam objeví jako „prvňí". Text jde
upravit a přečte se přesně tak, jak je napsaný. *Vygenerovat znovu* udělá
nový pokus hlasem knihy a přehraje ho, *nahradit v souboru* ho vymění. MP3 si
ponechá ID3 i obálku a jednou se překóduje, předchozí verze se zazálohuje do
`temp/zalohy_oprav/`. Rozepsaná kapitola přerušeného převodu se nenabízí —
navázání by ji usekl na uloženou délku.

Převod ukládá vedle výstupu `<kniha>.blocks.jsonl` s polohou každého bloku.
Soubory z doby před 1.11 mapu nemají: načtěte stejnou knihu se stejným počtem
znaků na blok a pauzou a bloky se dohledají podle ticha mezi nimi. Na všech
deseti kapitolách nahlášené knihy hranice seděly přesně.

## Použité modely a licence

| Součást | Licence | Poznámka |
|---|---|---|
| Audiobookery | MIT | tento repozitář |
| [Chatterbox TTS](https://github.com/resemble-ai/chatterbox) | MIT | vlastní engine |
| [`ResembleAI/chatterbox`](https://huggingface.co/ResembleAI/chatterbox) | viz karta modelu | základní váhy, stahují se za běhu |
| [`ResembleAI/chatterbox-turbo`](https://huggingface.co/ResembleAI/chatterbox-turbo) | MIT | rychlý dekodér, stahuje se jen po zapnutí |
| [`openai/whisper-large-v3-turbo`](https://huggingface.co/openai/whisper-large-v3-turbo) | MIT | kontrola přepisem, stahuje se jen po zapnutí |
| Jazykové checkpointy | viz karta každého modelu | komunitní práce, podmínky se liší |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | OFL 1.1 | přibalené ve `fonts/`, viz `fonts/OFL.txt` |
| Data výslovnosti z [Wikislovníku](https://cs.wiktionary.org/) | CC BY-SA 4.0 | přibalená jako `vyslovnost_cs.json` |

V repozitáři nejsou žádné váhy modelů. Vše se stahuje z Hugging Face při prvním
použití, za podmínek, které daný model nese.

## Poděkování

[@tomhol](https://github.com/tomhol) za dohledání dvou úniků, kvůli kterým se dlouhé
převody postupně zpomalovaly, a za tip na destilovaný dekodér.

