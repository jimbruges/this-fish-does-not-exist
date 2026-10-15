"""Invented scientific prose about an invented fish.

Tries the local LLM brick first, and falls back to a template built from the
fish's own generated traits. The fallback is not a degraded placeholder - it
reads as textbook prose and is always available - so the page mode works even
with no model downloaded, or when a small local model is slow or returns
something unusable.
"""

import logging
import random
import re
import threading

logger = logging.getLogger(__name__)

# A 0.8-1B model on this board can take a long time, and a hung generation
# would stall the whole draw cycle, so chat() runs in a thread we can abandon.
LLM_TIMEOUT_S = 75

_llm = None
_llm_failed = False

# Folded into the user turn rather than passed as a system prompt: Gemma's
# chat template has no system role and rejects one outright with
# "Conversation roles must alternate user/assistant".
PREAMBLE = (
    "You write entries for a 19th-century systematic ichthyology textbook. "
    "Invent plausible details confidently and never mention that the species "
    "is fictional. Plain prose only: no markdown, no headings, no lists, no "
    "quotation marks.\n\n"
)


def _get_llm():
    """Import and construct the brick lazily: the app must still run when no
    model is installed, and constructing it can be slow."""
    global _llm, _llm_failed
    if _llm is not None or _llm_failed:
        return _llm
    try:
        from arduino.app_bricks.llm import LargeLanguageModel
        try:
            _llm = LargeLanguageModel(timeout=LLM_TIMEOUT_S)
        except TypeError:   # older brick without a timeout argument
            _llm = LargeLanguageModel()
        return _llm
    except Exception:
        logger.exception("local LLM unavailable; using template descriptions")
        _llm_failed = True
        return None


def _chat_with_timeout(llm, prompt, timeout):
    """Run chat() on a worker thread so a slow or wedged model can't block the
    draw cycle. An abandoned thread is left to finish on its own."""
    result = {}

    def work():
        try:
            result["text"] = llm.chat(prompt)
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning("LLM did not answer within %ss; using template", timeout)
        return None
    if "error" in result:
        logger.warning("LLM call failed (%s); using template", result["error"])
        return None
    return result.get("text")


def _clean(text, max_words):
    text = re.sub(r"[*_#`>]+", "", text or "")
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words])
        cut = max(text.rfind("."), text.rfind(";"))
        if cut > 40:
            text = text[:cut + 1]
    return text


def from_llm(name, traits, max_words=110, timeout=LLM_TIMEOUT_S):
    llm = _get_llm()
    if llm is None:
        return None
    prompt = PREAMBLE + (
        "Write two short paragraphs (about %d words total) for the entry on "
        "%s, a marine fish. Mention its habitat, depth range, diet and "
        "behaviour, and refer to these observed features: %s. "
        "Write in formal taxonomic prose. Do not repeat the species name more "
        "than twice." % (max_words, name, _describe_traits(traits))
    )
    out = _chat_with_timeout(llm, prompt, timeout)
    if out is None:
        return None
    cleaned = _clean(out, max_words)
    if len(cleaned.split()) < 25:
        logger.warning("LLM description too short (%r); using template", cleaned[:60])
        return None
    return cleaned


def _describe_traits(traits):
    bits = []
    if not traits:
        return "an unremarkable body plan"
    length = traits.get("body_length", 300)
    height = traits.get("body_height", 90)
    ratio = height / max(1.0, length)
    bits.append("a deep, laterally compressed body" if ratio > 0.3
                else "an elongate, fusiform body")
    if traits.get("has_teeth"):
        bits.append("prominent recurved teeth")
    if traits.get("has_moustache") or traits.get("has_beard"):
        bits.append("sensory barbels about the jaw")
    if traits.get("eye_size", 10) > 16:
        bits.append("notably enlarged orbits")
    if traits.get("dorsal_length", 90) > 120:
        bits.append("a greatly extended dorsal fin")
    if traits.get("tail_length", 75) > 120:
        bits.append("a long, deeply forked caudal fin")
    if traits.get("scale_type") in (0, 1):
        bits.append("heavy imbricate scaling")
    return ", ".join(bits[:4])


_HABITAT = [
    "shallow coastal reefs and seagrass flats",
    "turbid estuarine channels",
    "cold offshore banks",
    "steep outer-reef walls",
    "brackish mangrove margins",
    "open midwater above the continental slope",
]
_DEPTH = ["4 and 40", "20 and 120", "60 and 310", "110 and 600", "2 and 18"]
_DIET = [
    "small crustaceans, polychaetes and the occasional juvenile fish",
    "planktonic copepods taken in open water",
    "molluscs and echinoderms crushed by the pharyngeal plates",
    "benthic amphipods sifted from soft sediment",
]
_BEHAVIOUR = [
    "It is solitary outside the spawning season, when loose aggregations form over hard bottom.",
    "Individuals hold station in midwater by day and disperse to feed after dusk.",
    "The species is strongly territorial, males defending crevices against conspecifics.",
    "Schools of several hundred have been recorded moving along the thermocline.",
]
_CLOSING = [
    "No commercial fishery exists, though specimens are taken incidentally in trawls.",
    "The flesh is reportedly bitter and the species is of no economic value.",
    "Known from fewer than a dozen preserved specimens, all from the type locality.",
    "Local fishermen regard its appearance inshore as a sign of changing weather.",
]


def from_template(name, traits, seed=None):
    rng = random.Random(seed if seed is not None else name)
    genus = name.split()[0]
    return (
        "%s is distinguished among its congeners by %s. The lateral line is "
        "complete and gently decurved above the pectoral insertion, and the "
        "head bears fine radiating striae visible in cleared material.\n"
        "The species frequents %s, generally between %s metres. It feeds "
        "chiefly upon %s. %s %s"
        % (
            name,
            _describe_traits(traits),
            rng.choice(_HABITAT),
            rng.choice(_DEPTH),
            rng.choice(_DIET),
            rng.choice(_BEHAVIOUR),
            rng.choice(_CLOSING),
        )
    ) + (" Juveniles of %s remain poorly known." % genus if rng.random() < 0.5 else "")


def describe(name, traits, use_llm=True, max_words=85):
    """Fits the portrait page's body area (~12 justified lines)."""
    """Returns (text, source) where source is "llm" or "template"."""
    if use_llm:
        text = from_llm(name, traits, max_words=max_words)
        if text:
            return text, "llm"
    return from_template(name, traits), "template"
