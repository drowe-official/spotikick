# London gigs → Spotify playlist

Two scripts that turn a day of London gig listings into a Spotify playlist:

1. `songkick_london.py` gets every artist playing in London on a given day from Songkick.
2. `spotify_playlist.py` finds each artist on Spotify and builds a playlist from their top tracks.

Both scripts need only Python 3.9+ and its standard library, so there's nothing to install.

## Quick start

```sh
python3 songkick_london.py                 # today's gigs
python3 spotify_playlist.py --dry-run      # check which tracks it picks
python3 spotify_playlist.py                # create the playlist
```

## 1. Get the gig listings: `songkick_london.py`

Goes through Songkick's London listings for one day, page by page, and writes:

| File | Contents |
|---|---|
| `london_events.csv` | One row per gig: `date, artist, venue, locality, event_name, url` |
| `london_artists.txt` | Each artist once, one per line. This is the input for the playlist script |

```sh
python3 songkick_london.py                          # today (London time)
python3 songkick_london.py --date 2026-11-01        # a specific day
python3 songkick_london.py --delay 3 --max-pages 5  # slower and shorter
```

| Option | Default | Meaning |
|---|---|---|
| `--date` | today | Day to fetch, `YYYY-MM-DD` |
| `--max-pages` | all | Stop after this many pages |
| `--delay` | `1.5` | Seconds to wait between requests |
| `--user-agent` | built-in | User-Agent header to send. Put your own contact details in it |
| `--events-csv` | `london_events.csv` | Where to write the gig list |
| `--artists-out` | `london_artists.txt` | Where to write the artist list |

You can edit `london_artists.txt` by hand before the next step to add or remove artists.

## 2. Build the playlist: `spotify_playlist.py`

### One-time setup

1. Create an app at <https://developer.spotify.com/dashboard>. The app's owner needs Spotify Premium.
2. In the app settings, add the redirect URI `http://127.0.0.1:8888/callback` and tick **Web API**.
3. `export SPOTIFY_CLIENT_ID=<your app's client ID>`

On the first run a browser opens so you can sign in to Spotify. If the browser can't reach
`127.0.0.1`, paste the URL it ends up on into the terminal instead. The login is saved in
`~/.cache/spotify_playlist_token.json` and renewed automatically after that.

### Running it

```sh
python3 spotify_playlist.py --dry-run                     # look up tracks and write the report only
python3 spotify_playlist.py                               # create a private playlist
python3 spotify_playlist.py --name "Friday gigs" --public
python3 spotify_playlist.py --exclude-genre "black metal" --exclude-genre ska
```

| Option | Default | Meaning |
|---|---|---|
| `--artists` | `london_artists.txt` | Artist list, one per line |
| `--name` | `London gigs <date>` | Playlist name |
| `--public` | off | Make the playlist public (it's private otherwise) |
| `--market` | `GB` | Country code used to check tracks are available |
| `--report` | `spotify_tracks.csv` | Where to write the report on each artist |
| `--artists-csv` | `playlist_artists.csv` | Where to write the artists that made the playlist, with their genres |
| `--genres-file` | `excluded_genres.txt` | Genres to skip, one per line |
| `--exclude-genre` | none | Another genre to skip for this run (repeatable) |
| `--dry-run` | off | Write the report without creating a playlist |
| `--client-id` | `$SPOTIFY_CLIENT_ID` | Spotify app client ID |

### Which artists end up in the playlist

Each artist gets their top track on Spotify. An artist is left out when:

- **No exact match:** no Spotify artist has exactly that name (capitals don't matter).
- **"DJ" in the name:** for example "DJ Rap" or "Plump DJs".
- **Excluded genre:** any of the artist's Spotify genres contains a word or phrase from
  `excluded_genres.txt` or `--exclude-genre`.
- **Remix:** the artist's top track has "remix" in its title.

### Choosing genres to skip

`excluded_genres.txt` holds one genre per line. Blank lines and lines starting with `#` are
ignored. The file starts with a list of electronic genres, but you can put any genres in it.

Matching ignores capitals and uses whole words only. So `house` matches "tech house" and
"afro house", but `electro` doesn't match "electropop". To skip nothing by genre, empty the
file or delete it.

Spotify lists no genres for many small artists, so the genre filter can't catch them. To
check which genres an artist has, look at the `genres` column of the report.

### The report

`spotify_tracks.csv` has one row per artist, with these columns:

`artist, spotify_artist, genres, track, track_uri, source`

`source` says how the track was chosen (`top-tracks` or `search`) or why the artist was
left out (`skipped (DJ)`, `skipped (genre)`, `skipped (remix)`, `no exact artist match` or
`no track`). Running with `--dry-run` and checking this file is the easiest way to tune the
filters before creating a playlist.
