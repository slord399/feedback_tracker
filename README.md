# VRChat Canny Status Tracking Bot

A distributed Discord bot to track feedback on `feedback.vrchat.com`.

## Features
- **Status Tracking**: Real-time updates for Canny posts.
- **Global/Local Modes**: Index posts across all servers or keep them local.
- **Search**: Interactive search with pagination.
- **Adaptive Polling**: Efficient updates based on post activity.
- **User App**: Context menu commands for ease of use.

## Commands
- `/stats`: Post activity metrics.
- `/search`: Search Canny posts.
- `/ping`: Check API latencies.
- `/credit`: Bot info.
- `/help`: Command guide.
- `/mode`: Toggle Global/Local mode (Admin).
- `/set_status_channel`: Set update channel (Admin).
- `/set_react_channel`: Set link-listening channel (Admin).
- `/set_language`: Set UI language (Admin).
- `/bulk_add`: Index all links in channel (Admin).

## Context Menus
- `Index this canny`: Index a post from a link.
- `Check canny status`: Get current status of a post.
- `Post what I indexed in hour`: Post your recent indexing activity.
