import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import sys
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlparse

import httpx
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from PIL import Image as PILImage
from starlette.responses import FileResponse, Response


def load_env(path):
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


load_env(Path(__file__).parent / ".env")

LOOPBACK = ("127.0.0.1", "localhost", "::1")
NEEDS = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY (or GOOGLE_API_KEY)",
    "fal": "FAL_KEY",
    "cloudflare": "CF_ACCOUNT_ID and CF_API_TOKEN",
}
DIRECT = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "fal": "https://queue.fal.run",
}
GATEWAY_SUFFIX = {"openai": "openai", "gemini": "google-ai-studio", "fal": "fal", "cloudflare": "workers-ai"}
POLL_SECONDS = 10
POLL_TIMEOUT = 20 * 60
PUBLIC_URL = None


def env(name):
    return os.environ.get(name) or None


def output_dir():
    return Path(env("MEDIA_OUTPUT_DIR") or Path.home() / "claude-media").expanduser()


def api_key(provider):
    if provider == "openai":
        return env("OPENAI_API_KEY")
    if provider == "gemini":
        return env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")
    if provider == "fal":
        return env("FAL_KEY")
    return env("CF_API_TOKEN")


def configured(provider):
    if provider == "cloudflare":
        return bool(env("CF_ACCOUNT_ID") and env("CF_API_TOKEN"))
    return bool(api_key(provider))


def gateway():
    if env("CF_AIG_GATEWAY") and env("CF_ACCOUNT_ID"):
        return f"https://gateway.ai.cloudflare.com/v1/{env('CF_ACCOUNT_ID')}/{env('CF_AIG_GATEWAY')}"
    return None


def base_url(provider):
    gw = gateway()
    if gw:
        return f"{gw}/{GATEWAY_SUFFIX[provider]}"
    if provider == "cloudflare":
        return f"https://api.cloudflare.com/client/v4/accounts/{env('CF_ACCOUNT_ID')}/ai/run"
    return DIRECT[provider]


def auth_headers(provider, via_gateway=True):
    key = api_key(provider)
    h = {
        "openai": {"Authorization": f"Bearer {key}"},
        "gemini": {"x-goog-api-key": key},
        "fal": {"Authorization": f"Key {key}"},
        "cloudflare": {"Authorization": f"Bearer {key}"},
    }[provider]
    if via_gateway and gateway() and env("CF_AIG_TOKEN"):
        h["cf-aig-authorization"] = f"Bearer {env('CF_AIG_TOKEN')}"
    return h


def pick_provider(provider, order):
    if provider:
        if provider not in order:
            raise ValueError(f"Unknown provider '{provider}'. Choose from: {', '.join(order)}")
        if not configured(provider):
            raise ValueError(f"Provider '{provider}' is not configured. Set {NEEDS[provider]}.")
        return provider
    for p in order:
        if configured(p):
            return p
    needs = "; ".join(f"{p}: {NEEDS[p]}" for p in order)
    raise ValueError(f"No provider configured. Set at least one of - {needs}")


def aspect_to_size(aspect_ratio, kind="image"):
    if kind == "video":
        return {"16:9": "1280x720", "9:16": "720x1280"}.get(aspect_ratio)
    if aspect_ratio == "1:1":
        return "1024x1024"
    if aspect_ratio in ("16:9", "3:2", "4:3"):
        return "1536x1024"
    if aspect_ratio in ("9:16", "2:3", "3:4"):
        return "1024x1536"
    return None


def collect_urls(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v.startswith(("http", "data:")):
                if v not in out:
                    out.append(v)
            else:
                collect_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_urls(v, out)
    return out


def pcm_to_wav(pcm, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}


def detect_ext(data, mime=None):
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in MIME_EXT:
        return MIME_EXT[mime]
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[4:8] == b"ftyp":
        return ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    return ".bin"


def save_file(data, provider, mime=None):
    d = output_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{provider}-{uuid.uuid4().hex[:8]}{detect_ext(data, mime)}"
    p = d / name
    p.write_bytes(data)
    return p.resolve()


def split_data_uri(uri):
    head, _, payload = uri.partition(",")
    mime = head[5:].split(";")[0] or "application/octet-stream"
    if ";base64" in head:
        return base64.b64decode(payload), mime
    return unquote_to_bytes(payload), mime


async def check(r, provider):
    if r.status_code >= 400:
        body = r.text[:2000]
        raise RuntimeError(f"{provider} HTTP {r.status_code}: {body}")
    return r


async def call(client, method, url, provider, **kw):
    r = await client.request(method, url, **kw)
    return await check(r, provider)


async def download(client, ref, headers=None):
    if ref.startswith("data:"):
        data, mime = split_data_uri(ref)
        return data, mime
    r = await client.get(ref, headers=headers, follow_redirects=True)
    await check(r, "download")
    return r.content, r.headers.get("content-type", "")


async def load_input(client, ref):
    if ref.startswith(("http://", "https://", "data:")):
        data, mime = await download(client, ref)
    else:
        p = Path(ref).expanduser()
        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or ""
    mime = mime.split(";")[0].strip()
    if not mime or mime == "application/octet-stream":
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(
            detect_ext(data), "application/octet-stream"
        )
    return data, mime


async def to_url(client, ref):
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    data, mime = await load_input(client, ref)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def poll(ctx, step, label, timeout=POLL_TIMEOUT):
    start = time.monotonic()
    while True:
        res = await step()
        if res is not None:
            return res
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return None
        if ctx is not None:
            await ctx.report_progress(elapsed, timeout, f"{label} in progress")
        await asyncio.sleep(POLL_SECONDS)


async def fal_submit(client, model, payload):
    gw = gateway()
    target = f"{DIRECT['fal']}/{model}"
    headers = auth_headers("fal")
    if gw:
        url = f"{base_url('fal')}/{model}"
        headers["x-fal-target-url"] = target
    else:
        url = target
    return (await call(client, "POST", url, "fal", headers=headers, json=payload)).json()


async def fal_run(client, model, payload, ctx=None):
    sub = await fal_submit(client, model, payload)
    direct_headers = auth_headers("fal", via_gateway=False)

    async def step():
        st = (await call(client, "GET", sub["status_url"], "fal", headers=direct_headers)).json()
        if st.get("status") == "COMPLETED":
            if st.get("error"):
                raise RuntimeError(f"fal failed: {str(st['error'])[:2000]}")
            return st
        return None

    if await poll(ctx, step, "fal job") is None:
        raise RuntimeError(f"fal job timed out after {POLL_TIMEOUT // 60} minutes")
    return (await call(client, "GET", sub["response_url"], "fal", headers=direct_headers)).json()


async def fal_files(client, result):
    files = []
    for u in collect_urls(result):
        data, mime = await download(client, u)
        files.append((data, mime, None if u.startswith("data:") else u))
    if not files:
        raise RuntimeError(f"fal returned no media: {json.dumps(result)[:2000]}")
    return files


def gemini_parts(resp):
    try:
        return resp["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"gemini returned no content: {json.dumps(resp)[:2000]}")


def part_inline(part):
    inl = part.get("inlineData") or part.get("inline_data")
    if not inl:
        return None
    return base64.b64decode(inl["data"]), inl.get("mimeType") or inl.get("mime_type") or ""


async def gemini_generate(client, model, body):
    url = f"{base_url('gemini')}/v1beta/models/{model}:generateContent"
    return (await call(client, "POST", url, "gemini", headers=auth_headers("gemini"), json=body, timeout=300)).json()


async def cf_run(client, model, body):
    r = await call(
        client, "POST", f"{base_url('cloudflare')}/{model}", "cloudflare", headers=auth_headers("cloudflare"), json=body
    )
    ct = r.headers.get("content-type", "")
    if ct.startswith(("image/", "audio/")):
        return r.content, ct
    result = r.json().get("result") or {}
    b64 = result.get("image") or result.get("audio")
    if not b64:
        raise RuntimeError(f"cloudflare returned no media: {r.text[:2000]}")
    return base64.b64decode(b64), ""


def multipart_fields(d):
    return {k: v if isinstance(v, str) else json.dumps(v) for k, v in d.items() if v is not None}


def preview(data):
    img = PILImage.open(io.BytesIO(data))
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


def finish(provider, files, texts, previews=False):
    out = []
    for data, mime, src in files:
        p = save_file(data, provider, mime)
        line = str(p)
        if PUBLIC_URL:
            line += f"\n{PUBLIC_URL}/files/{p.name}"
        if src:
            line += f"\nsource: {src}"
        out.append(line)
        if previews:
            try:
                out.append(preview(data))
            except Exception:
                pass
    out.extend(t for t in texts if t)
    return out


mcp = FastMCP("claude-media")


@mcp.custom_route("/files/{name}", methods=["GET"])
async def serve_file(request):
    p = output_dir() / Path(request.path_params["name"]).name
    if not p.is_file():
        return Response("not found", status_code=404)
    return FileResponse(p)


@mcp.tool()
async def generate_image(
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    aspect_ratio: str | None = None,
    input_images: list[str] | None = None,
    options: dict | None = None,
) -> list:
    """Generate or edit an image. Saves full-resolution files to disk and returns paths plus a JPEG preview.

    provider (default: first configured of openai, gemini, fal, cloudflare):
    - openai: default model gpt-image-2. aspect_ratio maps to size (1:1 -> 1024x1024; 16:9/3:2/4:3 -> 1536x1024; 9:16/2:3/3:4 -> 1024x1536). options merged into the request body (e.g. quality, background, n, output_format); options["size"] wins. input_images switches to /images/edits.
    - gemini: default gemini-3-pro-image (Nano Banana Pro). aspect_ratio -> imageConfig.aspectRatio (e.g. 16:9). options shallow-update generationConfig (e.g. imageConfig with imageSize).
    - fal: default fal-ai/nano-banana-pro. Input {prompt, aspect_ratio, image_urls} updated with options (model-specific params such as num_images, resolution, image_size). Any model id works.
    - cloudflare: default @cf/black-forest-labs/flux-1-schnell. Body {prompt} updated with options (e.g. steps, seed). No input_images.

    input_images: local file paths, http(s) URLs or data URIs, used as edit targets or references.
    """
    provider = pick_provider(provider, ["openai", "gemini", "fal", "cloudflare"])
    options = options or {}
    input_images = input_images or []
    files, texts = [], []
    async with httpx.AsyncClient(timeout=300) as client:
        if provider == "openai":
            size = options.get("size") or aspect_to_size(aspect_ratio)
            body = {"model": model or "gpt-image-2", "prompt": prompt, **({"size": size} if size else {}), **options}
            h = auth_headers("openai")
            if input_images:
                loaded = [await load_input(client, i) for i in input_images]
                parts = [("image[]", (f"image{n}", d, m)) for n, (d, m) in enumerate(loaded)]
                r = await call(
                    client, "POST", f"{base_url('openai')}/images/edits", "openai",
                    headers=h, data=multipart_fields(body), files=parts,
                )
            else:
                r = await call(client, "POST", f"{base_url('openai')}/images/generations", "openai", headers=h, json=body)
            for item in r.json().get("data", []):
                if item.get("b64_json"):
                    files.append((base64.b64decode(item["b64_json"]), "", None))
                elif item.get("url"):
                    data, mime = await download(client, item["url"])
                    files.append((data, mime, item["url"]))
        elif provider == "gemini":
            parts = []
            for i in input_images:
                data, mime = await load_input(client, i)
                parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
            parts.append({"text": prompt})
            cfg = {"responseModalities": ["TEXT", "IMAGE"]}
            if aspect_ratio:
                cfg["imageConfig"] = {"aspectRatio": aspect_ratio}
            cfg.update(options)
            resp = await gemini_generate(
                client, model or "gemini-3-pro-image",
                {"contents": [{"parts": parts}], "generationConfig": cfg},
            )
            for part in gemini_parts(resp):
                got = part_inline(part)
                if got:
                    files.append((got[0], got[1], None))
                elif part.get("text"):
                    texts.append(part["text"])
            if not files:
                raise RuntimeError(f"gemini returned no image: {' '.join(texts)[:2000]}")
        elif provider == "fal":
            payload = {"prompt": prompt}
            if aspect_ratio:
                payload["aspect_ratio"] = aspect_ratio
            if input_images:
                payload["image_urls"] = [await to_url(client, i) for i in input_images]
            payload.update(options)
            files = await fal_files(client, await fal_run(client, model or "fal-ai/nano-banana-pro", payload))
        else:
            if input_images:
                raise ValueError("cloudflare image generation does not support input_images")
            data, mime = await cf_run(
                client, model or "@cf/black-forest-labs/flux-1-schnell", {"prompt": prompt, **options}
            )
            files.append((data, mime, None))
    return finish(provider, files, texts, previews=True)


@mcp.tool()
async def start_video(
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    aspect_ratio: str | None = None,
    duration_seconds: int | None = None,
    input_image: str | None = None,
    options: dict | None = None,
) -> str:
    """Start a video generation job (text-to-video, or image-to-video with input_image) and return its job_id immediately. Then call check_video(job_id) to wait for and fetch the result.

    provider (default: first configured of openai, gemini, fal):
    - openai: default sora-2. aspect_ratio 16:9 -> 1280x720, 9:16 -> 720x1280. duration_seconds -> seconds. options merged into the request (e.g. size overrides). input_image sent as input_reference.
    - gemini: default veo-3.1-generate-preview. aspect_ratio and duration_seconds go to parameters.aspectRatio / durationSeconds; options merged into parameters (e.g. resolution, negativePrompt, personGeneration).
    - fal: default fal-ai/veo3.1/fast. Input {prompt, aspect_ratio, image_url} updated with options. Duration is model-specific on fal (e.g. "8s" vs "5"), so pass it via options such as {"duration": "8s"}; duration_seconds is rejected for fal.

    input_image: local path, http(s) URL or data URI.
    """
    provider = pick_provider(provider, ["openai", "gemini", "fal"])
    if provider == "fal" and duration_seconds is not None:
        raise ValueError('duration_seconds is not supported for fal; pass the model-specific value via options, e.g. {"duration": "8s"}')
    options = options or {}
    async with httpx.AsyncClient(timeout=300) as client:
        if provider == "openai":
            h = auth_headers("openai")
            b = base_url("openai")
            size = options.get("size") or aspect_to_size(aspect_ratio, "video")
            body = {"model": model or "sora-2", "prompt": prompt}
            if duration_seconds is not None:
                body["seconds"] = str(duration_seconds)
            if size:
                body["size"] = size
            body.update(options)
            if input_image:
                data, mime = await load_input(client, input_image)
                r = await call(
                    client, "POST", f"{b}/videos", "openai", headers=h,
                    data=multipart_fields(body), files={"input_reference": ("input", data, mime)},
                )
            else:
                r = await call(client, "POST", f"{b}/videos", "openai", headers=h, json=body)
            ref = r.json()["id"]
        elif provider == "gemini":
            h = auth_headers("gemini")
            b = base_url("gemini")
            instance = {"prompt": prompt}
            if input_image:
                data, mime = await load_input(client, input_image)
                instance["image"] = {"bytesBase64Encoded": base64.b64encode(data).decode(), "mimeType": mime}
            params = {}
            if aspect_ratio:
                params["aspectRatio"] = aspect_ratio
            if duration_seconds is not None:
                params["durationSeconds"] = duration_seconds
            params.update(options)
            r = await call(
                client, "POST", f"{b}/v1beta/models/{model or 'veo-3.1-generate-preview'}:predictLongRunning",
                "gemini", headers=h, json={"instances": [instance], "parameters": params},
            )
            ref = r.json()["name"]
        else:
            payload = {"prompt": prompt}
            if aspect_ratio:
                payload["aspect_ratio"] = aspect_ratio
            if input_image:
                payload["image_url"] = await to_url(client, input_image)
            payload.update(options)
            ref = (await fal_submit(client, model or "fal-ai/veo3.1/fast", payload))["response_url"]
    return f"Video job started. job_id: {provider}:{ref}\nCall check_video with this job_id next."


def parse_job(job_id):
    provider, sep, ref = job_id.partition(":")
    if not sep or not ref:
        raise ValueError(f"Invalid job_id '{job_id}'. Expected the job_id returned by start_video.")
    if provider == "fal" and not ref.startswith(f"{DIRECT['fal']}/"):
        raise ValueError(f"Invalid fal job_id '{job_id}': must point at {DIRECT['fal']}")
    return pick_provider(provider, ["openai", "gemini", "fal"]), ref


@mcp.tool()
async def check_video(job_id: str, ctx: Context, wait_seconds: int = 45) -> list:
    """Wait for a video job started by start_video. Polls for up to wait_seconds (0-300, default 45). When the video is ready, saves it to disk and returns its path. If it is still running, returns a status message with the job_id; call check_video again with the same job_id until the path is returned. Failures raise an error.
    """
    provider, ref = parse_job(job_id)
    wait = max(0, min(300, wait_seconds))
    last = {}
    files = []
    async with httpx.AsyncClient(timeout=300) as client:
        if provider == "openai":
            h = auth_headers("openai")
            b = base_url("openai")

            async def step():
                v = (await call(client, "GET", f"{b}/videos/{ref}", "openai", headers=h)).json()
                if v.get("status") == "failed":
                    raise RuntimeError(f"openai video failed: {str(v.get('error'))[:2000]}")
                last.update(status=v.get("status"), progress=v.get("progress"))
                return v if v.get("status") == "completed" else None

            if await poll(ctx, step, "openai video", wait) is not None:
                r = await call(client, "GET", f"{b}/videos/{ref}/content", "openai", headers=h, follow_redirects=True)
                files.append((r.content, r.headers.get("content-type", ""), None))
        elif provider == "gemini":
            h = auth_headers("gemini")
            b = base_url("gemini")

            async def step():
                op = (await call(client, "GET", f"{b}/v1beta/{ref}", "gemini", headers=h)).json()
                if op.get("error"):
                    raise RuntimeError(f"gemini video failed: {json.dumps(op['error'])[:2000]}")
                return op if op.get("done") else None

            op = await poll(ctx, step, "gemini video", wait)
            if op is not None:
                try:
                    uri = op["response"]["generateVideoResponse"]["generatedSamples"][0]["video"]["uri"]
                except (KeyError, IndexError, TypeError):
                    raise RuntimeError(f"gemini returned no video: {json.dumps(op)[:2000]}")
                data, mime = await download(client, uri, headers={"x-goog-api-key": api_key("gemini")})
                files.append((data, mime, uri))
        else:
            h = auth_headers("fal", via_gateway=False)

            async def step():
                st = (await call(client, "GET", f"{ref}/status", "fal", headers=h)).json()
                if st.get("status") == "COMPLETED":
                    if st.get("error"):
                        raise RuntimeError(f"fal failed: {str(st['error'])[:2000]}")
                    return st
                last.update(status=st.get("status"))
                return None

            if await poll(ctx, step, "fal job", wait) is not None:
                files = await fal_files(client, (await call(client, "GET", ref, "fal", headers=h)).json())
    if not files:
        status = last.get("status") or "running"
        pct = f" ({last['progress']}%)" if last.get("progress") is not None else ""
        return [f"Video still in progress: {status}{pct}. job_id: {job_id}\nCall check_video again with this job_id."]
    return finish(provider, files, [])


@mcp.tool()
async def generate_audio(
    text: str,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    options: dict | None = None,
) -> list:
    """Text-to-speech (and prompt-based audio on fal). Saves the audio file to disk and returns its path.

    provider (default: first configured of openai, gemini, fal, cloudflare):
    - openai: default gpt-4o-mini-tts, voice default alloy, mp3 output. options merged into the request (e.g. instructions, speed, response_format).
    - gemini: default gemini-3.8-flash-tts, voice default Kore (prebuilt voice name). options shallow-update generationConfig. Output is WAV.
    - fal: default fal-ai/elevenlabs/tts/turbo-v2.5. Input {text, voice} updated with options. Prompt-based fal audio models (music, sound effects) take options={"prompt": "..."}; any model id works.
    - cloudflare: default @cf/deepgram/aura-1. Body {text, speaker=voice} updated with options.
    """
    provider = pick_provider(provider, ["openai", "gemini", "fal", "cloudflare"])
    options = options or {}
    files = []
    async with httpx.AsyncClient(timeout=300) as client:
        if provider == "openai":
            body = {
                "model": model or "gpt-4o-mini-tts", "input": text, "voice": voice or "alloy",
                "response_format": "mp3", **options,
            }
            r = await call(
                client, "POST", f"{base_url('openai')}/audio/speech", "openai", headers=auth_headers("openai"), json=body
            )
            files.append((r.content, r.headers.get("content-type", "audio/mpeg"), None))
        elif provider == "gemini":
            cfg = {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or "Kore"}}},
            }
            cfg.update(options)
            resp = await gemini_generate(
                client, model or "gemini-3.8-flash-tts",
                {"contents": [{"parts": [{"text": text}]}], "generationConfig": cfg},
            )
            for part in gemini_parts(resp):
                got = part_inline(part)
                if got:
                    m = re.search(r"rate=(\d+)", got[1])
                    files.append((pcm_to_wav(got[0], int(m.group(1)) if m else 24000), "audio/wav", None))
            if not files:
                raise RuntimeError(f"gemini returned no audio: {json.dumps(resp)[:2000]}")
        elif provider == "fal":
            payload = {"text": text}
            if voice:
                payload["voice"] = voice
            payload.update(options)
            files = await fal_files(client, await fal_run(client, model or "fal-ai/elevenlabs/tts/turbo-v2.5", payload))
        else:
            body = {"text": text}
            if voice:
                body["speaker"] = voice
            body.update(options)
            data, mime = await cf_run(client, model or "@cf/deepgram/aura-1", body)
            files.append((data, mime, None))
    return finish(provider, files, [])


def run_http():
    global PUBLIC_URL
    host = env("MCP_HOST") or "127.0.0.1"
    port = int(env("MCP_PORT") or 47213)
    token = env("MCP_TOKEN")
    if host not in LOOPBACK and not token:
        sys.exit("Refusing to listen on a non-loopback host without MCP_TOKEN: this server spends API credits.")
    PUBLIC_URL = (env("MCP_PUBLIC_URL") or f"http://{host}:{port}").rstrip("/")
    pub = urlparse(PUBLIC_URL)
    hosts = [f"{h}:*" for h in ("127.0.0.1", "localhost", "[::1]")] + [f"{host}:{port}", pub.netloc, f"{pub.hostname}:*"]
    origins = [f"http://{h}:*" for h in ("127.0.0.1", "localhost", "[::1]")] + [f"{pub.scheme}://{pub.netloc}"]
    s = mcp.settings
    s.host, s.port = host, port
    s.streamable_http_path = f"/{token}/mcp" if token else "/mcp"
    s.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )
    mcp.run("streamable-http")


if __name__ == "__main__":
    if "--http" in sys.argv:
        run_http()
    else:
        mcp.run()
