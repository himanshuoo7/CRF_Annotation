# acrf-tools — Claude Code packaging for the aCRF skill

This marketplace distributes the `acrf` plugin, which is a thin wrapper around
the portable [`acrf` skill](../skill/acrf). The skill is the real artifact and
works with any agent; this just makes it installable in Claude Code.

```
/plugin marketplace add <owner>/<repo>
/plugin install acrf@acrf-tools
```

Requires the `crf-annotation` package (`pip install crf-annotation`). If you
run from a source checkout instead, set the plugin's **crf-annotation source
directory** option when enabling it.

## What you get

The `acrf` skill loads automatically whenever you are working on annotated
CRFs. Four named entry points are available when you want to invoke a step
directly:

| Command | Step |
|---|---|
| `/acrf:acrf-run` | Generate the aCRF |
| `/acrf:acrf-review` | Walk the non-exact matches and decide each |
| `/acrf:acrf-diagnose` | Explain missing annotations |
| `/acrf:acrf-profile` | Support an unrecognised EDC export |

The commands defer to the skill rather than restating it, so there is one copy
of the instructions.

## Keeping the copies in sync

`plugins/acrf/skills/acrf/` is a copy of `../skill/acrf/` — plugin component
paths cannot point outside the plugin root. Copy the canonical skill over it
after editing:

```bash
rm -rf plugins/acrf/skills/acrf && cp -R ../skill/acrf plugins/acrf/skills/acrf
```
