# Installation & Architecture

## Prerequisites
- VPS or server with **Docker** and **Docker Compose** installed.
- A Discord Bot Token (from the [Discord Developer Portal](https://discord.com/developers/applications)).
- **Permissions**: The bot requires `Manage Messages` for administrative tasks and `Embed Links` for status updates.
- Valkey Cluster (provided by default in `docker-compose.yml`).

## Initial Setup
1. **Clone the Repository**:
   ```bash
   git clone https://github.com/slord399/feedback_tracker.git
   cd feedback_tracker
   ```
2. **Configure Environment**: Create a `.env` file in the root directory:
   ```env
   DISCORD_TOKEN=your_actual_discord_bot_token_here
   ```
3. **Launch**:
   ```bash
   docker-compose up -d --build
   ```

## Architecture
The system is built with a distributed, microservices-inspired architecture for high availability and scalability:

- **Gateway**: Handles WebSocket connections to Discord, processes slash commands, and manages the command tree.
- **Worker**: Processes outgoing messages and status updates from a priority queue. Handles rate limits and image embedding.
- **Poller**: Recursively crawls Canny boards and individual posts to detect changes in votes, comments, or status.
- **Valkey (Cache)**: Serves as the central state engine and message broker between all components.

## Scaling Capacity
To increase the bot's capacity for handling more servers and high traffic, you can horizontally scale the `gateway` and `worker` services.

### Scaling Gateway (Sharding)
The gateway handles WebSocket connections to Discord. To scale it, you must increase the `TOTAL_SHARDS` count across all instances and assign a unique `SHARD_ID` to each.
1. In `docker-compose.yml`, add new `gateway-X` services.
2. Update `TOTAL_SHARDS` to the total number of gateway services you have.
3. Set `SHARD_ID` to a unique number from `0` to `TOTAL_SHARDS - 1`.

### Scaling Workers
Workers consume jobs from Valkey and post to the Discord REST API.
1. In `docker-compose.yml`, you can simply add more `worker-X` service definitions.
2. Workers are stateless and will automatically load-balance jobs via Valkey's `BRPOP`.

### Distributed Polling
The poller uses Valkey locks to ensure boards are not redundantly crawled by multiple instances.

## Localization
Localization is managed via a [Google Sheet](https://docs.google.com/spreadsheets/d/17sYQbx154noc42UO1vvm3VVNLdnSguTb6j-J5mszvtQ/edit?usp=sharing).
- **Syncing**: Use the `/update_localization` command in the admin guild to sync the latest strings.
- **Languages**: The bot supports 12+ languages including English, Japanese, German, etc. Adding a new language requires adding a column to the sheet.

## Upgrading
1. Pull latest code.
2. Run `docker-compose up -d --build` to recreate containers.
Valkey cache is persistent as long as the volumes are not deleted.

## Feature Expansion Methods
- **New Commands**: Add command decorators to `Bot/gateway/main.py` following the existing `app_commands` pattern.
- **Custom Embeds**: Modify `Bot/worker/embeds.py` to change the layout or visual style of Canny status updates.
- **Valkey Logic**: Add new coordination keys in `Bot/shared/valkey.py` for cross-service state.
