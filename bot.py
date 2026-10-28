import asyncio
import os
import discord
from discord.ext import tasks
from main import load_config, init_db, collect, already_notified, record_offer, is_future_or_unknown

from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

STORE_COLOURS = {
    "Epic Games Store": discord.Color.from_rgb(0, 120, 242),   # Epic Blue
    "Steam": discord.Color.from_rgb(23, 26, 33),               # Steam Dark
    "GOG": discord.Color.from_rgb(134, 0, 179),                # GOG Purple
    "Humble Bundle": discord.Color.from_rgb(204, 0, 0),         # Humble Red
}

STORE_LOGOS = {
    "Epic Games Store": "https://upload.wikimedia.org/wikipedia/commons/3/31/Epic_Games_logo.svg",
    "Steam": "https://upload.wikimedia.org/wikipedia/commons/8/83/Steam_icon_logo.svg",
    "GOG": "https://upload.wikimedia.org/wikipedia/commons/2/2e/GOG.com_logo.svg",
}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

config = load_config()

@tasks.loop(hours=1)
async def check_free_games():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        print(f"Could not find channel with ID {CHANNEL_ID}")
        return

    print("Running scheduled free game check for Discord...")
    try:
        offers, statuses = await asyncio.to_thread(collect, config)
        print(f"Collection finished. Statuses: {statuses}")
        print(f"Total offers found: {len(offers)}")
        
        for offer in offers:
            print(f"Checking offer: [{offer.store}] {offer.title} (Source: {offer.source})")
            
            if not is_future_or_unknown(offer.end_date):
                print(f" -> Skipped: End date has passed ({offer.end_date})")
                continue
                
            if already_notified(offer):
                print(f" -> Skipped: Already notified in database.")
                continue
                
            print(f" -> NEW OFFER! Sending to Discord: {offer.title}")
            
            colour = STORE_COLOURS.get(offer.store, discord.Color.green())

            embed = discord.Embed(
                title=f"🎮 {offer.title}",
                url=offer.url,
                color=colour,
                description="*Free to keep! Claim it before the offer expires.*"
            )
            embed.add_field(name="Store", value=offer.store, inline=True)
            if offer.store in STORE_LOGOS:
                embed.set_thumbnail(url=STORE_LOGOS[offer.store])

            # Use Discord's native Unix timestamp formatting (<t:TIMESTAMP:R>) for relative time ("Ends in 2 days")
            if offer.end_date:
                embed.add_field(name="⏳ Offer Ends", value=offer.end_date, inline=True)

            embed.set_footer(
                text=f"Source: {offer.source} • Automated Freebie Alert",
                icon_url=client.user.display_avatar.url
            )

            await channel.send(embed=embed)
            record_offer(offer)
            await asyncio.sleep(1)
            
    except Exception as exc:
        print(f"Error during collection loop: {exc}")

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    
    # Initialize the database table just in case
    init_db()
    
    # Display status activity
    activity = discord.Activity(
        type=discord.ActivityType.watching, 
        name="for limited time free games 🎮"
    )
    await client.change_presence(status=discord.Status.online, activity=activity)
    
    # START THE BACKGROUND LOOP HERE!
    if not check_free_games.is_running():
        check_free_games.start()
        print("Free game check background task started!")

client.run(TOKEN)