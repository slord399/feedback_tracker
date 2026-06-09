import discord
from Bot.shared.localization import get_localizer
from datetime import datetime

def create_canny_embed(post, old_status=None, user_info=None, lang="English"):
    loc = get_localizer()
    title = post.get("title")
    board = post.get("board", {})
    board_url_name = board.get("urlName", "p")
    post_url_name = post.get("urlName")
    url = f"https://feedback.vrchat.com/{board_url_name}/p/{post_url_name}"

    embed = discord.Embed(title=title, url=url, color=discord.Color.blue())
    author = post.get("author", {})
    if author: embed.set_author(name=author.get("name", "Unknown"), icon_url=author.get("avatarURL"))

    details = post.get("details", "")
    if len(details) > 1000: details = details[:1000] + loc.get("continue", lang)

    category = post.get("category")
    category_name = category.get("name", "None") if category else "None"
    if board: category_name = f"{board.get('name')} / {category_name}"

    current_status = post.get("status", "open")
    status_text = current_status
    if old_status and old_status != current_status: status_text = f"{old_status} > {current_status}"

    created_ts = post.get("created")
    try:
        dt = datetime.fromisoformat(created_ts.replace("Z", "+00:00"))
        created_display = f"<t:{int(dt.timestamp())}:R>"
    except: created_display = created_ts

    votes = str(post.get("score", 0))

    # Refined alignment without code block markdown.
    # We use bold headers and multiple spaces/tabs to simulate alignment.
    # Note: Discord variable-width fonts make pixel-perfect alignment with spaces difficult.
    # However, we'll try to provide a clean layout.
    header = f"**{loc.get('status', lang)}**\u2000\u2000\u2000\u2000|\u2000\u2000\u2000\u2000**{loc.get('created', lang)}**\u2000\u2000\u2000\u2000|\u2000\u2000\u2000\u2000**{loc.get('votes', lang)}**"
    values = f"{status_text}\u2000\u2000\u2000\u2000|\u2000\u2000\u2000\u2000{created_display}\u2000\u2000\u2000\u2000|\u2000\u2000\u2000\u2000{votes}"

    description = (
        f"{details}\n\n"
        f"**{loc.get('category', lang)}**\n"
        f"{category_name}\n\n"
        f"{header}\n"
        f"{values}"
    )

    if old_status and old_status != current_status:
        description = (
            f"**{loc.get('category', lang)}**\n"
            f"{category_name}\n\n"
            f"{header}\n"
            f"{values}"
        )

    embed.description = description

    image_urls = post.get("imageURLs", [])
    if image_urls: embed.set_image(url=image_urls[0])

    if user_info:
        footer_key = "indexed_by" if user_info.get("type") == "indexed" else "requested_by"
        embed.set_footer(text=loc.get(footer_key, lang, user=user_info.get("name")), icon_url=user_info.get("icon"))

    return embed

def create_canny_view(post_url):
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="feedback.vrchat.com", url=post_url))
    view.add_item(discord.ui.Button(label="vrchat.canny.io", url=post_url.replace("feedback.vrchat.com", "vrchat.canny.io")))
    view.add_item(discord.ui.Button(label="Archive.org", url=f"https://web.archive.org/web/{post_url}"))
    return view
