import io
from typing import List, Optional

from PIL import Image, UnidentifiedImageError

DEFAULT_GAP = 12
DEFAULT_BG = (255, 255, 255)


def merge_images(
    buffers: List[io.BytesIO],
    direction: str = "horizontal",
    gap: int = DEFAULT_GAP,
    bg=DEFAULT_BG,
) -> Optional[io.BytesIO]:
    """Stitch several PNG buffers into one image.

    direction="horizontal" places charts side by side, which survives Telegram's
    sendPhoto downscaling better than a tall vertical stack. Returns None if
    nothing decodable was supplied.
    """
    images = []
    try:
        for buf in buffers:
            if buf is None:
                continue
            try:
                buf.seek(0)
                images.append(Image.open(buf).convert("RGB"))
            except (UnidentifiedImageError, OSError, ValueError) as err:
                print(f"Skipping unreadable image buffer: {err}")

        if not images:
            return None
        if len(images) == 1:
            out = io.BytesIO()
            images[0].save(out, format="PNG", optimize=True)
            out.seek(0)
            return out

        total_gap = gap * (len(images) - 1)

        if direction == "vertical":
            width = max(im.width for im in images)
            height = sum(im.height for im in images) + total_gap
            canvas = Image.new("RGB", (width, height), bg)
            offset = 0
            for im in images:
                canvas.paste(im, ((width - im.width) // 2, offset))
                offset += im.height + gap
        else:
            width = sum(im.width for im in images) + total_gap
            height = max(im.height for im in images)
            canvas = Image.new("RGB", (width, height), bg)
            offset = 0
            for im in images:
                canvas.paste(im, (offset, (height - im.height) // 2))
                offset += im.width + gap

        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        canvas.close()
        out.seek(0)
        return out

    finally:
        for im in images:
            im.close()
