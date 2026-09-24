"""theoracle — the engine.

Five stages, one per module, in the order a visit moves through them:

    audio_in.py   the microphone and the end of speech      (Phase 5)
    stt.py        speech to text, and which language it is  (Phase 1)
    llm.py        the Oracle's answer, one visit long       (Phase 3)
    corpus.py     the story files, in the order the cache wants
    schema.py     the reply contract, and the fate sheet
    guard.py      what may reach the model, and leave it    (Phase 4)
    tts.py        the answer as a WAV TouchDesigner plays   (Phase 2)

Supporting them:

    config.py     config.yaml and .env, loaded and checked once
    audio_devices.py  device names, host APIs, sample rates
    wav_io.py     WAV files the standard library will not read

Every module is importable and runnable on its own — `python -m engine.stt`
and `python -m engine.tts` each run their phase's acceptance test. That is
deliberate: a stage you cannot exercise by itself is a stage you will end up
debugging through four others.
"""

__version__ = "0.2.0"
