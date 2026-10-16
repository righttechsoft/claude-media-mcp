# claude-media-mcp

An MCP server that lets any MCP-capable chat or agent (Claude Code, Claude Desktop, Codex, VS Code, Antigravity, Cursor, claude.ai connectors and others) generate **images, video and audio** through OpenAI, Google Gemini, fal.ai and Cloudflare Workers AI. You can also send every request through your **Cloudflare AI Gateway**.

One Python file, three dependencies, one `.env` for all your clients.

| | Image | Video | Audio |
| --- | --- | --- | --- |
| **OpenAI** | `gpt-image-2` (generate + edit) | `sora-2` | `gpt-4o-mini-tts` |
| **Gemini** | `gemini-3-pro-image` (Nano Banana Pro, generate + edit) | `veo-3.1-generate-preview` | `gemini-3.8-flash-tts` (WAV) |
| **fal.ai** | `fal-ai/nano-banana-pro`, or any fal model | `fal-ai/veo3.1/fast`, or any | `fal-ai/elevenlabs/tts/turbo-v2.5`, or any |
| **Cloudflare Workers AI** | `@cf/black-forest-labs/flux-1-schnell` | - | `@cf/deepgram/aura-1` |

Every tool also takes a `model` override, so any model the provider offers works.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```sh
git clone https://github.com/righttechsoft/claude-media-mcp.git
cd claude-media-mcp
uv sync
cp .env.example .env    # then fill in the keys you have
```

Only providers whose key is set are used. `.env` sits next to `server.py` and is read on every start; real environment variables take precedence.

## Connect a client

The server speaks MCP over stdio. Every client launches the same command:

```sh
uv run --directory /path/to/claude-media-mcp server.py
```

Or, to skip `uv` at launch time, the project's virtualenv Python directly: `/path/to/claude-media-mcp/.venv/bin/python /path/to/claude-media-mcp/server.py` (on Windows: `.venv\Scripts\python.exe`).

**Claude Code** (all projects):

```sh
claude mcp add -s user media -- uv run --directory /path/to/claude-media-mcp server.py
```

**Claude Desktop** (`claude_desktop_config.json`), **Antigravity** (`~/.gemini/antigravity/mcp_config.json`), **Cursor** (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "media": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/claude-media-mcp", "server.py"]
    }
  }
}
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.media]
command = "uv"
args = ["run", "--directory", "/path/to/claude-media-mcp", "server.py"]
```

**VS Code** (user `mcp.json`):

```json
{
  "servers": {
    "media": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/claude-media-mcp", "server.py"]
    }
  }
}
```

GUI apps sometimes don't see your shell `PATH`. If a client can't find `uv`, use its absolute path or the virtualenv Python shown above.

## Tools

All tools take optional `provider` (default: the first configured one in the order listed), `model` and `options`. `options` is merged into the provider's request, so you can pass any model-specific parameter (quality, seed, resolution, negative prompt and so on).

Files are saved to `MEDIA_OUTPUT_DIR` (default `~/claude-media`) as `YYYYmmdd-HHMMSS-provider-id.ext`. Each result has the absolute path. In HTTP mode it also has a download link.

### `generate_image(prompt, provider, model, aspect_ratio, input_images, options)`

Order: openai, gemini, fal, cloudflare. Returns the full-size file plus a JPEG preview (max 1024px) the chat can see inline.

- `aspect_ratio` like `16:9`. OpenAI maps it to the closest supported size.
- `input_images`: local paths, URLs or data URIs, used for editing or as references. Not supported on Cloudflare.

### `start_video(prompt, provider, model, aspect_ratio, duration_seconds, input_image, options)` and `check_video(job_id, wait_seconds=45)`

Order: openai, gemini, fal. Video takes minutes, longer than most clients wait for one tool call, so it is split in two:

1. `start_video` submits the job and returns a `job_id` immediately.
2. `check_video` waits up to `wait_seconds` (max 300). If the video is done it saves it and returns the path. If not, it returns the current status, and the chat calls it again.

The `job_id` contains the provider's own job reference, so a job started in one session can be collected in another. On fal, duration is model-specific: pass it via `options`, for example `{"duration": "8s"}`.

### `generate_audio(text, provider, model, voice, options)`

Order: openai, gemini, fal, cloudflare. Text-to-speech by default. Prompt-based fal models (music, sound effects) take `options={"prompt": "..."}`.

## Configuration

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Gemini |
| `FAL_KEY` | fal.ai |
| `CF_ACCOUNT_ID` + `CF_API_TOKEN` | Cloudflare Workers AI |
| `CF_AIG_GATEWAY` | Cloudflare AI Gateway id. With `CF_ACCOUNT_ID`, routes all providers through `gateway.ai.cloudflare.com` |
| `CF_AIG_TOKEN` | Optional, for authenticated gateways (`cf-aig-authorization` header) |
| `MEDIA_OUTPUT_DIR` | Output folder, default `~/claude-media` |
| `MCP_HOST`, `MCP_PORT` | HTTP mode bind address, default `127.0.0.1:47213` |
| `MCP_TOKEN` | HTTP mode secret: the endpoint becomes `/{MCP_TOKEN}/mcp`. Required when binding to a non-loopback host |
| `MCP_PUBLIC_URL` | Public base URL used in file download links, for example behind a tunnel |

With the AI Gateway enabled, each provider still needs its own key. Gateway-stored keys (BYOK) without a local key are not supported yet.

## HTTP mode (remote clients)

```sh
uv run server.py --http
```

Serves streamable HTTP at `http://127.0.0.1:47213/mcp` and generated files at `/files/<name>`. To reach it from claude.ai or another machine, set `MCP_TOKEN` and `MCP_PUBLIC_URL`, put it behind a tunnel (for example Cloudflare Tunnel), and add `https://your-host/{MCP_TOKEN}/mcp` as a custom connector.

The server spends your API credits, so it refuses to bind to a non-loopback address without `MCP_TOKEN`. Treat the token like a password.

## Development

```sh
uv run python test_server.py
```

Offline checks only: no API calls, no keys needed.

`mcp` is pinned below 2.0 because the `mcp.server.fastmcp` API this server uses was removed in the 2.x SDK.

## License

[MIT](LICENSE)
