import server


def test_finish_marker_maps_finishes_to_archidekt_syntax():
    assert server._finish_marker("foil") == "*F*"
    assert server._finish_marker("etched") == "*E*"


def test_finish_marker_is_empty_for_nonfoil_and_none():
    assert server._finish_marker("nonfoil") == ""
    assert server._finish_marker(None) == ""
    assert server._finish_marker("") == ""
