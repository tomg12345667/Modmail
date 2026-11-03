# Embed Builder plugin

Plugins for [Modmail](https://github.com/modmail-dev/modmail).

## embed: Embed Builder

Build, preview, post and edit embeds with buttons, and save them as templates.

```
?plugins add tomg12345667/Modmail/embed@master
```

| Command | What it does |
|---|---|
| `?embed create [#channel]` | Open the builder. Posts in this channel unless you name one. |
| `?embed load <template> [#channel]` | Open the builder starting from a saved template |
| `?embed post <template> [#channel]` | Post a saved template straight away |
| `?embed edit <message link>` | Edit an embed the bot already posted |
| `?embed json [#channel] <json>` | Open the builder from JSON (e.g. from [Discohook](https://discohook.app)), or attach a `.json` file |
| `?embed export <message link>` | Get a message's embed as JSON |
| `?embed templates` | List saved templates |
| `?embed deltemplate <name>` | Delete a template |

### The builder

The builder message shows a live preview of the embed with buttons underneath:

- **📝 Text**: title, title link, description, and message text above the embed (e.g. `@everyone`)
- **🎨 Colour**: hex (`#5865F2`), a name (`blue`, `green`, `red`...) or RGB (`88,101,242`)
- **👤 Author**: small name line at the top, with optional icon and link
- **🔻 Footer**: footer text, icon, and whether to show a timestamp
- **🖼️ Images**: large image and thumbnail
- **➕ Add field**: name/value pairs; choose side-by-side or full width
- **Field menu**: pick a field to edit, move up or remove
- **💾 Save template** / **📄 Export JSON**
- **✅ Post** (or **Save changes** when editing) / **Cancel**

Only the person who opened the builder can use its buttons. Discord's limits
(256-character title, 4096-character description, 25 fields, 6000 characters
total) are checked before posting, with a plain explanation if something's over.

Image links can be normal `https://` links. On bots with private-network image
support, a file path on the server or a local-network link also works: the bot
uploads the image itself.

### Permissions

All commands need Modmail's **Administrator** level by default. Change it with
`?permissions override embed <level>`.

The bot needs **Send Messages** and **Embed Links** in the channel it posts to
(and **Attach Files** for uploaded images).

Templates are stored in the bot's database, so they work with both MongoDB and
the JSON-file database.
