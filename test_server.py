import asyncio
import io
import os
import tempfile
import wave
from pathlib import Path

import server as s

KEYS = ["OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "FAL_KEY", "CF_ACCOUNT_ID", "CF_API_TOKEN",
        "CF_AIG_GATEWAY", "CF_AIG_TOKEN"]


def clear():
    for k in KEYS:
        os.environ.pop(k, None)


clear()
order = ["openai", "gemini", "fal", "cloudflare"]

try:
    s.pick_provider(None, order)
    raise SystemExit("expected error")
except ValueError as e:
    assert "No provider configured" in str(e) and "OPENAI_API_KEY" in str(e) and "FAL_KEY" in str(e)

os.environ["FAL_KEY"] = "x"
assert s.pick_provider(None, order) == "fal"
os.environ["GOOGLE_API_KEY"] = "x"
assert s.pick_provider(None, order) == "gemini"
os.environ["OPENAI_API_KEY"] = "x"
assert s.pick_provider(None, order) == "openai"
assert s.pick_provider("fal", order) == "fal"
try:
    s.pick_provider("cloudflare", order)
    raise SystemExit("expected error")
except ValueError as e:
    assert "CF_ACCOUNT_ID" in str(e)
os.environ["CF_API_TOKEN"] = "x"
assert not s.configured("cloudflare")
os.environ["CF_ACCOUNT_ID"] = "acc"
assert s.configured("cloudflare")
assert s.pick_provider(None, ["cloudflare"]) == "cloudflare"
try:
    s.pick_provider("openai", ["gemini"])
    raise SystemExit("expected error")
except ValueError:
    pass

assert s.base_url("openai") == "https://api.openai.com/v1"
assert s.base_url("gemini") == "https://generativelanguage.googleapis.com"
assert s.base_url("fal") == "https://queue.fal.run"
assert s.base_url("cloudflare") == "https://api.cloudflare.com/client/v4/accounts/acc/ai/run"
assert "cf-aig-authorization" not in s.auth_headers("openai")
assert s.auth_headers("fal")["Authorization"] == "Key x"
assert s.auth_headers("gemini")["x-goog-api-key"] == "x"

os.environ["CF_AIG_GATEWAY"] = "gw"
gw = "https://gateway.ai.cloudflare.com/v1/acc/gw"
assert s.gateway() == gw
assert s.base_url("openai") == gw + "/openai"
assert s.base_url("gemini") == gw + "/google-ai-studio"
assert s.base_url("fal") == gw + "/fal"
assert s.base_url("cloudflare") == gw + "/workers-ai"
assert "cf-aig-authorization" not in s.auth_headers("openai")
os.environ["CF_AIG_TOKEN"] = "tok"
assert s.auth_headers("openai")["cf-aig-authorization"] == "Bearer tok"
assert "cf-aig-authorization" not in s.auth_headers("fal", via_gateway=False)
clear()

os.environ["FAL_KEY"] = "x"
os.environ["OPENAI_API_KEY"] = "x"
os.environ["GEMINI_API_KEY"] = "x"
assert s.parse_job("openai:vid_1") == ("openai", "vid_1")
assert s.parse_job("gemini:models/veo/operations/abc") == ("gemini", "models/veo/operations/abc")
assert s.parse_job("fal:https://queue.fal.run/fal-ai/veo3.1/requests/r1") == ("fal", "https://queue.fal.run/fal-ai/veo3.1/requests/r1")
for bad in ("nocolon", "openai:", "bogus:x", "fal:https://evil.example/x", "fal:https://queue.fal.run.evil.example/x"):
    try:
        s.parse_job(bad)
        raise SystemExit("expected error")
    except ValueError:
        pass
os.environ.pop("GEMINI_API_KEY")
try:
    s.parse_job("gemini:x")
    raise SystemExit("expected error")
except ValueError as e:
    assert "not configured" in str(e)

s.POLL_SECONDS = 0


async def never():
    return None


assert asyncio.run(s.poll(None, never, "t", 0)) is None
assert asyncio.run(s.poll(None, never, "t", 0.05)) is None


async def done():
    return {"ok": 1}


assert asyncio.run(s.poll(None, done, "t", 0)) == {"ok": 1}


class Resp:
    def __init__(self, data=None, content=b"", ctype=""):
        self.data, self.content, self.headers = data, content, {"content-type": ctype}

    def json(self):
        return self.data


class Ctx:
    async def report_progress(self, *a, **k):
        pass


statuses = [{"status": "in_progress", "progress": 30}, {"status": "completed", "progress": 100}]
mp4 = b"\x00\x00\x00\x18ftypmp4" + b"\x00" * 32


async def fake_call(client, method, url, provider, **kw):
    if method == "POST":
        return Resp({"id": "vid_1"})
    if url.endswith("/content"):
        return Resp(content=mp4, ctype="video/mp4")
    return Resp(statuses.pop(0))


real_call = s.call
s.call = fake_call
with tempfile.TemporaryDirectory() as d:
    os.environ["MEDIA_OUTPUT_DIR"] = d
    started = asyncio.run(s.start_video("a cat", provider="openai"))
    assert "openai:vid_1" in started and "check_video" in started
    out = asyncio.run(s.check_video("openai:vid_1", Ctx(), 0))
    assert len(out) == 1 and "still in progress" in out[0] and "30%" in out[0] and "openai:vid_1" in out[0]
    out = asyncio.run(s.check_video("openai:vid_1", Ctx(), 60))
    assert len(out) == 1 and out[0].endswith(".mp4") and Path(out[0]).read_bytes() == mp4
s.call = real_call
clear()

res = {"images": [{"url": "https://a/x.png", "w": 1}, {"url": "https://a/x.png"}],
       "video": {"url": "data:image/png;base64,AAAA"}, "nope": {"url": "ftp://z"}, "list": [{"deep": {"url": "http://b/c"}}],
       "url": 5}
assert s.collect_urls(res) == ["https://a/x.png", "data:image/png;base64,AAAA", "http://b/c"]

pcm = b"\x00\x01" * 2400
w = wave.open(io.BytesIO(s.pcm_to_wav(pcm, 16000)))
assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 2400)

assert s.detect_ext(b"", "image/png") == ".png"
assert s.detect_ext(b"", "audio/mpeg; charset=x") == ".mp3"
assert s.detect_ext(b"\x89PNG....") == ".png"
assert s.detect_ext(b"\xff\xd8\xff", "application/octet-stream") == ".jpg"
assert s.detect_ext(b"RIFF\0\0\0\0WEBPVP8 ") == ".webp"
assert s.detect_ext(b"RIFF\0\0\0\0WAVEfmt ") == ".wav"
assert s.detect_ext(b"GIF89a") == ".gif"
assert s.detect_ext(b"\0\0\0\x18ftypmp42") == ".mp4"
assert s.detect_ext(b"\x1a\x45\xdf\xa3") == ".webm"
assert s.detect_ext(b"ID3\x03") == ".mp3"
assert s.detect_ext(b"???") == ".bin"

assert s.aspect_to_size("1:1") == "1024x1024"
for a in ("16:9", "3:2", "4:3"):
    assert s.aspect_to_size(a) == "1536x1024"
for a in ("9:16", "2:3", "3:4"):
    assert s.aspect_to_size(a) == "1024x1536"
assert s.aspect_to_size(None) is None and s.aspect_to_size("21:9") is None
assert s.aspect_to_size("16:9", "video") == "1280x720"
assert s.aspect_to_size("9:16", "video") == "720x1280"
assert s.aspect_to_size("1:1", "video") is None

with tempfile.TemporaryDirectory() as d:
    os.environ["MEDIA_OUTPUT_DIR"] = d
    p = s.save_file(b"\x89PNG....", "openai")
    assert p.parent == Path(d).resolve() and p.suffix == ".png" and "-openai-" in p.name and p.read_bytes() == b"\x89PNG...."
    env_file = Path(d) / "e.env"
    env_file.write_text('# c\nA_T1=one\nexport A_T2="two"\nA_T3=\'three\'\n')
    os.environ["A_T1"] = "keep"
    s.load_env(env_file)
    assert (os.environ["A_T1"], os.environ["A_T2"], os.environ["A_T3"]) == ("keep", "two", "three")
    data, mime = s.split_data_uri("data:image/png;base64,iVBORw0KGgo=")
    assert mime == "image/png" and data.startswith(b"\x89PNG")

print("all tests passed")
