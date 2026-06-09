# Installation & Upgrade

## Prerequisites
- VPS with Docker & Docker Compose.
- Valkey Cluster running on `booth_evaluation_ops_default` network.

## Initial Setup
1. Clone the repository.
2. Create a `.env` file in the root directory.
   - The `.env` file should contain your Discord Bot Token as follows:
     ```env
     DISCORD_TOKEN=your_actual_discord_bot_token_here
     ```
3. Run `docker-compose up -d --build`.

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

## Upgrading
1. Pull latest code.
2. Run `docker-compose up -d --build` to recreate containers.
Valkey cache will persist as it is external.

## Feature Expansion Methods
- **New Commands**: Add command decorators to `Bot/gateway/main.py` following the existing `app_commands` pattern.
- **Custom Embeds**: Modify `Bot/worker/embeds.py` to change the layout or visual style of Canny status updates.
- **Language Support**: Add new columns to `Locale/template.csv` and use `/update_localization` to sync changes from a Google Sheet.
- **Valkey Logic**: Add new coordination keys in `Bot/shared/valkey.py` for cross-service state.
