from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Union
import discord
import functools
import re

from .playerdict import PlayerDict
from .repoman import GameRepoManager
import utils


@dataclass
class _Quantity:
    game: object  # We can't access nomic.game.Game from here.
    name: str
    aliases: List[str] = field(default_factory=list)
    players: PlayerDict = None
    default_value: Union[int, float] = 0


@functools.total_ordering
class Quantity(_Quantity):
    """A dataclass representing a game quantity, such as points.

    Attributes:
    - game
    - name -- string

    Optional attributes:
    - aliases (default []) -- list of strings
    - players (default {}) -- PlayerDict
    - default_value (default 0) -- int or float
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.players = PlayerDict(self.game, self.players)

    def export(self) -> dict:
        return OrderedDict(
            name=self.name,
            aliases=sorted(self.aliases),
            players=self.players.export(),
            default_value=self.default_value,
        )

    def rename(self, new_name: str):
        self.game.rename_quantity(self, new_name)

    def set_aliases(self, new_aliases: List[str]):
        self.game.set_quantity_aliases(self, new_aliases)

    def set_default(self, new_default: float):
        self.game.set_quantity_default(self, new_default)

    def set(self, player: discord.Member, value: Union[int, float]):
        if int(value) == value:
            value = int(value)
        if value == self.default_value:
            if player in self.players:
                del self.players[player]
        else:
            self.players[player] = value

    def get(self, player: discord.Member):
        return self.players.get(player, self.default_value)

    def __str__(self):
        return f"quantity **{self.name}**"

    def __lt__(self, other):
        return self.name < other.name

    def __eq__(self, other):
        return self.name == other.name

    def __hash__(self):
        # This isn't ideal, but it should have all the necessary properties of a
        # __hash__().
        return id(self)


class QuantityManager(GameRepoManager):

    def load(self):
        db = self.get_db('quantities')
        self.quantities = {}
        if db:
            for name, quantity in db.items():
                self.quantities[name] = Quantity(game=self, **quantity)

    def save(self):
        db = self.get_db('quantities')
        db.replace(utils.sort_dict({k: q.export() for k, q in self.quantities.items()}))
        db.save()

    def add_quantity(self, quantity_name: str, aliases: List[str]):
        """Create a new game quantity.

        May throw `ValueError` if name or aliases are invalid.
        """
        self.assert_locked()
        quantity_name = quantity_name.lower()
        aliases = [s.lower() for s in aliases]
        for name in [quantity_name] + aliases:
            self._check_quantity_name(name)
        self.quantities[quantity_name] = quantity = Quantity(
            game=self,
            name=quantity_name,
            aliases=aliases,
        )
        self.save()
        return quantity

    def rename_quantity(self, quantity: Quantity, new_name: str):
        self.assert_locked()
        new_name = new_name.lower()
        self._check_quantity_name(new_name, ignore=quantity)
        if new_name in quantity.aliases:
            quantity.aliases.remove(new_name)
        del self.quantities[quantity.name]
        quantity.name = new_name
        self.quantities[quantity.name] = quantity
        self.save()

    def remove_quantity(self, quantity: Quantity):
        self.assert_locked()
        del self.quantities[quantity.name]
        self.save()

    def set_quantity_aliases(self, quantity: Quantity, new_aliases: List[str]):
        self.assert_locked()
        for name in new_aliases:
            self._check_quantity_name(name, ignore=quantity)
        quantity.aliases = sorted(new_aliases)
        self.save()

    def set_quantity_default(self, quantity: Quantity, new_default: float):
        self.assert_locked()
        quantity.default_value = new_default
        for player, value in quantity.players.sorted_items():
            quantity.set(player, value)
        self.save()

    def get_quantity(self, name: str) -> Optional[Quantity]:
        name = name.lower()
        if name in self.quantities:
            return self.quantities[name]
        for quantity in self.quantities.values():
            if name in quantity.aliases:
                return quantity

    def _check_quantity_name(self, name: str, *, ignore: Optional[Quantity] = None):
        # TODO: this is duplicated in cogs.quantities
        if len(name) > 32:
            raise ValueError(f"Quantity name {name!r} is too long")
        if not re.match(r'[0-9a-z][0-9a-z\-_]*', name):
            raise ValueError(f"Quantity name {name!r} is invalid; quantity names and aliases may only contain lowercase letters, numbers, hyphens, or underscores, and must begin with a lowercase letter or number")
        if not (ignore and name in ignore.aliases):
            if self.get_quantity(name):
                raise ValueError(f"Quantity name {name!r} is already in use")

    async def log_quantity_add(self, agent: discord.Member, quantity: Quantity):
        agent = utils.discord.fake_mention(agent)
        await self.log(f"{agent} added a new {quantity} with aliases {quantity.aliases!r}")

    async def log_quantity_remove(self, agent: discord.Member, quantity: Quantity):
        agent = utils.discord.fake_mention(agent)
        await self.log(f"{agent} removed {quantity}")

    async def log_quantity_rename(self,
                                  agent: discord.Member,
                                  old_name: str,
                                  new_name: str):
        agent = utils.discord.fake_mention(agent)
        await self.log(f"{agent} renamed quantity **{old_name}** to **{new_name}**")

    async def log_quantity_change_aliases(self,
                                          agent: discord.Member,
                                          quantity: Quantity,
                                          old_aliases: List[str],
                                          new_aliases: List[str]):
        agent = utils.discord.fake_mention(agent)
        await self.log(f"{agent} changed the aliases of {quantity} from {old_aliases!r} to {new_aliases!r}")

    async def log_quantity_change_default_value(self,
                                                agent: discord.Member,
                                                quantity: Quantity,
                                                old_default_value: float,
                                                new_default_value: float):
        agent = utils.discord.fake_mention(agent)
        await self.log(f"{agent} change the default value of {quantity} from {old_default_value} to {new_default_value}")

    async def log_quantity_set_value(self,
                                     agent: discord.Member,
                                     quantity: Quantity,
                                     player: discord.Member,
                                     old_value: int,
                                     new_value: int):
        agent = utils.discord.fake_mention(agent)
        player = utils.discord.fake_mention(player)
        if new_value >= old_value:
            verb = 'gave'
            preposition = 'to'
        else:
            verb = 'took'
            preposition = 'from'
        amount = abs(new_value - old_value)
        await self.log(f"{agent} {verb} **{amount}** of {quantity} {preposition} {player} (was {old_value}; now {new_value})")
