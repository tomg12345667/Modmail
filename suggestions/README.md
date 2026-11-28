# Suggestions plugin

```
?plugins add suggestions
?suggestions setup #suggestions
```

Members press **Make a suggestion** under the button message. A short form pops up, and the bot posts their suggestion with 👍/👎 votes and a discussion thread. Staff respond and the member gets a DM. You can also manage everything on the web dashboard, under **Suggestions**.

## Forum channels

Run `?suggestions setup` with a **forum** channel instead of a text channel:

- Each suggestion becomes its own post, named `#12 <suggestion>` and tagged **Suggestion**.
- The button is a pinned post at the top of the forum.
- Responding adds the matching tag (**Under review**, **Approved**, **Denied** or **Done**) next to Suggestion. Changing the response swaps that tag, and tags staff added by hand stay.
- Missing tags are created for you, which needs the bot to have **Manage Channels**. If you already have a tag with the same name, in any capitalisation, the bot uses that one.
- Rename the tags with `?suggestions set tag_approved Accepted` (also `tag_open`, `tag_considering`, `tag_denied`, `tag_done`). Leave one empty to skip it.
- Finished posts are locked so nobody can reply. Turn that off with `?suggestions set lock_finished off`.
- In a forum, the bot needs Create Posts, Embed Links, Read Message History, Add Reactions and **Manage Threads** (to pin and lock).

## Responding (Moderator)

| Command | What it does |
|---|---|
| `?suggestion approve 12 Great idea, adding it this week` | Turns it green and DMs the member with the reason |
| `?suggestion deny 12 We already have this in #phone-help` | Turns it red |
| `?suggestion consider 12` | Marks it as under review (yellow) |
| `?suggestion done 12` | Marks it as done |
| `?suggestion reopen 12` | Back to open |
| `?suggestion delete 12` | Removes it and its thread |
| `?suggestion list [status]` | Recent suggestions |

The reason is always optional. Approved, denied and done suggestions have their thread locked.

## Wording and options (Administrator)

`?suggestions show` lists everything. Change a setting with `?suggestions set <setting> <value>`, for example:

```
?suggestions set panel_title Have an idea for RingNet?
?suggestions set panel_text Tell us how we can make {server} better. Staff read every suggestion.
?suggestions set button_label Suggest something
?suggestions set threads off
?suggestions set cooldown 10
```

| Setting | What it is |
|---|---|
| panel_title | Title of the button message |
| panel_text | Text of the button message (`{server}` = server name) |
| button_label | The button's label |
| form_title | Title of the form (up to 45 characters) |
| thanks | What the member sees after submitting (`{channel}` = suggestions channel) |
| dm_text | DM when staff respond (`{number}`, `{server}`, `{status}`) |
| votes, vote_up, vote_down | Vote reactions on or off, and which emojis |
| threads | A discussion thread on each suggestion |
| dm_updates | DM members when staff respond |
| anonymous | Hide who made each suggestion |
| sticky_panel | Keep the button message at the bottom of the channel |
| cooldown | Minutes between suggestions per member |

## The form's questions

The form can have up to 5 questions, and the first answer becomes the suggestion's title.

```
?suggestions question                                   show them
?suggestions question add long What did you try? | Optional
?suggestions question edit 2 Describe it | What should it do?
?suggestions question optional 3
?suggestions question remove 3
?suggestions question reset
```

## Other

- `?suggest <idea>` posts a quick suggestion without the form.
- `?suggestions disable` closes suggestions. The button stays, but it tells members suggestions are closed.
- People blocked from Modmail can't make suggestions.
- **The bot needs** Send Messages, Embed Links, Add Reactions, Read Message History and Create Public Threads in the suggestions channel. If one is missing, it tells you which.
