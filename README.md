# Audiobookery

Turn the e-books you own into audiobooks, entirely on your own machine.

Text goes in, a finished audiobook comes out — read in a voice you choose, with
no cloud service, no account, and no per-character billing. Built on
[Chatterbox TTS](https://github.com/resemble-ai/chatterbox) by Resemble AI.

*[Česká verze / Czech version](README.cs.md)*

![Audiobookery](docs/screenshot.png)

---

## What it does

- Reads **TXT, EPUB, FB2, HTML** and Markdown, detecting the encoding on its own
- Cleans the text, rejoins hard-wrapped lines and splits it into blocks by sentence
- Generates speech in **29 selectable languages** — 23 built into the base model,
  6 more through community checkpoints downloaded on demand
- **Clones a voice** from a short reference recording
- **Plays while it converts**, so you can start listening before the book is done
- Writes one WAV as it goes, optionally converts to MP3 with an embedded cover
- Generates a **cover image** from the book title, entirely offline
- Dark, deliberately plain interface in English or Czech

## Requirements

| | |
|---|---|
| OS | Windows 10/11 (the launcher is a `.bat`; the Python code itself is portable) |
| GPU | NVIDIA with ~4 GB free VRAM. CPU works but is far slower |
| Python | 3.10+ — [uv](https://docs.astral.sh/uv/) is used if present, otherwise `venv` |
| Disk | ~8 GB — 3 GB base model, 2.5 GB PyTorch, 2.1 GB per language checkpoint |
| Optional | `ffmpeg` on PATH for MP3 export |

Measured on an RTX 2080 Ti: about **1× realtime** and ~3.3 GB VRAM. An
eight-hour audiobook takes roughly eight hours to generate.

## Getting started

```bash
git clone https://github.com/iammartinj/audiobookery-tts.git
```

Then run `run.bat`. On first launch it creates `.venv`, installs PyTorch with
CUDA and the remaining dependencies, and starts the application. The speech
model (~3 GB) downloads on first generation, not at install time.

Everything lands next to the script in `model_cache/` — nothing is written to
your user profile except a 34 MB Chinese segmenter that a dependency insists on
placing in `~/.pkuseg`.

## Using it

1. **Book** — pick a file and the language it is written in.
2. **Voice** — pick a reference recording, 10–20 s of clean speech, mono.
   Without one you get the model's built-in English-speaking voice, which will
   read every language with an English accent. Use **test voice** to check
   before committing to a whole book.
3. **Output** — folder, name, WAV or MP3.
4. **Start conversion.** Pause or stop at any point; whatever was generated
   stays in the file.

Advanced parameters — expressiveness, pace, temperature, block size, seed —
live behind the *advanced settings* toggle. Defaults are tuned for calm
narration.

### Listening while it converts

Generation runs slightly slower than playback (~0.95×), so the buffer drains
gradually. Audiobookery waits until a **head start** has accumulated and then
starts playing. The buffer shrinks about twenty times slower than it grows:

| head start | uninterrupted listening |
|---|---|
| 1 min | ~20 minutes |
| 3 min | ~1 hour |
| 30 min | a whole eight-hour book |

If the buffer does run dry, playback waits for the next block. Nothing breaks.

### Preparing a reference recording

Find a continuous stretch bounded by pauses:

```bash
ffmpeg -hide_banner -i source.mp3 -af "silencedetect=noise=-32dB:d=0.6" -f null -
```

Cut it straight into the format the model uses natively — mono, 24 kHz:

```bash
ffmpeg -y -ss 47.65 -t 14 -i source.mp3 -vn -ac 1 -ar 24000 -af "loudnorm=I=-20:TP=-3:LRA=7" -c:a pcm_s16le voices/my_voice.wav
```

Avoid the very beginning of a recording — it usually holds a jingle or a title
announcement in a different voice.

## Languages

The synthesis language is independent of the interface language: a Czech
interface can happily produce an English audiobook.

**23 languages are built into the base model** and need no download — English,
Spanish, German, French, Italian, Portuguese, Dutch, Polish, Russian, Swedish,
Danish, Norwegian, Finnish, Greek, Turkish, Hebrew, Arabic, Hindi, Japanese,
Korean, Chinese, Malay, Swahili.

**Six more come from community checkpoints** (~2.1 GB each, downloaded on first
use) that replace the weights of the T3 module:

| Language | Repository | Verified | Note |
|---|---|---|---|
| Czech | [`Thomcles/Chatterbox-TTS-Czech`](https://huggingface.co/Thomcles/Chatterbox-TTS-Czech) | yes | needs a Hugging Face login |
| Slovak | [`pekiskol/chatterbox-tts-slovak`](https://huggingface.co/pekiskol/chatterbox-tts-slovak) | yes | |
| Portuguese (BR) | [`ResembleAI/Chatterbox-Multilingual-pt-br`](https://huggingface.co/ResembleAI/Chatterbox-Multilingual-pt-br) | no | official, from Resemble AI |
| French | [`Thomcles/Chatterbox-TTS-French`](https://huggingface.co/Thomcles/Chatterbox-TTS-French) | no | improves a native language |
| Persian | [`Thomcles/Chatterbox-TTS-Persian-Farsi`](https://huggingface.co/Thomcles/Chatterbox-TTS-Persian-Farsi) | yes | tokenizer has no `[fa]` token |
| Estonian | [`Mamsu/chatterbox-tts-et-lobiseja`](https://huggingface.co/Mamsu/chatterbox-tts-et-lobiseja) | yes | tokenizer has no `[et]` token |

*Verified* means the checkpoint is byte-for-byte the same size as
`t3_mtl23ls_v2.safetensors` (2,143,989,752 B), so it is a fine-tune of the very
module Chatterbox loads and the weights map key for key. Unverified ones are
loaded anyway; the log reports how many keys did not match.

Add your own in [`modely.json`](modely.json) — no code changes needed:

```json
{"kod": "hu", "nazev": "Magyar", "zdroj": "finetune", "token": true,
 "repo": "user/my-chatterbox-hu", "gated": false, "overeno": false,
 "velikost_gb": 2.1}
```

`token` records whether the tokenizer has a `[code]` token for the language.
Beyond the 23 trained languages, tokens exist for `cs`, `sk`, `bg`, `hu`, `ro`,
`ta` and `vi`.

### A note on Czech

Chatterbox officially lists 23 languages and Czech is not among them, yet it
works. The tokenizer that actually loads contains the `[cs]` token, and Czech
diacritics survive NFKD decomposition intact. The only obstacle is a validation
list inside the package, which Audiobookery extends at startup. The base
weights were never trained on Czech, though — that is what the fine-tune fixes.

The same reasoning applies to Slovak, Bulgarian, Hungarian, Romanian, Tamil and
Vietnamese: the tokens are there, waiting for someone to train them.

## Please read this before publishing anything

**Rights to the book.** Convert only works you are allowed to convert — your own
writing, public-domain texts, or books whose licence permits it. A legally
purchased e-book does not generally give you the right to publish an audio
version of it.

**Rights to the voice.** A reference recording is a real person's voice. Cloning
a narrator or an actor from a commercial audiobook and publishing the result is
a problem on two counts at once — the recording is copyrighted and the voice
belongs to someone who did not agree to it. For private listening the picture is
different, but "I made it for myself" stops applying the moment you share it.

**Do not impersonate.** Do not use a cloned voice to make someone appear to say
something they never said.

**Watermarking.** Chatterbox embeds an inaudible
[Perth](https://github.com/resemble-ai/perth) watermark in everything it
generates. Audiobookery does not remove it, and removing it is not a supported
use of this project.

The authors of this tool are not responsible for what you make with it.

## Models and licensing

| Component | Licence | Note |
|---|---|---|
| Audiobookery | MIT | this repository |
| [Chatterbox TTS](https://github.com/resemble-ai/chatterbox) | MIT | the engine |
| [`ResembleAI/chatterbox`](https://huggingface.co/ResembleAI/chatterbox) | see model card | base weights, downloaded at runtime |
| Language checkpoints | see each model card | community work, terms vary |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | OFL 1.1 | bundled in `fonts/`, see `fonts/OFL.txt` |

No model weights are included in this repository. Everything downloads from
Hugging Face on first use, under whatever terms that model carries.

## Project layout

```
audiobookery/
  audiobookery.py      # the whole application
  preklady.py          # interface strings (en / cs)
  modely.json          # language catalogue
  run.bat              # install + launch
  requirements.txt
  fonts/               # bundled JetBrains Mono (OFL 1.1)
  docs/                # screenshots
  model_cache/         # downloaded models        (gitignored)
  voices/              # your reference recordings (gitignored)
  vystup/              # generated audiobooks      (gitignored)
```

Reference recordings, models and generated audio are deliberately kept out of
version control. See [`.gitignore`](.gitignore).

## Troubleshooting

**`CUDA: False`** — a CPU build of PyTorch got installed. Delete `.venv` and run
`run.bat` again. The torch version is pinned to 2.6.0 on purpose: chatterbox-tts
requires exactly that, and without the pin pip replaces the CUDA build.

**`TypeError: 'NoneType' object is not callable` at `PerthImplicitWatermarker`** —
the watermarker imports `pkg_resources`, which ships with setuptools but was
removed in version 81. Hence the `setuptools>=70,<81` pin.

**Install fails on `pkuseg`** — the resolver picked chatterbox-tts 0.1.3, which
needs a package that must be compiled on Windows. `requirements.txt` pins
`>=0.1.7`, which uses a prebuilt wheel.

**`CUDA out of memory`** — lower *chars per block* to about 120, or switch the
device to `cpu`.

**Robotic or unstable voice** — the reference recording matters more than any
parameter. Use 10–20 s of clean speech with no music or echo. Lowering
temperature to 0.6 also helps.

## Contributing

Bug reports and language checkpoints for `modely.json` are both welcome. If you
are adding a model, please say whether you verified it loads and what the log
reported about unmatched keys.
