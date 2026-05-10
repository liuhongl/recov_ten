from __future__ import annotations

import pytest

from app.playout_controller import (
    PlayoutController,
    PlayoutControllerConfig,
    PlayoutPacingMode,
)


def test_playout_controller_uses_hysteresis_between_fast_and_realtime():
    controller = PlayoutController(
        PlayoutControllerConfig(
            frame_duration_ms=20,
            fast_send_interval_ms=10,
            prefill_frames=25,
        )
    )
    state = controller.new_state()

    low_decision = controller.decide(state, queued_frames=12)
    high_decision = controller.decide(state, queued_frames=25)
    mid_decision = controller.decide(state, queued_frames=13)
    drained_decision = controller.decide(state, queued_frames=12)

    assert low_decision.mode == PlayoutPacingMode.REALTIME
    assert low_decision.interval_ms == 20
    assert low_decision.switched is False

    assert high_decision.mode == PlayoutPacingMode.FAST
    assert high_decision.interval_ms == 10
    assert high_decision.switched is True

    assert mid_decision.mode == PlayoutPacingMode.FAST
    assert mid_decision.interval_ms == 10
    assert mid_decision.switched is False

    assert drained_decision.mode == PlayoutPacingMode.REALTIME
    assert drained_decision.interval_ms == 20
    assert drained_decision.switched is True


def test_playout_controller_stays_realtime_when_fast_interval_is_disabled():
    controller = PlayoutController(
        PlayoutControllerConfig(
            frame_duration_ms=20,
            fast_send_interval_ms=20,
            prefill_frames=25,
        )
    )
    state = controller.new_state()

    decision = controller.decide(state, queued_frames=100)

    assert decision.mode == PlayoutPacingMode.REALTIME
    assert decision.interval_ms == 20
    assert decision.switched is False


def test_playout_controller_rejects_invalid_watermarks():
    with pytest.raises(ValueError, match="high_watermark_frames"):
        PlayoutController(
            PlayoutControllerConfig(
                frame_duration_ms=20,
                fast_send_interval_ms=10,
                prefill_frames=25,
                low_watermark_frames=20,
                high_watermark_frames=10,
            )
        )
