import re
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).parent / "data" / "corrections.yaml"

LLM_SYSTEM_PROMPT = (
    "You judge whether a user's reply corrects or rejects what an AI agent just did.\n"
    "Answer with exactly one word: yes, no, or unsure."
)


def _phrase_rx(phrase):
    return re.compile(r"(?<![\w'])" + re.escape(phrase.lower()) + r"(?![\w'])")


def _normalize(text):
    return (text or "").lower().replace("’", "'").replace("‘", "'").strip()


class CorrectionDetector:
    def __init__(self, rules):
        self.phrases = [_phrase_rx(p) for p in rules.get("phrases") or []]
        self.negations = tuple(rules.get("negations") or ())
        self.max_neg = int(rules.get("max_negation_chars") or 120)
        self.verbs = [_phrase_rx(v) for v in rules.get("action_verbs") or []]
        self.leading = [re.compile(r"^" + re.escape(p.lower()) + r"(?![\w'])") for p in rules.get("leading") or []]
        self._neg_rx = re.compile(
            r"^(" + "|".join(re.escape(n) for n in self.negations) + r")\b[\s,.!:;-]*") if self.negations else None

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        with open(path, "r", encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})

    def is_correction(self, message: str) -> bool:
        text = _normalize(message)
        if not text:
            return False
        if any(rx.search(text) for rx in self.phrases) or any(rx.match(text) for rx in self.leading):
            return True
        if self._neg_rx and len(text) <= self.max_neg:
            m = self._neg_rx.match(text)
            if m:
                rest = text[m.end():]
                return any(rx.search(rest) for rx in self.verbs)
        return False


def should_evaluate(prev_classes) -> bool:
    return any(c != "read" for c in prev_classes or ())


def parse_llm_verdict(text) -> bool:
    word = (text or "").strip().lower().split()
    return bool(word) and word[0].strip(".!,") == "yes"
