# Test samples

`slovak-test.ogg` is the built-in clip played by **Play test file** in the OBS script settings. It contains
three Slovak sentences read by native speakers, separated by short pauses:

1. Oplatí sa urobiť si polhodinovú prechádzku okolo zaujímavej dediny.
2. Vnútrozemské vodné cesty môžu byť dobrým námetom na výber dovolenky.
3. Argentína je dobre známa tým, že má jeden z najlepších pólových tímov a hráčov na svete.

Captions it produced with the default models (Whisper large-v3 + M2M-100 1.2B):

1. It is worth taking a half-hour walk around an interesting village.
2. Inland waterways can be a good idea for choosing a vacation.
3. Argentina is well known for having one of the best pole teams and players in the world.

The test only shows captions when **Slovak** is ticked under "Languages you speak" (or no language is ticked).

## Source and license

The recordings are from the Slovak (`sk_sk`) development set of FLEURS: A. Conneau, M. Ma, S. Khanuja,
Y. Zhang, V. Axelrod, S. Dalmia, J. Riesa, C. Rivera, A. Bapna, "FLEURS: Few-shot Learning Evaluation of
Universal Representations of Speech", 2022 ([dataset](https://huggingface.co/datasets/google/fleurs)),
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Changes: three utterances (`15402029524703728721.wav`, `2432121162436408285.wav`, `8641956139527434521.wav`)
were joined with silence, loudness-normalized, and encoded as Ogg Opus.
