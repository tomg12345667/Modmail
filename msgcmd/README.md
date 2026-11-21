# Message Commands plugin

Make your own commands that send a message with blanks (variables) to fill in.

```
?plugins add tomg12345667/Modmail/msgcmd@master
```

## Create one

`?msgcmd create` (Administrator) asks, one step at a time:

1. **Name**, e.g. `outage` makes `?outage`
2. **Message**, with variables in curly braces:
   `We have an outage from {start} to {end}. Sorry! - {staff}`
3. **A question for each variable**, e.g. "When does it start?"
4. **Where it sends**: `here`, `reply` (to the ticket's user), `anonreply`, or a #channel
5. **Who can use it**: supporter, moderator, administrator or owner

These fill themselves in and are never asked: `{user}`, `{mention}` (the ticket's user), `{staff}` (whoever ran it), `{server}`, `{channel}`.

## Use one

- `?outage`: the bot asks each question, shows a preview, and sends when you type `yes`.
- `?outage 2pm | 4pm`: fill the variables in order, separated by `|`, and send straight away.

Type `cancel` at any question to stop without sending.

## Manage

| Command | What it does |
|---|---|
| `?msgcmd list` | List your message commands |
| `?msgcmd show <name>` | Show a command's message and settings |
| `?msgcmd edit <name>` | Change it (type `skip` to keep a step) |
| `?msgcmd delete <name>` | Delete it |

Command levels can be changed later with `?perms override <name> <level>` like any other command.
