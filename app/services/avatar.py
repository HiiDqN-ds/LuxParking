"""Profile photo upload: validated with Pillow, cropped to a square and stored re-encoded as a small WebP.

Re-encoding drops EXIF data (GPS position, camera, ...) and anything hidden in the original file. Raises AvatarError
with a short code that the caller turns into a message for the visitor.
"""
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 3_000_000
MAX_PIXELS = 25_000_000
SIZE = 256
ALLOWED = {"PNG", "JPEG", "WEBP"}


class AvatarError(ValueError):
    """code: empty | too_large | format | pixels | invalid"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def process_avatar(file_storage) -> tuple[bytes, str]:
    """Return (webp_bytes, mime) for an uploaded photo."""
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1) if file_storage is not None else b""
    if not raw:
        raise AvatarError("empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AvatarError("too_large")
    try:
        Image.open(BytesIO(raw)).verify()
        image = Image.open(BytesIO(raw))  # verify() invalidates the first handle
        if image.format not in ALLOWED:
            raise AvatarError("format")
        if image.width * image.height > MAX_PIXELS:
            raise AvatarError("pixels")
        image = ImageOps.exif_transpose(image)  # phone photos: respect the rotation flag before it is dropped
        image.load()
    except AvatarError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, ValueError) as exc:
        raise AvatarError("invalid") from exc
    image = image.convert("RGBA")
    flat = Image.new("RGB", image.size, (255, 255, 255))  # transparent PNGs get a white background
    flat.paste(image, mask=image.split()[3])
    square = ImageOps.fit(flat, (SIZE, SIZE), method=Image.LANCZOS, centering=(0.5, 0.4))  # a little upward: faces
    out = BytesIO()
    square.save(out, "WEBP", quality=85, method=4)
    return out.getvalue(), "image/webp"
