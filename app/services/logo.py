"""Logo upload for invoices and key tags: validated with Pillow and stored re-encoded as a small PNG."""
from io import BytesIO

from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 1_000_000
MAX_SIDE = 600
ALLOWED = {"PNG", "JPEG", "WEBP"}


def process_logo(file_storage) -> tuple[bytes, str]:
    """Return (png_bytes, mime) for an uploaded logo, or raise ValueError with a message safe to show."""
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise ValueError("Choose a logo file to upload.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("The logo is larger than 1 MB. Use a smaller image.")
    try:
        probe = Image.open(BytesIO(raw))
        probe.verify()
        image = Image.open(BytesIO(raw))  # verify() invalidates the first handle
        if image.format not in ALLOWED:
            raise ValueError("Only PNG, JPEG and WebP logos are accepted.")
        if image.width * image.height > 25_000_000:
            raise ValueError("The logo image is too large.")
        image = image.convert("RGBA")
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("The uploaded file is not a valid image.") from exc
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue(), "image/png"
