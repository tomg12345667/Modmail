"""
Permission groups: named sets of commands handed to roles or members.

A group adds commands on top of someone's normal level. For example a
"Trainee" group could let the Trainee role use `reply`, `note` and `close`
without making them Supporters. Groups never grant Owner-level commands.

Stored in config["permission_groups"]:
    {"trainee": {"name": "Trainee", "commands": ["reply", "snippet *"], "members": ["<role or user id>", ...]}}

A command entry ending in " *" (e.g. "snippet *") covers that command and all its subcommands.
"""

import re

from core.models import PermissionLevel

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,31}$")
MAX_GROUPS = 25


class GroupError(Exception):
    """A problem to show to the person (in Discord or on the dashboard)."""


def key_for(name: str) -> str:
    return " ".join(str(name).split()).lower()


def get_groups(bot) -> dict:
    return bot.config.get("permission_groups") or {}


def find(bot, name: str) -> dict:
    group = get_groups(bot).get(key_for(name))
    if group is None:
        names = ", ".join(g["name"] for g in get_groups(bot).values()) or "none yet"
        raise GroupError(f"There's no permission group called `{name}`. Groups: {names}.")
    return group


def entry_matches(entry: str, command_name: str) -> bool:
    if entry.endswith(" *"):
        base = entry[:-2]
        return command_name == base or command_name.startswith(base + " ")
    return command_name == entry


def allows(groups: dict, command_name: str, ids) -> bool:
    """Whether any group that one of these role/user IDs belongs to includes this command."""
    ids = {str(i) for i in ids}
    for group in groups.values():
        if ids.intersection(group.get("members", [])) and any(entry_matches(e, command_name) for e in group.get("commands", [])):
            return True
    return False


def groups_for(groups: dict, ids) -> list:
    ids = {str(i) for i in ids}
    return [g for g in groups.values() if ids.intersection(g.get("members", []))]


def normalize_command(bot, text: str) -> str:
    """Check a command (or 'group *') can go in a permission group; return its canonical name."""
    text = " ".join(str(text).strip().lstrip(bot.prefix or "?").split()).lower()
    if not text:
        raise GroupError("Name a command.")
    wildcard = text.endswith(" *") or text.endswith("*") and text != "*"
    base = text.rstrip("*").strip()
    command = bot.get_command(base)
    if command is None:
        raise GroupError(f"There's no command called `{base}`.")
    name = command.qualified_name
    covered = [name]
    if wildcard:
        covered += [c.qualified_name for c in getattr(command, "walk_commands", lambda: [])()]
    blocked = [c for c in covered if bot.command_perm(c) is PermissionLevel.OWNER]
    if blocked and not wildcard:
        raise GroupError(f"`{name}` is an Owner-only command, so it can't be given out through a group.")
    if wildcard and len(blocked) == len(covered):
        raise GroupError(f"Everything under `{name}` is Owner-only, so it can't be given out through a group.")
    return f"{name} *" if wildcard else name


async def save(bot, groups: dict):
    bot.config["permission_groups"] = groups
    await bot.config.update()


async def create(bot, name: str) -> dict:
    name = " ".join(str(name).split())
    if not NAME_RE.match(name):
        raise GroupError("Group names can use letters, numbers, spaces, - and _ (up to 32 characters).")
    groups = dict(get_groups(bot))
    if key_for(name) in groups:
        raise GroupError(f"There's already a group called `{groups[key_for(name)]['name']}`.")
    if len(groups) >= MAX_GROUPS:
        raise GroupError(f"You can have up to {MAX_GROUPS} permission groups.")
    groups[key_for(name)] = {"name": name, "commands": [], "members": []}
    await save(bot, groups)
    return groups[key_for(name)]


async def delete(bot, name: str):
    group = find(bot, name)
    groups = dict(get_groups(bot))
    groups.pop(key_for(group["name"]), None)
    await save(bot, groups)


async def rename(bot, name: str, new_name: str) -> dict:
    group = find(bot, name)
    new_name = " ".join(str(new_name).split())
    if not NAME_RE.match(new_name):
        raise GroupError("Group names can use letters, numbers, spaces, - and _ (up to 32 characters).")
    groups = dict(get_groups(bot))
    if key_for(new_name) != key_for(group["name"]) and key_for(new_name) in groups:
        raise GroupError(f"There's already a group called `{new_name}`.")
    groups.pop(key_for(group["name"]))
    group = {**group, "name": new_name}
    groups[key_for(new_name)] = group
    await save(bot, groups)
    return group


async def set_commands(bot, name: str, commands) -> dict:
    group = find(bot, name)
    cleaned = []
    for c in commands:
        n = normalize_command(bot, c)
        if n not in cleaned:
            cleaned.append(n)
    group["commands"] = sorted(cleaned)
    await save(bot, dict(get_groups(bot)))
    return group


async def add_commands(bot, name: str, commands) -> dict:
    group = find(bot, name)
    return await set_commands(bot, name, [*group["commands"], *commands])


async def remove_commands(bot, name: str, commands) -> dict:
    group = find(bot, name)
    gone = set()
    for c in commands:
        text = " ".join(str(c).strip().lstrip(bot.prefix or "?").split()).lower()
        cmd = bot.get_command(text.rstrip("*").strip())
        canonical = (cmd.qualified_name + (" *" if text.endswith("*") else "")) if cmd else text
        if canonical not in group["commands"] and text not in group["commands"]:
            raise GroupError(f"`{text}` isn't in {group['name']}.")
        gone.update({canonical, text})
    group["commands"] = [c for c in group["commands"] if c not in gone]
    await save(bot, dict(get_groups(bot)))
    return group


async def set_members(bot, name: str, ids) -> dict:
    group = find(bot, name)
    out = []
    for i in ids:
        i = str(i).strip()
        if not i.isdigit():
            raise GroupError("Pick a role or a member.")
        if i not in out:
            out.append(i)
    group["members"] = out
    await save(bot, dict(get_groups(bot)))
    return group
