"""Raster profile card matching Melora's Java canvas style."""

from __future__ import annotations

import io
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 800, 500
PINK = (255, 228, 235)
WHITE = (255, 255, 255)
INK = (70, 70, 70)
MUTED = (120, 120, 120)
ACCENT = (230, 100, 150)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ),
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> str:
    text = str(text).replace("\n", " ").strip()
    if draw.textbbox((0, 0), text, font=font)[2] <= width:
        return text
    while text and draw.textbbox((0, 0), text + "…", font=font)[2] > width:
        text = text[:-1]
    return text.rstrip() + "…"


def render_profile(
    *,
    display_name: str,
    favorite_count: int,
    playlist_count: int,
    recent_tracks: Sequence[str],
    avatar: bytes | None = None,
) -> bytes:
    """Render the profile as an 800x500 PNG image for a Discord attachment."""
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            ratio = (x / WIDTH + y / HEIGHT) / 2
            pixels[x, y] = tuple(round(PINK[i] * (1 - ratio) + WHITE[i] * ratio) for i in range(3))

    draw = ImageDraw.Draw(image, "RGBA")
    shadow = (0, 0, 0, 24)
    panel = (255, 255, 255, 210)
    draw.ellipse((52, 23, 154, 125), fill=shadow)
    if avatar:
        try:
            face = Image.open(io.BytesIO(avatar)).convert("RGB").resize((96, 96), Image.Resampling.LANCZOS)
            mask = Image.new("L", (96, 96), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 95, 95), fill=255)
            image.paste(face, (54, 22), mask)
        except (OSError, ValueError):
            draw.ellipse((54, 22, 150, 118), fill=(255, 255, 255, 230))
    else:
        draw.ellipse((54, 22, 150, 118), fill=(255, 255, 255, 230))

    draw.text((170, 35), _fit(draw, display_name, _font(34, bold=True), 580), fill=INK, font=_font(34, bold=True))
    draw.text((170, 79), "Music Profile", fill=MUTED, font=_font(22))

    draw.rounded_rectangle((50, 150, 750, 230), radius=20, fill=panel)
    draw.text((80, 177), f"Favorite Tracks: {favorite_count}", fill=INK, font=_font(20, bold=True))
    draw.text((420, 177), f"Saved Playlists: {playlist_count}", fill=INK, font=_font(20, bold=True))

    draw.rounded_rectangle((50, 250, 750, 450), radius=20, fill=panel)
    draw.text((75, 270), "Recently Requested", fill=INK, font=_font(22, bold=True))
    body_font = _font(18)
    tracks = list(recent_tracks[:5])
    if tracks:
        for index, title in enumerate(tracks, start=1):
            y = 310 + (index - 1) * 27
            draw.text((78, y), f"{index}.", fill=ACCENT, font=body_font)
            draw.text((112, y), _fit(draw, title, body_font, 590), fill=INK, font=body_font)
    else:
        draw.text((78, 315), "No recent tracks recorded", fill=MUTED, font=body_font)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_stats(
    *,
    display_name: str,
    total_listening_ms: int,
    total_tracks_played: int,
    artists: Sequence[tuple[str, int]],
    tracks: Sequence[tuple[str, str, int]],
    avatar: bytes | None = None,
) -> bytes:
    """Render the personal listening stats canvas used by the Java bot."""
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        color = tuple(round(PINK[i] * (1 - ratio) + WHITE[i] * ratio) for i in range(3))
        for x in range(WIDTH):
            pixels[x, y] = color
    draw = ImageDraw.Draw(image, "RGBA")
    draw.ellipse((52, 23, 154, 125), fill=(0, 0, 0, 24))
    if avatar:
        try:
            face = Image.open(io.BytesIO(avatar)).convert("RGB").resize((96, 96), Image.Resampling.LANCZOS)
            mask = Image.new("L", (96, 96), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 95, 95), fill=255)
            image.paste(face, (54, 22), mask)
        except (OSError, ValueError):
            pass
    title_font = _font(34, bold=True)
    draw.text((170, 35), _fit(draw, display_name, title_font, 580), fill=INK, font=title_font)
    draw.text((170, 79), "Listening Stats", fill=MUTED, font=_font(22))

    panel = (255, 255, 255, 210)
    draw.rounded_rectangle((50, 150, 750, 230), radius=20, fill=panel)
    total_minutes = max(0, int(total_listening_ms)) // 60_000
    hours, minutes = divmod(total_minutes, 60)
    summary_font = _font(20, bold=True)
    draw.text((80, 177), f"Total Time: {hours}h {minutes}m", fill=INK, font=summary_font)
    draw.text((420, 177), f"Tracks Played: {max(0, int(total_tracks_played))}", fill=INK, font=summary_font)

    draw.rounded_rectangle((50, 250, 380, 450), radius=20, fill=panel)
    draw.rounded_rectangle((420, 250, 750, 450), radius=20, fill=panel)
    heading_font = _font(22, bold=True)
    body_font = _font(17)
    draw.text((70, 270), "Top Artists", fill=INK, font=heading_font)
    draw.text((440, 270), "Top Tracks", fill=INK, font=heading_font)
    if artists:
        for i, (artist, plays) in enumerate(artists[:4], start=1):
            y = 312 + (i - 1) * 32
            draw.text((72, y), _fit(draw, f"{i}. {artist}", body_font, 210), fill=INK, font=body_font)
            draw.text((318, y), str(plays), fill=ACCENT, font=body_font)
    else:
        draw.text((72, 312), "None yet", fill=MUTED, font=body_font)
    if tracks:
        for i, (title, artist, plays) in enumerate(tracks[:4], start=1):
            y = 310 + (i - 1) * 34
            label = f"{title} — {artist}" if artist else title
            draw.text((442, y), _fit(draw, f"{i}. {label}", body_font, 225), fill=INK, font=body_font)
            draw.text((690, y), str(plays), fill=ACCENT, font=body_font)
    else:
        draw.text((442, 312), "None yet", fill=MUTED, font=body_font)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
