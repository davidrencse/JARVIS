"""Small raster previews; the storage layer checks allowed roots before calling."""


def thumbnail_payload(path):
    # Keep this function self-contained: it also runs on legacy SSH devices.
    import base64
    import io
    import os
    import stat
    import warnings
    from PIL import Image, ImageOps

    if not stat.S_ISREG(os.stat(path).st_mode):
        raise ValueError("Preview requires an image file.")
    with open(path, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 20 * 1024 * 1024:
            raise ValueError("Preview requires an image smaller than 20 MB.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source, formats=("JPEG", "PNG", "WEBP", "GIF", "BMP")) as image:
                if image.width * image.height > 30_000_000:
                    raise ValueError("Image is too large to preview.")
                image.seek(0)
                image.thumbnail((320, 240))
                preview = ImageOps.exif_transpose(image).convert("RGBA")
                preview.thumbnail((240, 180))
                output = io.BytesIO()
                preview.save(output, format="PNG")
    return {"image": base64.b64encode(output.getvalue()).decode("ascii")}
