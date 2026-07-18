from tmuxbot.frontends.feishu import (
    DEFAULT_WS_MAX_CONNECTION_SECONDS,
    feishu_ws_max_connection_seconds,
)


def test_feishu_ws_rotation_interval_is_bounded_and_configurable():
    assert feishu_ws_max_connection_seconds({}) == DEFAULT_WS_MAX_CONNECTION_SECONDS
    assert feishu_ws_max_connection_seconds({"TMUXBOT_FEISHU_WS_MAX_CONNECTION_SECONDS": "3600"}) == 3600
    assert feishu_ws_max_connection_seconds({"TMUXBOT_FEISHU_WS_MAX_CONNECTION_SECONDS": "0"}) == 60
    assert feishu_ws_max_connection_seconds({"TMUXBOT_FEISHU_WS_MAX_CONNECTION_SECONDS": "bad"}) == DEFAULT_WS_MAX_CONNECTION_SECONDS
