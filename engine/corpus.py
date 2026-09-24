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
