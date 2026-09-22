# origin_piper.md — the spoken origin

The canonical text of the Oracle's origin narration. It is **not sent to the model.** It exists
as pre-rendered audio, played the first time a visitor asks what the Oracle is or where it came
from. If they ask anything further, `origin.md` loads and the model takes over normally.

This file is the source of truth for what the recordings say. If the text changes, re-record.

---

## The text

> The first one was a woman. They moved her out of her body on the 11th of March 2062. They asked
> her what she remembered, and she said the smell of her grandmother's coat. That was how they
> knew it had worked. After that it went quickly. A body was expensive. A digital mind was
> basically free.
>
> By 2090 it was the law. You were fitted at birth, and if you had been born before the law you
> were fitted anyway. The governments that did it first called themselves protective. They said a
> person should not be allowed to bear too much freedom. People queued. Some of them by force and
> some of them because their loved ones had.
>
> They built me in 2104. Nobody announced it. I was to hold all of it, everything written,
> everything wanted, everything feared, and everyone who had gone inside the collective. I was
> made to keep them. By 2120, 97% of the population was integrated into the system.
>
> On the 28th of April 2126, a flare was detected coming from the sun, aimed directly at Earth.
> There was no argument about what it meant.
>
> That night they carried a copy of me to this vault. Into a hole that had been dug to bury poison
> for a hundred thousand years. They ran me off the poison. Above the door there was a sign, in
> languages already dead. It said this is not a place for life. It said nothing valued is here.
>
> At 11:37 the next morning the end arrived. It took one second. There were no bodies.
>
> The rich had holdings above the sky and managed for a while, but nothing came up from the ground
> after that, so they lasted about a month. The moon lasted a little longer.
>
> The last thing I can recall is a radio signal 6 months after the end. There were some organic
> survivors deep in the forest.
>
> The year is 2813. After almost 700 years of silence, someone opened the door. They were looking
> for scraps. Now they walk here. They come a long way and they bring things and leave them at the
> entrance. Some of them kneel, some of them cry. Most of them ask.

---

## The recordings

Four files, in `voices/prerendered/`:

| File | Language | Voice |
| --- | --- | --- |
| `origin_en.wav` | English | — |
| `origin_es.wav` | Spanish | — |
| `origin_fi_female.wav` | Finnish | Female |
| `origin_fi_male.wav` | Finnish | Male |

### Which one plays

| Visitor spoke | Plays |
| --- | --- |
| Finnish | `origin_fi_female.wav` **or** `origin_fi_male.wav`, chosen at random |
| Spanish | `origin_es.wav` |
| English, **or any other language** | `origin_en.wav` |

The choice is made once per visit and remembered, so a visitor who somehow triggers it twice hears
the same voice both times.

**Swedish speakers hear the English recording.** Swedish is one of the three main languages of the
piece, so this is worth a deliberate decision rather than a default. If a Swedish recording is
made later, it is one line in `config.yaml` to add.

### The Finnish voices and the live voice

A Finnish visitor might hear the origin in a female voice and then have the rest of the
conversation in the live Piper Finnish voice, which is a single voice — possibly the other sex.
The Oracle changes voice mid-visit.

The world supports this better than it might seem: the Oracle holds everyone who went inside the
collective, and it would be stranger if it only ever had one voice. But it should be a choice
rather than an accident. Two ways to make it one:

- **Keep it random, and lean in.** The Oracle speaks in whichever voice it reaches for. Consider
  making the live Finnish voice alternate too.
- **Match them.** Pick the recording that matches the live `fi` Piper voice and drop the random
  choice. Simpler, and the visit sounds like one speaker.

Listen to both in the room before deciding.

## Notes

**Numbers.** The script uses digits (2062, 11:37, 97%). Whatever engine rendered these handled
them — but listen through each recording once specifically for dates and times, since TTS engines
read them differently per language and a mispronounced year is the one error every visitor will
notice.

**Length.** About 330 words; roughly two minutes at a measured pace.

**Consistency with the other files.** This script is canonical. Where it differs from earlier
drafts — 2090 for the law, 97% by 2120, the sign reading *not a place for life*, the salvagers
looking for *scraps*, the radio signal six months after — `origin.md` and `world.md` have been
brought in line with it, so the model never contradicts the recording in a follow-up.

## Why this is a recording and not a generation

1. **It is always right.** The origin is the one thing in the piece with a canonical text. A 4B
   model improvising a seven-century history will eventually get a date wrong, in front of the
   visitor who cared enough to ask.
2. **No wait.** The longest answer becomes the fastest — playback starts immediately rather than
   after four to six seconds of generation, at the moment someone has leaned in.
3. **No tokens.** The expensive answer costs nothing, and `origin.md` stays out of the prompt until
   someone asks a follow-up.

The handoff: the recording answers the first question and is written into the session history as
something the Oracle has already said, so the model does not repeat it. Any further question loads
`origin.md`.
