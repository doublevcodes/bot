import asyncio
import contextlib
import datetime
import re
import textwrap
from functools import partial
from signal import Signals
from typing import Awaitable, Callable, Optional, Tuple

from discord import AllowedMentions, HTTPException, Message, NotFound, Reaction, User
from discord.ext.commands import Cog, Command, Context, command, guild_only

from bot.bot import Bot
from bot.constants import Categories, Channels, Roles, URLs
from bot.decorators import redirect_output
from bot.log import get_logger
from bot.utils import scheduling, send_to_paste_service
from bot.utils.messages import wait_for_deletion

log = get_logger(__name__)

ESCAPE_REGEX = re.compile("[`\u202E\u200B]{3,}")
FORMATTED_CODE_REGEX = re.compile(
    r"(?P<delim>(?P<block>```)|``?)"        # code delimiter: 1-3 backticks; (?P=block) only matches if it's a block
    r"(?(block)(?:(?P<lang>[a-z]+)\n)?)"    # if we're in a block, match optional language (only letters plus newline)
    r"(?:[ \t]*\n)*"                        # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"                        # extract all code inside the markup
    r"\s*"                                  # any more whitespace before the end of the code markup
    r"(?P=delim)",                          # match the exact same delimiter from the start again
    re.DOTALL | re.IGNORECASE               # "." also matches newlines, case insensitive
)
RAW_CODE_REGEX = re.compile(
    r"^(?:[ \t]*\n)*"                       # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"                        # extract all the rest as code
    r"\s*$",                                # any trailing whitespace until the end of the string
    re.DOTALL                               # "." also matches newlines
)

TIMEIT_EVAL_WRAPPER = """
from contextlib import redirect_stdout
from io import StringIO

with redirect_stdout(StringIO()):
    del redirect_stdout, StringIO
{code}
"""

TIMEIT_OUTPUT_REGEX = re.compile(r"\d+ loops, best of \d+: \d(?:\.\d\d?)? [mnu]?sec per loop")

MAX_PASTE_LEN = 10000

# `!eval` command whitelists and blacklists.
NO_EVAL_CHANNELS = (Channels.python_general,)
NO_EVAL_CATEGORIES = ()
EVAL_ROLES = (Roles.helpers, Roles.moderators, Roles.admins, Roles.owners, Roles.python_community, Roles.partners)

SIGKILL = 9

REEVAL_EMOJI = '\U0001f501'  # :repeat:
REEVAL_TIMEOUT = 30

FormatFunc = Callable[[str], Awaitable[tuple[str, Optional[str]]]]


class Snekbox(Cog):
    """Safe evaluation of Python code using Snekbox."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.jobs = {}

    async def post_eval(self, code: str, *, args: Optional[list[str]] = None) -> dict:
        """Send a POST request to the Snekbox API to evaluate code and return the results."""
        url = URLs.snekbox_eval_api
        data = {"input": code}

        if args is not None:
            data["args"] = args

        async with self.bot.http_session.post(url, json=data, raise_for_status=True) as resp:
            return await resp.json()

    async def upload_output(self, output: str) -> Optional[str]:
        """Upload the eval output to a paste service and return a URL to it if successful."""
        log.trace("Uploading full output to paste service...")

        if len(output) > MAX_PASTE_LEN:
            log.info("Full output is too long to upload")
            return "too long to upload"
        return await send_to_paste_service(output, extension="txt")

    @staticmethod
    def prepare_input(code: str) -> str:
        """
        Extract code from the Markdown, format it, and insert it into the code template.

        If there is any code block, ignore text outside the code block.
        Use the first code block, but prefer a fenced code block.
        If there are several fenced code blocks, concatenate only the fenced code blocks.
        """
        if match := list(FORMATTED_CODE_REGEX.finditer(code)):
            blocks = [block for block in match if block.group("block")]

            if len(blocks) > 1:
                code = '\n'.join(block.group("code") for block in blocks)
                info = "several code blocks"
            else:
                match = match[0] if len(blocks) == 0 else blocks[0]
                code, block, lang, delim = match.group("code", "block", "lang", "delim")
                if block:
                    info = (f"'{lang}' highlighted" if lang else "plain") + " code block"
                else:
                    info = f"{delim}-enclosed inline code"
        else:
            code = RAW_CODE_REGEX.fullmatch(code).group("code")
            info = "unformatted or badly formatted code"

        code = textwrap.dedent(code)
        log.trace(f"Extracted {info} for evaluation:\n{code}")
        return code

    @staticmethod
    def get_results_message(results: dict) -> Tuple[str, str]:
        """Return a user-friendly message and error corresponding to the process's return code."""
        stdout, returncode = results["stdout"], results["returncode"]
        msg = f"Your eval job has completed with return code {returncode}"
        error = ""

        if returncode is None:
            msg = "Your eval job has failed"
            error = stdout.strip()
        elif returncode == 128 + SIGKILL:
            msg = "Your eval job timed out or ran out of memory"
        elif returncode == 255:
            msg = "Your eval job has failed"
            error = "A fatal NsJail error occurred"
        else:
            # Try to append signal's name if one exists
            try:
                name = Signals(returncode - 128).name
                msg = f"{msg} ({name})"
            except ValueError:
                pass

        return msg, error

    @staticmethod
    def get_status_emoji(results: dict) -> str:
        """Return an emoji corresponding to the status code or lack of output in result."""
        if not results["stdout"].strip():  # No output
            return ":warning:"
        elif results["returncode"] == 0:  # No error
            return ":white_check_mark:"
        else:  # Exception
            return ":x:"

    async def format_output(self, output: str) -> Tuple[str, Optional[str]]:
        """
        Format the output and return a tuple of the formatted output and a URL to the full output.

        Prepend each line with a line number. Truncate if there are over 10 lines or 1000 characters
        and upload the full output to a paste service.
        """
        output = output.rstrip("\n")
        original_output = output  # To be uploaded to a pasting service if needed
        paste_link = None

        if "<@" in output:
            output = output.replace("<@", "<@\u200B")  # Zero-width space

        if "<!@" in output:
            output = output.replace("<!@", "<!@\u200B")  # Zero-width space

        if ESCAPE_REGEX.findall(output):
            paste_link = await self.upload_output(original_output)
            return "Code block escape attempt detected; will not output result", paste_link

        truncated = False
        lines = output.count("\n")

        if lines > 0:
            output = [f"{i:03d} | {line}" for i, line in enumerate(output.split('\n'), 1)]
            output = output[:11]  # Limiting to only 11 lines
            output = "\n".join(output)

        if lines > 10:
            truncated = True
            if len(output) >= 1000:
                output = f"{output[:1000]}\n... (truncated - too long, too many lines)"
            else:
                output = f"{output}\n... (truncated - too many lines)"
        elif len(output) >= 1000:
            truncated = True
            output = f"{output[:1000]}\n... (truncated - too long)"

        if truncated:
            paste_link = await self.upload_output(original_output)

        output = output or "[No output]"

        return output, paste_link

    async def send_eval(
        self,
        ctx: Context,
        code: str,
        *,
        args: Optional[list[str]] = None,
        format_func: FormatFunc
    ) -> Message:
        """
        Evaluate code, format it, and send the output to the corresponding channel.

        Return the bot response.
        """
        async with ctx.typing():
            results = await self.post_eval(code, args=args)
            msg, error = self.get_results_message(results)

            if error:
                output, paste_link = error, None
            else:
                log.trace("Formatting output...")
                output, paste_link = await format_func(results["stdout"])

            icon = self.get_status_emoji(results)
            msg = f"{ctx.author.mention} {icon} {msg}.\n\n```\n{output}\n```"
            if paste_link:
                msg = f"{msg}\nFull output: {paste_link}"

            # Collect stats of eval fails + successes
            if icon == ":x:":
                self.bot.stats.incr("snekbox.python.fail")
            else:
                self.bot.stats.incr("snekbox.python.success")

            filter_cog = self.bot.get_cog("Filtering")
            filter_triggered = False
            if filter_cog:
                filter_triggered = await filter_cog.filter_eval(msg, ctx.message)
            if filter_triggered:
                response = await ctx.send("Attempt to circumvent filter detected. Moderator team has been alerted.")
            else:
                allowed_mentions = AllowedMentions(everyone=False, roles=False, users=[ctx.author])
                response = await ctx.send(msg, allowed_mentions=allowed_mentions)
            scheduling.create_task(wait_for_deletion(response, (ctx.author.id,)), event_loop=self.bot.loop)

            log.info(f"{ctx.author}'s job had a return code of {results['returncode']}")
        return response

    async def continue_eval(self, ctx: Context, response: Message) -> Optional[str]:
        """
        Check if the eval session should continue.

        Return the new code to evaluate or None if the eval session should be terminated.
        """
        _predicate_eval_message_edit = partial(predicate_eval_message_edit, ctx)
        _predicate_emoji_reaction = partial(predicate_eval_emoji_reaction, ctx)

        with contextlib.suppress(NotFound):
            try:
                _, new_message = await self.bot.wait_for(
                    'message_edit',
                    check=_predicate_eval_message_edit,
                    timeout=REEVAL_TIMEOUT
                )
                await ctx.message.add_reaction(REEVAL_EMOJI)
                await self.bot.wait_for(
                    'reaction_add',
                    check=_predicate_emoji_reaction,
                    timeout=10
                )

                code = await self.get_code(new_message, ctx.command)
                await ctx.message.clear_reaction(REEVAL_EMOJI)
                with contextlib.suppress(HTTPException):
                    await response.delete()

            except asyncio.TimeoutError:
                await ctx.message.clear_reaction(REEVAL_EMOJI)
                return None

            return self.prepare_input(code)

    async def get_code(self, message: Message, command: Command) -> Optional[str]:
        """
        Return the code from `message` to be evaluated.

        If the message is an invocation of the eval command, return the first argument or None if it
        doesn't exist. Otherwise, return the full content of the message.
        """
        log.trace(f"Getting context for message {message.id}.")
        new_ctx = await self.bot.get_context(message)

        if new_ctx.command is command:
            log.trace(f"Message {message.id} invokes eval command.")
            split = message.content.split(maxsplit=1)
            code = split[1] if len(split) > 1 else None
        else:
            log.trace(f"Message {message.id} does not invoke eval command.")
            code = message.content

        return code

    async def run_eval(
        self,
        ctx: Context,
        code: str,
        format_func: FormatFunc,
        *,
        args: Optional[list[str]] = None,
    ) -> None:
        """
        Handles checks, stats and re-evaluation of an eval.

        `format_func` is an async callable that takes a string (the output) and formats it to show to the user.
        """
        if ctx.author.id in self.jobs:
            await ctx.send(
                f"{ctx.author.mention} You've already got a job running - "
                "please wait for it to finish!"
            )
            return

        if Roles.helpers in (role.id for role in ctx.author.roles):
            self.bot.stats.incr("snekbox_usages.roles.helpers")
        else:
            self.bot.stats.incr("snekbox_usages.roles.developers")

        if ctx.channel.category_id == Categories.help_in_use:
            self.bot.stats.incr("snekbox_usages.channels.help")
        elif ctx.channel.id == Channels.bot_commands:
            self.bot.stats.incr("snekbox_usages.channels.bot_commands")
        else:
            self.bot.stats.incr("snekbox_usages.channels.topical")

        log.info(f"Received code from {ctx.author} for evaluation:\n{code}")

        while True:
            self.jobs[ctx.author.id] = datetime.datetime.now()
            try:
                response = await self.send_eval(ctx, code, args=args, format_func=format_func)
            finally:
                del self.jobs[ctx.author.id]

            code = await self.continue_eval(ctx, response)
            if not code:
                break
            log.info(f"Re-evaluating code from message {ctx.message.id}:\n{code}")

    async def format_timeit_output(self, output: str) -> tuple[str, str]:
        """
        Parses the time from the end of the output given by timeit.

        If an error happened, then it won't contain the time and instead proceed with regular formatting.
        """
        split_output = output.rstrip("\n").rsplit("\n", 1)
        if len(split_output) == 2 and TIMEIT_OUTPUT_REGEX.fullmatch(split_output[1]):
            return split_output[1], None

        return await self.format_output(output)

    @command(name="eval", aliases=("e",))
    @guild_only()
    @redirect_output(
        destination_channel=Channels.bot_commands,
        bypass_roles=EVAL_ROLES,
        categories=NO_EVAL_CATEGORIES,
        channels=NO_EVAL_CHANNELS,
        ping_user=False
    )
    async def eval_command(self, ctx: Context, *, code: str) -> None:
        """
        Run Python code and get the results.

        This command supports multiple lines of code, including code wrapped inside a formatted code
        block. Code can be re-evaluated by editing the original message within 10 seconds and
        clicking the reaction that subsequently appears.

        We've done our best to make this sandboxed, but do let us know if you manage to find an
        issue with it!
        """
        code = self.prepare_input(code)
        await self.run_eval(ctx, code, format_func=self.format_output)

    @command(name="timeit", aliases=("ti",))
    @guild_only()
    @redirect_output(
        destination_channel=Channels.bot_commands,
        bypass_roles=EVAL_ROLES,
        categories=NO_EVAL_CATEGORIES,
        channels=NO_EVAL_CHANNELS,
        ping_user=False
    )
    async def timeit_command(self, ctx: Context, *, code: str) -> str:
        """
        Profile Python Code to find execution time.

        This command supports multiple lines of code, including code wrapped inside a formatted code
        block. Code can be re-evaluated by editing the original message within 10 seconds and
        clicking the reaction that subsequently appears.

        We've done our best to make this sandboxed, but do let us know if you manage to find an
        issue with it!
        """
        code = self.prepare_input(code)
        await self.run_eval(
            ctx, TIMEIT_EVAL_WRAPPER.format(code=textwrap.indent(code, "    ")),
            format_func=self.format_timeit_output, args=["-m", "timeit"]
        )


def predicate_eval_message_edit(ctx: Context, old_msg: Message, new_msg: Message) -> bool:
    """Return True if the edited message is the context message and the content was indeed modified."""
    return new_msg.id == ctx.message.id and old_msg.content != new_msg.content


def predicate_eval_emoji_reaction(ctx: Context, reaction: Reaction, user: User) -> bool:
    """Return True if the reaction REEVAL_EMOJI was added by the context message author on this message."""
    return reaction.message.id == ctx.message.id and user.id == ctx.author.id and str(reaction) == REEVAL_EMOJI


def setup(bot: Bot) -> None:
    """Load the Snekbox cog."""
    bot.add_cog(Snekbox(bot))
