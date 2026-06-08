# ytdlp-web

A clean, self-hosted web UI for yt-dlp. Paste a URL, pick a format, download.

## Requirements

- Python 3.8+
- ffmpeg (for format merging and MP3 extraction)

## Setup

```bash
pip install -r requirements.txt
```

Install ffmpeg if you don't have it:
```bash
# Ubuntu/Debian
sudo apt install ffmpeg -y

# macOS
brew install ffmpeg
```

## Run

```bash
python app.py
```

Open `http://localhost:5000` in your browser.

## EC2 / server deploy

```bash
pip install -r requirements.txt
sudo apt install ffmpeg -y

# Background process
nohup python app.py > ytdlp.log 2>&1 &

# Custom port
PORT=8080 python app.py
```

Make sure the port is open in your security group. Access via `http://<ip>:5000`.

## Supported formats

| Option    | What it does                                      |
|-----------|---------------------------------------------------|
| Best      | Highest available quality (video + audio merged)  |
| 1080p     | Up to 1080p video + audio                         |
| 720p      | Up to 720p video + audio                          |
| 480p      | Up to 480p video + audio                          |
| MP3 Audio | Audio only, converted to 192kbps MP3              |

## Notes

- Downloads go to a system temp dir and are served back through the browser on request.
- No auth — run behind a firewall or VPN if deploying on a public server.
- Temp files accumulate per session; restart the server to clear them, or add a cleanup cron.
- Supports everything yt-dlp supports (YouTube, TikTok, Twitter/X, Vimeo, SoundCloud, etc.)
