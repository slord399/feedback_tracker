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

## Getting Started

### Prerequisites
- VPS or server with **Docker** and **Docker Compose** installed.
- A Discord Bot Token (from the [Discord Developer Portal](https://discord.com/developers/applications)).
- **Permissions**: The bot requires `Manage Messages` for administrative tasks and `Embed Links` for status updates.

### Deployment
1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/slord399/feedback_tracker.git
    cd feedback_tracker
    ```
2.  **Configure Environment**: Create a `.env` file in the root directory:
    ```env
    DISCORD_TOKEN=your_actual_discord_bot_token_here
    ```
3.  **Launch**: Run the deployment script:
    ```bash
    docker-compose up -d --build
    ```

## Architecture
The system is built with a distributed, microservices-inspired architecture for high availability and scalability:

- **Gateway**: Handles WebSocket connections to Discord, processes slash commands, and manages the command tree.
- **Worker**: Processes outgoing messages and status updates from a priority queue. Handles rate limits and image embedding.
- **Poller**: Recursively crawls Canny boards and individual posts to detect changes in votes, comments, or status.
- **Valkey (Cache)**: Serves as the central state engine and message broker between all components.

## Scaling
The bot is designed to handle thousands of servers through horizontal scaling:

- **Sharding**: Increase `TOTAL_SHARDS` in `docker-compose.yml` and add more gateway instances with unique `SHARD_ID` values to distribute server connections.
- **Worker Clusters**: Add more worker services to increase the throughput of status updates across many guilds.
- **Distributed Polling**: The poller uses Valkey locks to ensure boards are not redundantly crawled by multiple instances.

## Localization
Localization is managed via a [Google Sheet](https://docs.google.com/spreadsheets/d/17sYQbx154noc42UO1vvm3VVNLdnSguTb6j-J5mszvtQ/edit?usp=sharing).
- **Update**: Administrators can run `/update_localization` to sync the latest strings to the bot.
- **Add Languages**: New languages can be added by adding columns to the sheet and reloading.

## Commands
- `/stats`: View global and server-specific activity metrics.
- `/metrics`: View top contributors and trending posts.
- `/search`: Search Canny posts with interactive filters.
- `/ping`: Check API and bot latencies.
- `/credit`: Bot information and affiliation details.
- `/help`: Detailed command guide and usage tips.

### Administrative Commands
*These commands require the **Manage Messages** permission.*

- `/settings`: View current server configuration.
- `/mode`: Toggle between **Global** and **Local** tracking.
- `/set_status_channel`: Select where updates are posted (Supports Text, Threads, and News channels).
- `/react_channel`: Manage auto-indexing channels (Supports Text, Threads, Forum, and News channels).
- `/set_language`: Change the UI language.
- `/bulk_add`: Index Canny links from channel history.

## Context Menus (User Apps)
- `Index this canny`: Track a post from any message link.
- `Check canny status`: Instant status/vote overview.
- `Post what I indexed in hour`: Summary of your recent activity.
- `Check Trending Canny`: View weekly/monthly trending feedback.
- `Check Canny Author Metrics`: View top feedback authors.

## Affiliation & Legal
**This bot is an independent project and is not affiliated with VRChat Inc.**

All data is sourced from the public VRChat Canny feedback portal. Use of this bot is subject to the [Terms of Service](Terms/tos.md) and [Privacy Policy](Terms/privacy.md).

## Credits
Hosted by [VRCβフォース](https://discord.gg/XJHRXwd).
Open Source under the MIT License.
