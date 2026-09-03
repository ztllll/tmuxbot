from tmuxbot.core.im_presentation import IMPresentationPolicy, PresentationMode


def test_im_presentation_defaults_to_compact_result_first_policy():
    policy = IMPresentationPolicy.from_environment({})

    assert policy.mode is PresentationMode.COMPACT
    assert policy.progress_enabled
    assert policy.progress_delay_seconds == 4.0
    assert policy.progress_update_interval_seconds == 2.0
    assert policy.progress_max_steps == 3


def test_result_only_disables_progress_but_preserves_attention_and_results():
    policy = IMPresentationPolicy.from_environment(
        {"TMUXBOT_IM_PRESENTATION": "result_only"}
    )

    assert policy.mode is PresentationMode.RESULT_ONLY
    assert not policy.progress_enabled


def test_verbose_can_override_delay_interval_and_step_budget():
    policy = IMPresentationPolicy.from_environment(
        {
            "TMUXBOT_IM_PRESENTATION": "verbose",
            "TMUXBOT_IM_PROGRESS_DELAY": "1.5",
            "TMUXBOT_IM_PROGRESS_UPDATE_INTERVAL": "0.5",
            "TMUXBOT_IM_PROGRESS_MAX_STEPS": "5",
        }
    )

    assert policy.mode is PresentationMode.VERBOSE
    assert policy.progress_delay_seconds == 1.5
    assert policy.progress_update_interval_seconds == 0.5
    assert policy.progress_max_steps == 5
