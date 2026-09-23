from autovidedit.core.transcribe import group_words_into_sentences


def test_groups_words_at_pauses_and_keeps_word_timings():
    words = [
        {"start": 0.0, "end": 0.4, "word": " Hello"},
        {"start": 0.5, "end": 0.9, "word": " there."},
        {"start": 2.0, "end": 2.3, "word": " Next"},
        {"start": 2.3, "end": 2.4, "word": "  "},
        {"start": 2.4, "end": 2.8, "word": " one"},
    ]
    sentences = group_words_into_sentences(words, pause_threshold=0.5)
    assert [s["text"] for s in sentences] == ["Hello there.", "Next one"]
    assert (sentences[0]["start"], sentences[0]["end"]) == (0.0, 0.9)
    assert [w["word"] for w in sentences[1]["words"]] == ["Next", "one"]
