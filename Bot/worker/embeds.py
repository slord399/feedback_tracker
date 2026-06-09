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
    if author:
        embed.set_author(name=author.get("name", "Unknown"), icon_url=author.get("avatarURL"))

    details = post.get("details", "")
    if len(details) > 1000:
        details = details[:1000] + loc.get("continue", lang)

    category = post.get("category")
    category_name = category.get("name", "None") if category else "None"
    if board: category_name = f"{board.get('name')} / {category_name}"

    # Precise section formatting
    # 1st section: text
    # {blank line}
    # 2nd section: Category in bold
    # 3rd section: full name of category
    # {blank line}
    # 4th section: Status Created Votes headers
    # 5th section: values

    current_status = post.get("status", "open")
    status_text = current_status
    if old_status and old_status != current_status:
        status_text = f"{old_status} > {current_status}"

    created_ts = post.get("created")
    try:
        dt = datetime.fromisoformat(created_ts.replace("Z", "+00:00"))
        created_display = f"<t:{int(dt.timestamp())}:R>"
    except: created_display = created_ts

    votes = post.get("score", 0)

    description = (
        f"{details}\n\n"
        f"**{loc.get('category', lang)}**\n"
        f"{category_name}\n\n"
        f"**{loc.get('status', lang)}** | **{loc.get('created', lang)}** | **{loc.get('votes', lang)}**\n"
        f"{status_text} | {created_display} | {votes}"
    )

    if old_status and old_status != current_status:
        # "If in feed as status update... you don't need to post 1st section of text as it's known already."
        description = (
            f"**{loc.get('category', lang)}**\n"
            f"{category_name}\n\n"
            f"**{loc.get('status', lang)}** | **{loc.get('created', lang)}** | **{loc.get('votes', lang)}**\n"
            f"{status_text} | {created_display} | {votes}"
        )

    embed.description = description

    if current_status.lower() in ["complete", "completed", "available in future release"]:
        image_urls = post.get("imageURLs", [])
        if image_urls: embed.set_thumbnail(url=image_urls[0])

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
