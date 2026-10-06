# Flat watch

Checks Rightmove every few minutes for new 1–2 bed rentals at £1,800–£2,600 pcm in Walthamstow, Stoke Newington, Bethnal Green, Bow and Forest Gate. Each new match is pushed to your phone through the [ntfy](https://ntfy.sh) app, with the link, price, floor area (when the agent gives one), availability, furnishing, highlights and a short description.

It runs on GitHub's servers, so your own computer can be off.

## How it works

- GitHub runs `flat_watch.py --once` roughly every 5 minutes. At busy times runs can start late or occasionally be skipped. Nothing is lost when that happens: the next run still picks up every new listing.
- After each run it saves `seen.json` (the listings it has already seen) back into this repository. That's the stream of "Update seen listings" commits.
- `matches.csv` collects every listing it has sent you.
- It skips checks between midnight and 6am UK time and sends a short check-in each morning around 9am, so you know it's still running.

The notification channel name is stored as a repository secret called `NTFY_TOPIC`, so it isn't visible in this public repository.

## Everyday use

All of this is done on github.com, in this repository.

| To... | Do this |
|---|---|
| **Run a test** | **Actions** tab → **Flat watch** → **Run workflow** → choose `test` → **Run workflow**. You'll get a sample notification. |
| **Check it's running** | **Actions** tab: each run gets a green tick or a red cross. Click one to see its log. |
| **Pause it** | **Actions** tab → **Flat watch** → **⋯** menu → **Disable workflow**. Enable it again the same way. |
| **Change the search** | Open `config.json` → pencil icon → edit → **Commit changes**. The next run uses the new settings. |
| **Change the notification channel** | **Settings** → **Secrets and variables** → **Actions** → edit `NTFY_TOPIC`. |
| **Stop for good** | Disable the workflow, or delete the repository. |

### Settings in `config.json`

| Setting | What it does |
|---|---|
| `min_price`, `max_price` | Monthly rent range |
| `min_beds`, `max_beds` | Bedroom range |
| `ideal_min_sqm` | Size that earns a ✅ (60) |
| `skip_if_stated_below_sqm` | Listings whose stated floor area is below this are dropped (55). Set to 0 to see everything |
| `pause_overnight` | No checks between these hours, UK time |
| `daily_summary_hour` | Hour of the morning check-in |
| `radius_miles` | 0 means just the area itself; 0.25 or 0.5 widens each area slightly |
| `areas` | Rightmove's area codes. To add one, open a Rightmove search for that area and copy the `locationIdentifier=REGION%5E…` value from the address bar, writing `%5E` as `^` |

`check_every_minutes` only applies when running on your own computer. On GitHub the timing is set in `.github/workflows/flat-watch.yml`.

## Notifications

```
£2,250 pcm · 2 bed · Bow
Grove Road, London, E3
📐 64 m² / 689 sq ft ✅
Available 01/11/2026 · Unfurnished
Garden · Period property

A first floor two bedroom period flat moments from Victoria Park...
- Agent name, Bow
```

| You see | Meaning |
|---|---|
| ✅ | The agent's stated floor area is 60 m² or more |
| ⚠️ under 60 | Between 55 and 60 m², or a figure found in the description text (check the floorplan) |
| Size not stated | No floor area given; the line says if there's a floorplan to check |

House shares, studios, let-agreed and out-of-budget listings are dropped, as are listings with a stated floor area under 55 m².

Other alerts you might see:

- **Flat watch paused**: Rightmove refused a request. It backs off (up to 90 minutes) and resumes by itself.
- **Flat watch still blocked**: Rightmove has refused requests for a few hours and may be blocking GitHub's servers. Running it on a computer at home avoids that.
- **Flat watch can't read Rightmove**: three checks in a row couldn't read the results, probably because Rightmove changed its pages. Open the latest run in the **Actions** tab, download **debug-pages** at the bottom, and send it to Claude.

## Running it on your own computer instead

It needs only Python 3:

```
python3 flat_watch.py --test   # one-off check and a sample notification
python3 flat_watch.py          # keeps running and checks every ~8 minutes
```

On a computer, the ntfy channel goes in `config.json` (`ntfy_topic`). Disable the GitHub workflow first, or you'll get every alert twice.

Rightmove's terms of use prohibit scraping, so running this is at your own discretion.
