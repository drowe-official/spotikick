#!/usr/bin/env python3
"""Build a Spotify playlist of each artist's top song from london_artists.txt.

Reads the artist list written by songkick_london.py, finds each artist on Spotify
(exact, case-insensitive name match), takes their top track and adds it to a new
private playlist on your account. Standard library only.

Top track: GET /artists/{id}/top-tracks is used when the app has access to it
(Extended Quota apps). Development Mode apps lost that endpoint in February 2026,
so the fallback is the first track Spotify's own search ranks for the artist.

Setup (once):
    1. Create an app at https://developer.spotify.com/dashboard (owner needs Premium).
    2. Add the redirect URI http://127.0.0.1:8888/callback and tick "Web API".
    3. export SPOTIFY_CLIENT_ID=<the app's client ID>

Usage:
    python3 songkick_london.py && python3 spotify_playlist.py
    python3 spotify_playlist.py --name "London tonight" --dry-run
    python3 spotify_playlist.py --exclude-genre "big beat" --exclude-genre ebm
"""

import argparse
import base64
import csv
import datetime
import hashlib
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "playlist-modify-private playlist-modify-public"
TOKEN_CACHE = os.path.expanduser("~/.cache/spotify_playlist_token.json")
# Used when SPOTIFY_CLIENT_ID isn't set (a PKCE client ID isn't a secret).
DEFAULT_CLIENT_ID = "fa890f5c2d234772adee43560a1770af"


class SpotifyError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


# --- auth (Authorization Code with PKCE, so no client secret is needed) ---


def _post_form(url, fields):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SpotifyError(e.code, e.read().decode("utf-8", errors="replace")) from None


def _save_token(token):
    token["expires_at"] = time.time() + token.get("expires_in", 3600) - 60
    os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
    with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
        json.dump(token, f)
    os.chmod(TOKEN_CACHE, 0o600)
    return token


def _wait_for_code(auth_url, state):
    """Catch the redirect on 127.0.0.1, or let the user paste the redirected URL."""
    codes = queue.Queue()

    def extract(url):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if params.get("state", [""])[0] != state:
            return None
        if "error" in params:
            sys.exit(f"Spotify authorisation failed: {params['error'][0]}")
        return params.get("code", [None])[0]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            code = extract(self.path)
            self.send_response(200 if code else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Authorised - you can close this tab." if code else b"Missing or mismatched code."
            )
            if code:
                codes.put(code)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def read_paste():
        while True:
            try:
                line = input().strip()
            except EOFError:
                return
            code = extract(line) if line else None
            if code:
                codes.put(code)
                return
            print("That URL has no matching code, try again:", file=sys.stderr)

    threading.Thread(target=read_paste, daemon=True).start()

    print(f"\nOpen this URL to authorise Spotify access:\n\n  {auth_url}\n", file=sys.stderr)
    print(
        "Waiting for the redirect... (if the browser can't reach 127.0.0.1, paste the URL "
        "it ended up on here and press Enter)",
        file=sys.stderr,
    )
    webbrowser.open(auth_url)
    code = codes.get()
    server.shutdown()
    return code


def get_token(client_id):
    try:
        with open(TOKEN_CACHE, encoding="utf-8") as f:
            token = json.load(f)
    except (OSError, ValueError):
        token = None

    if token and token.get("client_id") == client_id:
        if time.time() < token.get("expires_at", 0):
            return token
        if token.get("refresh_token"):
            try:
                fresh = _post_form(
                    TOKEN_URL,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": token["refresh_token"],
                        "client_id": client_id,
                    },
                )
                fresh.setdefault("refresh_token", token["refresh_token"])
                fresh["client_id"] = client_id
                return _save_token(fresh)
            except SpotifyError as e:
                print(f"Token refresh failed ({e}), re-authorising", file=sys.stderr)

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    state = secrets.token_urlsafe(16)
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge.decode(),
        }
    )
    code = _wait_for_code(auth_url, state)
    token = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    token["client_id"] = client_id
    return _save_token(token)


# --- API ---


class Spotify:
    def __init__(self, client_id):
        self.client_id = client_id
        self.token = get_token(client_id)

    def request(self, method, path, params=None, body=None, retries=5):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(retries):
            if time.time() >= self.token["expires_at"]:
                self.token = get_token(self.client_id)
            req = urllib.request.Request(
                url,
                method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Authorization": f"Bearer {self.token['access_token']}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 or (e.code >= 500 and attempt < retries - 1):
                    wait = int(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
                    print(f"  HTTP {e.code}, retrying in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise SpotifyError(e.code, e.read().decode("utf-8", errors="replace")) from None
        raise SpotifyError(429, "gave up after repeated rate limiting")


def london_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def _norm(name):
    return unicodedata.normalize("NFKC", name).casefold().strip()


def is_dj(name):
    return "dj" in name.casefold()


def is_remix(track):
    return "remix" in track["name"].casefold()


def read_genres(path):
    """One genre per line; blank lines and # comments ignored. Missing file -> none."""
    try:
        with open(path, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def genre_matcher(words):
    words = [w for w in words if w.strip()]
    if not words:
        return lambda artist: False
    # Whole words, so e.g. "electropop" and "garage rock" don't count.
    pattern = re.compile(r"\b(" + "|".join(re.escape(w.casefold().strip()) for w in words) + r")\b")
    return lambda artist: any(pattern.search(g.casefold()) for g in artist.get("genres", []))


def find_artist(sp, name):
    """Exact (case-insensitive) name match among the top search results, else None."""
    res = sp.request("GET", "/search", {"q": f'artist:"{name}"', "type": "artist", "limit": 10})
    for artist in res.get("artists", {}).get("items", []):
        if _norm(artist["name"]) == _norm(name):
            return artist
    return None


class TopTrackFinder:
    def __init__(self, sp, market):
        self.sp, self.market = sp, market
        self.top_tracks_available = True

    def __call__(self, artist):
        if self.top_tracks_available:
            try:
                res = self.sp.request(
                    "GET", f"/artists/{artist['id']}/top-tracks", {"market": self.market}
                )
                tracks = res.get("tracks", [])
                return (tracks[0], "top-tracks") if tracks else (None, "top-tracks")
            except SpotifyError as e:
                if e.status not in (403, 404):
                    raise
                print(
                    f"  top-tracks endpoint unavailable (HTTP {e.status}); "
                    "falling back to search ranking",
                    file=sys.stderr,
                )
                self.top_tracks_available = False

        res = self.sp.request(
            "GET",
            "/search",
            {"q": f'artist:"{artist["name"]}"', "type": "track", "limit": 10, "market": self.market},
        )
        items = res.get("tracks", {}).get("items", [])
        # Prefer tracks where they're the lead artist, then any they appear on.
        for lead_only in (True, False):
            for track in items:
                ids = [a["id"] for a in track.get("artists", [])]
                if artist["id"] in (ids[:1] if lead_only else ids):
                    return track, "search"
        return None, "search"


def build_playlist(sp, name, description, public, uris):
    playlist = sp.request(
        "POST", "/me/playlists", body={"name": name, "description": description, "public": public}
    )
    for i in range(0, len(uris), 100):
        sp.request("POST", f"/playlists/{playlist['id']}/items", body={"uris": uris[i : i + 100]})
    return playlist


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artists", default="london_artists.txt", help="one artist per line")
    ap.add_argument("--client-id", default=os.environ.get("SPOTIFY_CLIENT_ID") or DEFAULT_CLIENT_ID)
    ap.add_argument("--name", default=f"London gigs")
    ap.add_argument("--public", action="store_true", help="make the playlist public (default: private)")
    ap.add_argument("--market", default="GB", help="country code for track availability (default: GB)")
    ap.add_argument("--report", default="spotify_tracks.csv", help="artist -> track match report")
    ap.add_argument(
        "--artists-csv",
        default="playlist_artists.csv",
        help="artists that made the playlist, with their genres (default: playlist_artists.csv)",
    )
    ap.add_argument(
        "--genres-file",
        default="excluded_genres.txt",
        help="skip artists with any of these genres, one per line (default: excluded_genres.txt)",
    )
    ap.add_argument(
        "--exclude-genre",
        action="append",
        default=[],
        metavar="WORD",
        help="also skip artists with this genre, on top of --genres-file (repeatable)",
    )
    ap.add_argument("--dry-run", action="store_true", help="look up tracks but don't create a playlist")
    args = ap.parse_args()

    if not args.client_id:
        ap.error("set SPOTIFY_CLIENT_ID or pass --client-id (see setup in --help)")
    with open(args.artists, encoding="utf-8") as f:
        artists = [line.strip() for line in f if line.strip()]
    if not artists:
        sys.exit(f"No artists in {args.artists}")

    sp = Spotify(args.client_id)
    top_track = TopTrackFinder(sp, args.market)
    excluded_genre = genre_matcher(read_genres(args.genres_file) + args.exclude_genre)
    rows, uris = [], []
    for i, name in enumerate(artists, 1):
        print(f"[{i}/{len(artists)}] {name}", file=sys.stderr)
        if is_dj(name):
            rows.append([name, "", "", "", "", "skipped (DJ)"])
            continue
        artist = find_artist(sp, name)
        if not artist:
            rows.append([name, "", "", "", "", "no exact artist match"])
            continue
        url, genres = artist["external_urls"]["spotify"], "; ".join(artist.get("genres", []))
        if excluded_genre(artist):
            rows.append([name, url, genres, "", "", "skipped (genre)"])
            continue
        track, source = top_track(artist)
        if not track:
            rows.append([name, url, genres, "", "", f"no track ({source})"])
            continue
        if is_remix(track):
            rows.append([name, url, genres, track["name"], "", "skipped (remix)"])
            continue
        rows.append([name, url, genres, track["name"], track["uri"], source])
        if track["uri"] not in uris:
            uris.append(track["uri"])

    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "spotify_artist", "genres", "track", "track_uri", "source"])
        w.writerows(rows)

    skipped = sum(1 for r in rows if not r[4])
    print(
        f"Matched {len(rows) - skipped}/{len(rows)} artists ({len(uris)} unique tracks); "
        f"report -> {args.report}",
        file=sys.stderr,
    )
    included = [r for r in rows if r[4]]
    with open(args.artists_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "genres"])
        w.writerows([r[0], r[2]] for r in included)
    print(f"{len(included)} playlist artists -> {args.artists_csv}", file=sys.stderr)
    if args.dry_run or not uris:
        return

    playlist = build_playlist(
        sp, args.name, f"Top song from each artist playing London ({len(uris)} tracks)", args.public, uris
    )
    print(f"Playlist created: {playlist['external_urls']['spotify']}", file=sys.stderr)


if __name__ == "__main__":
    main()
