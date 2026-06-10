# VRChat Canny Status Tracker

A powerful, distributed Discord bot designed to keep your community informed about feedback and feature requests from `feedback.vrchat.com`.

## Features
- **Real-time Status Tracking**: Automatically receive updates when a Canny post's status changes or hits vote milestones.
- **Global & Local Modes**: Choose whether to receive updates for all tracked posts globally or keep things focused on your server's interests.
- **Deep Search**: Utilize an interactive search system with advanced filters (boards, status, votes, etc.) to find exactly what you're looking for.
- **Adaptive Polling**: Intelligent polling logic ensures active posts are updated frequently while conserving resources for older content.
- **User App Integration**: Convenient context menu commands allow users to interact with Canny links directly from any message.
- **Metrics & Insights**: Track trending feedback and recognize top contributors with detailed weekly and monthly analytics.
- **Multi-lingual Support**: Fully localizable UI with support for 12+ languages.

## Commands
- `/stats`: View global and server-specific activity metrics.
- `/metrics`: View top contributors and trending posts (Weekly/Monthly Trending, Top Authors, Milestone Masters).
- `/search`: Search Canny posts with interactive filters.
- `/ping`: Check API and bot latencies.
- `/credit`: Bot information and affiliation details.
- `/help`: Detailed command guide and usage tips.

### Administrative Commands
*These commands require the **Manage Messages** permission.*

- `/settings`: View current server configuration, including mode and target channels.
- `/mode`: Toggle between **Global** (track everything) and **Local** (track only what you index) modes.
- `/set_status_channel`: Select the channel where status updates and notifications will be posted.
- `/react_channel add/remove`: Manage channels where the bot will automatically listen for and index Canny links.
- `/set_language`: Choose the preferred language for the bot's interface.
- `/bulk_add`: Quickly index all Canny links found in a channel's recent history.

## Context Menus
- `Index this canny`: Quickly track a post by right-clicking a message containing a Canny link.
- `Check canny status`: Instantly retrieve the current status, category, and votes for a post.
- `Post what I indexed in hour`: Share a list of posts you've recently indexed.
- `Check Trending Canny`: Interactive menu to view active feedback (Weekly/Monthly).
- `Check Canny Author Metrics`: Interactive menu to view contributor rankings (Posts/Milestones).
