"""Kanha Ji auto-poster. Usage: python pipeline.py make | publish"""
import asyncio, json, os, subprocess, sys, time, urllib.parse
from pathlib import Path
import requests, edge_tts

OUT = Path("out"); OUT.mkdir(exist_ok=True)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")S
VOICE = os.getenv("VOICE", "hi-IN-MadhurNeural")   # Gujarati: gu-IN-NiranjanNeural
LANG = os.getenv("LANG_NAME", "Hindi")
STYLE = ("cute 3D animated Pixar-style, Bal Krishna as a small blue-skinned child with a peacock "
         "feather crown, yellow dhoti, soft warm lighting, Vrindavan village, vibrant colors, "
         "devotional, family friendly, vertical 9:16")

def sh(cmd): subprocess.run(cmd, check=True, capture_output=True)

def duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())

# ---------- 1. script ----------
def make_script(topic):
    prompt = f"""You write 35-45 second devotional animated short videos about Bal Krishna (Kanha Ji).
Topic: {topic}
Language for narration: {LANG}. Tone: warm, simple, respectful, child friendly.
Rules: stay faithful to Bhagavata Purana / well-known tradition, do NOT invent facts, mention the
source (e.g. Bhagavata Purana, Canto 10) in the last scene, start with a 1-sentence hook, end with "Jai Shri Krishna".
Return ONLY JSON:
{{"title": "<catchy title in {LANG}, max 70 chars>",
 "description": "<2 lines in {LANG} + source>",
 "hashtags": ["#BalKrishna","#Kanha","#Shorts", "... up to 8"],
 "scenes": [{{"narration": "<1-2 short sentences in {LANG}>",
              "image_prompt": "<English visual description of the scene, no text in image>"}}]}}
Exactly 6 scenes."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, timeout=120, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}})
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])

# ---------- 2. assets ----------
def make_image(prompt, path, seed):
    q = urllib.parse.quote(f"{prompt}. {STYLE}")
    url = f"https://image.pollinations.ai/prompt/{q}?width=1080&height=1920&nologo=true&seed={seed}"
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=180)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                path.write_bytes(r.content); return
        except requests.RequestException:
            pass
        time.sleep(10 * (attempt + 1))
    raise RuntimeError("image generation failed")

async def make_voice(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))

def build_scene(i, scene):
    img, aud, vid = OUT / f"s{i}.jpg", OUT / f"s{i}.mp3", OUT / f"s{i}.mp4"
    make_image(scene["image_prompt"], img, seed=1000 + i)
    asyncio.run(make_voice(scene["narration"], aud))
    dur = duration(aud) + 0.4
    frames = int(dur * 30) + 1
    vf = ("scale=1296:2304:force_original_aspect_ratio=increase,crop=1296:2304,"
          f"zoompan=z='min(zoom+0.0007,1.2)':d={frames}:x='iw/2-(iw/zoom/2)':"
          "y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,format=yuv420p")
    sh(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-i", str(aud), "-vf", vf,
        "-af", "apad=pad_dur=0.4", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        "-ar", "44100", "-ac", "2", "-shortest", str(vid)])
    return vid

def cmd_make():
    topics = [t.strip() for t in Path("topics.txt").read_text().splitlines() if t.strip()]
    state = json.loads(Path("state.json").read_text()) if Path("state.json").exists() else {"i": 0}
    topic = topics[state["i"] % len(topics)]
    print("Topic:", topic)
    data = make_script(topic)
    clips = [build_scene(i, s) for i, s in enumerate(data["scenes"])]
    (OUT / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips))
    sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(OUT / "list.txt"),
        "-c", "copy", str(OUT / "video.mp4")])
    data["next_i"] = state["i"] + 1
    (OUT / "meta.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))

# ---------- 3. publish ----------
def caption(m): return f"{m['title']}\n\n{m['description']}\n\n{' '.join(m['hashtags'])}"

def post_youtube(m):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"])
    yt = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": m["title"][:95], "description": caption(m), "categoryId": "24",
                        "tags": [h.strip("#") for h in m["hashtags"]]},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    yt.videos().insert(part="snippet,status", body=body,
                       media_body=MediaFileUpload(str(OUT / "video.mp4"), resumable=True)).execute()

def post_instagram(m, video_url):
    g, uid, tok = "https://graph.facebook.com/v21.0", os.environ["IG_USER_ID"], os.environ["META_PAGE_TOKEN"]
    r = requests.post(f"{g}/{uid}/media", data={"media_type": "REELS", "video_url": video_url,
                      "caption": caption(m), "access_token": tok}, timeout=120)
    r.raise_for_status(); cid = r.json()["id"]
    for _ in range(40):
        s = requests.get(f"{g}/{cid}", params={"fields": "status_code", "access_token": tok}, timeout=60).json()
        if s.get("status_code") == "FINISHED": break
        if s.get("status_code") == "ERROR": raise RuntimeError(s)
        time.sleep(15)
    requests.post(f"{g}/{uid}/media_publish", data={"creation_id": cid, "access_token": tok},
                  timeout=120).raise_for_status()

def post_facebook(m, video_url):
    g, pid, tok = "https://graph.facebook.com/v21.0", os.environ["FB_PAGE_ID"], os.environ["META_PAGE_TOKEN"]
    requests.post(f"{g}/{pid}/videos", data={"file_url": video_url, "description": caption(m),
                  "title": m["title"], "access_token": tok}, timeout=300).raise_for_status()

def cmd_publish():
    m = json.loads((OUT / "meta.json").read_text())
    url = os.environ["VIDEO_URL"]; ok = 0
    for name, fn in [("youtube", lambda: post_youtube(m)), ("instagram", lambda: post_instagram(m, url)),
                     ("facebook", lambda: post_facebook(m, url))]:
        try: fn(); ok += 1; print("posted:", name)
        except Exception as e: print("FAILED:", name, repr(e))
    if ok == 0: sys.exit(1)
    Path("state.json").write_text(json.dumps({"i": m["next_i"]}))

if __name__ == "__main__":
    {"make": cmd_make, "publish": cmd_publish}[sys.argv[1]]()
