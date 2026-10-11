import io
import secrets

from PIL import Image, ImageDraw, ImageFont


def render(code: str) -> io.BytesIO:
    image = Image.new("RGB", (340, 110), (18, 22, 37))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=44)
    for _ in range(16):
        draw.line(
            [
                (secrets.randbelow(340), secrets.randbelow(110)),
                (secrets.randbelow(340), secrets.randbelow(110)),
            ],
            fill=(61, 67, 99),
            width=2,
        )
    for index, char in enumerate(code):
        tile = Image.new("RGBA", (58, 80))
        ImageDraw.Draw(tile).text((8, 8), char, font=font, fill=(204, 191, 255))
        tile = tile.rotate(
            secrets.randbelow(25) - 12, resample=Image.Resampling.BICUBIC
        )
        image.paste(tile, (10 + index * 53, 12 + secrets.randbelow(12)), tile)
    for _ in range(180):
        draw.point(
            (secrets.randbelow(340), secrets.randbelow(110)), fill=(130, 114, 169)
        )
    data = io.BytesIO()
    image.save(data, format="PNG")
    data.seek(0)
    return data
