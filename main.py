import asyncio
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import nextcord
from dotenv import load_dotenv
from nextcord import SelectOption, ui
from nextcord.ext import commands
from nextcord.ui import Select, View
from openai import OpenAI

# ------------------------ Init ------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[logging.FileHandler("log.txt"), logging.StreamHandler(sys.stdout)],
)

load_dotenv()

DAILY_USES = 3
GPT_MODEL = "gpt-4"
# GPT_MODEL = "gpt-3.5-turbo-0125"
TOKEN = os.environ["DISCORD_TOKEN"]

OpenAI.api_key = os.environ["OPENAI_API_KEY"]

initial_prompt = ""

with open("bot_prompt.txt", "r") as f:
    initial_prompt = f.read()

ai_client = OpenAI()

intents = nextcord.Intents.all()
intents.messages = True
intents.members = True
client = commands.Bot(intents=intents, help_command=None)

# ------------------------ Helper Functions ------------------------


def getTimeUntilRefresh():
    # Get the current time
    now = datetime.now()

    # Calculate the time until midnight
    midnight = datetime(now.year, now.month, now.day, 23, 59, 59)
    time_remaining = midnight - now
    # Convert time remaining to hours and minutes
    hours, remainder = divmod(time_remaining.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return (hours, minutes)


def split_text_nicely(text, max_length=2000):
    # Check if the text is shorter or equal to the max_length
    if len(text) <= max_length:
        return text, ""

    # Try to split by paragraph
    paragraphs = text.split("\n")
    temp_text = ""
    for i, paragraph in enumerate(paragraphs):
        if len(temp_text) + len(paragraph) + 2 > max_length:  # +2 for the '\n\n'
            return "\n".join(paragraphs[:i]), "\n".join(paragraphs[i:])
        temp_text += paragraph + "\n"

    # If not split by paragraph, try to split by sentence
    sentences = text.split(". ")
    temp_text = ""
    for i, sentence in enumerate(sentences):
        # Adding 2 for the '. ' that was removed by split
        if len(temp_text) + len(sentence) + 2 > max_length:
            first_half = ". ".join(sentences[:i]) + "."
            second_half = ". ".join(sentences[i:])
            # Check if the second half starts with a space (due to the split), and if so, remove it
            if second_half.startswith(" "):
                second_half = second_half[1:]
            return first_half, second_half
        temp_text += sentence + ". "

    # If it's not possible to nicely split, do a hard split at max_length
    return text[:max_length], text[max_length:]


async def timeout(member: nextcord.Member, seconds: int, reason="No reason provided"):
    duration = timedelta(seconds=seconds)
    try:
        await member.timeout(duration, reason=reason)
    except Exception as e:
        logging.error(f"Failed to timeout {member}: {e}")

async def daily_reset():
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()
    cur.execute("UPDATE Users SET NumUses = ?", (0,))
    conn.commit()


async def schedule_reset():
    tz = timezone(timedelta(hours=11))  # AEDT timezone
    while True:
        now = datetime.now(tz)
        target_time = datetime(
            now.year, now.month, now.day, 0, 0, tzinfo=tz
        )  # 12 AM AEDT\
        if now >= target_time:
            tomorrow = target_time + timedelta(days=1)
            seconds_until_tomorrow = (tomorrow - now).total_seconds()
            await asyncio.sleep(seconds_until_tomorrow)
        else:
            seconds_until_target = (target_time - now).total_seconds()
            await asyncio.sleep(seconds_until_target)
        await daily_reset()

# Constants for rate limiting
MAX_COMMANDS = 5  # Max number of commands a user can issue within the time period
TIME_PERIOD = timedelta(seconds=20)  # Time period for rate limit in seconds

# Dictionary to track command usage: {user_id: [timestamps]}
command_usage_tracker = {}

# Function to check if a user is spamming commands
async def check_command_spam(interaction: nextcord.Interaction) -> bool:
    user_id = interaction.user.id
    current_time = datetime.now()

    if user_id not in command_usage_tracker:
        command_usage_tracker[user_id] = [current_time]
        return False

    # Filter out commands outside of the time period
    command_usage_tracker[user_id] = [timestamp for timestamp in command_usage_tracker[user_id] if current_time - timestamp <= TIME_PERIOD]

    # Check if current command exceeds rate limit
    if len(command_usage_tracker[user_id]) >= MAX_COMMANDS:
        # Timeout for 60 seconds
        await timeout(interaction.user, 60, "Spamming commands")
        logging.info(f"Timed out {interaction.user} for spamming commands.")
        return True

    # Record current command usage
    command_usage_tracker[user_id].append(current_time)
    return False

# --------------------------UI Buttons----------------------------
class ButtonView(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(
            ui.Button(
                label="test",
                style=nextcord.ButtonStyle.link,
                url="https://github.com/AISoc-UNSW",
            )
        )


class PatreonButtonView(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(
            ui.Button(
                label="💳 Pay up buddy",
                style=nextcord.ButtonStyle.link,
                url="https://www.patreon.com/",
            )
        )


class BeemButtonView(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(
            ui.Button(
                label="💳 Beem me at raymondcen",
                style=nextcord.ButtonStyle.link,
                url="https://www.beemit.com.au/split-expenses",
            )
        )


# ------------------------ GPT Functions ------------------------


def askGPT(text):
    try:
        response = ai_client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": initial_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.7,
            max_tokens=2000,
            top_p=1.0,
            frequency_penalty=0.1,
            presence_penalty=0.1,
        )
        return response.usage, response.choices[0].message.content
    except Exception as e:
        logging.error(f"Failed to prompt openai API: {e}. Prompt: {text}")
        return e


async def _say(interaction, response="", embed=None, view=None):
    async def _do_say(response="", embed=None, view=None):
        try:
            if view:
                await interaction.followup.send(response, embed=embed, view=view)
            else:
                await interaction.followup.send(response, embed=embed)
        except Exception as e:
            await interaction.followup.send(
                f"Sadly, your prompt could not be processed.\n```{e}```"
            )
            raise

    if len(response) > 1999:
        r_one, r_two = split_text_nicely(response)
        # await _do_say("Response over 2000 characters detected.", embed=None)
        await _do_say(r_one)
        await _say(interaction, r_two, embed=embed, view=view)
    else:
        await _do_say(response, embed=embed, view=view)


# ------------------------ SQL Functions ------------------------


def getUser(cur, id):
    cur.execute("SELECT * FROM Users WHERE ID = ?", (id,))
    return cur.fetchone()


def insertUser(conn, cur, id, name):
    try:
        cur.execute(
            "INSERT INTO Users (ID, Name, MemberStatus, NumUses) VALUES (?, ?, 0, 0)",
            (id, name),
        )
        conn.commit()
        logging.info(f"User with ID: {id}, Name: {name} was added to the database.")
    except sqlite3.IntegrityError:
        logging.error(
            f"Error: Failed to insert the row for user with ID: {id}, Name: {name}. 'ID' might not be unique."
        )
    except Exception as e:
        logging.error(
            f"An error occurred during member creation user with ID: {id}, Name: {name}: {e}"
        )

    return getUser(cur, id)


def insertMsg(
    conn, cur, interaction, prompt, response, num_context_tokens, num_response_tokens
):
    id = interaction.id
    user_id = interaction.user.id
    # TODO: the msg column better
    msg = json.dumps(repr(interaction))
    timestamp = interaction.created_at.strftime("%d/%m/%Y, %H:%M:%S")
    try:
        cur.execute(
            "INSERT INTO messages (ID, user_id, msg, prompt, response, num_context_tokens, num_response_tokens, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                user_id,
                msg,
                prompt,
                response,
                num_context_tokens,
                num_response_tokens,
                timestamp,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        logging.error(
            f"Error: Failed to insert the row for message with ID: {interaction.message.id}, Name: {interaction.user.name}. 'ID' might not be unique."
        )
    except Exception as e:
        logging.error(
            f"An error occurred during message storage message with ID: {interaction.message.id}, Name: {interaction.user.name}: {e}"
        )


# ------------------------ Bot logic ------------------------


@client.event
async def on_ready():
    logging.info(f"{client.user} has connected to Discord!")
    client.loop.create_task(schedule_reset())


class DropdownView(View):
    def __init__(self):
        super().__init__()
        self.add_item(CommandSelect())


class CommandSelect(Select):
    def __init__(self):
        options = [
            SelectOption(label="Hello", value="/hello"),
            SelectOption(label="Fact", value="/fact"),
            SelectOption(label="Prompts Left", value="/promptsleft"),
            SelectOption(label="Prompt", value="/prompt <your prompt>"),
        ]
        super().__init__(
            placeholder="Choose a command...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: nextcord.Interaction):
        await interaction.response.send_message(self.values[0])


@client.slash_command(name="help")
async def help(interaction):
    """Displays all commands."""
    await interaction.response.defer()
    if await check_command_spam(interaction):
        return
    embed = nextcord.Embed(
        title="🤖 Bot Commands Guide 🤖", color=nextcord.Color.blue()
    )
    embed.add_field(
        name="General Commands:",
        value="""\n
                            - </hello:1218037818756956181> - Greets the user.\n
                            - </fact:1218037820627882096> - Gives a random cool fact!\n
                            - </promptsleft:1218037822636953640> - Shows how many prompts you have left for the day.\n
                            - </prompt:1218037824637374494> *[prompt]* - Sends your prompt to the AI for a response. Usage is limited to 3 per day.\n\n
                            *Remember, prompt usage is limited each day. Use your prompts wisely!*\n\n
                            *If you need more help or have suggestions, feel free to reach out to the admins.*
                        """,
        inline=False,
    )

    await _say(interaction, embed=embed)
    logging.info(f"Help command executed by {interaction.user}")


# Add your other bot event and command handlers here as previously defined
# Define a simple command


@client.slash_command(name="hello")
async def hello(interaction):
    """Greets the user."""
    await interaction.response.defer()
    if await check_command_spam(interaction):
        return
    await _say(interaction, "Hello!")
    logging.info(f"Executed hello command by {interaction.user}: Hello!")


@client.slash_command(name="fact")
async def fact(interaction):
    """Gives a random cool fact!"""
    await interaction.response.defer()
    if await check_command_spam(interaction):
        return

    view = BeemButtonView()
    await _say(interaction, "Fact APIs are too expensive!", view=view)
    logging.info(f"Executed fact command by {interaction.user}")


@client.slash_command(name="promptsleft")
async def prompts_left(interaction):
    """Shows how many prompts you have left for the day."""
    await interaction.response.defer()
    if await check_command_spam(interaction):
        return
    try:
        user_id = interaction.user.id
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()

        user = getUser(cur, user_id)
        if user is None:
            # User does not exist, create a new entry
            user = insertUser(conn, cur, user_id, interaction.user.name)

        user_count = user[3]
        p_left = DAILY_USES - user_count

        if p_left < 1:
            (hours, minutes) = getTimeUntilRefresh()
            await _say(
                interaction,
                f"You have prompted {user_count} time(s) today. You do not have any prompts left. Prompts refresh in {hours} hrs and {minutes} mins",
            )
        else:
            await _say(
                interaction,
                f"You have prompted {user_count} time(s) today. You have {DAILY_USES - user_count} prompt(s) left",
            )

        logging.info(
            f"Executed promptsleft command by {interaction.user}: {p_left} prompt(s) left"
        )
    except Exception as e:
        logging.error(
            f"Error handling database for prompt command by {interaction.user}: {e}"
        )
        await _say(interaction, "Something went wrong, try again later.")
    conn.close()


@client.slash_command(name="prompt")
async def gpt(interaction, prompt):
    """Sends the user's prompt to the AI for a response. Usage is limited to 3 per day."""
    # TODO: Implement a cooldown?
    await interaction.response.defer()
    if await check_command_spam(interaction):
        return
    try:
        author = interaction.user
        user_id = author.id
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        # Check if the user exists
        user = getUser(cur, user_id)
        if user is None:
            # User does not exist, create a new entry
            user = insertUser(conn, cur, user_id, author.name)

        user_member = user[2]
        user_count = user[3]
        if (user_member < 0) or user_count < DAILY_USES:
            cur.execute(
                "UPDATE Users SET NumUses = ? WHERE ID = ?", (user_count + 1, user_id)
            )
            conn.commit()
            usage, response = askGPT(prompt)
            try:
                embed = nextcord.Embed(description="**Your prompt:**")
                embed.set_footer(text=prompt)
                await _say(interaction, response, embed=embed)
                logging.info(
                    f"Executed prompt command by {author}: Prompt: {prompt}, Response: {response}"
                )
                # Store Everything
                insertMsg(
                    conn,
                    cur,
                    interaction,
                    prompt,
                    response,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                )
            except Exception as e:
                logging.error(
                    f"Failed to send response with discord API: {e}. Prompt: {prompt}"
                )
        else:
            (hours, minutes) = getTimeUntilRefresh()
            logging.info(
                f"'Not enough prompts by {author}: Prompt: {prompt}. Command on cooldown for {author}: {prompt}"
            )
            view = PatreonButtonView()
            await interaction.followup.send(
                f"Prompts refresh in {hours} hours and {minutes} mins", view=view
            )
    except Exception as e:
        await _say(interaction, "Something went wrong, try again later.")
        logging.error(f"Error handling database for prompt command by {author}: {e}")


# Track user messages
user_messages = defaultdict(list)


@client.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        return
    # Process commands first
    await client.process_commands(message)

    # Basic spam detection (for demonstration purposes)
    user_id = message.author.id
    user_messages[user_id].append(message.created_at)

    # Check the last 5 messages for this user
    if len(user_messages[user_id]) > 5:
        # Keep only the last 5 timestamps
        user_messages[user_id] = user_messages[user_id][-5:]

    if len(user_messages[user_id]) == 5:
        # If 5 messages in less than 10 seconds, consider it spamming
        if (
            user_messages[user_id][-1] - user_messages[user_id][0]
        ).total_seconds() < 10:
            try:
                # Timeout for 60 seconds
                await timeout(message.author, 60, "Spamming in chat")
                await message.channel.send(
                    f"{message.author.mention} has been timed out for 60 seconds. Reason: Spamming in chat"
                )
                logging.info(
                    f"{message.author} has been timed out for 60 seconds. Reason: Spamming in chat"
                )
                # Clear the messages for this user to reset detection
                del user_messages[user_id]
            except Exception as e:
                logging.error(f"Failed to time out {message.author}: {e}")


@client.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await _say(
            ctx,
            f"This command is on cooldown. Please try again in {error.retry_after+0.01:.2f} seconds.",
        )
        logging.info(f"Command on cooldown for {ctx.author}: {ctx.message.content}")
    elif isinstance(error, commands.CommandNotFound):
        await _say(
            ctx,
            "Sorry, I don't recognize that command. Try !help to see all available commands.",
        )
        logging.warning(f"Command not found for {ctx.author}: {ctx.message.content}")
    else:
        # If the error is not a CommandOnCooldown, it raises the error normally.
        logging.error(f"Unhandled command error for {ctx.author}: {error}")
        raise error


def main():
    client.run(TOKEN)


if __name__ == "__main__":
    main()
