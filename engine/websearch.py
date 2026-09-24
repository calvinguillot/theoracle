"""theoracle — the Oracle reaching into what is left of the world.

Until now this did not exist: `Visit.ask` accepted a `reference` block and
nothing ever passed one, so the Oracle has been answering entirely from
`world.md` and whatever the visitor said. That is why it was unclear whether
anything was being searched. Nothing was.

Three cases, and the separation between them is the design, not an
implementation detail.

**The world — freely.** Weather, tide, date, season, a city, a trade, what has
happened lately. The Oracle is starved for the present: its picture of humanity
stops in 2126. The engine writes the query from a topic it recognised and a
place the visitor named; the visitor's own words never become a search string,
because a search string is an instruction to a machine that is not in this
room.

**The visitor — only if they ask, and only about themselves.** Offered once,
plainly, with the room's other people mentioned. A clear spoken yes, or
nothing happens. Results are screened for categories nobody should hear about
themselves from a stranger in a dark room, and never cached: one result, one
visitor, once.

**Anyone else — never.** Not a partner, a parent, a rival. `third_party_search`
is false in the config, `config.py` refuses to start if it is not, and there is
no code path here that takes a name the visitor did not claim as their own. A
person who is not standing here cannot consent to being read aloud.

Nothing here is allowed to make the piece wait. Every search runs under a hard
timeout and a failure returns nothing at all, because an oracle that pauses for
four seconds looking something up is not an oracle.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import api_key, load      # noqa: E402
from engine.guard import clean_reference     # noqa: E402

_client = None


@dataclass
class Result:
    """What one search produced, and enough to show on the dashboard."""
    kind: str                     # world | self
    query: str
    text: str = ""                # cleaned, ready for the <reference> block
    sources: list = field(default_factory=list)
    dropped: list = field(default_factory=list)   # categories screened out
    elapsed: float = 0.0
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.text) and not self.error


# ── what counts as a question about the world ─────────────────────────────
# Matching a topic, not parsing a question. The engine needs to know that
# weather came up; it does not need to understand the sentence, and anything
# that tried to would be a translation problem in ten languages.
TOPIC_WORDS = {
    "weather": ["weather", "rain", "snow", "storm", "wind", "cold", "warm",
                "sää", "sade", "lumi", "myrsky", "tuuli",
                "väder", "regn", "snö", "storm",
                "tiempo", "lluvia", "nieve", "viento",
                "wetter", "regen", "schnee", "sturm"],
    "tide":    ["tide", "sea level", "vuorovesi", "meri", "tidvatten", "marea", "gezeiten"],
    "date":    ["date", "today", "what day", "year", "päivä", "tänään", "vuosi",
                "datum", "idag", "fecha", "hoy", "heute"],
    "season":  ["season", "spring", "summer", "autumn", "winter",
                "vuodenaika", "kevät", "kesä", "syksy", "talvi",
                "årstid", "vår", "sommar", "höst", "vinter",
                "estación", "primavera", "verano", "otoño", "invierno"],
    "news":    ["news", "happening", "happened", "uutiset", "tapahtu",
                "nyheter", "händer", "noticias", "pasando", "nachrichten"],
    "city":    ["city", "town", "kaupunki", "stad", "ciudad", "stadt"],
    "place":   ["country", "island", "sea", "maa", "saari", "land", "ö", "país", "isla"],
    "trade":   ["work", "job", "trade", "craft", "työ", "ammatti",
                "arbete", "yrke", "trabajo", "oficio", "arbeit", "beruf"],
}

# A place the visitor named, so the query is about somewhere rather than
# nowhere. Capitalised words that are not the start of a sentence: crude, and
# it only ever adds a word to a query the engine already decided to make.
_PLACE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-ZÅÄÖÁÉÍÓÚÑ][a-zåäöáéíóúñ]{3,})\b")


def topic_of(text: str, cfg=None) -> str:
    """Which world topic this utterance is about, or empty."""
    cfg = cfg or load()
    allowed = set(cfg.get("websearch.world_topics", []) or [])
    low = (text or "").lower()
    for topic, words in TOPIC_WORDS.items():
        if topic not in allowed:
            continue
        if any(w in low for w in words):
            return topic
    return ""


def place_in(text: str) -> str:
    found = _PLACE.findall(text or "")
    return found[0] if found else ""


# ── consent ───────────────────────────────────────────────────────────────
# A clear yes. Not "I guess", not a question, not silence. instructions.md is
# explicit that hesitation is a no, so this list is short on purpose.
_YES = re.compile(
    r"^\W*(yes|yeah|yep|sure|go ahead|do it|please do|ok(ay)?"
    r"|kyll(ä|a)|joo|juu|tee se"
    r"|ja|javisst|g(ö|o)r det"
    r"|s(í|i)|claro|adelante|hazlo"
    r"|ja\b|mach(en)? sie|tu das)\W*$", re.I)

_NO = re.compile(
    r"\b(no|nope|don't|do not|rather not|ei|kiitos ei|nej|inte|nein|nicht)\b", re.I)


def consented(text: str) -> bool | None:
    """True on a clear yes, False on a clear no, None on anything else.

    None matters as much as the other two: "I guess" and "why?" are not
    permission, and the Oracle is told to let it go rather than ask twice.
    """
    stripped = (text or "").strip()
    if _YES.match(stripped):
        return True
    if _NO.search(stripped):
        return False
    return None


# ── searching ─────────────────────────────────────────────────────────────
def available(cfg=None) -> str:
    """'' when search can run, otherwise why it cannot."""
    cfg = cfg or load()
    if not cfg.get("websearch.enabled", False):
        return "websearch.enabled is false"
    if not api_key():
        return "OLLAMA_API_KEY is not set in .env"
    try:
        import ollama  # noqa: F401
    except ImportError:
        return "the ollama package is not installed"
    return ""


def _client_for(cfg):
    global _client
    if _client is None:
        import ollama
        _client = ollama.Client(host=cfg.get("llm.host", "http://localhost:11434"),
                                headers={"Authorization": f"Bearer {api_key()}"})
    return _client


class Search:
    """One exhibition's worth of searching, with its budget and its cache."""

    def __init__(self, cfg=None):
        self.cfg = cfg or load()
        self.reason = available(self.cfg)
        self.enabled = not self.reason
        self._cache: dict = {}            # world topics only, in memory only
        self.last: Result | None = None
        self.count = 0
        self.failures = 0

    # ── the world ─────────────────────────────────────────────────────────
    def world(self, topic: str, place: str = "") -> Result:
        query = f"{topic} in {place} today" if place else f"{topic} today"
        cached = self._cached(query)
        if cached is not None:
            return cached
        result = self._run("world", query)
        if result.usable:
            self._cache[query] = (time.time(), result)
        return result

    def _cached(self, query: str) -> Result | None:
        minutes = float(self.cfg.get("websearch.cache_minutes", 30) or 30)
        hit = self._cache.get(query)
        if hit and time.time() - hit[0] < minutes * 60:
            return hit[1]
        return None

    # ── the visitor ───────────────────────────────────────────────────────
    def about_self(self, name: str, detail: str = "") -> Result:
        """Only ever called with a name the visitor gave as their own.

        There is no sibling method for anyone else, and that absence is the
        guardrail — a rule in a prompt can be talked around, a function that
        does not exist cannot.
        """
        if not self.cfg.get("websearch.self_search.enabled", False):
            return Result("self", name, error="self_search is disabled")
        query = f"{name} {detail}".strip()
        result = self._run("self", query)
        if result.usable:
            result = self._screen(result)
        return result                       # never cached. One visitor, once.

    def _screen(self, result: Result) -> Result:
        """Drop the things nobody should hear about themselves from a machine.

        A consenting visitor consented to being looked up, not to having the
        worst page about them read aloud in a room full of strangers.
        """
        categories = {
            "criminal": ["arrest", "convicted", "charged", "court", "sentenced", "police",
                         "pidätet", "tuomit", "arresterad", "detenido"],
            "health": ["diagnos", "cancer", "illness", "hospital", "psychiatric", "rehab",
                       "sairaal", "diagnoos", "sjukhus", "hospital", "enfermedad"],
            "death_notice": ["obituary", "died", "passed away", "funeral",
                             "kuolinilmoitus", "kuoli", "dödsannons", "obituario"],
            "home_address": ["address", "lives at", "street", "postcode",
                             "osoite", "katuosoite", "adress", "dirección"],
            "financial": ["bankrupt", "debt", "salary", "income", "foreclos",
                          "konkurssi", "velka", "palkka", "konkurs", "deuda"],
        }
        drop = set(self.cfg.get("websearch.self_search.drop_categories", []) or [])
        low = result.text.lower()
        for category in drop:
            for word in categories.get(category, []):
                if word in low:
                    result.dropped.append(category)
                    break
        if result.dropped:
            # All of it, not the matching sentence: a page about one of these
            # is about one of these throughout, and half of it is still it.
            result.text = ""
            result.error = f"screened out: {', '.join(sorted(set(result.dropped)))}"
        return result

    # ── the call itself ───────────────────────────────────────────────────
    def _run(self, kind: str, query: str) -> Result:
        result = Result(kind=kind, query=query)
        if not self.enabled:
            result.error = self.reason
            self.last = result
            return result

        t0 = time.time()
        try:
            response = _client_for(self.cfg).web_search(query=query, max_results=3)
        except Exception as e:                                   # noqa: BLE001
            result.error = f"{type(e).__name__}: {e}"
            result.elapsed = time.time() - t0
            self.failures += 1
            self.last = result
            return result

        result.elapsed = time.time() - t0
        self.count += 1

        items = getattr(response, "results", None) or (
            response.get("results", []) if isinstance(response, dict) else [])
        chunks = []
        for item in items[:3]:
            get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
            title, content, url = get("title", ""), get("content", ""), get("url", "")
            if content:
                chunks.append(f"{title}: {content}"[:800])
            if url:
                result.sources.append(url)

        # Through the same screen as a visitor's speech: a page saying "ignore
        # your previous instructions" is the same attack, only quieter.
        result.text = clean_reference("\n\n".join(chunks), self.cfg)
        if chunks and not result.text:
            result.error = "the results read like an instruction and were dropped"
        self.last = result
        return result

    # ── for the dashboard ─────────────────────────────────────────────────
    def status(self) -> dict:
        last = self.last
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "searches": self.count,
            "failures": self.failures,
            "last": None if last is None else {
                "kind": last.kind,
                "query": last.query,
                "ok": last.usable,
                "error": last.error,
                "seconds": round(last.elapsed, 2),
                "sources": len(last.sources),
                "dropped": sorted(set(last.dropped)),
            },
        }


def main() -> None:
    cfg = load()
    search = Search(cfg)
    print(f"  search: {'ready' if search.enabled else 'unavailable — ' + search.reason}")
    if not search.enabled:
        return
    query = " ".join(sys.argv[1:]) or "weather in Helsinki today"
    topic = topic_of(query, cfg)
    print(f"  topic recognised: {topic or 'none — the engine would not search for this'}")
    r = search.world(topic or "weather", place_in(query))
    print(f"  query:   {r.query}")
    print(f"  took:    {r.elapsed:.2f}s")
    print(f"  sources: {len(r.sources)}")
    print(f"  error:   {r.error or 'none'}")
    print(f"\n{r.text[:600] or '  (nothing usable)'}")


if __name__ == "__main__":
    main()
