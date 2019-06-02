from collections import OrderedDict
from discord.ext import commands
from typing import Dict, Optional, Union
import asyncio
import discord
import re
import threading

from constants import colors, emoji
from database import get_db
from utils import l, mutget
from .gameflags import GameFlags
from .playerdict import PlayerDict
from .proposal import Proposal
from .quantity import Quantity
from .rule import Rule
import utils


class Game:
    """A Nomic game, including proposals, rules, etc.

    Do not instantiate this class directly; use game.get_game() instead.
    """

    def __init__(self, guild: discord.Guild, do_not_instantiate_directly=None):
        """Do not instantiate this class directly; use game.get_game() instead.
        """
        if do_not_instantiate_directly != 'ok':
            # I'm not sure whether TypeError is really the best choice here.
            raise TypeError("Do not instantiate DB object directly; use get_db() instead")
        self._lock = asyncio.Lock()
        self.guild = guild
        self.db = get_db('guild_' + str(guild.id))
        self.flags = GameFlags(**self.db.get('flags', {}))
        self.proposals = [Proposal(game=self, **p) for p in mutget(self.db, 'proposals', [])]
        self.quantities = {k: Quantity(game=self, **q) for k, q in mutget(self.db, 'quantities', {}).items()}
        self.rules = {}
        self._load_rule(mutget(self.db, 'rules', {
            'root': {'tag': 'root', 'title': None, 'content': None},
        }), 'root')
        self.player_activity = PlayerDict(self, mutget(self.db, 'player_activity', {}))
        channels = mutget(self.db, 'channels', {})
        self.proposals_channel  = guild.get_channel(channels.get('proposals'))
        self.rules_channel      = guild.get_channel(channels.get('rules'))

    def _load_rule(self, rules_dict: Dict[str, Dict], tag: str) -> None:
        if tag not in rules_dict:
            l.warning(f"No such rule found: {tag!r}")
            return
        if tag in self.rules:
            l.warning(f"Rule recursion or repetition found: {tag!r} is a child of multiple rules")
            return
        rule = Rule(game=self, **rules_dict[tag])
        if rule.tag != 'root':
            if rule.parent is None:
                l.warning(f"Rule section inconsistency found; {tag!r} is not root but has no parent")
            if rule not in rule.parent.children:
                l.warning(f"Rule section inconsistency found; {tag!r} is not a child of its parent, {rule.parent.tag!r}")
                return
        self.rules[tag] = rule
        for child in rule.children:
            self._load_rule(rules_dict, child)

    def __enter__(self):
        raise RuntimeError("Use 'async with', not plain 'with'")

    def __exit__(self):
        raise RuntimeError("Use 'async with', not plain 'with'")

    async def __aenter__(self):
        await self._lock.acquire()
        self._owned_thread_id = threading.get_ident()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self._owned_thread_id = None
        self._lock.release()

    def assert_locked(self):
        if not self._owned_thread_id == threading.get_ident():
            raise RuntimeError("Expected {self} to be locked by current thread, but it isn't")

    async def save(self) -> None:
        self.assert_locked()
        self.db.clear()
        self.db.update(self.export())
        self.db.save()

    def export(self) -> dict:
        return OrderedDict(
            channels=OrderedDict(
                proposals=self.proposals_channel and self.proposals_channel.id,
                rules=self.rules_channel and self.rules_channel.id,
            ),
            flags=self.flags.export(),
            player_activity=self.player_activity.export(),
            quantities=utils.sort_dict(
                {k: q.export() for k, q in self.quantities.items()}
            ),
            proposals=[p.export() for p in self.proposals],
            rules=utils.sort_dict(
                {k: r.export() for k, r in self.rules.items()}
            ),
        )

    def get_member(self, user_id: Union[int, discord.abc.User]) -> discord.Member:
        if isinstance(user_id, discord.Member):
            return user_id
        elif isinstance(user_id, discord.abc.User):
            return self.guild.get_member(user_id.id)
        else:
            return self.guild.get_member(user_id)

    def record_activity(self, user: discord.abc.User) -> None:
        """Mark a player as being active right now."""
        self.assert_locked()
        self.player_activity[user] = utils.now()

    def get_activity_diff(self, user: discord.abc.User) -> Optional[int]:
        """Get the number of seconds since a player was last active, or None if
        they do not exist.
        """
        if user in self.player_activity:
            return utils.now() - self.player_activity.get(user)

    @property
    def activity_diffs(self) -> PlayerDict:
        """Get a PlayerDict of values returned by Game.get_activity_diff()."""
        return PlayerDict(self, {
            user: self.get_activity_diff(user) for user in self.player_activity
        })

    def is_active(self, user: Union[int, discord.abc.User]) -> bool:
        diff = self.get_activity_diff(user)
        seconds_cutoff = self.flags.player_activity_cutoff * 3600
        return diff is not None and diff <= seconds_cutoff

    def is_inactive(self, user: Union[int, discord.abc.User]) -> bool:
        return not self.is_active(user)

    def _check_proposal(self, *ns) -> None:
        for n in ns:
            if not isinstance(n, int):
                raise TypeError(f"Invalid proposal ID: {n!r}")
            if not 1 <= n <= len(self.proposals):
                raise ValueError(f"No such proposal with ID: {n!r}")

    def has_proposal(self, n: int) -> bool:
        return isinstance(n, int) and 1 <= n <= len(self.proposals)

    def get_proposal(self, n: int) -> Proposal:
        if self.has_proposal(n):
            return self.proposals[n - 1]

    async def get_proposal_messages(self) -> set:
        messages = set()
        for proposal in self.proposals:
            messages.add(await proposal.fetch_message())
        return messages

    def get_quantity(self, name: str) -> Quantity:
        name = name.lower()
        if name in self.quantities:
            return self.quantities[name]
        for quantity in self.quantities.values():
            if name in quantity.aliases:
                return quantity

    def _check_rule(self, *tags) -> None:
        for tag in tags:
            if not isinstance(tag, str):
                raise TypeError(f"Invalid rule tag: {tag!r}")
            if not re.match(r'^[a-z\-]+$', tag):
                raise ValueError(f"Invalid rule tag: {tag!r}")
            if tag not in self.rules:
                raise KeyError(f"No such rule with tag: {tag!r}")

    def has_rule(self, tag: str) -> bool:
        try:
            self._check_rule(tag)
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def get_rule(self, tag: str) -> Rule:
        if self.has_rule(tag):
            return self.rules[tag]

    async def get_rule_messages(self) -> set:
        messages = set()
        for rule in self.rules.values():
            messages.add(await rule.fetch_message())
        return messages

    @property
    def root_rule(self) -> Rule:
        return self.get_rule('root')

    async def refresh_proposal(self, *ns: int) -> None:
        """Update the messages for one or more proposals.

        May throw `TypeError`, `ValueError`, or `discord.Forbidden` exceptions.
        """
        self.assert_locked()
        for n in ns:
            self._check_proposal(n)
        for n in sorted(set(ns)):
            proposal = self.get_proposal(n)
            try:
                m = await proposal.fetch_message()
                await m.clear_reactions()
                await m.edit(embed=proposal.embed)
                await m.add_reaction(emoji.VOTE_FOR)
                await m.add_reaction(emoji.VOTE_AGAINST)
                await m.add_reaction(emoji.VOTE_ABSTAIN)
            except discord.NotFound:
                await self.repost_proposal(n)
                return

    async def repost_proposal(self, *ns: int) -> None:
        """Remove and repost the messages for one or more proposals.

        May throw `TypeError`, `ValueError`, or `discord.Forbidden` exceptions.
        """
        self.assert_locked()
        for n in ns:
            self._check_proposal(n)
        proposal_range = range(min(ns), len(self.proposals) + 1)
        proposals = list(map(self.get_proposal, proposal_range))
        proposal_messages = []
        for proposal in proposals:
            m = await proposal.fetch_message()
            if m:
                proposal_messages.append(m)
        if proposal_messages:
            await self.proposals_channel.delete_messages(*proposal_messages)
        for n, proposal in zip(proposal_range, proposals):
            m = await self.proposals_channel.send(embed=discord.Embed(
                color=colors.TEMPORARY,
                title=f"Preparing proposal #{n}\N{HORIZONTAL ELLIPSIS}",
            ))
            proposal.message_id = m.id
        await self.save()
        await self.refresh_proposal(*proposal_range)

    async def refresh_rule(self, *tags: str) -> None:
        """Update the messages for one or more rules.

        May throw `KeyError`, `TypeError`, `ValueError`, `discord.NotFound`,
        or `discord.Forbidden` exceptions.
        """
        self.assert_locked()
        for tag in tags:
            self._check_rule(tag)
        for tag in sorted(set(tags)):
            rule = self.get_rule(tag)
            m = await rule.fetch_message()
            await m.edit(embed=rule.embed)

    async def repost_rules(self) -> None:
        """Delete and repost the messages for all rules."""
        self.assert_locked()
        await self.rules_channel.delete_messages(*(await self.get_rule_messages()))
        await self._repost_child_rules(self.root_rule)

    async def _repost_child_rules(self, parent: Rule) -> None:
        self.assert_locked()
        for rule in parent.children:
            self.rules_channel.send(embed=rule.embed)
            self._repost_child_rules(rule)


_GAMES = {}


def get_game(arg: Union[discord.Guild, commands.Context]):
    if isinstance(arg, discord.Guild):
        guild = arg
    elif isinstance(arg, commands.Context):
        ctx = arg
        guild = ctx.guild
    else:
        raise TypeError(f"Cannot get game from type {type(arg)!r}: {arg!r}")
    if guild.id not in _GAMES:
        _GAMES[guild.id] = Game(guild, 'ok')
    return _GAMES[guild.id]
