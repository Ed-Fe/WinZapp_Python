"""The connection-engine rules: pure functions, no window."""

from core import connection_engine as engines


def test_the_engine_comes_from_the_existing_connection_settings():
    local = ("http://127.0.0.1", "ws://127.0.0.1", 6300)
    assert engines.engine_of(False, *local) == engines.WPPCONNECT
    assert engines.engine_of(True, *local) == engines.NEONIZE
    assert engines.engine_of(True, "http://127.0.0.1/", "ws://127.0.0.1", "6300") == engines.NEONIZE
    assert engines.engine_of(True, "http://192.0.2.10", "ws://192.0.2.10", 6300) == engines.CUSTOM
    assert engines.engine_of(True, "http://127.0.0.1", "ws://127.0.0.1", 8443) == engines.CUSTOM
    assert engines.engine_of(True, "http://127.0.0.1", "ws://127.0.0.1", "x") == engines.CUSTOM


def test_only_the_bundled_server_is_not_a_custom_api():
    assert not engines.uses_custom_api(engines.WPPCONNECT)
    assert engines.uses_custom_api(engines.NEONIZE)
    assert engines.uses_custom_api(engines.CUSTOM)


def test_neonize_has_a_fixed_address_and_only_custom_is_editable():
    assert engines.fixed_address(engines.NEONIZE) == (
        "http://127.0.0.1", "ws://127.0.0.1", 6300)
    assert engines.fixed_address(engines.WPPCONNECT) is None
    assert engines.fixed_address(engines.CUSTOM) is None
    assert engines.addresses_editable(engines.CUSTOM)
    assert not engines.addresses_editable(engines.NEONIZE)
    assert not engines.addresses_editable(engines.WPPCONNECT)


def test_index_and_engine_are_inverse_and_safe_out_of_range():
    for engine in engines.ENGINES:
        assert engines.engine_at(engines.index_of(engine)) == engine
    assert engines.engine_at(-1) == engines.WPPCONNECT
    assert engines.engine_at(99) == engines.WPPCONNECT
    assert engines.index_of("nope") == 0
