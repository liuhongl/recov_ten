from __future__ import annotations

import pytest

from app.audio_codec import samples_to_pcm_s16le
from app.config import FreeSwitchConfig
from app.media_contract import build_realtime_phone_contract
from app.playout_engine import PlayoutEngine


def test_playout_engine_downsamples_model_audio_to_phone_frame():
    engine = _engine(tail_silence_ms=0)

    frames = engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(480),
    )

    assert len(frames) == 1
    assert frames[0].turn_id == 1
    assert frames[0].response_id == "resp-1"
    assert frames[0].sequence == 0
    assert frames[0].duration_ms == 20
    assert len(frames[0].payload) == 320
    assert engine.queue_depth == 1

    drained = engine.drain_frames()
    assert drained == frames
    assert engine.stats.drained_frames == 1


def test_playout_engine_holds_partial_frames_until_enough_audio_arrives():
    engine = _engine(tail_silence_ms=0)

    first = engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(240),
    )
    second = engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(240),
    )

    assert first == []
    assert len(second) == 1
    assert len(second[0].payload) == 320


def test_finish_response_flushes_partial_frame_and_tail_silence():
    engine = _engine(tail_silence_ms=40)

    frames = engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(240),
    )
    finished = engine.finish_response(turn_id=1, response_id="resp-1")

    assert frames == []
    assert len(finished) == 3
    assert finished[0].is_tail_silence is False
    assert finished[1].is_tail_silence is True
    assert finished[2].is_tail_silence is True
    assert finished[1].payload == b"\x00" * 320
    assert finished[2].payload == b"\x00" * 320
    assert engine.stats.tail_silence_frames == 2


def test_cancel_active_removes_queued_audio_and_drops_late_audio():
    engine = _engine(tail_silence_ms=0)

    engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(480 * 3),
    )
    cancelled = engine.cancel_active()
    late = engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(480),
    )

    assert cancelled == 3
    assert late == []
    assert engine.queue_depth == 0
    assert engine.stats.cancelled_frames == 3
    assert engine.stats.dropped_stale_audio_bytes == 960


def test_stale_turn_audio_is_dropped_when_new_turn_is_active():
    engine = _engine(tail_silence_ms=0)
    engine.start_response(turn_id=2, response_id="resp-2")

    stale = engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(480),
    )
    fresh = engine.append_audio(
        turn_id=2,
        response_id="resp-2",
        model_pcm=_model_pcm_24k(480),
    )

    assert stale == []
    assert len(fresh) == 1
    assert engine.stats.dropped_stale_audio_bytes == 960


def test_playout_sequences_are_monotonic_across_responses():
    engine = _engine(tail_silence_ms=0)

    first = engine.append_audio(
        turn_id=1,
        response_id="resp-1",
        model_pcm=_model_pcm_24k(480),
    )
    engine.finish_response(turn_id=1, response_id="resp-1")
    second = engine.append_audio(
        turn_id=2,
        response_id="resp-2",
        model_pcm=_model_pcm_24k(480),
    )

    assert first[0].sequence == 0
    assert second[0].sequence == 1


def test_tail_silence_must_align_to_phone_frame_duration():
    contract = build_realtime_phone_contract(FreeSwitchConfig())

    with pytest.raises(ValueError, match="tail_silence_ms"):
        PlayoutEngine(contract=contract, tail_silence_ms=10)


def _engine(*, tail_silence_ms: int) -> PlayoutEngine:
    return PlayoutEngine(
        contract=build_realtime_phone_contract(FreeSwitchConfig()),
        tail_silence_ms=tail_silence_ms,
    )


def _model_pcm_24k(sample_count: int) -> bytes:
    return samples_to_pcm_s16le([1200] * sample_count)
