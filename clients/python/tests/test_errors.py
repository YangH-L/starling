import pytest

from starling_client import errors as E


@pytest.mark.parametrize(
    "code,status,cls",
    [
        ("recall_degraded", 503, E.RecallDegradedError),
        ("event_conflict", 409, E.EventConflict),
        ("handle_closed", 409, E.MemoryClosed),
        ("protocol_mismatch", 426, E.ProtocolMismatch),
        ("session_expired", 401, E.SessionExpired),
        ("world_not_found", 404, E.WorldError),
        ("invalid_world_name", 422, E.WorldError),
        ("snapshot_busy", 409, E.WorldError),
        ("endpoint_changed", 409, E.WorldError),
        ("event_not_found", 404, KeyError),
        ("internal_error", 500, E.ServiceError),
        ("unauthorized", 401, E.Unauthorized),
    ],
)
def test_error_body_maps_to_named_exceptions(code, status, cls):
    body = {"code": code, "transient": status >= 500, "detail": code}
    exc = E.from_body(status, body)
    assert isinstance(exc, cls)
    assert isinstance(exc, E.StarlingClientError)
    assert exc.code == code
    assert exc.transient is body["transient"]


def test_memory_closed_is_runtime_error():
    assert isinstance(E.from_body(409, {"code": "handle_closed", "transient": False, "detail": "handle_closed"}), RuntimeError)


def test_legacy_body_without_transient():
    e5 = E.from_body(500, {"detail": "boom"})
    assert isinstance(e5, E.ServiceError) and e5.transient is True and e5.code == "boom"
    e4 = E.from_body(404, {"detail": "world_not_found"})
    assert isinstance(e4, E.WorldError) and e4.transient is False


def test_non_dict_body_does_not_crash():
    e = E.from_body(502, None)
    assert isinstance(e, E.ServiceError) and e.transient is True


def test_event_not_found_str_is_plain():
    e = E.from_body(404, {"code": "event_not_found", "transient": False, "detail": "event_not_found"})
    assert isinstance(e, KeyError) and str(e) == "event_not_found"


def test_missing_code_falls_back_to_http_status():
    e = E.from_body(404, None)
    assert e.code == "http_404" and e.transient is False
