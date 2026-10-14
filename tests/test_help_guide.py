"""Help > Usage guide: which file opens, and that it opens in the browser."""

from core.help_guide import find_help_guide_path, open_help_guide
from tests.locales import registered_locale_codes


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


class TestOpenHelpGuide:
    def test_opens_the_guide_of_the_language_with_the_given_opener(self):
        opened = []
        assert open_help_guide("es-ES", open_file=opened.append) is True
        assert len(opened) == 1 and opened[0].endswith("es-ES.html")

    def test_an_unknown_language_opens_the_pt_br_guide(self):
        opened = []
        assert open_help_guide("xx", open_file=opened.append) is True
        assert opened[0].endswith("pt-BR.html")
