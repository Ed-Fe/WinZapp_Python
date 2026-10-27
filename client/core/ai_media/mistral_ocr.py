"""Read every OCR page in order; incomplete/malformed documents are failures."""
from .errors import DescriptionError


def parse_document(body):
    if not isinstance(body, dict) or not isinstance(body.get("pages"), list) or not body["pages"]:
        raise DescriptionError("response")
    pages = body["pages"]
    usage = body.get("usage_info")
    reported = usage.get("doc_pages") if isinstance(usage, dict) else None
    if reported is not None and (type(reported) is not int or reported != len(pages)):
        raise DescriptionError("response")
    by_index = {}
    for page in pages:
        if not isinstance(page, dict) or type(page.get("index")) is not int \
                or not isinstance(page.get("markdown"), str) or page["index"] in by_index:
            raise DescriptionError("response")
        by_index[page["index"]] = page["markdown"]
    if set(by_index) != set(range(len(pages))):
        raise DescriptionError("response")
    return "\n\n".join(by_index[index] for index in range(len(pages)))
