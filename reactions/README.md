# Reaction Roles & Auto Reactions plugin

```
?plugins add reactions
```

Everything here can also be done on the web dashboard, under **Reactions**. These commands need the Administrator level.

## Reaction roles

Members react to a message to get a role.

1. Post a menu: `?rr post #roles Pick your pings | React to get notified about each topic.`
2. Add roles to it, using the link the bot gives you:
   `?rr add <message link> 🎮 @Gamer`
   `?rr add <message link> 📢 @Announcements`

You can also add roles to any message that's already posted, like rules or an embed. Use its link, and the message itself isn't changed.

Each menu has a mode, set with `?rr mode <message link> <mode>`:

| Mode | What happens |
|---|---|
| `normal` | React to get the role, remove the reaction to lose it. |
| `unique` | One role from the menu at a time. Picking another one swaps it. |
| `keep` | React to get the role. Removing the reaction doesn't take it away. |

The other commands are:

- `?rr remove <message link> <emoji>`
- `?rr list`
- `?rr delete <message link>` (members keep the roles they already have)

**The bot needs** Manage Roles, and its own role must be **above** the roles it gives out (Server Settings > Roles). In the menu's channel it needs Add Reactions and Read Message History. Unique mode also needs Manage Messages, so it can clear the old reaction.

## Auto reactions

The bot reacts to every new message in a channel.

- `?autoreact set #suggestions 👍 👎`
- `?autoreact bots #suggestions on` also reacts to bots' and webhooks' messages. This is off by default.
- `?autoreact remove #suggestions`
- `?autoreact list`

Up to 10 emojis per channel. Custom emojis work if the bot is in the server they belong to.
