"""theoracle — the story material, loaded in the order the cache wants it.

`instructions.md` and `world.md` are who the Oracle is. They are always loaded,
always identical, and always first, because Ollama caches a prompt by its
prefix: anything fixed that sits at the front is processed once for the whole
visit, and anything variable in front of it throws that away every turn. About
7,000 tokens of the Oracle's character are re-read for free on turn eight only
because nothing moves above them.

`origin.md` is different. It is loaded only when a visitor asks a follow-up
about what the Oracle is, which most visitors never do. Keeping it out of the
system prompt is worth more than the tokens it costs: it means the Oracle
cannot recite its own history at someone who did not ask, which `instructions.md`
spends a whole section warning against.

`lineage.md` is never loaded. It is the references behind the piece, for you.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import REPO, load        # noqa: E402

STORIES = REPO / "stories"

ALWAYS = ("instructions.md", "world.md")    # the system prompt, in this order
ON_DEMAND = ("origin.md",)                  # loaded when the visit needs it
NEVER = ("lineage.md", "origin_piper.md")   # research, and the recording's script


class CorpusError(Exception):
    """Story material is missing. The Oracle has nothing to be."""


def approx_tokens(text: str) -> int:
    """Rough token count. 1.35 per word, the same figure check_env.py uses."""
    return int(len(text.split()) * 1.35)


class Corpus:
    """The story files, read once per process."""

    def __init__(self, cfg=None):
        self.cfg = cfg or load()
        self._cache: dict[str, str] = {}

    def read(self, name: str) -> str:
        if name not in self._cache:
            path = STORIES / name
            if not path.exists():
                raise CorpusError(
                    f"stories/{name} is missing. The Oracle's character lives in these "
                    f"files; without them it is a chatbot with a good voice.")
            self._cache[name] = path.read_text(encoding="utf-8").strip()
        return self._cache[name]

    def system_prompt(self) -> str:
        """The fixed prefix. Identical every turn, of every visit, or the cache dies."""
        return "\n\n".join(self.read(n) for n in ALWAYS)

    def lore(self, name: str = "origin.md") -> str:
        """Trusted material loaded for one turn, when the visit has earned it."""
        if name not in ON_DEMAND:
            raise CorpusError(f"{name} is not loadable into a prompt")
        return self.read(name)

    def budget(self) -> dict:
        """What the material costs against num_ctx. Call it at startup, once."""
        always = approx_tokens(self.system_prompt())
        origin = approx_tokens(self.read("origin.md")) if (STORIES / "origin.md").exists() else 0
        num_ctx = int(self.cfg.get("llm.num_ctx", 16384) or 16384)
        reply = int(self.cfg.get("llm.num_predict", 200) or 200)
        return {
            "always_on": always,
            "origin": origin,
            "num_ctx": num_ctx,
            "reply": reply,
            # What is left for the conversation itself once the character, the
            # heaviest optional block and one reply are all accounted for.
            "for_conversation": num_ctx - always - origin - reply,
        }

    def check(self) -> list[str]:
        """Problems worth knowing about before a visitor is standing there."""
        problems = []
        for name in ALWAYS:
            if not (STORIES / name).exists():
                problems.append(f"stories/{name} is missing")
        b = self.budget()
        if b["for_conversation"] < 2000:
            problems.append(
                f"only ~{b['for_conversation']} tokens left for the conversation after the "
                f"story material ({b['always_on']}) and origin.md ({b['origin']}) — "
                f"raise llm.num_ctx or cut the corpus")
        return problems


# ── "what are you?" ───────────────────────────────────────────────────────
# The one question that changes which material a turn needs, so the test for it
# lives here with the material rather than in the state machine.
#
# Narrow on the left, wide on the right. A false positive plays a three-minute
# recording at someone who asked something else, which is the worst thing this
# piece can do to a visitor. But the first version was too narrow in the other
# direction and missed "tell me the story behind the oracle" — which is how
# people actually ask, because nobody interrogates an oracle. They ask it for a
# story. Every phrasing below was either observed or is a close neighbour of
# one; add to it whenever the room produces a new one.
_ORIGIN = re.compile(
    # Asking what it is. The lookaheads matter: "what are you going to tell me"
    # and "who are you to judge me" are not this question, and both get said.
    r"\bwhat\s+are\s+you\b(?!\s+(?:to|going|gonna|\w+ing)\b)"
    r"|\bwho\s+(are|were)\s+you\b(?!\s+(?:to|going|gonna|\w+ing)\b)"
    r"|\bwhat\s+(is|are)\s+(this|the)\s+(place|oracle)\b"

    # Asking to be told. This is the phrasing that actually came up in the room
    # and that the first version missed entirely — people do not interrogate an
    # oracle, they ask it for a story.
    # "tell me the story of my life" is a reading, not this question.
    r"|\btell\s+me\s+(about\s+)?(your|the)\s+(story|history|past|origin|beginning)"
    r"\b(?!\s+of\s+(my|our|his|her|their|the\s+visitor))"
    r"|\btell\s+me\s+about\s+your\s*self\b"
    r"|\b(the\s+)?story\s+(behind|of)\s+(you|this|the\s+oracle)\b"
    r"|\bwhat\s+(is|was)\s+your\s+(story|history|origin|past)\b"
    r"|\bhow\s+(long|many\s+years)\s+have\s+you\s+been\s+(here|down\s+here)\b"

    # Asking how it came to be.
    r"|\bhow\s+(were|was)\s+(you|this)\s+(made|built|created)\b"
    r"|\bwho\s+(made|built|created)\s+you\b"
    r"|\bwhere\s+(did|do)\s+you\s+come\s+from\b"
    r"|\bwhat\s+happened\s+to\s+you\b"
    r"|\bwhy\s+are\s+you\s+(here|down\s+here)\b"

    # fi — Finnish drops the pronoun, and the first version required it.
    # "Kuka olet?" is how anyone actually asks; "Kuka sinä olet?" is the
    # textbook form nobody says. Every pronoun below is optional.
    r"|mik(ä|a)\s+(sin(ä|a)\s+|se\s+)?olet\b"
    r"|kuka\s+(sin(ä|a)\s+)?olet\b"
    r"|mik(ä|a)\s+(t(ä|a)m(ä|a)\s+)?paikka\s+on\b"
    r"|miten\s+(sinut|t(ä|a)m(ä|a))\s+(tehtiin|rakennettiin|luotiin)\b"
    r"|mist(ä|a)\s+(sin(ä|a)\s+)?(tulit|olet\s+kotoisin)\b"
    r"|kerro\s+(minulle\s+)?(tarina|tarinasi|itsest(ä|a)si|historia|menneisyyde)"
    r"|kuka\s+(sinut|t(ä|a)m(ä|a)n)\s+(teki|rakensi|loi)\b"
    r"|kauanko\s+olet\s+ollut\s+t(ä|a)(ä|a)ll(ä|a)\b"
    r"|miksi\s+olet\s+t(ä|a)(ä|a)ll(ä|a)\b"
    r"|mik(ä|a)\s+oraakkeli\s+on\b"

    # sv
    r"|vad\s+(ä|a)r\s+du\b|vem\s+(ä|a)r\s+du\b|hur\s+(gjordes|byggdes)\s+du\b"
    r"|ber(ä|a)tta\s+(om\s+)?(dig|din\s+historia)"

    # es — pro-drop as well, and "¿Qué es esto?" is the common opening.
    r"|qu(é|e)\s+eres\b|qui(é|e)n\s+eres\b"
    r"|qu(é|e)\s+es\s+(esto|este\s+lugar|el\s+or(á|a)culo)\b"
    r"|c(ó|o)mo\s+te\s+(hicieron|crearon|construyeron)\b"
    r"|qui(é|e)n\s+te\s+(hizo|cre(ó|o)|construy(ó|o))\b"
    r"|cu(é|e)ntame\s+(tu\s+)?(historia|sobre\s+ti|el\s+origen)"
    r"|de\s+d(ó|o)nde\s+(vienes|eres)\b"
    r"|cu(á|a)nto\s+tiempo\s+llevas\s+aqu(í|i)\b"
    r"|por\s+qu(é|e)\s+est(á|a)s\s+aqu(í|i)\b"

    # de
    r"|was\s+bist\s+du\b|wer\s+bist\s+du\b"
    r"|erz(ä|a)hl\s+(mir\s+)?(von\s+dir|deine\s+geschichte)",
    re.I)


def wants_origin(text: str) -> bool:
    """Is this a question about what the Oracle is?"""
    return bool(_ORIGIN.search(text or ""))


def main() -> None:
    corpus = Corpus()
    b = corpus.budget()
    print(f"  always on       ~{b['always_on']:6d} tokens  ({', '.join(ALWAYS)})")
    print(f"  origin.md       ~{b['origin']:6d} tokens  (loaded only on a follow-up)")
    print(f"  one reply       ~{b['reply']:6d} tokens")
    print(f"  num_ctx          {b['num_ctx']:6d}")
    print(f"  left over       ~{b['for_conversation']:6d} tokens for the visit itself")
    for problem in corpus.check():
        print(f"\n  WARNING: {problem}")


if __name__ == "__main__":
    main()
