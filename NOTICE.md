# Third-party components

The MIT licence in [`LICENSE`](LICENSE) covers the Audiobookery source code
only. The pieces below carry their own terms.

## Bundled in this repository

| Component | Licence | Location |
|---|---|---|
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | SIL Open Font License 1.1 | `fonts/` — full text in [`fonts/OFL.txt`](fonts/OFL.txt) |

The font is redistributed under the OFL, which permits bundling provided the
licence travels with it. Audiobookery loads it for its own process only, via
`AddFontResourceEx` with `FR_PRIVATE`; nothing is installed into the system.

## Downloaded at runtime — not part of this repository

| Component | Licence | Note |
|---|---|---|
| [Chatterbox TTS](https://github.com/resemble-ai/chatterbox) | MIT | the synthesis engine, installed via pip |
| [`ResembleAI/chatterbox`](https://huggingface.co/ResembleAI/chatterbox) | see model card | base weights, ~3 GB |
| [`Thomcles/Chatterbox-TTS-Czech`](https://huggingface.co/Thomcles/Chatterbox-TTS-Czech) | see model card | Czech checkpoint, access-gated |
| [`pekiskol/chatterbox-tts-slovak`](https://huggingface.co/pekiskol/chatterbox-tts-slovak) | see model card | Slovak checkpoint |
| [`ResembleAI/Chatterbox-Multilingual-pt-br`](https://huggingface.co/ResembleAI/Chatterbox-Multilingual-pt-br) | see model card | Brazilian Portuguese |
| [`Thomcles/Chatterbox-TTS-French`](https://huggingface.co/Thomcles/Chatterbox-TTS-French) | see model card | French |
| [`Thomcles/Chatterbox-TTS-Persian-Farsi`](https://huggingface.co/Thomcles/Chatterbox-TTS-Persian-Farsi) | see model card | Persian, access-gated |
| [`Mamsu/chatterbox-tts-et-lobiseja`](https://huggingface.co/Mamsu/chatterbox-tts-et-lobiseja) | see model card | Estonian |
| [Perth](https://github.com/resemble-ai/perth) | see repository | watermarker, pulled in by Chatterbox |

**No model weights are committed to this repository.** Everything is fetched
from Hugging Face on first use, under whatever terms the individual model
carries. Check the model card before using a checkpoint commercially — the
community fine-tunes vary, and some are trained on datasets with their own
restrictions.

Adding a model to [`modely.json`](modely.json) does not grant you any rights to
it. That stays between you and whoever published it.
