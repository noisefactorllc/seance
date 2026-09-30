import base64
import hashlib
import json
from pathlib import Path

import pytest

from app.config import Limits
from app.images import ImageError, validate_image

FIXTURES = json.loads((Path(__file__).parent / "fixtures/images.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda image: image["mimeType"])
def test_raster_formats_preserve_bytes_and_oriented_dimensions(fixture):
    data = base64.b64decode(fixture["base64"])
    asset = {"id": hashlib.sha256(data).hexdigest(),
             "dataUrl": f"data:{fixture['mimeType']};base64,{fixture['base64']}",
             "width": 999, "height": 999}
    image = validate_image(asset, Limits())
    assert image.data == data
    assert (image.width, image.height) == (fixture["width"], fixture["height"])


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda image: image["mimeType"])
def test_truncated_and_mislabeled_images_are_refused(fixture):
    data = base64.b64decode(fixture["base64"])[:10]
    with pytest.raises(ImageError):
        validate_image({"id": hashlib.sha256(data).hexdigest(),
                        "dataUrl": f"data:{fixture['mimeType']};base64,"
                        + base64.b64encode(data).decode()}, Limits())
    full = base64.b64decode(fixture["base64"])
    with pytest.raises(ImageError):
        validate_image({"id": hashlib.sha256(full).hexdigest(),
                        "dataUrl": "data:image/png;base64," + fixture["base64"]}, Limits())
