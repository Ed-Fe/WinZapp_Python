"""Mistral routes each media kind to its own model; no network in this suite."""
import json
import pytest
from core.ai_media import config, model_catalog, providers, service
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import Media
from tests.test_ai_media_core import FakeHTTP, FakeResponse

AUDIO = Media("audio", b"synthetic-recording", "audio/ogg", "audio.ogg")
IMAGE = Media("image", b"synthetic-picture", "image/jpeg", "image.jpg")
PDF = Media("pdf", b"synthetic-pdf", "application/pdf", "document.pdf")


def request(media, model="mistral-small-latest", history=()):
    return providers.build_request("mistral", model, "synthetic-key", media,
                                   history, "Describe this", "Accessible description", "balanced")


@pytest.mark.parametrize("model", ["mistral-small-latest", "ministral-3b-latest"])
def test_audio_always_uses_voxtral_independently_of_the_vision_model(model):
    result = request(AUDIO, model)
    assert result.url == "https://api.mistral.ai/v1/audio/transcriptions"
    assert result.headers == {"Authorization": "Bearer synthetic-key"}
    assert result.data == {"model": "voxtral-mini-transcribe-3"}
    assert result.files == {"file": ("audio.ogg", AUDIO.data, "audio/ogg")}
    assert result.json is None


def test_images_and_stickers_use_selected_vision_model_and_history():
    for kind in ("image", "sticker"):
        result = request(Media(kind, IMAGE.data, IMAGE.mime, IMAGE.filename),
                         "ministral-3b-latest", (("assistant", "a green rectangle"),))
        assert result.url == "https://api.mistral.ai/v1/chat/completions"
        assert result.json["model"] == "ministral-3b-latest"
        assert result.json["messages"][0] == {"role": "system", "content": "Accessible description"}
        assert result.json["messages"][1]["content"] == "a green rectangle"
        part = result.json["messages"][-1]["content"][-1]
        assert part["image_url"].startswith("data:image/jpeg;base64,")
        assert result.files is None


def test_pdf_always_uses_ocr_in_a_single_inline_request():
    result = request(PDF, "ministral-3b-latest")
    assert result.url == "https://api.mistral.ai/v1/ocr"
    assert result.json["model"] == "mistral-ocr-latest"
    assert result.json["document"]["document_url"].startswith("data:application/pdf;base64,")
    assert result.json["include_image_base64"] is False
    assert "pages" not in result.json
    assert result.files is None


def test_saved_audio_only_preview_model_becomes_the_vision_default():
    prefs = config.preferences({"ai_media": {"order": ["groq", "openai"],
        "models": {"mistral": "voxtral-mini-transcribe-3"}}})
    assert prefs["order"][:2] == ["groq", "openai"]
    assert prefs["models"]["mistral"] == "ministral-3b-latest"
    assert "mistral" in prefs["auto_models"]
    for kind in config.KINDS:
        assert config.supports("mistral", kind)
        assert config.chain(prefs, kind, lambda p: p == "mistral") == ["mistral"]


def test_get_models_lists_only_vision_and_uses_mistral():
    body = {"data": [{"id": name} for name in (
        "mistral-small-latest", "ministral-3b-latest", "mistral-ocr-latest",
        "voxtral-mini-transcribe-3", "voxtral-mini-latest", "../unsafe")]}
    http = FakeHTTP(FakeResponse(data=json.dumps(body).encode()))
    options = model_catalog.fetch_models("mistral", "key", service.RequestToken(),
                                         session_factory=lambda: http)
    assert [option.id for option in options] == ["mistral-small-latest", "ministral-3b-latest"]
    assert http.calls[0][0] == "https://api.mistral.ai/v1/models"
    assert http.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("body", [{}, {"text": None}, {"text": []}, {"text": " "},
                                   {"text": "partial", "finish_reason": "length"}])
def test_malformed_or_partial_transcripts_are_not_success(body):
    with pytest.raises(DescriptionError, match="ai_error_response"):
        providers.parse_answer("mistral", body, "audio")


def test_complete_ocr_pages_are_returned_in_reading_order():
    body = {"pages": [{"index": 1, "markdown": "Page two"},
                       {"index": 0, "markdown": "Page one"}], "usage_info": {"doc_pages": 2}}
    assert providers.parse_answer("mistral", body, "pdf") == "Page one\n\nPage two"


@pytest.mark.parametrize("body", [
    {}, {"pages": []}, {"pages": [{"index": 0, "markdown": None}]},
    {"pages": [{"index": 1, "markdown": "missing page zero"}]},
    {"pages": [{"index": 0, "markdown": "a"}, {"index": 0, "markdown": "duplicate"}]},
    {"pages": [{"index": 0, "markdown": "a"}], "usage_info": {"doc_pages": 2}},
])
def test_incomplete_ocr_is_not_announced_as_complete(body):
    with pytest.raises(DescriptionError, match="ai_error_response"):
        providers.parse_answer("mistral", body, "pdf")


def test_long_transcripts_are_never_cut_silently():
    with pytest.raises(DescriptionError, match="ai_error_output_limit"):
        providers.parse_answer("mistral", {"text": "x"*(config.MAX_OUTPUT_CHARS["audio"]+1)}, "audio")


def test_mistral_service_reads_the_response_and_probe_is_metadata_only():
    http = FakeHTTP(FakeResponse(data=b'{"text":"synthetic transcript"}'))
    text = service.request_answer("mistral", "mistral-small-latest", "key", AUDIO,
                                  (), "", "", "balanced", service.RequestToken(),
                                  session_factory=lambda: http)
    assert text == "synthetic transcript"
    assert http.calls[0][1]["data"] == {"model": "voxtral-mini-transcribe-3"}
    assert service.probe_request("mistral", "mistral-small-latest", "key")[0] == (
        "https://api.mistral.ai/v1/models/mistral-small-latest")
