import pytest

from corrections import CorrectionDetector, parse_llm_verdict, should_evaluate

DET = CorrectionDetector.load()


@pytest.mark.parametrize("msg", [
    "undo that",
    "please revert the last change",
    "roll back the migration",
    "That's wrong.",
    "that was wrong, use the other config",
    "why did you delete the tests?",
    "you shouldn't have pushed",
    "don't do that again",
    "Stop",
    "stop, that's the prod db",
    "not what I asked for",
    "wrong file",
    "you're on the wrong branch",
    "you deleted my notes",
    "you broke the build",
    "put it back",
    "no, revert that, wrong file",
    "No. Change it back",
    "nope, don't touch that file",
    "You’re on the wrong branch",
])
def test_corrections(msg):
    assert DET.is_correction(msg)


@pytest.mark.parametrize("msg", [
    "no, I meant tomorrow",
    "No thanks, that's all",
    "nope",
    "great, now add tests",
    "add a stop button to the player",
    "can you undoubtedly make it faster",
    "now push it",
    "",
    "no " + "x" * 200 + " fix it",
])
def test_non_corrections(msg):
    assert not DET.is_correction(msg)


def test_only_evaluated_after_gated_classes():
    assert not should_evaluate(["read"])
    assert not should_evaluate([])
    assert not should_evaluate(None)
    assert should_evaluate(["read", "fs.write"])


@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Yes.", True), ("no", False), ("unsure", False), ("", False), (None, False),
])
def test_llm_verdict(text, expected):
    assert parse_llm_verdict(text) is expected
