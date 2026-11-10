# Verification plugin

Verify new members through Modmail tickets.

```
?plugins add tomg12345667/Modmail/verification@master
```

## How it works

1. A new member joins. They get the unverified role and auto role, and a welcome DM telling them to reply with the keyword.
2. They DM the bot the keyword (default `verify`). A ticket opens, the bot replies with your verification message, and staff get an info card (account age, join date, status) with an optional staff ping.
3. Staff check them and run `?verify` in the ticket. The bot gives them the verified role, removes the unverified role and tells them.

Setup creates the alias `verifyprompt` and an autotrigger for your keyword. You can also run `verifyprompt` from a thread-menu option.

## Commands

| Command | Who | What it does |
|---|---|---|
| `?verification setup` | Owner | Step-by-step setup |
| `?verification show` | Owner | Show the settings |
| `?verification set <setting> <value>` | Owner | Change one setting (run without anything for the list) |
| `?verification enable` / `disable` | Owner | Turn it on or off (settings are kept) |
| `?verification reset` | Owner | Remove all settings, the alias and the autotrigger |
| `?verify [member]` | Supporter | Verify someone (in a ticket, defaults to the ticket's user) |
| `?unverify <member>` | Moderator | Take verification away |
| `?verification pending` | Supporter | Who is waiting to verify, and when they'll be kicked |

## Settings

Roles (verified, unverified, auto role, staff ping), welcome DM, keyword, verification reply, verified message, log channel, kick timer (e.g. `24h`), kick DM, minimum account age in days with `flag` or `kick`, and close-after-verify.

Messages can use `{user}`, `{mention}`, `{server}` and `{keyword}`.

The kick timer only applies to people who join after it's turned on. Existing members are never kicked.

## Bot permissions

**Manage Roles**, with the bot's role above the roles it gives out, and **Kick Members** if you use the kick timer or account-age kicks.
