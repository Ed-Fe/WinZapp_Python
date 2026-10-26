"""Session name and body file: nothing that leaves data or reads the disk."""
import base64

import pytest

import inputs


@pytest.mark.parametrize("name", ["account1", "My_Account-2", "a" * 128])
def test_common_session_names_are_accepted(name):
    assert inputs.valid_session_name(name)


@pytest.mark.parametrize("name", ["", "..", "../x", "a/b", "a\\b", "c:", "a b", "a" * 129, None])
def test_a_name_that_leaves_the_folder_is_refused(name):
    assert not inputs.valid_session_name(name)


def test_body_bytes_accepts_data_uri_and_plain_base64():
    data = b"\x89PNG-fake"
    b64 = base64.b64encode(data).decode()
    assert inputs.body_bytes({"path": "data:image/png;base64," + b64}) == data
    assert inputs.body_bytes({"path": b64}) == data


@pytest.mark.parametrize("path", ["/etc/passwd", "C:\\Users\\x\\secret.key", "../../x.png", "", None])
def test_body_bytes_never_reads_a_disk_path(path):
    with pytest.raises(ValueError):
        inputs.body_bytes({"path": path})
