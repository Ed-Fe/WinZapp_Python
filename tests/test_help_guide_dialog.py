"""The in-app usage guide: which file it opens, and which links stay inside it.

Only the pure decisions are tested here. Building `HelpGuideDialog` opens a real
window (and WebView2), which a plain `pytest` must never do on somebody's
desktop - see tests/test_no_desktop_visible_windows.py.
"""

import pytest

from tests.locales import registered_locale_codes
from ui.dialogs.help_guide_dialog import find_help_guide_path, is_external_url


def _fake_path_for(*parts):
    return "/".join(parts)


class TestFindHelpGuidePath:
    def test_uses_the_current_language_when_its_guide_exists(self):
        found = find_help_guide_path(
            "en-US", path_for=_fake_path_for, exists=lambda p: True
        )
        assert found == "data/help_guide/en-US.html"

    def test_falls_back_to_pt_br_when_the_language_has_no_guide(self):
        found = find_help_guide_path(
            "pl", path_for=_fake_path_for,
            exists=lambda p: p.endswith("pt-BR.html"),
        )
        assert found == "data/help_guide/pt-BR.html"

    def test_returns_none_when_no_guide_exists_at_all(self):
        assert find_help_guide_path(
            "pl", path_for=_fake_path_for, exists=lambda p: False
        ) is None

    def test_every_registered_language_ships_its_own_guide(self):
        for code in registered_locale_codes():
            path = find_help_guide_path(code)
            assert path is not None and path.endswith(f"{code}.html"), code


class TestIsExternalUrl:
    @pytest.mark.parametrize("url", [
        "https://github.com/x/y",
        "http://example.com",
        "HTTPS://EXAMPLE.COM",
        "  https://example.com",
        "mailto:someone@example.com",
    ])
    def test_web_links_leave_the_guide(self, url):
        assert is_external_url(url) is True

    @pytest.mark.parametrize("url", [
        "file:///C:/WinZapp/data/help_guide/pt-BR.html#envio",
        "file:///C:/WinZapp/data/help_guide/pt-BR.html",
        "about:blank",
        "#envio",
        "",
        None,
    ])
    def test_the_guides_own_navigation_stays_inside(self, url):
        assert is_external_url(url) is False
