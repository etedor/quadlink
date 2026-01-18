# QuadLink

Companion to QuadStream for Apple tvOS.

## Development

```bash
nix develop           # enter dev shell
pytest                # run tests
python -m quadlink    # run daemon
python -m quadlink --one-shot  # single iteration
```

### Workflow

Before committing:
1. **Test**: `pytest` - ensure all tests pass
2. **Format**: `black src/ tests/` - apply consistent formatting
3. **Type check**: `mypy src/` - verify type safety (3 streamlink errors expected)

## Style

- Comments: lowercase, terse
- Acronyms in comments: UPPERCASE (URL, API, HTTP, HLS, M3U8, etc.)
- Product names: capitalize properly (Twitch, QuadStream)
- Docstrings: standard Python style (sentence case)

## Commits

```
<terse description>

<optional details>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

## Project Structure

```
src/quadlink/
├── daemon.py        # main loop
├── health.py        # health check server
├── quad.py          # quad selection algorithm
├── quadstream.py    # QuadStream API client
├── types.py         # core data types
├── config/          # configuration loading
└── stream/          # stream fetching, filtering, processing
```

## External Dependencies

### streamlink-ttvlol plugin

Source: https://github.com/2bc4/streamlink-ttvlol (BSD-2-Clause)
Current: `8a2ebd30dbcbd3caff3f171a1a8c84bc50bc8bd5`

Twitch plugin with ad-blocking proxy support. Fetched at build time via nix `fetchurl`.

Update to new commit:

```bash
# 1. get commit hash from https://github.com/2bc4/streamlink-ttvlol
# 2. compute hash
nix-prefetch-url https://raw.githubusercontent.com/2bc4/streamlink-ttvlol/<COMMIT>/twitch.py
nix hash convert --to sri sha256:<HASH>
# 3. update nix/package.nix (url + hash)
# 4. update CLAUDE.md (current version above)
# 5. update flake.nix shellHook (url in curl command)
```
