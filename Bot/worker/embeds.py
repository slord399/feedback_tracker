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
    category_raw = category.get("name", "None") if category else "None"
    category_name = loc.get(category_raw.lower(), lang)
    if board:
        board_raw = board.get('name', 'None')
        board_name = loc.get(board_raw.lower(), lang)
        category_name = f"{board_name} / {category_name}"

    current_status = post.get("status", "open")
    localized_status = loc.get(current_status.lower(), lang)
    status_text = localized_status
    if old_status and old_status != current_status:
        localized_old = loc.get(old_status.lower(), lang)
        status_text = f"{localized_old} > {localized_status}"

    created_ts = post.get("created")
    try:
        dt = datetime.fromisoformat(created_ts.replace("Z", "+00:00"))
        created_display = f"<t:{int(dt.timestamp())}:R>"
    except: created_display = created_ts

    votes = str(post.get("score", 0))

    # Using Embed Fields for perfect alignment
    description = f"{details}\n\n**{loc.get('category', lang)}**\n{category_name}\n\n"
    if old_status and old_status != current_status:
         description = f"**{loc.get('category', lang)}**\n{category_name}\n\n"

    embed.description = description

    # Add status, created, and votes as inline fields
    embed.add_field(name=loc.get('status', lang), value=status_text, inline=True)
    embed.add_field(name=loc.get('created', lang), value=created_display, inline=True)
    embed.add_field(name=loc.get('votes', lang), value=votes, inline=True)

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
