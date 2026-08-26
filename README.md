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

Measured on an RTX 2080 Ti: **1.44× realtime** with two parallel processes,
0.91× with one. An eight-hour audiobook takes about five and a half hours.

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

With parallel generation the buffer **grows** while you listen, so a head start
of a minute or two is enough and you can keep listening indefinitely. On a
single process generation runs slightly slower than playback (0.91×) and the
buffer drains — roughly twenty times slower than it fills, so a three-minute
head start buys about an hour.

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

## Parallel generation

Generation is not limited by the GPU's compute power. Measured during a single
stream, the card sits at **38 % utilisation with 15 % memory bandwidth** — the
bottleneck is per-token overhead, not arithmetic. The fix is genuine
parallelism, which Python threads cannot provide because of the GIL.

Audiobookery therefore runs several **separate processes**, each with its own
copy of the model, and reassembles the blocks in order. Measured on an
RTX 2080 Ti:

| processes | speed | GPU |
|---|---|---|
| 1 | 0.91× realtime | 38 % |
| 2 | **1.44× realtime** | 100 % |

The number of processes is chosen from free VRAM (~4.3 GB each) and capped at
four; set it manually under *advanced settings*, or leave it at 0 for automatic.
When more than one process is used the parent does not load a model at all —
that memory goes to a worker instead.

Each block's seed is derived from its index, so the result does not depend on
how many processes are running or which one happened to take the block.

### What it costs you while it runs

Parallel generation works precisely by keeping the GPU busy, and that has a
price. Measured with two processes on an RTX 2080 Ti:

| | |
|---|---|
| GPU utilisation | 98 % (median), 100 % peak |
| VRAM | 10.2 GB of 11.2 GB — about 1 GB left |

The machine stays perfectly usable for ordinary work — browsing, writing, mail —
because the processes spend their time waiting on the GPU rather than the CPU.
Anything that wants the graphics card, though, will not get it: games, video
editing, other local models. There is no VRAM left for them either.

If you want to keep the card free, set *parallel processes* to **1**. That drops
the speed to 0.91× realtime but leaves the GPU at around 38 %, which is what the
application did before this feature existed.

### Removing clicks from pauses

Audiobookery attenuates short impulses that appear in silent stretches. It is
on by default and can be turned off under *advanced settings*.

The filter is deliberately narrow. It works out the noise floor of each block,
marks pauses where the envelope stays below three times that floor for at least
150 ms, **shrinks each pause by 30 ms at both ends** so that speech onsets and
tails are out of reach, and only then looks for impulses above eight times the
floor that last **less than 15 ms** — short enough to exclude a breath. Those
are faded down to the level of the surrounding room tone rather than cut out,
since a hard edit would produce a click of its own.

Measured on three recordings, it attenuates 22–34 spots per five minutes,
touching **0.04–0.08 % of the track** and reducing the loudest offenders by
10–23 dB. The property that matters is verifiable rather than promised:

```
changed samples outside pauses : 0
speech identical bit for bit   : True
```

The pause keeps its cloned room tone, so nothing drops into dead digital
silence.

### min_p, and a measurement that failed

Occasional short clicks appear in quiet passages. Measuring a six-minute
recording against its reference clip pinned down where they come from:

| | generated | reference |
|---|---|---|
| noise floor in silence | −57 dBFS | −58 dBFS |
| spikes in silence (\|x\| > 0.02) | 53 | 0 |

The **noise floor is faithfully cloned** from the reference — room tone and all —
which is expected. The spikes are not in the reference at all, and they sit
seconds away from block joins, so they are neither the concatenation nor the
inserted pauses. The model produces them.

The obvious suspect was low-probability token sampling, so `min_p` is now
exposed under *advanced settings*. It cuts the tail of the probability
distribution using a threshold relative to the best token, which in theory
removes glitch tokens without flattening natural variation.

**It did not work.** Measured over 280 s per setting with identical seeds:

| min_p | clusters | per minute | forced EOS (repetition) |
|---|---|---|---|
| 0.05 (default) | 21 | 4.5 | 1 |
| 0.12 | 25 | 5.3 | 2 |
| 0.20 | — | 6.6 | — |

Raising it did not reduce the clicks and mildly increased the repetition stalls
that the alignment analyser has to break by forcing an end-of-speech token. The
default therefore stays at the library's 0.05. The control is left in place for
anyone who wants to experiment — and so the negative result is on record rather
than repeated.

## Chapters and resuming

**Chapters.** EPUB and FB2 carry their own chapter structure, so Audiobookery
uses it: with MP3 output you get one file per chapter, numbered and named after
the chapter, each with the cover embedded and proper track metadata. Plain text
has no reliable structure to read, so it stays a single file.

Choosing MP3 means the intermediate WAV is deleted once the conversion
succeeds — no 2 GB leftovers. If the run is interrupted, the WAV is kept
instead: MP3 cannot be appended to, so converting a half-finished book would
close the door on resuming it.

**Resuming.** An eight-hour book is an overnight job, and things get in the way:
a power cut, a reboot, needing the GPU for something else. A progress file is
written next to the output after every block, recording the source fingerprint,
the block reached and the exact sample count in the file being written.

Start the same book again and Audiobookery offers to continue where it stopped.
The unfinished chapter is truncated to the last recorded sample — so a block cut
in half by a crash is discarded rather than left as a glitch — and generation
picks up from the next block.

The fingerprint covers the source file and every setting that affects the sound.
Change the voice, the language or the temperature and resuming is refused, since
the second half of the book would not match the first.

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
Hugging Face on first use, under whatever terms that model carries. Full
attribution and per-model links are in [NOTICE.md](NOTICE.md).

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
